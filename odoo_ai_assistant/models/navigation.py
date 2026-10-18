import ast
import importlib.util
import re

from lxml import etree

from odoo import _, release
from odoo.exceptions import AccessError, ValidationError
from odoo.tools.safe_eval import safe_eval

from .limits import (
    CREDENTIAL_FIELD, CURRENT_USER, MAX_CONTEXT_SCREENS, MAX_FIELD_SUGGESTIONS, MAX_LOOKUP_NAME_CHARS, MAX_SCREEN_BUTTONS,
    MAX_SCREEN_FIELDS, MAX_SCREEN_MATCHES, is_restricted_model,
)
from .providers import _vocabulary_pattern

# Classes that style a view button, as Odoo's ViewButton reads them (oe_highlight/oe_link are its aliases);
# sizes are left out so every preview in the chat has the same size.
_BUTTON_STYLE = re.compile(r"btn(?:-(?!sm$|lg$)[a-z-]+)?|oe_highlight|oe_link")
_TRUE = {"1", "True", "true"}
# A dotted lowercase name such as sale.order, outside links, e-mail addresses and file paths.
_MODEL_LIKE = re.compile(r"(?<![\w./@:-])[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+(?![\w/@-]|\.\w)")


def button_label(node):
    """A view button's label as the screen shows it."""
    return node.get("string") or " ".join("".join(node.itertext()).split())


def eval_view_expression(expression, record, extra=None):
    """Evaluate a view expression (an invisible condition, a button's context) on ``record``'s values as Odoo's web
    client does: a link is its ID, a list of links their IDs."""
    names = {node.id for node in ast.walk(ast.parse(expression, mode="eval")) if isinstance(node, ast.Name)}
    values = {}
    for name in names & set(record._fields):
        value = record[name]
        values[name] = (value.ids if record._fields[name].type in {"one2many", "many2many"} else value.id
                        ) if record._fields[name].relational else value
    return safe_eval(expression, {**values, "id": record.id, "uid": record.env.uid,
                                  "context": dict(record.env.context), **(extra or {})})


def visible_for(node, record):
    """Whether ``node`` of ``record``'s view shows for that record: its and its parents' invisible conditions are
    evaluated on the record. A condition that cannot be evaluated (one about a parent form) hides it."""
    for element in [node, *node.iterancestors()]:
        condition = (element.get("invisible") or "").strip()
        if condition in {"", "0", "False", "false"}:
            continue
        try:
            if condition in _TRUE or eval_view_expression(condition, record):
                return False
        except Exception:  # noqa: BLE001 - any name or construct the record cannot answer hides the button
            return False
    return True


def _visibility(node):
    """'hidden' when the node or an ancestor is always invisible, 'conditional' when that depends on the record."""
    ancestors = [node, *node.iterancestors()]
    if any(element.get(attribute, "").strip() in _TRUE
           for element in ancestors for attribute in ("invisible", "column_invisible")):
        return "hidden"
    if any(element.get("invisible") or element.get("column_invisible") for element in ancestors):
        return "conditional"
    return "visible"


def accessible_apps(env):
    menus = env["ir.ui.menu"].load_menus(False)
    return [menus[menu_id]["name"] for menu_id in menus["root"]["children"]]


def apps_named_in(env, text):
    """Menu IDs of the accessible apps that ``text`` names (singular or plural), in home menu order.

    A name right after another app's name describes it ("the CRM app") rather than naming a second app."""
    menus = env["ir.ui.menu"].load_menus(False)
    apps = menus["root"]["children"]
    matches = sorted((match.start(), match.end(), menu_id) for menu_id in apps
                     if (pattern := _vocabulary_pattern([menus[menu_id]["name"]]))
                     for match in pattern.finditer(text))
    named, previous_end = set(), None
    for start, end, menu_id in matches:
        if previous_end is None or text[previous_end:start].strip():
            named.add(menu_id)
        previous_end = end
    return [menu_id for menu_id in apps if menu_id in named]


def menus_named_in(env, text):
    """The accessible menus inside apps that ``text`` names, singular or plural, with their full path and the model
    of their records, so the AI knows where a named screen is and what it holds without searching every app's
    menus. A word that names an app means the app (see apps_named_in), not a menu of the same name; the longest,
    most specific names come first."""
    apps = accessible_apps(env)
    app_names = {name.casefold() for name in apps}
    rows = {row["menu"]: row for app in apps for row in app_structure(env, {"app": app})["rows"]}
    named = [row for path, row in rows.items() if " / " in path
             and (name := path.rsplit(" / ", 1)[1]).casefold() not in app_names
             and (pattern := _vocabulary_pattern([name])) and pattern.search(text)]
    named.sort(key=lambda row: -len(row["menu"].rsplit(" / ", 1)[1]))
    return [{"path": row["menu"], **({"model": row["model"]} if row["model"] else {})}
            for row in named[:MAX_SCREEN_MATCHES]]


def filters_named_in(env, text, models):
    """Odoo's own search filters of ``models`` (label, model and search) whose label starts with a word ``text``
    uses ("overdue" names Overdue Opportunities, not every Opportunities filter), so the AI searches the way Odoo's
    screens do instead of guessing a meaning. uid becomes the AI's placeholder for the signed-in user."""
    found = []
    for model in models:
        for node in etree.fromstring(env[model].get_view(view_type="search")["arch"]).iter("filter"):
            label, domain = node.get("string") or "", node.get("domain") or ""
            first = next((word for word in label.split() if len(word) >= 3), "")
            if domain.strip() not in {"", "[]"} and (pattern := _vocabulary_pattern([first])) and pattern.search(text):
                found.append({"model": model, "filter": label,
                              "domain": re.sub(r"\buid\b", repr(CURRENT_USER), " ".join(domain.split()))})
    return found[:MAX_SCREEN_MATCHES]


def unknown_model_names(env, text):
    """Names in ``text`` written like a model of this database (their first part starts a real model's name, as
    sale in sale.order) that are not one, nor a record's XML ID or a Python module: an answer must not present a
    guessed model as real."""
    prefixes = {model.split(".", 1)[0] for model in env}
    data = env["ir.model.data"].sudo()
    return list(dict.fromkeys(
        name for name in _MODEL_LIKE.findall(text)
        if name.split(".", 1)[0] in prefixes and name not in env
        and not importlib.util.find_spec(name.split(".", 1)[0])
        and not data._xmlid_to_res_id(name, raise_if_not_found=False)))


def apps_using_models(env, models):
    """Accessible apps that show the most of `models` in their screens, most first (menu order on ties);
    apps showing none of them are left out."""
    menus = env["ir.ui.menu"].load_menus(False)

    def window_actions(menu):
        is_window = menu.get("action_model") == "ir.actions.act_window" and menu.get("action_id")
        own = [menu["action_id"]] if is_window else []
        return own + [action for child in menu["children"] for action in window_actions(menus[child])]

    apps = [(menus[app]["name"], window_actions(menus[app])) for app in menus["root"]["children"]]
    # Native menu loading already checked menu groups; only the actions' target models are read here.
    targets = {action.id: action.res_model for action in env["ir.actions.act_window"].sudo().browse(
        {action_id for _name, action_ids in apps for action_id in action_ids})}
    counts = [(name, len({targets.get(action_id) for action_id in action_ids} & models)) for name, action_ids in apps]
    return [name for name, count in sorted(counts, key=lambda item: -item[1]) if count]


def accessible_menu_names(env):
    """Names of every app and menu the current user can open."""
    menus = env["ir.ui.menu"].load_menus(False)
    return sorted({menu["name"] for menu_id, menu in menus.items()
                   if menu_id != "root" and isinstance(menu.get("name"), str)})


def current_screen(env, screen):
    """The app and screen the user has open, as reported by their browser.

    Only names the user can already see in their own menus are kept, plus the view type and the kind
    of record (never a record's name or values). Anything else the browser sends is dropped."""
    if not isinstance(screen, dict):
        return None
    current = {}
    if screen.get("app") in accessible_apps(env):
        current["app"] = screen["app"]
    if screen.get("screen") in accessible_menu_names(env):
        current["screen"] = screen["screen"]
    if screen.get("view_type") in dict(env["ir.ui.view"]._fields["type"].selection):
        current["view_type"] = screen["view_type"]
    model = screen.get("model")
    if (isinstance(model, str) and model in env and not is_restricted_model(env, model)
            and env[model].has_access("read")):
        current["record_type"] = env[model]._description
    return current or None


def screen_context(env, app="", menus=True):
    """Navigation metadata for the AI: the running version and, with ``menus``, the menus of ``app``
    (all accessible apps when empty). Without menus the AI fetches one app's menus on demand with the
    odoo_app_structure tool, which keeps ordinary requests about 5,000 tokens lighter."""
    if not menus:
        return {"running_version": release.version}
    rows = []
    names = [app] if app else accessible_apps(env)
    for name in names:
        for row in app_structure(env, {"app": name})["rows"]:
            rows.append({key: row[key] for key in ("menu", "screen", "views")})
    return {
        "running_version": release.version, "menus": rows[:MAX_CONTEXT_SCREENS],
        "total_screens": len(rows), "truncated": len(rows) > MAX_CONTEXT_SCREENS,
    }


def _app_menus(env, app):
    menus = env["ir.ui.menu"].load_menus(False)
    query = app.strip().casefold()
    roots = [menus[menu_id] for menu_id in menus["root"]["children"]]
    exact = [root for root in roots if root["name"].casefold() == query]
    matches = exact or [root for root in roots if query in root["name"].casefold()]
    if not matches:
        raise ValidationError(_("No accessible Odoo app matches that name. Ask to list available apps first."))
    entries = []

    def visit(menu, path):
        path = path + [menu["name"]]
        entries.append((menu, " / ".join(path)))
        for child_id in menu["children"]:
            visit(menus[child_id], path)

    for root in matches:
        if query:
            visit(root, [])
        else:
            entries.append((root, root["name"]))
    return entries


def app_structure(env, arguments):
    if (not isinstance(arguments, dict) or set(arguments) != {"app"}
            or not isinstance(arguments["app"], str) or len(arguments["app"]) > MAX_LOOKUP_NAME_CHARS):
        raise ValidationError(_("App discovery requires an app name of at most %s characters.")
                              % MAX_LOOKUP_NAME_CHARS)
    rows = []
    for menu, path in _app_menus(env, arguments["app"]):
        action_model = menu["action_model"]
        model, views = "", ""
        if action_model == "ir.actions.act_window":
            # Native menu loading already checked menu groups and target-model read access.
            action = env[action_model].sudo().browse(menu["action_id"])
            model, views = action.res_model, action.view_mode
        rows.append({
            "menu": path,
            "screen": (_("Record view") if model else _("App screen") if action_model else _("Menu group")),
            "model": model, "views": views,
        })
        if not arguments["app"].strip():
            rows[-1].update(screen=_("Available app"), model="", views="")
    return {
        "kind": "table",
        "title": (_("Accessible Odoo app screens (not business records)") if arguments["app"].strip()
                  else _("Available Odoo apps")),
        "columns": [
            {"key": "menu", "label": _("App / Menu")},
            {"key": "screen", "label": _("Screen type")},
            {"key": "model", "label": _("Record model")},
            {"key": "views", "label": _("Available views")},
        ],
        "rows": rows, "truncated": False,
    }


def screen_details(env, arguments):
    if not env["odoo.ai.assistant.config"]._configuration().screen_metadata:
        raise AccessError(_("Screen metadata sharing is disabled in AI Settings."))
    if (not isinstance(arguments, dict) or set(arguments) != {"app", "screen"}
            or any(not isinstance(arguments[key], str) or not arguments[key].strip()
                   or len(arguments[key]) > MAX_LOOKUP_NAME_CHARS for key in ("app", "screen"))):
        raise ValidationError(_("Screen discovery requires an app and a screen name of 1 to %s characters.")
                              % MAX_LOOKUP_NAME_CHARS)
    entries = _app_menus(env, arguments["app"])
    query = arguments["screen"].strip().replace(" > ", " / ").casefold()
    exact = [(menu, path) for menu, path in entries if query in {menu["name"].casefold(), path.casefold()}]
    matches = exact or [(menu, path) for menu, path in entries if query in path.casefold()]
    if not matches:
        # Listing the app's screens lets the AI choose a real one when it retries.
        raise ValidationError(_("No accessible screen matches that name in this app. Its screens are: %s")
                              % ", ".join([path for menu, path in entries if menu["action_model"]]
                                          [:MAX_FIELD_SUGGESTIONS]))
    # A menu group has no screen of its own: inspect the screens inside it instead.
    groups = [path for menu, path in matches if not menu["action_model"]]
    inside = [(menu, path) for menu, path in entries if menu["action_model"] == "ir.actions.act_window"
              and any(path.startswith(f"{group} / ") for group in groups)]
    if inside:
        matches = list({path: (menu, path) for menu, path in
                        [match for match in matches if match[0]["action_model"]] + inside}.values())
    if len(matches) > MAX_SCREEN_MATCHES:
        raise ValidationError(_("Several screens match. Specify a more precise menu path."))
    return {
        "screens": [_inspect_screen(env, menu, path) for menu, path in matches],
        "scope": "Accessible view field labels and types, and button labels and styles only; "
                 "no record values or field options.",
    }


def app_screens(env, app_ids):
    """The model-backed screens of the apps ``app_ids`` the user can read, inspected as screen_details does,
    so buttons a question names can be found without the AI choosing the screen."""
    menus = env["ir.ui.menu"].load_menus(False)
    Action = env["ir.actions.act_window"].sudo()
    return [_inspect_screen(env, menu, path)
            for app_id in app_ids for menu, path in _app_menus(env, menus[app_id]["name"])
            if menu["action_model"] == "ir.actions.act_window"
            and env[Action.browse(menu["action_id"]).res_model].has_access("read")]


def _inspect_screen(env, menu, path):
    """Permitted field labels and the buttons (labels and looks) of one accessible menu screen."""
    screen = {"menu": path, "views": [], "fields": [], "buttons": [], "truncated": False}
    if menu["action_model"] != "ir.actions.act_window":
        screen["limitation"] = "This menu is a group or client screen; it has no model-backed form to inspect."
        return screen
    action = env["ir.actions.act_window"].sudo().browse(menu["action_id"])
    model = env[action.res_model]
    model.check_access("read")
    # Technical screens list their buttons (labels and styles only), never their fields.
    restricted = is_restricted_model(env, model._name)
    if restricted:
        screen["limitation"] = "Field discovery excludes technical, user-security and settings screens."
    definitions = {} if restricted else model.fields_get(attributes=["string", "type", "required"])
    view_specs = action.views
    selected = [spec for kind in ("form", "list", "kanban", "pivot", "graph")
                for spec in view_specs if spec[1] == kind][:3]
    seen, buttons = {}, {}
    for view_id, view_type in selected:
        view = model.get_view(view_id=view_id or None, view_type=view_type)
        arch = etree.fromstring(view["arch"].encode())
        screen["views"].append(view_type)
        for node in arch.xpath(".//field[not(ancestor::field)]"):
            name = node.get("name")
            definition = definitions.get(name)
            if not definition:
                continue
            label = node.get("string") or definition["string"]
            if (CREDENTIAL_FIELD.search(name) or CREDENTIAL_FIELD.search(label)
                    or definition["type"] in {"binary", "html"}
                    or node.get("password") in _TRUE):
                continue
            visibility = _visibility(node)
            if visibility == "hidden":
                continue
            if name in seen:
                if view_type not in seen[name]["views"]:
                    seen[name]["views"].append(view_type)
                continue
            field = {
                "label": label, "type": definition["type"], "required": bool(definition["required"]),
                "views": [view_type], "visibility": visibility,
            }
            seen[name] = field
            screen["fields"].append(field)
        for node in arch.xpath(".//button | .//a[contains(concat(' ', normalize-space(@class), ' '), ' btn ')]"):
            label = button_label(node)
            visibility = _visibility(node)
            if not label or visibility == "hidden":
                continue
            styles = [cls for cls in (node.get("class") or "").split() if _BUTTON_STYLE.fullmatch(cls)]
            if not styles and view_type == "list":
                continue  # unstyled list buttons are row links; elsewhere ViewButton's default style applies
            # ViewButton adds "btn" itself, so beside another style it changes nothing: "btn btn-primary" and
            # "btn-primary" are one look.
            classes = " ".join(cls for cls in styles if cls != "btn" or len(styles) == 1)
            icon = node.get("icon") or ""
            look = {"classes": classes, "icon": icon if icon.startswith(("fa-", "oi-")) else ""}
            # One entry per label: a button such as Confirm can be highlighted in one state and plain in another.
            button = buttons.setdefault(label, {"label": label, "looks": [], "views": [], "visibility": visibility})
            if look not in button["looks"]:
                button["looks"].append(look)
            if view_type not in button["views"]:
                button["views"].append(view_type)
            if visibility == "conditional":
                button["visibility"] = visibility
    screen["buttons"] = list(buttons.values())[:MAX_SCREEN_BUTTONS]
    if len(screen["fields"]) > MAX_SCREEN_FIELDS or len(buttons) > MAX_SCREEN_BUTTONS:
        screen["fields"] = screen["fields"][:MAX_SCREEN_FIELDS]
        screen["truncated"] = True
    return screen


def screen_details_for_ai(details):
    """``screen_details`` as the AI reads it: each button's styles in words ("primary", "highlight", "info"),
    without the CSS classes the chat draws it with, so no answer quotes class names."""
    def style(classes):
        return " ".join(cls.removeprefix("btn-").removeprefix("oe_") for cls in classes.split() if cls != "btn")

    return {**details, "screens": [{**screen, "buttons": [
        {**{key: value for key, value in button.items() if key != "looks"},
         "styles": [style(look["classes"]) or "default" for look in button["looks"]]}
        for button in screen["buttons"]]} for screen in details["screens"]]}


def buttons_named_in(env, screens, text):
    """The buttons of ``screens`` whose exact label ``text`` names, with their screen and view names, so the chat
    can show what they look like; a button that looks the same on several screens is listed once. Labels match
    exactly, unlike app names: "opportunity" names a record there, not the Opportunities button."""
    view_names = dict(env["ir.ui.view"]._fields["type"]._description_selection(env))
    named = {}
    for screen in screens:
        for button in screen.get("buttons", []):
            if re.search(rf"(?<!\w){re.escape(button['label'])}(?!\w)", text, re.IGNORECASE):
                key = (button["label"], tuple((look["classes"], look["icon"]) for look in button["looks"]))
                entry = named.setdefault(key, {**button, "screens": [], "views": []})
                entry["screens"].append(screen["menu"])
                entry["views"] += [view_names.get(view, view) for view in button["views"]
                                   if view_names.get(view, view) not in entry["views"]]
                if button["visibility"] == "conditional":
                    entry["visibility"] = "conditional"
    return list(named.values())
