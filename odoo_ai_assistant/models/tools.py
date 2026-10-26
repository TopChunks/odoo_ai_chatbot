import ast
import contextlib
import difflib
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
    CLOSE_FIELD_NAME, CREDENTIAL_FIELD, CURRENT_USER, MAX_CHART_TITLE_CHARS, MAX_FIELD_SUGGESTIONS, MAX_REPORT_MEASURES, MAX_TIED_NAMES,
    is_restricted_model,
)
from .navigation import accessible_apps, app_structure, screen_details
from .providers import CHART_TOOL

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

_CHART_TYPES = ("bar", "line", "pie", "doughnut", "scatter", "table")
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
            "description": "Business figures from Odoo as a chart or table: totals, counts, rankings, trends "
                           "and comparisons. Odoo groups the records by one field and computes the measures with "
                           "the user's permissions, as its own reports and dashboards do; the result stays in Odoo. "
                           "Use real field names (from the field list or mcp_get_fields); dates group by the "
                           "chosen interval. Set model to the records the user asked about; it must be one "
                           "native_schema lists with read.",
            "inputSchema": {"type": "object", "properties": {
                "model": {"type": "string", "description": "Technical model of the records to chart"},
                "group_by": {"type": "string", "description": "Field to group by"},
                "interval": {"type": "string", "enum": list(_DATE_INTERVALS),
                             "description": "Grouping for date fields; default month"},
                "measures": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                             "maxItems": MAX_REPORT_MEASURES,
                             "description": "count (how many records or documents), a numeric field to sum, "
                                            "or field:avg; several give a table"},
                "domain": {"type": "array",
                           "description": "Odoo domain filter such as [[\"state\", \"=\", \"sale\"]]; [] for all"},
                "chart_type": {"type": "string", "enum": list(_CHART_TYPES)},
                "limit": {"type": "integer", "minimum": 1,
                          "description": "Only the N largest groups (top N); the rest add up as Others"},
                "smallest": {"type": "boolean", "description": "Smallest groups first (bottom N, least)"},
                "title": {"type": "string", "description": "Short title without record values"},
            }, "required": ["model", "group_by", "measures", "chart_type", "title"], "additionalProperties": False},
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


def clean_order(order):
    """A sort as models often write it, made valid: a list, also sent as text ('["name desc"]'), is joined into one
    sort. Anything else is returned as it is, for MCP Server to refuse when invalid."""
    for parse in (json.loads, ast.literal_eval):
        if isinstance(order, str) and order.strip().startswith("["):
            with contextlib.suppress(ValueError, SyntaxError):
                order = parse(order.strip())
    if isinstance(order, (list, tuple)) and all(isinstance(term, str) for term in order):
        order = ", ".join(order)
    return order


def clean_field_names(model, arguments):
    """Field names in a filter, grouping, totals, sort or field list made valid where a model took them from a similar
    model (date_order on Sales Analysis): a name ``model`` lacks becomes its one field with the label that name has
    elsewhere (date, "Order Date"), or else its one close name: an older Odoo's name or a typo (groups_id: group_ids).
    Names without exactly one such field are left for Odoo to refuse."""
    registry = model.env.registry

    def rename(spec):
        name = re.match(r"\s*(\w*)", spec).group(1) if isinstance(spec, str) else ""
        if not name or name in model._fields or name.startswith("__"):
            return spec
        labels = {registry[other]._fields[name].string for other in registry if name in registry[other]._fields}
        same = [field.name for field in model._fields.values() if field.string in labels] or difflib.get_close_matches(
            name, model._fields, n=2, cutoff=CLOSE_FIELD_NAME)
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
    """Business figures for the AI's report tool: records grouped by one field with one or more measures, computed
    as Odoo's own reports compute them (on_report), as a chart (one measure) or a table."""
    arguments = parse_tool_arguments(arguments)
    if not isinstance(arguments, dict):
        raise ValidationError(_("Report arguments must be an object."))
    # Only the documented arguments are used; omitted optional ones take their documented defaults.
    arguments = {"interval": "month", "domain": [], "measures": ["count"], **{
        key: value for key, value in arguments.items()
        if key in {"model", "group_by", "interval", "measures", "domain", "chart_type", "limit", "title", "smallest"}
    }}
    name, group, interval = arguments.get("model"), arguments.get("group_by"), arguments["interval"]
    measures, domain = arguments["measures"], clean_domain(arguments["domain"])
    chart, title, limit = arguments.get("chart_type"), arguments.get("title"), arguments.get("limit")
    smallest = arguments.get("smallest") is True
    if name not in chart_models(env):
        raise AccessError(_("'%(model)s' is not available for reports. An administrator can enable reading it "
                            "in %(menus)s.") % {"model": name, "menus": data_access_menus(env)})
    if (not all(isinstance(value, str) for value in (group, interval, chart, title))
            or not isinstance(measures, list) or not 1 <= len(measures) <= MAX_REPORT_MEASURES
            or not all(isinstance(measure, str) and measure for measure in measures)
            or not isinstance(domain, list) or chart not in _CHART_TYPES
            or interval not in _DATE_INTERVALS or not 1 <= len(title) <= MAX_CHART_TITLE_CHARS
            or not (limit is None or (type(limit) is int and limit > 0))):
        raise ValidationError(_("Choose a model, a grouping field, one to %(count)s measures (count or numeric "
                                "fields), a chart type and a short title.") % {"count": MAX_REPORT_MEASURES})
    if any(CREDENTIAL_FIELD.search(field) for field in measures + [
            term[0] for term in domain if isinstance(term, (list, tuple)) and term and isinstance(term[0], str)]):
        raise AccessError(_("Credential fields cannot be used in reports."))
    # As for the native tools, Odoo puts in the signed-in user where the AI wrote the placeholder ("my pipeline").
    domain = [[term[0], term[1], env.uid] if isinstance(term, list) and len(term) == 3 and term[2] == CURRENT_USER
              else term for term in domain]
    # "count", a numeric field (summed) or "field:avg" as Odoo aggregates them, computed as Odoo's reports do.
    model, named = on_report(env[name], clean_field_names(env[name], {"domain": domain, "groupby": [group], "aggregates": [
        "__count" if measure == "count" else measure if ":" in measure else f"{measure}:sum" for measure in measures]}))
    name, domain, group, specs = model._name, named["domain"], named["groupby"][0], named["aggregates"]
    definitions = model.fields_get(attributes=["string", "type", "groupable", "aggregator"])

    def groupable(field):
        definition = definitions.get(field) or {}
        return (definition.get("groupable") and definition.get("type") in _GROUPABLE_TYPES
                and not CREDENTIAL_FIELD.search(field))

    if not groupable(group):
        options = sorted(field for field in definitions if groupable(field))
        raise ValidationError(_("'%(field)s' cannot group a report of %(model)s. Groupable fields: %(fields)s.") % {
            "field": group, "model": name, "fields": ", ".join(options[:MAX_FIELD_SUGGESTIONS])})
    group_type = definitions[group]["type"]
    if chart == "scatter" and group_type not in _NUMERIC_TYPES:
        raise ValidationError(_("Scatter charts require a numeric grouping field."))
    labels, note = [], ""
    for spec in specs:
        field, _sep, operation = spec.partition(":")
        definition = definitions.get(field) or {}
        if field == "__count" or operation == "count_distinct" and definition:
            labels.append(_("Count") if field == "__count" else _("%s count") % definition["string"])
            continue
        if operation not in ("sum", "avg") or definition.get("type") not in _NUMERIC_TYPES \
                or not definition.get("aggregator"):
            options = sorted(field for field, item in definitions.items()
                             if item.get("type") in _NUMERIC_TYPES and item.get("aggregator"))
            raise ValidationError(_("'%(field)s' is not a numeric field of %(model)s that can be summed. "
                                    "Numeric fields: %(fields)s.") % {
                                        "field": field, "model": name, "fields": ", ".join(options[:MAX_FIELD_SUGGESTIONS])})
        labels.append(definition["string"] if operation == "sum" else _("Average %s") % definition["string"])
        if definition["type"] == "monetary" and not model._auto:
            # Odoo's reports convert amounts to the company's currency (their own currency column is fixed).
            labels[-1] += f" ({env.company.currency_id.name})"
            continue
        # Odoo leaves currency_field empty when it defaults to currency_id; resolve it the ORM's way.
        currency = model._fields[field].get_currency_field(model) if definition["type"] == "monetary" else None
        if currency:
            counts = dict(model._read_group(domain, groupby=[currency], aggregates=["__count"]))
            if len(counts) > 1:
                # Amounts in different currencies cannot be added up: keep the currency most of the records use
                # (the company's on a tie), as Odoo's own reports show one currency, and say what is left out.
                kept = max((key for key in counts if key),
                           key=lambda key: (counts[key], key == env.company.currency_id))
                domain = domain + [[currency, "=", kept.id]]
                note = _("Only amounts in %(kept)s are shown. Left out, in %(others)s: %(count)s.") % {
                    "kept": kept.name, "count": sum(number for key, number in counts.items() if key != kept),
                    "others": ", ".join(key.name for key in counts if key and key != kept) or _("no currency")}
                counts = {kept: counts[kept]}
            if counts and next(iter(counts)):
                labels[-1] += f" ({next(iter(counts)).name})"
    grouping = f"{group}:{interval}" if group_type in {"date", "datetime"} else group
    result = env["odoo.ai.assistant.mcp"]._execute("ai_group_records", {
        "model": name, "domain": domain, "group": grouping, "measures": specs, "smallest": smallest,
    })
    # A group with nothing to add up comes back from Odoo as False (or None); it counts as zero.
    rows = [(row["label"], [0 if value is None or value is False else value for value in row["values"]])
            for row in result["rows"]]
    if any(type(value) not in {int, float} or not math.isfinite(value) for _label, values in rows for value in values):
        raise UserError(_("The report contains invalid numeric values."))
    if chart in {"pie", "doughnut"} and any(values[0] < 0 for _label, values in rows):
        raise ValidationError(_("Pie and doughnut charts cannot display negative values."))
    fields_used = [spec.split(":")[0] for spec in specs]

    def shown(values):
        return [cell_value(model, field, value) for field, value in zip(fields_used, values)]

    # From every group, before a limit keeps some: groups that share the asked-for value are all named.
    answer = summary_answer(labels[0], [(label, shown(values)[0], values[0]) for label, values in rows],
                            not result["truncated"], smallest if limit else None)
    totals = [0 if value is None or value is False else value for value in result["totals"]]
    # Averages and counts of distinct documents do not add up across groups.
    additive = all(spec == "__count" or spec.endswith(":sum") for spec in specs)
    if limit:
        rows.sort(key=lambda row: row[1][0], reverse=not smallest)
        while limit < len(rows) and rows[limit][1][0] == rows[limit - 1][1][0]:  # ties at the cut stay, as Power BI's TOPN
            limit += 1
        # The rest add up as one "Others" group where the measures add up.
        rows = rows[:limit] + ([(_("Others"), [total - sum(values[index] for _label, values in rows[:limit])
                                               for index, total in enumerate(totals)])] if rows[limit:] and additive else [])
    basis = based_on(model, domain, what=_("%(value)s by %(group)s", value=", ".join(labels),
                                             group=definitions[group]["string"]) + (
        ", " + (_("smallest first") if smallest else _("largest first")) if limit else ""))
    if chart == "table" or len(specs) > 1:
        return {"kind": "table", "title": title, "columns": [{"key": "label", "label": definitions[group]["string"]}] + [
            {"key": f"m{index}", "label": label} for index, label in enumerate(labels)],
            "rows": [{"label": label, **{f"m{index}": value for index, value in enumerate(shown(values))}}
                     for label, values in rows],
            "truncated": result["truncated"], "note": note, "answer": answer, "basis": basis}
    values = [values[0] for _label, values in rows]
    # A total that cannot be matched to the rows shown (top 3 by order count, no "Others") is left out.
    total = None if specs[0].endswith(":avg") or not additive and len(rows) < len(result["rows"]) else totals[0]
    return {"kind": "chart", "title": title, "chart_type": chart, "labels": [label for label, _values in rows],
            "operation": "avg" if specs[0].endswith(":avg") else "sum" if specs[0].endswith(":sum") else "count",
            "values": values, "shown": [shown([value])[0] for value in values], "value_label": labels[0],
            "group_label": definitions[group]["string"],
            "truncated": result["truncated"], "note": note, "answer": answer, "basis": basis,
            # Bottom N: "Others" is far larger than them, so it stays in the table and total but is not drawn.
            "bars": limit if smallest and limit and len(values) > limit else None,
            # Shown under the chart: the AI never sees these values, so it cannot state the total itself.
            "total": total, "total_shown": None if total is None else shown([total])[0],
            # A document on several rows (an order with two products) is in the total once: say so.
            "total_label": _("Total") if additive else _("Total (each %s counted once)", definitions[fields_used[0]]["string"])}


def document_link(report, document=None):
    """The field linking an Odoo report's rows to the documents they come from (Sales Analysis: its order
    reference), to ``document`` when given; documents are records with a stored status. None without one."""
    env = report.env
    for field in sorted(report._fields.values(), key=lambda field: field.type != "reference"):
        targets = ([name for name, _label in field._description_selection(env)] if field.type == "reference"
                   else [field.comodel_name] if field.type == "many2one" else [])
        if any(target in env and (target == document if document else "state" in env[target]._fields
                                  and env[target]._fields["state"].store) for target in targets):
            return field.name
    return None


def on_report(model, arguments):
    """``arguments`` of a report (domain, groupby, aggregates) computed as Odoo's own reports compute them, as its
    dashboards do. Documents (records with a status: orders, RFQs, invoices) move to the enabled report linked to
    them when it has every field used, the same or the one with the same label (order date: date), and are counted
    there once each, so empty ones are left out; otherwise they keep that report's usual filters. On a report,
    "count" counts its documents. Returns the model and arguments to use."""
    env = model.env
    if not model._auto:
        link = document_link(model)
        return model, dict(arguments, domain=report_filters(model, arguments["domain"], arguments["groupby"]),
                           aggregates=[f"{link}:count_distinct" if link and spec == "__count" else spec
                                       for spec in arguments["aggregates"]])
    if "state" not in model._fields:
        return model, arguments
    used = {spec.split(":")[0] for spec in arguments["groupby"] + arguments["aggregates"] if spec != "__count"} | {
        str(term[0]).split(".")[0] for term in arguments["domain"] if isinstance(term, (list, tuple)) and term}
    linked = [(env[name], link) for name in chart_models(env)
              if not env[name]._auto and (link := document_link(env[name], model._name))]
    for report, link in linked:
        moved = clean_field_names(report, json.loads(json.dumps(arguments)))
        mapped = clean_field_names(report, {"fields": sorted(used)})["fields"]
        if all(name in model._fields and target in report._fields and (
                (report._fields[target].type, report._fields[target].comodel_name)
                == (model._fields[name].type, model._fields[name].comodel_name)
                or {report._fields[target].type, model._fields[name].type} <= {"date", "datetime"})
               for name, target in zip(sorted(used), mapped)):
            return report, dict(moved, domain=report_filters(report, moved["domain"], moved["groupby"]), aggregates=[
                f"{link}:count_distinct" if spec == "__count" else spec for spec in moved["aggregates"]])
    return model, dict(arguments, domain=report_filters(model, arguments["domain"], arguments["groupby"],
                                                        linked[0][0])) if linked else arguments


def report_filters(model, domain, groupby=(), source=None):
    """``domain`` with the filters an Odoo report opens with from its own menu (Sales Analysis: confirmed sales,
    not quotations or cancelled ones), where neither ``domain`` nor ``groupby`` ("by status") uses their fields,
    so figures match Odoo's dashboards. Read from the menu's action and search view, never written here; date
    filters are left out, as the question sets the period, and so are filters of other kinds than plain
    conditions. They come from ``source``, the report ``model``'s documents are analysed in, when given."""
    report = model if source is None else source  # an empty recordset is false: test for None
    if report._auto or not isinstance(domain, list):
        return domain
    env = model.env
    actions = env["ir.actions.act_window"].sudo().search([("res_model", "=", report._name)], order="id")
    action = next((action for action in actions if env["ir.ui.menu"].sudo().search_count(
        [("action", "=", f"ir.actions.act_window,{action.id}")], limit=1)), None)
    if not action:
        return domain
    with contextlib.suppress(ValueError, NameError, SyntaxError, TypeError):
        context = safe_eval(action.context or "{}", {"active_id": False, "active_ids": [], "uid": env.uid})
        search = etree.fromstring(report.get_view(action.search_view_id.id or None, "search")["arch"])
        used = {term[0].split(".")[0] for term in domain if isinstance(term, (list, tuple)) and term
                and isinstance(term[0], str)} | {str(spec).split(":")[0] for spec in groupby}
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
        if field.type == "selection" and item[1] in ("!=", "not in"):
            # "Status: Sales Order" reads better than the three statuses left out.
            excluded = item[2] if isinstance(item[2], (list, tuple)) else [item[2]]
            kept = [key for key, _label in field._description_selection(env) if key not in excluded]
            if 0 < len(kept) < len(excluded):
                item = (item[0], "in", kept)
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
        names = [row[0] for row in tied[:MAX_TIED_NAMES]] + (
            [_("%s more", len(tied) - MAX_TIED_NAMES)] if len(tied) > MAX_TIED_NAMES else [])
        names = _("%(first)s and %(last)s", first=", ".join(names[:-1]), last=names[-1]) if len(names) > 1 else names[0]
        return names, tied[0][1] if len(tied) == 1 else _("%s each", tied[0][1])

    if smallest is None:
        (high, high_value), (low, low_value) = end(max), end(min)
        if high == low:  # every group has the same value
            return _("%(measure)s: %(names)s (%(value)s).", measure=measure, names=high, value=high_value)
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
