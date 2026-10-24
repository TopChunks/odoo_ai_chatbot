import ast
import contextlib
import json
import math
import re
from datetime import date, datetime

from odoo import _, fields, release
from lxml import etree

from odoo.tools.misc import formatLang, get_lang
from odoo.tools.safe_eval import safe_eval
from odoo.exceptions import AccessError, UserError, ValidationError

from .limits import (
    CREDENTIAL_FIELD, CURRENT_USER, MAX_CHART_TITLE_CHARS, MAX_FIELD_SUGGESTIONS, MAX_TIED_NAMES, is_restricted_model,
)
from .navigation import accessible_apps, app_structure, screen_details

TOOLS = [
    {
        "name": "odoo_instance_info",
        "description": "Read the actual running Odoo server version and installed edition locally. "
                       "Use for instance version questions, never guess.",
        "inputSchema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "odoo_app_structure",
        "description": "Discover the actual apps and their accessible menus/screens in this Odoo instance. "
                       "Use for what is inside an app, what apps are installed/available, and navigation questions. "
                       "No hardcoded app list. Supply app='' to list the installed apps, or an app name to inspect only "
                       "that app's own menus, even when the app itself manages apps or modules. "
                       "Menu paths, record models and view types are displayed locally, not business records.",
        "inputSchema": {"type": "object", "properties": {
            "app": {"type": "string", "description": "App name; empty to list available apps"},
        }, "required": ["app"], "additionalProperties": False},
    },
    {
        "name": "odoo_screen_details",
        "description": "Inspect actual accessible form/list fields and buttons for a screen in ANY installed app, "
                       "including custom apps. Use when asked what a specific screen contains, its fields, "
                       "how to use those fields, or which buttons it has and how they look (you cannot see the "
                       "screen: never guess colours). Supply app and screen/menu name from navigation context. "
                       "Returns permitted field labels/types and button labels with their styles, "
                       "not record values, credentials or settings. "
                       "Do not confuse field descriptions with a request to list actual records.",
        "inputSchema": {"type": "object", "properties": {
            "app": {"type": "string"}, "screen": {"type": "string"},
        }, "required": ["app", "screen"], "additionalProperties": False},
    },
]
_TOOL = {tool["name"]: tool for tool in TOOLS}

CHART_TOOL = "odoo_chart"
_CHART_TYPES = ("bar", "line", "pie", "doughnut", "scatter", "table")
_CHART_OPERATIONS = ("count", "sum", "avg")
_DATE_INTERVALS = ("day", "week", "month", "quarter", "year")
_GROUPABLE_TYPES = {"char", "selection", "boolean", "many2one", "many2many", "date", "datetime",
                    "integer", "float", "monetary"}
_NUMERIC_TYPES = {"integer", "float", "monetary"}


def chart_models(env):
    """Models the current user may chart: enabled for MCP reads, readable and not technical."""
    gates = env["mcp.enabled.model"].sudo()
    names = gates.search([("active", "=", True)]).mapped("model_name")
    return sorted(name for name in set(names)
                  if not is_restricted_model(env, name) and gates.check_model_operation_enabled(name, "read")
                  and env[name].has_access("read"))


def data_access_menus(env, settings=True):
    """Where an administrator chooses the data the AI may use, as the menus are currently named
    (this app's settings unless ``settings`` is False, then MCP Server's model list)."""
    menus = env["ir.ui.menu"].sudo()
    if settings:
        menus |= env.ref("odoo_ai_assistant.assistant_settings_menu", raise_if_not_found=False) or menus
    models_action = env.ref("mcp_server.action_mcp_enabled_models", raise_if_not_found=False)
    if models_action:
        menus |= menus.search([("action", "=", "%s,%s" % (models_action._name, models_action.id))])
    return " or ".join(menus.mapped(lambda menu: menu.complete_name.replace("/", " > ")))


def requested_chart_tool(tools):
    """The chart tool for a request classified as CHART, when it is available to the user."""
    return next((tool for tool in tools if tool.get("name") == CHART_TOOL), None)


def available_tools(env):
    app_tool = _TOOL["odoo_app_structure"]
    tools = [_TOOL["odoo_instance_info"], {
        **app_tool, "description": app_tool["description"] + " Available apps: " + ", ".join(accessible_apps(env)),
    }]
    config = env["odoo.ai.assistant.config"]._configuration()
    if config.screen_metadata:
        tools.append(_TOOL["odoo_screen_details"])
    if not config.local_tools:
        return tools
    models = chart_models(env)
    if models:
        tools.append({
            "name": CHART_TOOL,
            # Domains are free-form Odoo filters, which OpenAI's strict mode cannot describe.
            "strict": False,
            "description": "Draw a chart or grouped table of Odoo records. Odoo groups the records "
                           "by one field and counts them, or sums or averages a numeric field, with the "
                           "user's permissions; the result stays in Odoo. Use real field names (from the "
                           "field list or mcp_get_fields); dates group by the chosen interval. "
                           "Set model to the technical model of the records the user asked about, and never "
                           "substitute a different model: Odoo explains when it is not available. Charts use the "
                           "models native_schema lists with read.",
            "inputSchema": {"type": "object", "properties": {
                "model": {"type": "string", "description": "Technical model of the records to chart"},
                "group_by": {"type": "string", "description": "Field to group by"},
                "interval": {"type": "string", "enum": list(_DATE_INTERVALS),
                             "description": "Grouping for date fields; default month"},
                "operation": {"type": "string", "enum": list(_CHART_OPERATIONS)},
                "measure": {"type": "string", "description": "Numeric field for sum or avg; empty for count"},
                "domain": {"type": "array",
                           "description": "Odoo domain filter such as [[\"state\", \"=\", \"sale\"]]; [] for all"},
                "chart_type": {"type": "string", "enum": list(_CHART_TYPES)},
                "limit": {"type": "integer", "minimum": 1,
                          "description": "Only the N largest groups (top N); the rest add up as Others"},
                "smallest": {"type": "boolean", "description": "Smallest groups first (bottom N, least)"},
                "title": {"type": "string", "description": "Short title without record values"},
            }, "required": ["model", "group_by", "operation", "chart_type", "title"], "additionalProperties": False},
        })
    tools.extend(env["odoo.ai.assistant.native.mcp"]._provider_tools())
    return tools


def parse_tool_arguments(arguments):
    """Tool arguments arrive as an object or, from some providers, as JSON text."""
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except ValueError as error:
            raise ValidationError(_("Invalid MCP tool arguments.")) from error
    return arguments


def clean_order(order, groupby=(), aggregates=()):
    """A sort as models often write it, made valid: a list, also sent as text ('["name desc"]'), is joined into one
    sort, and a summary's sort by a totalled field ("amount_total desc") becomes that field's total
    ("amount_total:sum desc"). Anything else is returned as it is, for MCP Server to refuse when invalid."""
    for parse in (json.loads, ast.literal_eval):
        if isinstance(order, str) and order.strip().startswith("["):
            with contextlib.suppress(ValueError, SyntaxError):
                order = parse(order.strip())
    if isinstance(order, (list, tuple)) and all(isinstance(term, str) for term in order):
        order = ", ".join(order)
    if not isinstance(order, str):
        return order
    totals = {spec.partition(":")[0]: spec for spec in aggregates if isinstance(spec, str) and ":" in spec}
    terms = []
    for term in order.split(","):
        name, _sep, direction = term.strip().partition(" ")
        if name not in groupby:
            name = totals.get(name, name)
        terms.append(" ".join(filter(None, (name, direction.strip()))))
    return ", ".join(terms)


def clean_field_names(model, arguments):
    """Field names in a filter, grouping, totals, sort or field list made valid where a model took them from a similar
    model (date_order on Sales Analysis): a name ``model`` lacks becomes its one field with the label that name has
    elsewhere (date, "Order Date"). Names without exactly one such field are left for Odoo to refuse."""
    registry = model.env.registry

    def rename(spec):
        name = re.match(r"\s*(\w*)", spec).group(1) if isinstance(spec, str) else ""
        if not name or name in model._fields or name.startswith("__"):
            return spec
        labels = {registry[other]._fields[name].string for other in registry if name in registry[other]._fields}
        same = [field.name for field in model._fields.values() if field.string in labels]
        return spec.replace(name, same[0], 1) if len(same) == 1 else spec

    if isinstance(arguments.get("domain"), list):
        arguments["domain"] = [[rename(term[0]), *term[1:]] if isinstance(term, (list, tuple)) and term else term
                               for term in arguments["domain"]]
    for key in ("groupby", "aggregates", "fields"):
        if isinstance(arguments.get(key), list):
            arguments[key] = [rename(spec) for spec in arguments[key]]
    if isinstance(arguments.get("order"), str):
        arguments["order"] = ", ".join(rename(term.strip()) for term in arguments["order"].split(","))
    return arguments


def clean_domain(domain):
    """A domain's terms as models often write them, made valid: a term sent as text, and the values of an
    "in" / "not in" term spread over the term or given as one value instead of a list. Spread values with a
    bracket are a list the model broke apart, so a value may be lost: that term is left for Odoo to refuse,
    and the AI is asked to write it again, instead of searching with part of the list. A domain sent as text is
    read first; anything that is not a list is returned as it is, for MCP Server to refuse."""
    for parse in (json.loads, ast.literal_eval):  # as MCP Server reads a domain sent as text
        if isinstance(domain, str):
            with contextlib.suppress(ValueError, SyntaxError):
                domain = parse(domain.strip())
    if not isinstance(domain, (list, tuple)):
        return domain
    terms = []
    for term in domain:
        if isinstance(term, str) and term.strip().startswith(("[", "(")):
            with contextlib.suppress(ValueError, SyntaxError):
                term = ast.literal_eval(term.strip())
        if isinstance(term, (list, tuple)) and len(term) >= 3 and term[1] in ("in", "not in"):
            spread = len(term) > 3
            values = term[2:] if spread else term[2]
            if not (spread and any("[" in str(value) or "]" in str(value) for value in values)):
                term = [term[0], term[1], list(values) if isinstance(values, (list, tuple)) else [values]]
        terms.append(term)
    return terms


def execute_tool(env, name, arguments):
    arguments = parse_tool_arguments(arguments)
    if not isinstance(arguments, dict):
        raise ValidationError(_("MCP arguments must be an object."))
    if name not in {tool["name"] for tool in available_tools(env)}:
        raise AccessError(_("This MCP tool is not available to your user."))
    if name == "odoo_app_structure":
        return json.dumps(app_structure(env, arguments), ensure_ascii=False)
    if name == "odoo_screen_details":
        return json.dumps(screen_details(env, arguments), ensure_ascii=False)
    if name == "odoo_instance_info":
        if arguments:
            raise ValidationError(_("Instance information does not accept arguments."))
        enterprise = bool(env["ir.module.module"].sudo().search_count([
            ("name", "=", "web_enterprise"), ("state", "=", "installed"),
        ]))
        return _("This instance is running Odoo %(version)s (%(edition)s Edition).") % {
            "version": release.version, "edition": "Enterprise" if enterprise else "Community",
        }
    raise UserError(_("Unknown MCP tool."))


def execute_chart(env, arguments):
    """Group any chartable model by one field with Odoo's read_group and return a local chart."""
    arguments = parse_tool_arguments(arguments)
    if not isinstance(arguments, dict):
        raise ValidationError(_("Chart arguments must be an object."))
    # Only the documented arguments are used; omitted optional ones take their documented defaults.
    arguments = {"interval": "month", "measure": "", "domain": [], **{
        key: value for key, value in arguments.items()
        if key in {"model", "group_by", "interval", "operation", "measure", "domain", "chart_type", "limit", "title",
                   "smallest"}
    }}
    name, group, interval = arguments.get("model"), arguments.get("group_by"), arguments["interval"]
    operation, measure = arguments.get("operation"), arguments["measure"]
    domain = clean_domain(arguments["domain"])
    chart, title, limit = arguments.get("chart_type"), arguments.get("title"), arguments.get("limit")
    smallest = arguments.get("smallest") is True
    if name not in chart_models(env):
        raise AccessError(_("'%(model)s' is not available for charts. An administrator can enable reading it "
                            "in %(menus)s.") % {"model": name, "menus": data_access_menus(env)})
    if (not all(isinstance(value, str) for value in (group, interval, operation, measure, chart, title))
            or not isinstance(domain, list) or operation not in _CHART_OPERATIONS or chart not in _CHART_TYPES
            or interval not in _DATE_INTERVALS or not 1 <= len(title) <= MAX_CHART_TITLE_CHARS
            or not (limit is None or (type(limit) is int and limit > 0))):
        raise ValidationError(_("Choose a model, a grouping field, count, sum or avg, a supported chart type "
                                "and a short title."))
    if any(isinstance(term, (list, tuple)) and term and isinstance(term[0], str) and CREDENTIAL_FIELD.search(term[0])
           for term in domain):
        raise AccessError(_("Credential fields cannot be used in chart filters."))
    # As for the native tools, Odoo puts in the signed-in user where the AI wrote the placeholder ("my pipeline").
    domain = [[term[0], term[1], env.uid] if isinstance(term, list) and len(term) == 3 and term[2] == CURRENT_USER
              else term for term in domain]
    model = env[name]
    named = clean_field_names(model, {"domain": domain, "groupby": [group], "aggregates": [measure]})
    domain, group, measure = report_filters(model, named["domain"]), named["groupby"][0], named["aggregates"][0]
    definitions = model.fields_get(attributes=["string", "type", "groupable", "aggregator"])

    def groupable(field):
        definition = definitions.get(field) or {}
        return (definition.get("groupable") and definition.get("type") in _GROUPABLE_TYPES
                and not CREDENTIAL_FIELD.search(field))

    if not groupable(group):
        options = sorted(field for field in definitions if groupable(field))
        raise ValidationError(_("'%(field)s' cannot group a chart of %(model)s. Groupable fields: %(fields)s.") % {
            "field": group, "model": name, "fields": ", ".join(options[:MAX_FIELD_SUGGESTIONS])})
    group_type = definitions[group]["type"]
    if chart == "scatter" and group_type not in _NUMERIC_TYPES:
        raise ValidationError(_("Scatter charts require a numeric grouping field."))
    value_label, note = _("Record count"), ""
    if operation == "count":
        if measure:
            raise ValidationError(_("Leave measure empty for a count chart."))
        aggregate = "__count"
    else:
        definition = definitions.get(measure) or {}
        if definition.get("type") not in _NUMERIC_TYPES or not definition.get("aggregator") \
                or CREDENTIAL_FIELD.search(measure):
            options = sorted(field for field, item in definitions.items()
                             if item.get("type") in _NUMERIC_TYPES and item.get("aggregator"))
            raise ValidationError(_("'%(field)s' is not a numeric field of %(model)s that can be summed. "
                                    "Numeric fields: %(fields)s.") % {
                                        "field": measure, "model": name, "fields": ", ".join(options[:MAX_FIELD_SUGGESTIONS])})
        aggregate = f"{measure}:{operation}"
        value_label = definition["string"] if operation == "sum" else _("Average %s") % definition["string"]
        # Odoo leaves currency_field empty when it defaults to currency_id; resolve it the ORM's way.
        currency = model._fields[measure].get_currency_field(model) if definition["type"] == "monetary" else None
        if currency:
            counts = dict(model._read_group(domain, groupby=[currency], aggregates=["__count"]))
            if len(counts) > 1:
                # Amounts in different currencies cannot be added up: chart the currency most of the records use
                # (the company's on a tie), as Odoo's own reports show one currency, and say what is left out.
                kept = max((key for key in counts if key),
                           key=lambda key: (counts[key], key == env.company.currency_id))
                domain = domain + [[currency, "=", kept.id]]
                note = _("Only amounts in %(kept)s are shown. Left out, in %(others)s: %(count)s.") % {
                    "kept": kept.name, "count": sum(number for key, number in counts.items() if key != kept),
                    "others": ", ".join(key.name for key in counts if key and key != kept) or _("no currency")}
                counts = {kept: counts[kept]}
            if counts and next(iter(counts)):
                value_label += f" ({next(iter(counts)).name})"
    grouping = f"{group}:{interval}" if group_type in {"date", "datetime"} else group
    result = env["odoo.ai.assistant.mcp"]._execute("ai_group_records", {
        "model": name, "domain": domain, "group": grouping, "measure": aggregate, "smallest": smallest,
    })
    # A group with nothing to add up comes back from Odoo as False (or None); it counts as zero.
    values = [0 if row["value"] is None or row["value"] is False else row["value"] for row in result["rows"]]
    if any(type(value) not in {int, float} or not math.isfinite(value) for value in values):
        raise UserError(_("The chart contains invalid numeric values."))
    if chart in {"pie", "doughnut"} and any(value < 0 for value in values):
        raise ValidationError(_("Pie and doughnut charts cannot display negative values."))
    labels = [row["label"] for row in result["rows"]]
    # From every group, before a limit keeps some: groups that share the asked-for value are all named.
    answer = summary_answer(value_label, [(label, cell_value(model, measure, value), value)
                                          for label, value in zip(labels, values)],
                            not result["truncated"], smallest if limit else None)
    if limit:
        pairs = sorted(zip(labels, values), key=lambda pair: pair[1], reverse=not smallest)
        while limit < len(pairs) and pairs[limit][1] == pairs[limit - 1][1]:  # ties at the cut stay, as Power BI's TOPN
            limit += 1
        # The rest add up as one "Others" group; an average of averages would mislead, so averages keep the top N only.
        pairs = pairs[:limit] + ([(_("Others"), (result["total"] or 0) - sum(value for _label, value in pairs[:limit]))]
                                 if pairs[limit:] and operation != "avg" else [])
        labels, values = [pair[0] for pair in pairs], [pair[1] for pair in pairs]
    shown = [cell_value(model, measure, value) for value in values]
    basis = based_on(model, domain, what=_("%(value)s by %(group)s", value=value_label,
                                             group=definitions[group]["string"]) + (
        ", " + (_("smallest first") if smallest else _("largest first")) if limit else ""))
    if chart == "table":
        return {"kind": "table", "title": title, "columns": [
            {"key": "label", "label": definitions[group]["string"]},
            {"key": "value", "label": value_label},
        ], "rows": [{"label": label, "value": value} for label, value in zip(labels, shown)],
            "truncated": result["truncated"], "note": note, "answer": answer, "basis": basis}
    total = None if operation == "avg" else result["total"] or 0
    return {"kind": "chart", "title": title, "chart_type": chart, "operation": operation, "labels": labels,
            "values": values, "shown": shown, "value_label": value_label, "truncated": result["truncated"],
            "note": note, "answer": answer, "basis": basis,
            # Bottom N: "Others" is far larger than them, so it stays in the table and total but is not drawn.
            "bars": limit if smallest and limit and len(labels) > limit else None,
            # Shown under the chart: the AI never sees these values, so it cannot state the total itself.
            "total": total, "total_shown": None if total is None else cell_value(model, measure, total)}


def report_filters(model, domain):
    """``domain`` with the filters an Odoo report opens with from its own menu (Sales Analysis: confirmed sales,
    not quotations or cancelled ones), where ``domain`` has no condition on their fields, so figures match Odoo's
    dashboards. Read from the menu's action and search view, never written here; date filters are left out, as the
    question sets the period, and so are filters of other kinds than plain conditions."""
    if model._auto or not isinstance(domain, list):
        return domain
    env = model.env
    actions = env["ir.actions.act_window"].sudo().search([("res_model", "=", model._name)], order="id")
    action = next((action for action in actions if env["ir.ui.menu"].sudo().search_count(
        [("action", "=", f"ir.actions.act_window,{action.id}")], limit=1)), None)
    if not action:
        return domain
    with contextlib.suppress(ValueError, NameError, SyntaxError, TypeError):
        context = safe_eval(action.context or "{}", {"active_id": False, "active_ids": [], "uid": env.uid})
        search = etree.fromstring(model.get_view(action.search_view_id.id or None, "search")["arch"])
        used = {term[0].split(".")[0] for term in domain if isinstance(term, (list, tuple)) and term
                and isinstance(term[0], str)}
        for key, value in context.items():
            node = search.find(f".//filter[@name='{key.removeprefix('search_default_')}']") \
                if key.startswith("search_default_") and value else None
            terms = ast.literal_eval(node.get("domain") or "None") if node is not None and not node.get("date") else None
            if isinstance(terms, list) and terms and all(
                    isinstance(term, (list, tuple)) and len(term) == 3 and term[0] in model._fields
                    and model._fields[term[0]].type not in ("date", "datetime") and term[0] not in used
                    for term in terms):
                domain = domain + [list(term) for term in terms]
    return domain


def based_on(model, domain, order="", what=""):
    """What a result was based on, in the user's words, built from the call that ran (not the AI's account of it):
    the records, each filter condition with field labels and readable values, the sort and ``what`` was added up.
    A model with a status says so when no condition is on it, the most common reason a figure differs."""
    env, domain = model.env, domain if isinstance(domain, list) else []

    def field_of(path):
        owner, labels = model, []
        for name in path.split("."):
            field = owner._fields.get(name)
            if field is None:
                return path, None, None
            labels.append(field._description_string(env))
            if name != path.split(".")[-1] and field.relational:
                owner = env[field.comodel_name]
        return " › ".join(labels), field, owner

    def shown(field, owner, value):
        if isinstance(value, (list, tuple)):
            return ", ".join(shown(field, owner, item) for item in value) or _("none")
        if isinstance(value, bool):
            return _("yes") if value else _("no")
        if field.relational and type(value) is int:
            return env[field.comodel_name].browse(value).exists()._filtered_access("read").display_name or str(value)
        if field.type == "datetime" and isinstance(value, str) and len(value) == 10:  # a bare day reads as that day
            return fields.Date.to_date(value).strftime(get_lang(env).date_format)
        return str(cell_value(owner, field.name, value))

    def term(item):
        name, field, owner = field_of(item[0]) if isinstance(item, (list, tuple)) and len(item) == 3 \
            and isinstance(item[0], str) else (None, None, None)
        if field is None:
            return str(item)
        dated = field.type in ("date", "datetime")
        words = {"=": "%s", "in": "%s", "!=": _("not %s"), "not in": _("not %s"),
                 ">": _("after %s") if dated else _("more than %s"), ">=": _("from %s") if dated else _("at least %s"),
                 "<": _("before %s") if dated else _("less than %s"), "<=": _("until %s") if dated else _("at most %s"),
                 "ilike": _("contains “%s”"), "not ilike": _("does not contain “%s”")}
        return "%s: %s" % (name, words.get(item[1], item[1] + " %s") % shown(field, owner, item[2]))

    def node(position):
        token = domain[position]
        if token in ("&", "|"):
            left, position = node(position + 1)
            right, position = node(position)
            return "(%s %s %s)" % (left, _("and") if token == "&" else _("or"), right), position
        if token == "!":
            inner, position = node(position + 1)
            return _("not (%s)", inner), position
        return term(token), position + 1

    parts, position = [model._description], 0
    while position < len(domain):
        part, position = node(position)
        parts.append(part)
    if "state" in model._fields and not any(isinstance(item, (list, tuple)) and str(item[0]).split(".")[0] == "state"
                                            for item in domain):
        parts.append(_("%s: any", field_of("state")[0]))
    for item in filter(None, (piece.strip() for piece in str(order or "").split(","))):
        name, _sep, direction = item.partition(" ")
        label = _("Count") if name.startswith("__count") else field_of(name.split(":")[0])[0]
        parts.append(_("sorted by %(field)s, %(direction)s", field=label, direction=_("descending")
                       if direction.strip().lower() == "desc" else _("ascending")))
    return _("Based on: %s", " · ".join(parts + ([what] if what else [])))


def summary_answer(measure, rows, complete=True, smallest=None):
    """A plain first answer from summary rows (name, value shown, value), all of the groups when ``complete``: the
    one value, the highest and lowest, or only the end ``smallest`` asks for (True: lowest). Groups sharing that
    value are all named. None when part of the groups cannot tell."""
    if not rows:
        return _("No matching records found.")
    if len(rows) == 1:
        return (_("%(names)s — %(measure)s: %(value)s.", names=rows[0][0], measure=measure, value=rows[0][1])
                if rows[0][0] else _("%(measure)s: %(value)s.", measure=measure, value=rows[0][1]))
    if smallest is None and not complete:
        return None

    def end(pick):
        tied = [row for row in rows if row[2] == pick(row[2] for row in rows)]
        names = ", ".join(row[0] for row in tied[:MAX_TIED_NAMES]) + (
            _(" and %s more", len(tied) - MAX_TIED_NAMES) if len(tied) > MAX_TIED_NAMES else "")
        return names, tied[0][1] if len(tied) == 1 else _("%s each", tied[0][1])

    if smallest is None:
        (high, high_value), (low, low_value) = end(max), end(min)
        return _("Highest %(measure)s: %(high)s (%(high_value)s). Lowest: %(low)s (%(low_value)s).",
                 measure=measure, high=high, high_value=high_value, low=low, low_value=low_value)
    name, value = end(min if smallest else max)
    return (_("Lowest %(measure)s: %(name)s (%(value)s).", measure=measure, name=name, value=value) if smallest
            else _("Highest %(measure)s: %(name)s (%(value)s).", measure=measure, name=name, value=value))


def cell_value(model, name, value, row=None):
    """A table cell as Odoo's own screens show it: a selection's label, dates in the user's language format and
    time zone, numbers grouped with the field's decimals (and the row's currency when the row has it).
    Odoo reads an empty field as False; it shows empty unless the field is a checkbox."""
    field = model._fields.get(name)
    if value is False and field and field.type != "boolean":
        return None
    env = model.env
    if field and field.type in ("one2many", "many2many") and isinstance(value, list):
        # Linked records by name, as far as the user may read them: their IDs mean nothing to a reader.
        value = ", ".join(env[field.comodel_name].browse(value).exists()._filtered_access("read").mapped(
            "display_name")) or None
    elif field and field.type == "selection" and isinstance(value, str):
        value = dict(field._description_selection(env)).get(value, value)
    elif field and field.type in ("date", "datetime") and isinstance(value, str):
        lang = get_lang(env)
        with contextlib.suppress(ValueError):  # a grouped period ("September 2026") is already a label
            # Odoo stores UTC; show the user's own time, as Odoo's screens do.
            value = (fields.Date.to_date(value).strftime(lang.date_format) if field.type == "date" else
                     fields.Datetime.context_timestamp(model, datetime.fromisoformat(value)).strftime(
                         "%s %s" % (lang.date_format, lang.time_format.replace(":%S", ""))))
    elif field and field.type in ("float", "monetary") and type(value) in (int, float):
        linked = (row or {}).get(field.get_currency_field(model)) if field.type == "monetary" else None
        currency = env["res.currency"].browse(linked[0]).exists() if isinstance(linked, (list, tuple)) and linked else None
        # Money: the currency's decimals (2 without a currency); a decimal field: its own, or 2 as Odoo's screens show.
        digits = (field.get_digits(env) if field.type == "float" else None) or (16, 2)
        value = formatLang(env, value, digits=digits[1], currency_obj=currency or None)
    return table_value(value)


def table_value(value):
    if isinstance(value, datetime):
        return fields.Datetime.to_string(value)
    if isinstance(value, date):
        return fields.Date.to_string(value)
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, (list, tuple)):
        return [table_value(item) for item in value]
    raise UserError(_("The local table contains an unsupported or invalid value."))
