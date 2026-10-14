from lxml import etree

from odoo import _, release
from odoo.exceptions import AccessError, ValidationError

from .limits import (
    CREDENTIAL_FIELD, MAX_CONTEXT_SCREENS, MAX_LOOKUP_NAME_CHARS, MAX_SCREEN_FIELDS, MAX_SCREEN_MATCHES,
    is_restricted_model,
)


def accessible_apps(env):
    menus = env["ir.ui.menu"].load_menus(False)
    return [menus[menu_id]["name"] for menu_id in menus["root"]["children"]]


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
        raise ValidationError(_("No accessible screen matches that name in this app."))
    if len(matches) > MAX_SCREEN_MATCHES:
        raise ValidationError(_("Several screens match. Specify a more precise menu path."))
    screens = []
    for menu, path in matches:
        screen = {"menu": path, "views": [], "fields": [], "truncated": False}
        screens.append(screen)
        if menu["action_model"] != "ir.actions.act_window":
            screen["limitation"] = "This menu is a group or client screen; it has no model-backed form to inspect."
            continue
        action = env["ir.actions.act_window"].sudo().browse(menu["action_id"])
        model = env[action.res_model]
        model.check_access("read")
        if is_restricted_model(env, model._name):
            screen["limitation"] = "Field discovery excludes technical, user-security and settings screens."
            continue
        definitions = model.fields_get(attributes=["string", "type", "required"])
        view_specs = action.views
        selected = [spec for kind in ("form", "list", "kanban", "pivot", "graph")
                    for spec in view_specs if spec[1] == kind][:3]
        seen = {}
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
                        or node.get("password") in {"1", "true", "True"}):
                    continue
                ancestors = [node, *node.iterancestors()]
                if any(element.get("invisible", "").strip() in {"1", "True", "true"}
                       or element.get("column_invisible", "").strip() in {"1", "True", "true"}
                       for element in ancestors):
                    continue
                if name in seen:
                    if view_type not in seen[name]["views"]:
                        seen[name]["views"].append(view_type)
                    continue
                field = {
                    "label": label, "type": definition["type"], "required": bool(definition["required"]),
                    "views": [view_type],
                    "visibility": "conditional" if any(element.get("invisible") or element.get("column_invisible")
                                                       for element in ancestors) else "visible",
                }
                seen[name] = field
                screen["fields"].append(field)
        if len(screen["fields"]) > MAX_SCREEN_FIELDS:
            screen["fields"] = screen["fields"][:MAX_SCREEN_FIELDS]
            screen["truncated"] = True
    return {
        "screens": screens,
        "scope": "Accessible view field labels and types only; no record values or field options.",
    }
