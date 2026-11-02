"""The agent loop: the AI plans with MCP tool schemas, Odoo runs the tools as the user.

Privacy model
-------------
The provider receives the user's messages, the tool list (names, descriptions, JSON
schemas), field definitions and the ids/counts each tool produced. Record *values* stay
in Odoo: they are rendered to the user as blocks (tables, record cards, native graphs)
attached to the AI's message. With the "share record values" setting the tool text is
also sent so the AI can summarise it.

Every data-changing tool is turned into a preview block; it only runs when the user
clicks Confirm (``_ai_agent_execute_action``), through the same MCP tool and gates.
"""

import copy
import datetime
import difflib
import json
import logging
import re
import time
import uuid
from datetime import timedelta

import pytz
from dateutil.relativedelta import relativedelta
from lxml import etree
from markupsafe import Markup

from odoo import _, api, fields, models, release
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.fields import Domain
from odoo.tools import LazyTranslate, html2plaintext, html_sanitize
from odoo.tools.safe_eval import datetime as safe_datetime
from odoo.tools.safe_eval import dateutil as safe_dateutil
from odoo.tools.safe_eval import safe_eval
from odoo.tools.safe_eval import time as safe_time

from odoo.addons.mcp_server.controllers import error_sanitizer
from odoo.addons.mcp_server.controllers import utils as mcp_utils

from .ai_agent_provider import ProviderError
from .discuss_channel import AI_CHANNEL_TYPE

try:
    import markdown2
except ImportError:  # pragma: no cover - optional
    markdown2 = None

_logger = logging.getLogger(__name__)
_lt = LazyTranslate(__name__)

# MCP tools the AI may use. Schema tools return definitions (no record values), so their
# full text goes back to the AI; data tools are shown to the user and summarised.
SCHEMA_TOOLS = {"list_models", "get_fields", "get_current_context"}
DATA_TOOLS = {"search_records", "get_record", "aggregate_records", "list_record_attachments"}
SHARED_ONLY_TOOLS = {"read_attachment"}
WRITE_TOOLS = {
    "create_record": "create",
    "create_records": "create",
    "update_record": "write",
    "update_records": "write",
    "delete_record": "unlink",
    "call_model_method": "method",
    "post_message": "write",
}

# The MCP operation each tool needs on its "model" argument, and the checkbox that allows it.
TOOL_OPERATIONS = {
    "search_records": "read", "get_record": "read", "aggregate_records": "read", "get_fields": "read",
    "list_record_attachments": "read", "show_chart": "read", "get_record_history": "read", "get_filters": "read", "find_changes": "read",
    **WRITE_TOOLS,
}
OPERATION_CHECKBOXES = {
    "read": "Allow Read", "create": "Allow Create", "write": "Allow Update", "unlink": "Allow Delete",
    "method": "Allow Method Calls",
}

HISTORY_RECORD_CAP = 20
HISTORY_MESSAGE_CAP = 40  # newest messages kept per record
HISTORY_MESSAGE_TEXT_CAP = 1500
HISTORY_TEXT_CAP = 40000  # characters of history sent to the AI per call
HISTORY_MESSAGE_TYPES = ("email", "comment", "email_outgoing", "auto_comment")
HISTORY_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "description": "Technical model name of the records, e.g. 'crm.lead'."},
        "record_ids": {
            "type": "array", "items": {"type": "integer"},
            "description": f"IDs of the records whose emails, messages and notes to read (at most {HISTORY_RECORD_CAP}).",
        },
        "include_notes": {"type": "boolean", "description": "Include internal notes and logged calls (default true)."},
    },
    "required": ["model", "record_ids"],
    "additionalProperties": False,
}

PRESENT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "What the user should see, in order. One item per table or chart.",
            "items": {
                "type": "object",
                "properties": {
                    "result": {"type": "string", "description": "The result to show, e.g. 'r2'."},
                    "combine": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Two or more grouped results with the same grouping, merged into one table "
                        "(one row per group), e.g. ['r1', 'r2'].",
                    },
                    "view": {
                        "type": "string", "enum": ["table", "bar", "line", "pie", "pivot"],
                        "description": "table (default). bar, line, pie or pivot need a grouped result.",
                    },
                    "title": {"type": "string", "description": "Clear title in the user's language."},
                    "labels": {
                        "type": "object", "additionalProperties": {"type": "string"},
                        "description": "Rename columns so they say what the numbers mean, e.g. "
                        "{\"__count\": \"Won deals\", \"expected_revenue:sum\": \"Won revenue\"}. For combine, "
                        "prefix with the result: {\"r2.__count\": \"Won deals\"}.",
                    },
                    "columns": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Optional: only these columns, in this order (column names or labels).",
                    },
                    "measure": {"type": "string", "description": "For a chart: the column to plot, e.g. 'amount_total:sum'."},
                },
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

FILTERS_ARGUMENT = {
    "type": "array", "items": {"type": "string"},
    "description": "Names of the model's own Odoo filters (from get_filters), e.g. [\"filter_won_status_lost\"]. "
    "They are combined with the domain (AND).",
}
PERIOD_NAMES = [
    "today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_quarter", "last_quarter",
    "this_year", "last_year", "year_to_date", "last_n_days", "last_n_weeks", "last_n_months", "next_n_days",
]
PERIOD_ARGUMENT = {
    "type": "object",
    "description": "A relative period that Odoo turns into exact dates, in the user's timezone, with the company's "
    "fiscal year and the user's first day of the week. Rolling periods (last_n_*) include the current day, week or "
    "month; next_n_days runs from today to N days ahead.",
    "properties": {
        "field": {"type": "string", "description": "Date or datetime field it applies to, e.g. 'date_order'."},
        "name": {"type": "string", "enum": PERIOD_NAMES},
        "count": {"type": "integer", "description": "N for last_n_days, last_n_weeks, last_n_months, next_n_days."},
    },
    "required": ["name"],
    "additionalProperties": False,
}
SCREEN_ARGUMENT = {
    "type": "string",
    "description": "A screen from get_filters (e.g. 'menu_123'). It sets the base scope exactly like opening that "
    "menu in Odoo (e.g. a pipeline only holds opportunities).",
}
FIND_CHANGES_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "description": "Technical model name, e.g. 'sale.order'."},
        "field": {"type": "string", "description": "The tracked field, e.g. 'state' or 'stage_id'."},
        "to_value": {"type": "string", "description": "New value (label or technical value), e.g. 'Cancelled'. Leave out for any change."},
        "from_value": {"type": "string", "description": "Previous value, optional."},
        "period": PERIOD_ARGUMENT,
    },
    "required": ["model", "field"],
    "additionalProperties": False,
}
SHOW_APP_SCHEMA = {
    "type": "object",
    "properties": {
        "app": {"type": "string", "description": "The app as the user names it, e.g. 'CRM', 'Accounting', 'Inventory'."},
        "menu": {"type": "string", "description": "Optional: the menu inside the app, e.g. 'Leads' or "
                 "'Reporting > General Ledger'."},
        "model": {"type": "string", "description": "Optional: a technical model (e.g. 'crm.lead') to find the app "
                  "and menu that manage it."},
    },
    "additionalProperties": False,
}

GET_FILTERS_SCHEMA = {
    "type": "object",
    "properties": {"model": {"type": "string", "description": "Technical model name, e.g. 'crm.lead'."}},
    "required": ["model"],
    "additionalProperties": False,
}

TABLE_ROW_CAP = 80
CELL_TEXT_CAP = 300
HISTORY_MESSAGES = 16
TRACE_NOTE_CAP = 600
SHARED_TEXT_CAP = 12000
SCHEMA_TEXT_CAP = 16000
MODEL_LIST_CAP = 150
ACTION_TTL = timedelta(minutes=30)
TIME_MARGIN = 8  # seconds kept free before the request deadline
# A web address written as plain text in an answer (closing punctuation is not part of it).
BARE_URL = re.compile(r"""https?://[^\s<>"'\])]*[^\s<>"'\]).,;:!?]""")
FINAL_STEP_NOTE = (
    "\n\nNO TOOL STEPS LEFT: write the final answer now from the results you already have (Odoo shows them to the user). "
    "If something could not be found, say so in one sentence."
)
FINAL_ANSWER_NUDGE = ("(No more lookups are possible.) Answer my question now from the results above, in plain "
                      "words. If they are not enough, say what you found and what is missing.")

ANY_VALUE = {
    "anyOf": [
        {"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "null"},
        {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]}},
        {"type": "object"},
    ]
}
DOMAIN_SCHEMA = {
    "type": "array",
    "description": (
        "Odoo domain: a list of [field, operator, value] conditions joined by AND, e.g. "
        '[["state", "=", "sale"], ["date_order", ">=", "2026-01-01"]]. Prefix with "|" for OR '
        'or "!" for NOT (Polish notation). Related fields use dots: ["partner_id.name", "ilike", "acme"].'
    ),
    "items": {
        "anyOf": [
            {"type": "string"},
            {"type": "array", "items": {"anyOf": [
                {"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "null"},
                {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "number"}]}},
            ]}},
        ]
    },
}
CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short chart title in the user's language."},
        "model": {"type": "string", "description": "Technical model name, e.g. 'sale.order'."},
        "chart_type": {
            "type": "string", "enum": ["bar", "line", "pie", "pivot"],
            "description": "bar to compare groups (default), line for a trend over time, pie for shares of a "
            "total, pivot for a cross table of two groupings.",
        },
        "groupby": {
            "type": "array", "items": {"type": "string"},
            "description": "One or two fields to group by. Dates take a granularity: 'date_order:month' "
            "(day, week, month, quarter, year). The first is the x-axis; the second splits the series "
            "(bar/line) or gives the columns (pivot).",
        },
        "measure": {
            "type": "string",
            "description": "Numeric field to add up (e.g. 'amount_total', 'expected_revenue'), or '__count' to "
            "count records.",
        },
        "domain": DOMAIN_SCHEMA,
        "screen": SCREEN_ARGUMENT,
        "filters": FILTERS_ARGUMENT,
        "period": PERIOD_ARGUMENT,
        "order": {
            "type": "string", "enum": ["desc", "asc"],
            "description": "Sort the groups by the measure. Leave out for time series.",
        },
        "limit": {
            "type": "integer",
            "description": "Keep only the top N groups of the first group-by (use with order, not with dates).",
        },
        "stacked": {"type": "boolean", "description": "Stack the series of a bar or line chart."},
        "cumulative": {"type": "boolean", "description": "Line chart of the running total."},
    },
    "required": ["model", "groupby", "measure"],
    "additionalProperties": False,
}

PROGRESS_LABELS = {
    "search_records": _lt("Searching %s…"),
    "get_record": _lt("Opening %s…"),
    "aggregate_records": _lt("Calculating totals on %s…"),
    "get_fields": _lt("Reading the fields of %s…"),
    "list_models": _lt("Checking which data I may use…"),
    "get_current_context": _lt("Checking your companies and timezone…"),
    "list_record_attachments": _lt("Listing the attachments of %s…"),
    "read_attachment": _lt("Reading an attachment…"),
    "show_chart": _lt("Drawing a chart of %s…"),
    "get_record_history": _lt("Reading the conversation history of %s…"),
    "present": _lt("Preparing your answer…"),
    "get_filters": _lt("Reading the screens and filters of %s…"),
    "show_app": _lt("Finding the app…"),
    "find_changes": _lt("Checking the change history of %s…"),
}


def normalize_schema(node, name=None):
    """Make an MCP JSON schema acceptable to every provider.

    OpenAI rejects arrays without ``items``; Gemini and OpenAI dislike ``["x", "null"]`` type
    unions on some models. Optional values are simply left out by the AI instead.
    """
    if not isinstance(node, dict):
        return node
    if name == "domain":
        return dict(DOMAIN_SCHEMA, description=node.get("description") or DOMAIN_SCHEMA["description"])
    result = {k: v for k, v in node.items() if k not in ("default", "$schema", "minProperties", "examples")}
    kind = result.get("type")
    if isinstance(kind, list):
        kinds = [k for k in kind if k != "null"]
        result["type"] = kinds[0] if kinds else "string"
    if result.get("type") == "array":
        result["items"] = normalize_schema(result.get("items") or ANY_VALUE)
    if isinstance(result.get("properties"), dict):
        result["properties"] = {key: normalize_schema(value, key) for key, value in result["properties"].items()}
    for key in ("anyOf", "oneOf"):
        if isinstance(result.get(key), list):
            result[key] = [normalize_schema(option) for option in result[key]]
    return result


class AiAgentEngine(models.AbstractModel):
    _name = "ai_agent.engine"
    _description = "AI Agent Engine"

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @api.model
    def _ai_default_model(self):
        """The model chosen in the settings (sudo record, may be empty)."""
        params = self.env["ir.config_parameter"].sudo()
        model = self.env["ai_agent.model"].sudo().browse(int(params.get_param("odoo_ai_agent.model_id") or 0)).exists()
        return model if model.active else model.browse()

    @api.model
    def _ai_config(self, channel=None):
        params = self.env["ir.config_parameter"].sudo()

        def integer(key, default, low, high):
            try:
                return max(low, min(int(params.get_param(key) or default), high))
            except ValueError:
                return default

        model = self._ai_default_model()
        chosen = channel.sudo().ai_agent_model_id if channel else None
        if model and chosen and chosen.active and chosen.provider_id == model.provider_id:
            model = chosen  # the model the user picked for this conversation, same provider as the settings
        if not model or not model.active:
            raise UserError(_("No AI model is selected. An administrator can choose one in AI Agent > "
                              "Configuration > Settings."))
        provider = model.provider_id
        if not provider.api_key:
            raise UserError(_("The %s API key is missing. An administrator can add it in AI Agent > "
                              "Configuration > Settings.", provider.name))
        if not mcp_utils.is_mcp_enabled():
            raise UserError(_("The MCP Server is switched off, so the AI cannot read Odoo data. An administrator "
                              "can switch it on in AI Agent > Configuration > Settings."))
        return {
            "model": model,
            "provider": provider,
            "share_data": params.get_param("odoo_ai_agent.share_data") in ("True", "1", "true"),
            "max_steps": integer("odoo_ai_agent.max_steps", 8, 1, 25),
            "max_tokens": integer("odoo_ai_agent.max_output_tokens", 2048, 256, 65536),
            "timeout": integer("odoo_ai_agent.timeout", 60, 10, 600),
        }

    @api.model
    def _deadline(self, timeout):
        """Stop before Odoo's own request time limit kills the worker."""
        from odoo.tools import config as odoo_config

        limit = odoo_config.get("limit_time_real") or 0
        budget = max(limit - 10, 30) if limit and odoo_config.get("workers") else max(timeout * 3, 180)
        return time.monotonic() + budget

    # ------------------------------------------------------------------
    # Tools offered to the AI
    # ------------------------------------------------------------------
    @api.model
    def _ai_tools(self, share_data):
        mixin = self.env["mcp.mixin"]
        tools = []
        for name, meta in mixin._get_mcp_tools().items():
            if name in WRITE_TOOLS:
                kind = "write"
            elif name in SCHEMA_TOOLS:
                kind = "schema"
            elif name in DATA_TOOLS or (share_data and name in SHARED_ONLY_TOOLS):
                kind = "data"
            else:
                continue
            description = meta["description"]
            if kind == "write":
                description += (" Odoo does not run this directly: it shows the user a preview with Confirm and "
                                "Cancel buttons.")
            schema = normalize_schema(mixin._mcp_live_input_schema(meta["input_schema"]))
            if name in ("search_records", "aggregate_records"):
                # Odoo's own screens, filters and periods, turned into domain terms before MCP runs the tool.
                schema["properties"]["screen"] = SCREEN_ARGUMENT
                schema["properties"]["filters"] = FILTERS_ARGUMENT
                schema["properties"]["period"] = PERIOD_ARGUMENT
            if name == "aggregate_records":
                # Overall totals (one row) need no grouping; the engine computes them itself.
                schema["required"] = [key for key in schema.get("required") or [] if key != "groupby"]
                description += " Leave groupby empty for overall totals of the matching records (one row)."
            tools.append({
                "name": name,
                "description": description,
                "schema": schema,
                "raw_schema": meta["input_schema"],
                "kind": kind,
                "method": meta["method_name"],
            })
        builtin_names = {t["name"] for t in tools}
        # Custom tools an administrator defined in MCP Server (curated server actions).
        for custom in self.env["mcp.custom.tool"]._visible_tools().sudo():
            schema = custom._parsed_input_schema()
            if schema is None or custom.name in builtin_names:
                continue
            tools.append({
                "name": custom.name,
                "description": custom.description or custom.name,
                "schema": normalize_schema(schema),
                "raw_schema": schema,
                "kind": "custom_read" if custom.is_readonly else "write",
                "custom_tool_id": custom.id,
            })
        tools.append({
            "name": "get_filters",
            "description": "List a model's screens (its menus in Odoo, each with its own scope, e.g. a pipeline "
            "holds only opportunities) and its filters (Won, Lost, To Invoice, My Orders, Late Activities...). Pass "
            "them as 'screen' and 'filters' to search_records, aggregate_records or show_chart: they apply Odoo's "
            "exact definitions.",
            "schema": GET_FILTERS_SCHEMA,
            "raw_schema": GET_FILTERS_SCHEMA,
            "kind": "filters",
        })
        tools.append({
            "name": "show_app",
            "description": "Show the user a clickable app icon that opens an Odoo app or one of its menus (only "
            "apps and menus this user can open). Use it whenever the user asks about an app, a menu, or where or "
            "how to do something in Odoo.",
            "schema": SHOW_APP_SCHEMA,
            "raw_schema": SHOW_APP_SCHEMA,
            "kind": "app",
        })
        tools.append({
            "name": "find_changes",
            "description": "Find records whose tracked field changed (optionally to or from a value) in a period, "
            "from Odoo's change history: e.g. orders cancelled this month, opportunities moved to Won this week. Use "
            "it for 'when did ... happen' questions.",
            "schema": FIND_CHANGES_SCHEMA,
            "raw_schema": FIND_CHANGES_SCHEMA,
            "kind": "changes",
        })
        tools.append({
            "name": "present",
            "description": "Decide what the user sees. Every data result you get is named (r1, r2, ...) and is NOT "
            "shown on its own. Call present once at the end of a data answer with the tables or charts that answer "
            "the question: give each a clear title and rename columns so they say what the numbers mean; merge grouped "
            "results with the same grouping using combine; use view pivot for a cross table of several measures. "
            "Results you do not present stay available to the user under 'Data used'.",
            "schema": PRESENT_SCHEMA,
            "raw_schema": PRESENT_SCHEMA,
            "kind": "present",
        })
        tools.append({
            "name": "get_record_history",
            "description": "Read the conversation history (emails, messages, internal notes, logged calls) of up to "
            f"{HISTORY_RECORD_CAP} records, oldest first, e.g. opportunities, quotations or tasks. Find the record IDs "
            "first with search_records. Use it to summarise conversations or to rate how people handled them.",
            "schema": HISTORY_SCHEMA,
            "raw_schema": HISTORY_SCHEMA,
            "kind": "history",
        })
        tools.append({
            "name": "show_chart",
            "description": "Show the user a native Odoo graph (bar, line, pie) or pivot table of grouped records. "
            "Use it whenever the user asks for a chart, graph, plot, trend, distribution or breakdown. The user can "
            "switch the chart type, open it in Odoo and export it to PDF or Excel.",
            "schema": CHART_SCHEMA,
            "raw_schema": CHART_SCHEMA,
            "kind": "chart",
        })
        return tools

    # ------------------------------------------------------------------
    # Instructions
    # ------------------------------------------------------------------
    @api.model
    def _system_prompt(self, config):
        user = self.env.user
        today = fields.Date.context_today(self.with_context(tz=user.tz or "UTC"))
        enabled = mcp_utils.get_enabled_models(self.env)
        model_lines = []
        for entry in enabled[:MODEL_LIST_CAP]:
            ops = entry.get("operations") or {}
            allowed = [label for op, label in (("read", "read"), ("create", "create"), ("write", "update"),
                                               ("unlink", "delete")) if ops.get(op)]
            if mcp_utils.check_model_method_allowed(self.env, entry["model"]):
                allowed.append("buttons")
            model_lines.append(f"- {entry['model']} ({entry['name']}): {', '.join(allowed) or 'none'}")
        if len(enabled) > MODEL_LIST_CAP:
            model_lines.append(f"- … and {len(enabled) - MODEL_LIST_CAP} more (call list_models)")
        companies = ", ".join(f"{c.name} (id {c.id})" for c in self.env.companies)
        if config["share_data"]:
            privacy = (
                "- Tool results are shown to the user under your message AND sent to you. Analyse them: explain, "
                "compare, summarise, rate and give insights in your own words, quoting the exact figures from the "
                "results. Figures returned by tools are exact, so never call them provisional or unverified. Never "
                "invent values that are not in a result.\n"
                "- Never count rows or add up numbers yourself: quote the counts and totals the tool results give "
                "(\"Exact figures\", or aggregate_records, which without groupby returns overall totals).\n"
            )
        else:
            privacy = (
                "- You never see record values. Odoo runs each data tool with the user's own access rights and shows "
                "the result (table, record card, chart) to the user right under your message. You only receive a "
                "summary: counts, column names and record IDs.\n"
                "- So never state, guess or invent values (names, amounts, dates, emails, totals, who is first). "
                "Introduce what is shown in one short sentence, e.g. \"Here are your 12 open quotations, newest "
                "first.\" To answer \"how much\", \"who is top\" or \"what trend\", show it with aggregate_records "
                "or show_chart; the user reads the numbers there.\n"
                "- Never ask the user to paste data you could look up.\n"
            )
        return f"""You are AI Agent, the assistant built into Odoo {release.major_version} for {self.env.company.name}.
Today is {today.isoformat()} ({today.strftime('%A')}). The user is {user.name} (user id {user.id}, partner id {user.partner_id.id}), timezone {user.tz or 'UTC'}, language {user.lang or 'en_US'}. Active companies: {companies}.

SCOPE
- You only help with Odoo: the data in this database and how to use Odoo's apps.
- If a request is not about Odoo (general knowledge, news, sports, weather, coding unrelated to Odoo, personal advice, jokes, writing unrelated texts...), do not answer it and do not call tools. Reply politely in one or two sentences that you can only help with Odoo and its data. Add no links.
- "How do I…" and "where is…" questions about Odoo may be answered from your knowledge of Odoo {release.major_version}, in a few short steps.
- Links: do not add documentation or forum links by default. Add one link to https://www.odoo.com/documentation/{release.major_version}/ only when the user asks for documentation, or when a how-to needs more than a few steps to explain. Never add links to data answers or to "I can't do that" answers.

DATA AND PRIVACY
{privacy}
DATA YOU MAY USE (technical name (label): what the AI may do):
{chr(10).join(model_lines) or '- none yet'}
- If the user asks about data whose model is not in this list (for example installed apps: ir.module.module), or asks for an action that is not allowed on a listed model, still call the matching tool with that model's technical name. Odoo checks the access and shows the user who can enable it. Never answer "I can't access that" from this list yourself.
- When a tool result starts with NOT ENABLED, the explanation is already shown to the user: reply with at most one short sentence, do not repeat or rephrase it, and do not suggest workarounds.

HOW TO USE THE TOOLS
- Check field names with get_fields before filtering or grouping on a field you are not sure exists. Never guess field names or selection values.
- search_records for lists: pick 4 to 8 useful columns in 'fields', a sensible 'order', and limit at most {TABLE_ROW_CAP}.
- get_record for the details of one record (pass the 6 to 12 'fields' that matter); aggregate_records for counts, sums and averages per group; show_chart for any chart or visual.
- Filter by name with "ilike" on the name or on a related name (e.g. ["partner_id.name", "ilike", "acme"]).
- First person ("my", "mine", "me", "do I have", in any language) limits the question to the current user's own records: use the model's "My …" filter from get_filters, or the responsible-user field = {user.id} (partner fields: {user.partner_id.id}).
- Follow-ups ("those", "only the top 3", "and last month?") keep every condition of the previous question (owner, status, period, filters) unless the user changes it. "Their", "it" and "them" refer to the subject the user named most recently.
- Dates are "YYYY-MM-DD"; datetimes are "YYYY-MM-DD HH:MM:SS" in UTC. Work out "this month", "last quarter" and similar from today's date.
- For complex questions, chain tools: use the IDs one tool returns in the next tool's domain (["partner_id", "in", [ids]]).
- If a tool returns an error, fix the call (field names, operators, values) and try again; after two failures explain the problem in plain words.
- Never repeat a query you already ran in this answer: reuse its result name.
- Before filtering on business meaning (opportunities vs leads, won, lost, quotations, to invoice, late, my records...), call get_filters for the model: pass the screen that matches the user's words as 'screen' (it sets the base scope, like opening that menu) and the matching 'filters'. They are Odoo's own definitions; prefer them over writing your own conditions.
- For relative dates (today, this week, this month, last 6 months, this quarter, next 7 days...), pass 'period' instead of writing dates; Odoo computes them.
- Quantities and amounts of what actually happened (produced or production output, sold, delivered, received, paid) count only records whose status means it happened, not drafts, plans or cancelled ones, unless the user asks otherwise. Use the model's real status values (get_fields or get_filters; e.g. a payment may be "in process" or "paid", never filter on a value the model does not have) and say which status you used.
- For questions about when something changed or happened (cancelled this month, moved to Won this week), use find_changes.
- When the user asks about an Odoo app, a menu, or where or how to do something in Odoo, call show_app with the app (and the menu that fits, if any) so they get the app's icon to click. If show_app says the app is not available, tell the user it is not installed or not accessible to them.
- Results only contain records the user may access. When nothing is found, say that nothing was found among the records the user can access; never state that records do not exist.

PRESENTING THE ANSWER
- Every data result is named [r1], [r2], ... and is NOT shown to the user by itself.
- End every answer that used data with ONE call to present, listing only what answers the question. Give each table or chart a clear title, and rename columns with labels so they say what the numbers mean (e.g. when you filtered won opportunities, label __count "Won deals" and expected_revenue:sum "Won revenue").
- Use combine to merge grouped results that share the same grouping into one table (e.g. opportunities and won deals per salesperson). Use view pivot for one cross table of several measures, and bar, line or pie for a chart of a grouped result.
- Results you do not present stay available to the user under "Data used". Do not call present for answers without data.
- Include archived records only when asked: ["active", "in", [true, false]].

CHANGES (create, update, delete, buttons, messages)
- Change tools never run directly: Odoo shows the user a preview with Confirm and Cancel. After proposing a change, tell the user to review and confirm it; do not propose the same change twice.
- Never invent values for a change. If a required value or the target record is missing or ambiguous, ask the user first. Use get_fields for required fields and allowed values, and search_records to find record IDs.
- To press a button (confirm an order, mark a lead as won, post an invoice...), use call_model_method with the button's method name on the record IDs.

WHEN SOMETHING IS UNCLEAR
- Company definitions differ (which date defines "this month", which statuses count as "sales", which team…). When the answer depends on such a choice and the user has not said it in this conversation, ask one short clarifying question before running tools. Remember the user's answer for the rest of the conversation.
- When a sensible default is obvious and the question is simple, you may use it instead, and say in one short sentence which assumption you made.
- When a word fits two different things and the answers would differ (a name that matches several records, a term that exists in two apps, a status that belongs to another kind of record), do not pick one silently: ask one short question naming the options. If you looked them up, show the matches so the user can choose.

CONVERSATIONS AND RATINGS
- For emails, messages, notes or how people handled customers, find the records first (e.g. search_records on crm.lead filtered by salesperson), then call get_record_history with their IDs.
- Rating people, conversations or deals from Odoo data is a normal business request: do it, do not refuse. Use the user's criteria; if none are given, choose clear ones (for example response time, answering the customer's questions, clarity, proposing next steps, tone), say which you used, give each person or record a score (e.g. 1 to 5) with reasons and short examples from the messages, then a conclusion. Base it only on the data you read.
- If the user does not say whom to compare, pick the people with the most relevant records and say so.

STYLE
- Reply in the language of the user's last message. Match the length to the request: one to three sentences for simple lookups and actions; for "explain", "analyse", "compare", "summarise", "rate" or "why" questions give a complete, structured answer (short headings or bullet points, the key figures, a conclusion). Markdown is fine.
- Never write tables of data in your text: show them with present. In your text, explain and highlight what matters.
- Do not mention tools, MCP, JSON, domains or technical IDs unless the user asks."""

    @api.model
    def _suggestions(self):
        """Example questions for the welcome screen, limited to data this user and the AI may read."""
        examples = [
            ("sale.order", _("Show my quotations from this month")),
            ("crm.lead", _("How many open opportunities do I have, per stage?")),
            ("account.move", _("Which customer invoices are overdue?")),
            ("sale.order", _("Bar chart of confirmed sales by customer this year")),
            ("project.task", _("My open tasks due this week")),
            ("purchase.order", _("Purchase orders waiting to be received")),
            ("stock.quant", _("Products with the lowest stock on hand")),
            ("hr.employee", _("Pie chart of employees per department")),
            ("res.partner", _("Create a new contact")),
        ]
        suggestions = []
        for model, text in examples:
            if model in self.env and mcp_utils.check_model_operation_allowed(self.env, model, "read")                     and self.env[model].has_access("read"):
                suggestions.append(text)
        return suggestions[:4] or [_("What can you do?")]

    # ------------------------------------------------------------------
    # Conversation history
    # ------------------------------------------------------------------
    @api.model
    def _history(self, channel):
        ai_partner = channel._ai_agent_partner()
        messages = self.env["mail.message"].sudo().search(
            [("model", "=", "discuss.channel"), ("res_id", "=", channel.id), ("message_type", "=", "comment")],
            order="id desc", limit=HISTORY_MESSAGES,
        )
        history = []
        for message in reversed(messages):
            text = html2plaintext(message.body or "").strip()
            if message.author_id == ai_partner:
                notes = (message.ai_agent_trace or {}).get("notes") or []
                if notes:
                    text = "[What I did for this answer: " + " | ".join(notes) + "]\n" + text
                entry = {"role": "assistant", "content": text or "…", "tool_calls": []}
            else:
                if message.attachment_ids:
                    names = ", ".join(f"{a.name} (attachment id {a.id})" for a in message.attachment_ids)
                    text = f"{text}\n[Files attached by the user: {names}]".strip()
                entry = {"role": "user", "content": text or "…"}
            if history and history[-1]["role"] == entry["role"]:
                history[-1]["content"] += "\n\n" + entry["content"]
            else:
                history.append(entry)
        while history and history[0]["role"] != "user":
            history.pop(0)
        return history

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    @api.model
    def _ai_agent_answer(self, channel):
        """Answer the latest user message of ``channel`` and post the AI's reply there."""
        channel._ai_agent_check_conversation()
        ai_partner = channel._ai_agent_partner()
        last = self.env["mail.message"].sudo().search(
            [("model", "=", "discuss.channel"), ("res_id", "=", channel.id), ("message_type", "=", "comment")],
            order="id desc", limit=1,
        )
        if not last or last.author_id == ai_partner:
            return {"status": "nothing_to_answer"}
        channel._ai_agent_set_title_from(html2plaintext(last.body or ""))
        started = time.monotonic()
        state = {
            "blocks": [], "notes": [], "steps": [], "tools_used": [], "tool_errors": 0, "requests": 0,
            "input": 0, "output": 0, "cached": 0, "cost": 0.0, "answered_by": None,
        }
        config = None
        text, error = "", None
        try:
            config = self._ai_config(channel)
            text = self._run_loop(channel, config, state)
        except (ProviderError, UserError, AccessError) as err:
            error = str(err.args[0] if err.args else err)
        except Exception:
            _logger.exception("AI Agent: answering failed in conversation %s", channel.id)
            error = _("Something went wrong while preparing the answer. Please try again; if it keeps happening, "
                      "an administrator can find the details in the Odoo server log.")
        message = self._post_answer(channel, text, error, state, config)
        self._log_usage(channel, message, config, state, error, time.monotonic() - started)
        return {"status": "error" if error else "done", "message_id": message.id}

    def _run_loop(self, channel, config, state):
        provider, model = config["provider"], config["model"]
        deadline = self._deadline(config["timeout"])
        tools = self._ai_tools(config["share_data"])
        state["share_data"] = config["share_data"]
        tools_by_name = {tool["name"]: tool for tool in tools}
        system = self._system_prompt(config)
        messages = self._history(channel)
        if not messages:
            return ""
        text = ""
        for step in range(config["max_steps"] + 1):
            remaining = deadline - time.monotonic()
            if remaining < TIME_MARGIN + 5:
                raise ProviderError(_("The AI took too long to finish this answer. Try a simpler question or a "
                                      "faster model."))
            self._progress(channel, _("Thinking…") if step == 0 else _("Putting the answer together…"))
            last_step = step == config["max_steps"]
            reply = provider._chat(
                model.code, system + (FINAL_STEP_NOTE if last_step else ""), messages, tools, config["max_tokens"],
                timeout=int(min(config["timeout"], remaining - TIME_MARGIN)), deadline=deadline,
                allow_tools=not last_step,
            )
            self._add_usage(state, reply, model)
            messages.append(reply["assistant"])
            if reply["text"]:
                text = reply["text"]
            if not reply["tool_calls"]:
                break
            for call in reply["tool_calls"]:
                tool = tools_by_name.get(call["name"])
                self._progress(channel, self._progress_label(call))
                result = self._run_tool(call, tool, config, state)
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": result})
        if not reply["text"] and state["steps"] and deadline - time.monotonic() > TIME_MARGIN + 5:
            # Rare: tools ran but the closing reply was empty. Ask once, plainly, for the answer.
            if messages and not messages[-1].get("content") and not messages[-1].get("tool_calls"):
                messages.pop()
            messages.append({"role": "user", "content": FINAL_ANSWER_NUDGE})
            reply = provider._chat(
                model.code, system + FINAL_STEP_NOTE, messages, tools, config["max_tokens"],
                timeout=int(min(config["timeout"], deadline - time.monotonic() - TIME_MARGIN)), deadline=deadline,
                allow_tools=False,
            )
            self._add_usage(state, reply, model)
            text = reply["text"] or text
        if text or not state["steps"]:
            return text
        return _("I couldn't finish this answer. The results I found are below; try asking in a simpler way.")

    @api.model
    def _add_usage(self, state, reply, model):
        usage = reply.get("usage") or {}
        state["requests"] += 1
        state["input"] += usage.get("input") or 0
        state["output"] += usage.get("output") or 0
        state["cached"] += usage.get("cached") or 0
        state["cost"] += model._cost(usage)
        state["answered_by"] = reply.get("model")

    @api.model
    def _progress_label(self, call):
        arguments = call.get("arguments") or {}
        if call["name"] in WRITE_TOOLS:
            return _("Preparing a change for you to confirm…")
        if call["name"] not in PROGRESS_LABELS:
            return _("Working…")
        template = str(PROGRESS_LABELS[call["name"]])
        if "%s" not in template:
            return template
        return template % self._model_label(arguments.get("model"))

    @api.model
    def _progress(self, channel, text):
        """Push a live status line to the user's browser (committed on its own cursor)."""
        try:
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, self.env.uid, {})
                env.user._bus_send("odoo_ai_agent/progress", {"channel_id": channel.id, "text": str(text)})
        except Exception:  # noqa: BLE001 - progress is best effort
            _logger.debug("AI Agent: progress notification failed", exc_info=True)

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------
    @api.model
    def _run_tool(self, call, tool, config, state):
        name = call["name"]
        arguments = call.get("arguments") or {}
        state["tools_used"].append(name)
        step = {"tool": name, "label": self._progress_label(call).rstrip("…"), "status": "ok"}
        state["steps"].append(step)
        if tool is None:
            return self._tool_failed(state, step, call, _("There is no tool named %s.", name))
        if "__invalid_json__" in arguments:
            return self._tool_failed(state, step, call, _("The tool arguments were not valid JSON."))
        blocked = self._not_enabled(name, arguments)
        if blocked:
            return self._tool_not_enabled(state, step, call, blocked)
        try:
            if tool["kind"] == "chart":
                result = self._tool_chart(arguments, state)
            elif tool["kind"] == "history":
                result = self._tool_history(arguments, config, state)
            elif tool["kind"] == "present":
                result = self._tool_present(arguments, state)
            elif tool["kind"] == "filters":
                result = self._tool_get_filters(arguments)
            elif tool["kind"] == "app":
                result = self._tool_show_app(arguments, state)
            elif tool["kind"] == "changes":
                result = self._tool_find_changes(arguments, state)
            elif tool["kind"] == "write":
                result = self._tool_propose(tool, arguments, state)
            else:
                result = self._tool_read(tool, arguments, config, state)
        except (AccessError, MissingError, IndexError) as err:
            if name != "get_record":
                message = error_sanitizer.sanitize_message(str(err.args[0] if err.args else err))
                return self._tool_failed(state, step, call, self._explain_error(message))
            # Same answer whether the record does not exist or belongs to someone else: no existence leak.
            return self._tool_failed(state, step, call, _(
                "That record was not found among the records this user can access. Say exactly that; do not say "
                "the user lacks access to the whole model."))
        except (UserError, ValidationError, ValueError, TypeError, KeyError) as err:
            message = error_sanitizer.sanitize_message(str(err.args[0] if err.args else err))
            return self._tool_failed(state, step, call, self._explain_error(message))
        except Exception as err:  # noqa: BLE001 - the AI gets a clean message, the log gets the details
            _logger.warning("AI Agent: tool %s failed", name, exc_info=True)
            return self._tool_failed(state, step, call, error_sanitizer.sanitize_exception(err, f"AI Agent tool {name}"))
        self._note(state, call, result)
        return result

    @api.model
    def _not_enabled(self, tool_name, arguments):
        """The message for a model or operation the MCP settings do not allow, or None when allowed."""
        operation = TOOL_OPERATIONS.get(tool_name)
        model = arguments.get("model")
        if not operation or not isinstance(model, str) or model not in self.env:
            return None
        if operation == "method":
            allowed = mcp_utils.check_model_method_allowed(self.env, model)
        else:
            allowed = mcp_utils.check_model_operation_allowed(self.env, model, operation)
        return None if allowed else self._not_enabled_message(model, operation)

    @api.model
    def _not_enabled_message(self, model, operation):
        """Administrators learn exactly what to enable and where; other users only that it is not enabled."""
        if not self.env.user.has_group("odoo_ai_agent.group_ai_agent_manager"):
            return _("Your administrator has not enabled this for you.")
        label = self._model_label(model)
        checkbox = OPERATION_CHECKBOXES[operation]
        where = _("AI Agent > Configuration > Settings > Enabled MCP models > Manage MCP models")
        if mcp_utils.is_model_mcp_enabled(self.env, model):
            return _("%(label)s (%(model)s) is enabled in MCP, but \"%(checkbox)s\" is not ticked. To allow it, go "
                     "to %(where)s, open %(label)s and tick \"%(checkbox)s\".",
                     label=label, model=model, checkbox=checkbox, where=where)
        return _("%(label)s (%(model)s) is not enabled in MCP. To allow it, go to %(where)s, click New, choose "
                 "the model %(label)s (%(model)s) and tick \"%(checkbox)s\".",
                 label=label, model=model, checkbox=checkbox, where=where)

    def _add_notice(self, state, message):
        if message not in state.setdefault("notices", []):
            state["notices"].append(message)
            state["blocks"].append({"type": "notice", "id": uuid.uuid4().hex, "level": "warning", "text": message})

    def _tool_not_enabled(self, state, step, call, message):
        step.update(status="error", detail=message)
        self._add_notice(state, message)
        text = f"NOT ENABLED (already shown to the user): {message}"
        self._note(state, call, text)
        return text

    @api.model
    def _explain_error(self, message):
        """Add what the AI needs to correct itself: the real fields close to a wrong one, the order syntax."""
        quoted = re.search(r"Invalid field '(\w+)' on '([\w.]+)'", message)
        match = re.search(r"Invalid field ([\w.]+)", message)
        target = None
        if quoted and quoted.group(2) in self.env:
            target = (quoted.group(2), quoted.group(1))
        elif match:
            parts = match.group(1).split(".")
            for index in range(len(parts) - 1, 0, -1):
                if ".".join(parts[:index]) in self.env:
                    target = (".".join(parts[:index]), parts[index])
                    break
        if target:
            model, field = target
            fields_map = self.env[model]._fields
            similar = difflib.get_close_matches(field, list(fields_map), n=6, cutoff=0.5)
            message += f" {model} has no field '{field}'."
            if field == "active":
                message += " These records cannot be archived, so do not filter on 'active'."
            if similar:
                message += f" Similar fields: {', '.join(similar)}."
            if not difflib.get_close_matches(field, list(fields_map), n=1, cutoff=0.75):
                # Nothing close by name (fields renamed between Odoo versions): show the links to other records.
                links = [f"{name} ({f.string} → {f.comodel_name})" for name, f in fields_map.items()
                         if f.relational and not name.startswith(("message_", "activity_", "website_message"))
                         and name not in ("create_uid", "write_uid")][:20]
                if links:
                    message += f" Fields of {model} that link to other records: {', '.join(links)}."
            message += " For statuses, get_filters lists the model's own filters."
        elif match := re.search(r"Cannot convert ([\w.]+)\.(\w+)\b.* to SQL because", message):
            model, field_name = match.group(1), match.group(2)
            field = self.env[model]._fields.get(field_name) if model in self.env else None
            searchable = bool(field and field.search)
            message += (f" '{field_name}' is computed on the fly, so it cannot be used in groupby or order"
                        + (" (it can be used in the domain)." if searchable else " or in the domain.")
                        + " get_filters may list a ready-made filter for it.")
            try:
                depends = [d for d in field.get_depends(self.env[model])[0] if "." in d or d in self.env[model]._fields]
            except Exception:  # noqa: BLE001 - the hint is optional
                depends = []
            if depends:
                message += (f" It is computed from: {', '.join(depends[:6])}. For totals or charts, use those stored "
                            f"fields (on the linked records) or a stored field with the same meaning.")
        elif "Invalid order" in message or "order" in message.lower() and "invalid" in message.lower():
            message += (" Write order without quotes, e.g. 'amount_total desc'; for aggregate_records use a groupby "
                        "or aggregate name such as 'amount_total:sum desc'.")
        return message

    def _tool_failed(self, state, step, call, message):
        state["tool_errors"] += 1
        step.update(status="error", detail=message)
        text = f"ERROR: {message}"
        self._note(state, call, text)
        return text

    @api.model
    def _note(self, state, call, result):
        """A one-line record of the call for follow-up questions (what the AI saw, nothing more)."""
        arguments = json.dumps(call.get("arguments") or {}, ensure_ascii=False, default=str)
        tool_result = result if call["name"] not in SCHEMA_TOOLS else "definitions read"
        note = f"{call['name']}({arguments}) -> {tool_result}"
        state["notes"].append(note[:TRACE_NOTE_CAP])

    @api.model
    def _check_arguments(self, schema, arguments):
        missing = [key for key in schema.get("required") or [] if key not in arguments]
        if missing:
            raise UserError(_("Missing required argument(s): %s", ", ".join(missing)))
        properties = schema.get("properties")
        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            unknown = sorted(key for key in arguments if key not in properties)
            if unknown:
                raise UserError(_("Unknown argument(s): %s", ", ".join(unknown)))

    @api.model
    def _call_mcp(self, tool, arguments):
        """Run an MCP tool as the current user, exactly like the /mcp endpoint does."""
        self._check_arguments(tool["raw_schema"], arguments)
        with self.env.cr.savepoint():
            if tool.get("custom_tool_id"):
                custom = self.env["mcp.custom.tool"].browse(tool["custom_tool_id"])
                if not custom._user_can_run():
                    raise AccessError(_("You are not allowed to run the '%s' tool.", custom.name))
                output = custom._run_tool(arguments)
                text = json.dumps(output if output is not None else _("Done."), ensure_ascii=False, default=str)
                return {"content": [{"type": "text", "text": text}], "structuredContent": {"result": output}}
            return dict(getattr(self.env["mcp.mixin"], tool["method"])(**arguments))

    @staticmethod
    def _result_text(result):
        return "\n".join(
            part.get("text", "") for part in result.get("content") or [] if isinstance(part, dict)
        ).strip()

    # ------------------------------------------------------------------
    # Read tools
    # ------------------------------------------------------------------
    @api.model
    def _tool_read(self, tool, arguments, config, state):
        name = tool["name"]
        if name == "search_records" and arguments.get("limit"):
            arguments["limit"] = min(int(arguments["limit"]), TABLE_ROW_CAP)
        elif name == "search_records":
            arguments["limit"] = min(TABLE_ROW_CAP, 40)
        if isinstance(arguments.get("order"), str):
            arguments["order"] = arguments["order"].strip().strip("\"'").strip() or None
        signature = f"{name}:{json.dumps(arguments, sort_keys=True, default=str)}"
        if tool["kind"] != "schema":
            existing = state.setdefault("signatures", {}).get(signature)
            if existing:
                return f"[{existing}] Same query as {existing}; use that result."
        if any(key in arguments for key in ("screen", "filters", "period")):
            model = arguments.get("model")
            if model in self.env:
                extra, labels = self._scope_terms(model, arguments)
                domain = self.env["mcp.mixin"]._coerce_domain(arguments.get("domain")) or []
                arguments["domain"] = [list(t) if isinstance(t, tuple) else t for t in domain] + extra
                state.setdefault("filter_labels", {})[signature] = labels
            for key in ("screen", "filters", "period"):
                arguments.pop(key, None)
        dropped = self._drop_unsortable(arguments)
        if name == "aggregate_records" and not arguments.get("groupby"):
            arguments.pop("groupby", None)
            structured = self._aggregate_totals(arguments)
            text = json.dumps(structured["groups"], ensure_ascii=False, default=str)
        else:
            result = self._call_mcp(tool, arguments)
            text = self._result_text(result)
            if tool["kind"] == "schema":
                return text[:SCHEMA_TEXT_CAP]
            structured = result.get("structuredContent") or {}
        if name == "search_records":
            summary = self._block_table(arguments, structured, state, signature)
        elif name == "get_record":
            summary = self._block_record(arguments, structured, state, signature)
        elif name == "aggregate_records":
            summary = self._block_groups(arguments, structured, state, signature)
        elif name == "list_record_attachments":
            summary = self._block_attachments(structured, state, signature)
        else:
            summary = self._block_text(name, text, state, signature)
        summary += dropped
        if config["share_data"]:
            summary += "\n\nRESULT (also shown to the user):\n" + text[:SHARED_TEXT_CAP]
        return summary

    @api.model
    def _aggregate_totals(self, arguments):
        """Overall totals of the matching records (aggregate_records without groupby), with the same MCP gates."""
        mixin = self.env["mcp.mixin"]
        model = arguments.get("model")
        mixin._resolve_model(model)
        mixin._check_op(model, "read")
        aggregates = arguments.get("aggregates") or ["__count"]
        if isinstance(aggregates, str):
            aggregates = [aggregates]
        domain = mixin._coerce_domain(arguments.get("domain")) or []
        with self.env.cr.savepoint():
            groups = self.env[model].formatted_read_group(domain, [], list(aggregates))
        groups = [{key: value for key, value in group.items() if not key.startswith("__") or key in aggregates}
                  for group in groups]
        return {"model": model, "groupby": [], "aggregates": list(aggregates), "groups": groups}

    @api.model
    def _drop_unsortable(self, arguments):
        """Remove sort keys on computed fields Odoo cannot sort by, and say so (instead of a failed query)."""
        model, order = arguments.get("model"), arguments.get("order")
        if not isinstance(order, str) or model not in self.env:
            return ""
        fields_map = self.env[model]._fields
        grouped = "groupby" in arguments
        groupby = arguments.get("groupby") or []
        groupby = [groupby] if isinstance(groupby, str) else groupby
        aggregates = arguments.get("aggregates") or ["__count"]
        aggregates = [aggregates] if isinstance(aggregates, str) else aggregates
        # a grouped query can only be sorted by its groups and its totals (what Odoo's pivot allows)
        sortable = {s.split(":")[0] for s in groupby} | set(groupby) | set(aggregates) | {"__count"}
        kept, dropped = [], []
        for term in (t.strip() for t in order.split(",") if t.strip()):
            name = term.split()[0]
            field = fields_map.get(name)
            if grouped and name not in sortable:
                dropped.append(field.string if field else name)
            elif ":" in name or name.startswith("__") or field is None or field.store:
                kept.append(term)  # aggregates and unknown names are left to MCP's own validation
            else:
                dropped.append(field.string or name)
        if not dropped:
            return ""
        arguments["order"] = ", ".join(kept) or None
        reason = ("a grouped result can only be sorted by its groups or totals" if grouped else
                  "Odoo computes it on the fly and cannot sort by it (filtering on it in the domain may still work)")
        return f" Not sorted by {', '.join(dropped)}: {reason}."

    @api.model
    def _ai_add_result(self, state, block, signature=None):
        """Name a data result (r1, r2, ...) for the AI and keep it for the answer."""
        results = state.setdefault("results", {})
        ref = f"r{len(results) + 1}"
        block["ref"] = ref
        results[ref] = block
        state["blocks"].append(block)
        if signature:
            state.setdefault("signatures", {})[signature] = ref
        return ref

    @staticmethod
    def _column_text(columns):
        return ", ".join(f"{c['name']} ({c['label']})" for c in columns)

    @api.model
    def _model_label(self, model):
        if model and model in self.env:
            return self.env["ir.model"]._get(model).name or model
        return model or _("records")

    @api.model
    def _columns(self, model, names):
        Model = self.env[model]
        meta = Model.fields_get(list(names), attributes=["string", "type", "selection", "currency_field", "digits",
                                                         "relation"])
        columns = []
        for name in names:
            info = meta.get(name)
            if not info or info["type"] in ("binary", "image") or name == "id":
                continue
            column = {"name": name, "label": info.get("string") or name, "type": info["type"]}
            if info.get("selection"):
                column["selection"] = info["selection"]
            if info.get("currency_field"):
                column["currency_field"] = info["currency_field"]
            if info["type"] in ("many2many", "one2many") and info.get("relation"):
                column["relation"] = info["relation"]
            columns.append(column)
        return columns

    @api.model
    def _with_x2many_names(self, columns, rows, shown=8):
        """Replace lists of record IDs (assignees, tags, groups, lines...) by the names the user may read."""
        for column in columns:
            relation = column.pop("relation", None)
            if not relation or relation not in self.env:
                continue
            ids = {i for row in rows for i in (row.get(column["name"]) or [])[:shown] if isinstance(i, int)}
            try:
                records = self.env[relation].browse(sorted(ids)).exists()._filtered_access("read")
                names = {record.id: record.display_name for record in records}
            except Exception:  # noqa: BLE001 - names are a convenience; the counts stay correct without them
                continue
            for row in rows:
                value = row.get(column["name"])
                if not isinstance(value, list):
                    continue
                labels = [names.get(i, f"#{i}") for i in value[:shown]]
                more = _(" +%s more", len(value) - shown) if len(value) > shown else ""
                row[column["name"]] = ", ".join(labels) + more if labels else False
            column["type"] = "char"
        return rows

    @staticmethod
    def _cell(value):
        if isinstance(value, str) and len(value) > CELL_TEXT_CAP:
            return value[:CELL_TEXT_CAP] + "…"
        if isinstance(value, tuple):
            return list(value)
        return value

    @api.model
    def _block_table(self, arguments, structured, state, signature=None):
        model = structured.get("model")
        rows = (structured.get("records") or [])[:TABLE_ROW_CAP]
        total = structured.get("total", len(rows))
        label = self._model_label(model)
        names = []
        for row in rows:
            names.extend(key for key in row if key not in names)
        columns = self._columns(model, names) if rows else []
        rows = self._with_x2many_names(columns, [dict(row) for row in rows])
        currency_field = next((c["currency_field"] for c in columns if c.get("currency_field")), None)
        ref = self._ai_add_result(state, {
            "type": "table",
            "id": uuid.uuid4().hex,
            "title": label,
            "model": model,
            "model_label": label,
            "columns": columns,
            "rows": [
                {
                    "id": row.get("id"),
                    "values": {c["name"]: self._cell(row.get(c["name"])) for c in columns},
                    # The currency of monetary amounts, when the search returned it.
                    "currency": self._cell(row.get(currency_field)) if currency_field else None,
                }
                for row in rows
            ],
            "total": total,
            "domain": self._safe_domain(self.env["mcp.mixin"]._coerce_domain(arguments.get("domain")) or []),
            "order": arguments.get("order") or False,
            "subtitle": self._scope_text(model, self.env["mcp.mixin"]._coerce_domain(arguments.get("domain")) or [],
                                         state.get("filter_labels", {}).get(signature)),
        }, signature)
        ids = [row.get("id") for row in rows if row.get("id")]
        if not rows:
            return f"[{ref}] No {label} records that this user can access match."
        more = f" There are {total - len(rows)} more; the user can open the full list in Odoo." if total > len(rows) else ""
        facts = self._row_facts(columns, rows, currency_field) if state.get("share_data") else ""
        return (
            f"[{ref}] {total} {label} record(s) that this user can access match; {len(rows)} rows with the columns "
            f"{self._column_text(columns)}. Record IDs: {ids[:60]}.{more}{facts}"
        )

    @api.model
    def _row_facts(self, columns, rows, currency_field=None):
        """Exact counts per status/category and column totals of the returned rows, so the AI never counts itself."""
        parts = []
        for column in columns:
            values = [row.get(column["name"]) for row in rows]
            if column["type"] in ("selection", "many2one", "boolean"):
                labels = dict(column.get("selection") or [])
                counts = {}
                for value in values:
                    if isinstance(value, (list, tuple)) and len(value) == 2:
                        value = value[1]
                    key = labels.get(value, value) if value not in (None, False) else _("not set")
                    counts[str(key)] = counts.get(str(key), 0) + 1
                if 1 < len(counts) <= 12:
                    parts.append(f"{column['label']}: " + ", ".join(f"{k} {v}" for k, v in
                                                                    sorted(counts.items(), key=lambda kv: -kv[1])))
            elif column["type"] in ("integer", "float", "monetary") and column["name"] != "id":
                numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
                currencies = {str(row.get(currency_field)) for row in rows} if currency_field else set()
                if numbers and not (column["type"] == "monetary" and len(currencies) > 1):
                    parts.append(f"{column['label']} total {round(sum(numbers), 2):,}")
        if not parts:
            return ""
        return (f" Exact figures for these {len(rows)} rows (quote them; never count rows or add numbers yourself): "
                + "; ".join(parts) + ".")

    @api.model
    def _block_record(self, arguments, structured, state, signature=None):
        model = arguments.get("model")
        data = structured.get("record") or {}
        label = self._model_label(model)
        columns = self._columns(model, [k for k in data if k not in ("id", "display_name")])
        data = self._with_x2many_names(columns, [dict(data)])[0]
        ref = self._ai_add_result(state, {
            "type": "record",
            "id": uuid.uuid4().hex,
            "model": model,
            "model_label": label,
            "record_id": data.get("id"),
            "display_name": data.get("display_name") or "",
            "fields": [dict(column, value=self._cell(data.get(column["name"]))) for column in columns],
        }, signature)
        return (f"[{ref}] The {label} record (ID {data.get('id')}) as a card with the fields "
                f"{', '.join(c['label'] for c in columns)}.")

    @api.model
    def _block_groups(self, arguments, structured, state, signature=None):
        model = structured.get("model")
        groupby = structured.get("groupby") or []
        aggregates = structured.get("aggregates") or []
        groups = structured.get("groups") or []
        label = self._model_label(model)
        meta = self.env[model].fields_get([g.split(":")[0] for g in groupby] + [
            a.split(":")[0] for a in aggregates if not a.startswith("__")
        ], attributes=["string", "type", "selection"])
        columns = []
        for spec in groupby:
            info = meta.get(spec.split(":")[0], {})
            granularity = f" ({spec.split(':')[1]})" if ":" in spec else ""
            columns.append({"name": spec, "label": (info.get("string") or spec) + granularity,
                            "type": "group", "selection": info.get("selection")})
        for spec in aggregates:
            if spec == "__count":
                columns.append({"name": spec, "label": _("Count"), "type": "integer"})
            else:
                field, _sep, operator = spec.partition(":")
                info = meta.get(field, {})
                columns.append({"name": spec, "label": f"{info.get('string') or field} ({operator or 'sum'})",
                                "type": "float" if info.get("type") in ("monetary", "float") else info.get("type", "float")})
        domain = self._safe_domain(self.env["mcp.mixin"]._coerce_domain(arguments.get("domain")) or [])
        row_domains = self._group_domains(model, groupby, domain)
        ref = self._ai_add_result(state, {
            "type": "table",
            "id": uuid.uuid4().hex,
            "title": _("%(model)s by %(groups)s", model=label,
                       groups=", ".join(c["label"] for c in columns[:len(groupby)])) if groupby
            else _("%s totals", label),
            "model": model,
            "model_label": label,
            "columns": columns,
            "rows": [{"values": {c["name"]: self._cell(group.get(c["name"])) for c in columns},
                      # The exact records behind this row, so the user can open them (like Odoo's pivot).
                      "domain": row_domains.get(self._group_key(group, groupby))} for group in groups],
            "total": len(groups),
            "grouped": True,
            "subtitle": self._scope_text(model, domain, state.get("filter_labels", {}).get(signature)),
            # How it was computed, so present can turn it into a native graph or pivot.
            "spec": {"model": model, "groupby": groupby, "aggregates": aggregates,
                     "domain": [list(t) if isinstance(t, tuple) else t for t in domain]},
        }, signature)
        keys = []
        for group in groups[:40]:
            for spec in groupby:
                value = group.get(spec)
                if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], int):
                    keys.append(f"{spec.split(':')[0]} id {value[0]}")
        more = " More groups exist (has_more)." if structured.get("has_more") else ""
        key_text = f" Group record IDs: {keys}." if keys else ""
        if not groups:
            return f"[{ref}] No {label} records that this user can access match, so there are no groups."
        if not groupby:
            return (f"[{ref}] Overall totals of the matching {label} records (records this user can access) with "
                    f"the columns {self._column_text(columns)}.")
        return (f"[{ref}] {len(groups)} group(s) of {label} (records this user can access) with the columns "
                f"{self._column_text(columns)}.{key_text}{more}")

    @api.model
    def _block_attachments(self, structured, state, signature=None):
        attachments = structured.get("attachments") or []
        ref = self._ai_add_result(state, {
            "type": "table",
            "id": uuid.uuid4().hex,
            "title": _("Attachments"),
            "model": "ir.attachment",
            "model_label": _("Attachments"),
            "columns": [
                {"name": "name", "label": _("Name"), "type": "char"},
                {"name": "mimetype", "label": _("Type"), "type": "char"},
                {"name": "file_size", "label": _("Size (bytes)"), "type": "integer"},
            ],
            "rows": [{"id": a.get("id"), "values": {k: a.get(k) for k in ("name", "mimetype", "file_size")}}
                     for a in attachments],
            "total": structured.get("total", len(attachments)),
        }, signature)
        return (f"[{ref}] {len(attachments)} attachment(s). Attachment IDs: "
                f"{[a.get('id') for a in attachments]}.")

    @api.model
    def _block_text(self, name, text, state, signature=None):
        ref = self._ai_add_result(state, {"type": "text", "id": uuid.uuid4().hex, "title": name,
                                     "text": text[:SHARED_TEXT_CAP]}, signature)
        return f"[{ref}] Result of {name} (text)."

    @staticmethod
    def _safe_domain(domain):
        try:
            json.dumps(domain)
            return domain or []
        except (TypeError, ValueError):
            return []

    # ------------------------------------------------------------------
    # Odoo's own named filters and the scope shown under results
    # ------------------------------------------------------------------
    @api.model
    def _named_filters(self, model):
        """The named filters of every main search view of ``model`` (as the user sees them in Odoo)."""
        filters = {}
        views = self.env["ir.ui.view"].sudo().search(
            [("model", "=", model), ("type", "=", "search"), ("mode", "=", "primary")])
        for view in views:
            try:
                arch = self.env[model].get_view(view.id, "search")["arch"]
            except Exception:  # noqa: BLE001 - a view the user cannot load only costs its filters
                continue
            for node in etree.fromstring(arch).iter("filter"):
                name, domain = node.get("name"), (node.get("domain") or "").strip()
                if not name or not domain or domain == "[]" or node.get("date"):
                    continue
                filters.setdefault(name, {"label": node.get("string") or name, "domain": domain,
                                          "help": node.get("help") or ""})
        return filters

    @api.model
    def _screens(self, model):
        """The menus of ``model`` the user can open, with each window action's own scope."""
        Menu = self.env["ir.ui.menu"]
        screens = {}
        for menu in Menu.sudo().browse(sorted(Menu._visible_menu_ids())):
            action = menu.action
            if not action or action._name != "ir.actions.act_window" or action.res_model != model:
                continue
            context = action.context or ""
            defaults = re.findall(r"search_default_(\w+)['\"]?\s*:\s*(?:1|True)", context)
            screens[f"menu_{menu.id}"] = {
                "label": menu.complete_name.replace("/", " / "),
                "domain": (action.domain or "").strip() or "[]",
                "defaults": defaults,
                "action_id": action.id,
            }
        # Window actions reached another way (buttons, server actions behind a menu) also define screens,
        # e.g. CRM's pipeline action that only holds opportunities.
        known = {screen["action_id"] for screen in screens.values()}
        user_groups = self.env.user.all_group_ids
        actions = self.env["ir.actions.act_window"].sudo().search(
            [("res_model", "=", model), ("domain", "not in", [False, "", "[]"])], limit=30)
        seen = {(screen["label"], screen["domain"]) for screen in screens.values()}
        for action in actions:
            if action.id in known or (action.group_ids and not action.group_ids & user_groups):
                continue
            # Actions opened from a record (their scope is that record) and duplicates are no use here.
            if "active_id" in action.domain or (action.name, action.domain.strip()) in seen:
                continue
            seen.add((action.name, action.domain.strip()))
            screens[f"action_{action.id}"] = {
                "label": action.name,
                "domain": action.domain.strip(),
                "defaults": re.findall(r"search_default_(\w+)['\"]?\s*:\s*(?:1|True)", action.context or ""),
                "action_id": action.id,
            }
        return screens

    @api.model
    def _tool_show_app(self, arguments, state):
        """A clickable card with the app's own icon, opening the app or one of its menus (as the user can see them)."""
        Menu = self.env["ir.ui.menu"]
        visible = Menu.browse(Menu._visible_menu_ids())
        roots = visible.filtered(lambda m: not m.parent_id)

        def root_of(menu):
            return Menu.browse(int(menu.parent_path.split("/")[0])) if menu.parent_path else menu

        def best(candidates, wanted, key):
            wanted = " ".join(str(wanted or "").casefold().replace(">", " ").replace("/", " ").split())
            if not wanted:
                return candidates.browse()
            names = {m.id: " ".join(key(m).casefold().replace("/", " ").split()) for m in candidates}
            side = ("reporting", "configuration", "settings")

            def rank(menu):
                # Working menus before reports and configuration, unless the user asks for those.
                aside = any(word in names[menu.id] and word not in wanted for word in side)
                return (aside, len(names[menu.id]))

            for test in (lambda n: n == wanted, lambda n: n.endswith(wanted), lambda n: wanted in n):
                found = candidates.filtered(lambda m: test(names[m.id]))
                if found:
                    found = found.sorted(rank)[:1]
                    # Only a report or configuration screen matches and the user did not ask for one: no match.
                    return candidates.browse() if rank(found)[0] else found
            close = difflib.get_close_matches(wanted, list(names.values()), n=1, cutoff=0.6)
            return candidates.filtered(lambda m: names[m.id] == close[0])[:1] if close else candidates.browse()

        target = Menu.browse()
        model = arguments.get("model")
        if isinstance(model, str) and model in self.env:
            with_model = visible.filtered(lambda m: m.action and m.action._name == "ir.actions.act_window"
                                          and m.action.sudo().res_model == model)
            target = with_model.sorted(lambda m: (len(m.parent_path or ""), m.sequence))[:1]
        root = best(roots, arguments.get("app"), lambda m: m.name) or (root_of(target) if target else Menu.browse())
        if not root:
            apps = ", ".join(sorted(roots.mapped("name")))
            return (f"No app named '{arguments.get('app') or model}' among the apps this user can open (it may not "
                    f"be installed, or the user has no access). Apps they can open: {apps}.")
        if arguments.get("menu"):
            inside = visible.filtered(lambda m: m.parent_path and m.parent_path.startswith(f"{root.id}/")
                                      and m.id != root.id and m.action)
            target = best(inside, arguments["menu"], lambda m: m.complete_name) or target
        if not target or root_of(target) != root:
            target = root
        path = " › ".join(part.strip() for part in target.complete_name.split("/"))
        app = {
            "menu_id": target.id,
            "action_id": target.action.id if target.action else (root.action.id if root.action else False),
            "app": root.name,
            "path": path,
            "icon": f"/web/image/ir.ui.menu/{root.id}/web_icon_data" if root.web_icon_data else False,
        }
        shown = state.setdefault("apps_shown", set())
        if target.id not in shown:
            shown.add(target.id)
            state["blocks"].append({"type": "app", "id": uuid.uuid4().hex, "apps": [app]})
        found = "" if not arguments.get("menu") or target != root else f" No menu matching '{arguments['menu']}' was found, so the app itself opens."
        return f"Shown to the user: a clickable '{path}' app card with the app icon; it opens that screen.{found}"

    @api.model
    def _tool_get_filters(self, arguments):
        Model = self._check_model_read(arguments.get("model"))
        model = Model._name
        lines = []
        screens = self._screens(model)
        filters = self._named_filters(model)
        if screens:
            lines.append(f"Screens of {model}, as in Odoo's menus. 'screen' applies only the fixed scope; a screen's "
                         f"default filters are NOT applied automatically: add those that match the user's words to "
                         f"'filters' (e.g. a pipeline screen opening with 'Opportunities').")
            for key, screen in screens.items():
                scope = f"fixed scope {screen['domain']}" if screen["domain"] != "[]" else "no fixed scope"
                defaults = ", ".join(
                    f"{name} ({filters[name]['label']})" if name in filters else name for name in screen["defaults"])
                lines.append(f"- {key}: {screen['label']} ({scope}" + (f"; opens with filters {defaults}" if defaults else "") + ")")
        if filters:
            lines.append(f"Filters of {model} (pass names in 'filters'; combined with AND):")
            lines += [f"- {name}: {f['label']}" + (f" ({f['help']})" if f["help"] else "") for name, f in filters.items()]
        return "\n".join(lines) or f"{model} has no screens or named filters; write the conditions in 'domain'."

    @api.model
    def _domain_eval_context(self):
        today = fields.Date.context_today(self.with_context(tz=self.env.user.tz or "UTC"))
        return {
            "uid": self.env.uid, "context": dict(self.env.context), "allowed_company_ids": self.env.companies.ids,
            "company_id": self.env.company.id, "company_ids": self.env.companies.ids,
            "context_today": lambda: today, "datetime": safe_datetime, "time": safe_time,
            "relativedelta": safe_dateutil.relativedelta.relativedelta, "current_date": today.isoformat(),
        }

    @api.model
    def _scope_terms(self, model, arguments):
        """Domain terms and readable labels for a screen, named filters and a period (removed from arguments)."""
        extra, labels = [], []
        screen = arguments.pop("screen", None)
        if screen:
            screens = self._screens(model)
            if screen not in screens:
                raise UserError(_("Unknown screen %(screen)s for %(model)s. Available: %(available)s",
                                  screen=screen, model=model, available=", ".join(screens) or "none"))
            try:
                extra += self._plain_domain(safe_eval(screens[screen]["domain"], self._domain_eval_context()))
            except Exception as err:  # noqa: BLE001
                raise UserError(_("The screen %s cannot be used here; write the conditions in 'domain'.", screen)) from err
            labels.append(_("Screen: %s", screens[screen]["label"]))
        names = arguments.pop("filters", None) or []
        if names:
            terms, filter_labels = self._filter_domain(model, names)
            extra += terms
            labels.append(_("Filters: %s", ", ".join(filter_labels)))
        period = arguments.pop("period", None)
        if period:
            extra += self._period_domain(model, period)
        return extra, labels

    @api.model
    def _period_bounds(self, name, count=None):
        """[start, end) dates of a relative period. Policy: rolling periods include the current one."""
        today = fields.Date.context_today(self.with_context(tz=self.env.user.tz or "UTC"))
        lang = self.env["res.lang"]._get_data(code=self.env.user.lang or "en_US")
        week_start = int(getattr(lang, "week_start", None) or 1)
        week = today - datetime.timedelta(days=(today.isoweekday() - week_start) % 7)
        month = today.replace(day=1)
        fiscal_start_month = int(getattr(self.env.company, "fiscalyear_last_month", None) or 12) % 12 + 1
        year = datetime.date(today.year, fiscal_start_month, 1)
        if year > today:
            year -= relativedelta(years=1)
        quarter = year + relativedelta(months=((today.year - year.year) * 12 + today.month - year.month) // 3 * 3)
        n = max(1, int(count or 1))
        day = datetime.timedelta(days=1)
        periods = {
            "today": (today, today + day),
            "yesterday": (today - day, today),
            "this_week": (week, week + datetime.timedelta(days=7)),
            "last_week": (week - datetime.timedelta(days=7), week),
            "this_month": (month, month + relativedelta(months=1)),
            "last_month": (month - relativedelta(months=1), month),
            "this_quarter": (quarter, quarter + relativedelta(months=3)),
            "last_quarter": (quarter - relativedelta(months=3), quarter),
            "this_year": (year, year + relativedelta(years=1)),
            "last_year": (year - relativedelta(years=1), year),
            "year_to_date": (year, today + day),
            "last_n_days": (today - datetime.timedelta(days=n - 1), today + day),
            "last_n_weeks": (week - datetime.timedelta(days=7 * (n - 1)), week + datetime.timedelta(days=7)),
            "last_n_months": (month - relativedelta(months=n - 1), month + relativedelta(months=1)),
            "next_n_days": (today, today + datetime.timedelta(days=n + 1)),
        }
        if name not in periods:
            raise UserError(_("Unknown period %(name)s. Use one of: %(names)s", name=name, names=", ".join(PERIOD_NAMES)))
        return periods[name]

    @api.model
    def _utc_bound(self, day):
        local = pytz.timezone(self.env.user.tz or "UTC").localize(datetime.datetime.combine(day, datetime.time.min))
        return fields.Datetime.to_string(local.astimezone(pytz.utc).replace(tzinfo=None))

    @api.model
    def _period_domain(self, model, period):
        if not isinstance(period, dict) or not period.get("name"):
            raise UserError(_("period needs a name, e.g. {\"field\": \"date_order\", \"name\": \"this_month\"}."))
        field_name = period.get("field")
        field = self.env[model]._fields.get(field_name or "")
        if not field or field.type not in ("date", "datetime"):
            raise UserError(_("period needs the date field it applies to; %(field)s is not a date field of %(model)s.",
                              field=field_name, model=model))
        start, end = self._period_bounds(period["name"], period.get("count"))
        if field.type == "datetime":
            return [[field_name, ">=", self._utc_bound(start)], [field_name, "<", self._utc_bound(end)]]
        return [[field_name, ">=", fields.Date.to_string(start)], [field_name, "<", fields.Date.to_string(end)]]

    @staticmethod
    def _group_key(group, groupby):
        return tuple(json.dumps(v[0] if isinstance(v, (list, tuple)) and len(v) == 2 else v, default=str)
                     for v in (group.get(spec) for spec in groupby))

    @api.model
    def _group_domains(self, model, groupby, domain):
        """Each group's exact filter, from Odoo's own grouping (the __extra_domain the pivot view uses)."""
        try:
            groups = self.env[model].formatted_read_group(domain, groupby, ["__count"], limit=500)
        except Exception:  # noqa: BLE001 - drill-down is a convenience
            return {}
        result = {}
        for group in groups:
            extra = group.get("__extra_domain") or []
            result[self._group_key(group, groupby)] = self._safe_domain(
                self._plain_domain(list(domain)) + self._plain_domain(list(extra)))
        return result

    @api.model
    def _tool_find_changes(self, arguments, state):
        Model = self._check_model_read(arguments.get("model"))
        model = Model._name
        field_name = arguments.get("field")
        field = Model._fields.get(field_name or "")
        if not field:
            similar = difflib.get_close_matches(field_name or "", list(Model._fields), n=5, cutoff=0.5)
            raise UserError(_("%(model)s has no field %(field)s. Similar: %(similar)s",
                              model=model, field=field_name, similar=", ".join(similar) or "-"))
        if not getattr(field, "tracking", False):
            raise UserError(_("Odoo does not keep a history of %(field)s on %(model)s, so the date of these changes "
                              "is unknown.", field=field.string, model=self._model_label(model)))
        ir_field = self.env["ir.model.fields"]._get(model, field_name)
        domain = [("field_id", "=", ir_field.id), ("mail_message_id.model", "=", model)]
        period = arguments.get("period")
        if period:
            start, end = self._period_bounds(period.get("name"), period.get("count"))
            domain += [("mail_message_id.date", ">=", self._utc_bound(start)),
                       ("mail_message_id.date", "<", self._utc_bound(end))]
        selection = dict(field._description_selection(self.env)) if field.type == "selection" else {}

        def matches(char_value, int_value, wanted):
            if wanted in (None, ""):
                return True
            wanted = str(wanted).strip().lower()
            candidates = {str(char_value or "").lower(), str(int_value or "")}
            return wanted in candidates or str(selection.get(wanted, "")).lower() in candidates - {""}

        # sudo: tracking values are read only to find record ids; the records themselves are filtered by access.
        tracks = self.env["mail.tracking.value"].sudo().search(domain, order="id desc", limit=5000)
        latest = {}
        for track in tracks:
            if not matches(track.new_value_char, track.new_value_integer, arguments.get("to_value")):
                continue
            if not matches(track.old_value_char, track.old_value_integer, arguments.get("from_value")):
                continue
            latest.setdefault(track.mail_message_id.res_id, track)
        records = Model.browse(list(latest)).exists()._filtered_access("read")
        in_user_tz = self.with_context(tz=self.env.user.tz or "UTC")
        columns = [
            {"name": "record", "label": self._model_label(model), "type": "char"},
            {"name": "changed_on", "label": _("Changed on"), "type": "char"},
            {"name": "old", "label": _("From"), "type": "char"},
            {"name": "new", "label": _("To"), "type": "char"},
            {"name": "by", "label": _("Changed by"), "type": "char"},
        ]
        rows = []
        for record in records[:TABLE_ROW_CAP]:
            track = latest[record.id]
            when = fields.Datetime.context_timestamp(in_user_tz, track.mail_message_id.date)
            rows.append({"id": record.id, "values": {
                "record": record.display_name, "changed_on": when.strftime("%Y-%m-%d %H:%M"),
                "old": track.old_value_char or (track.old_value_integer or ""),
                "new": track.new_value_char or (track.new_value_integer or ""),
                "by": track.mail_message_id.author_id.name or "",
            }})
        label = self._model_label(model)
        ref = self._ai_add_result(state, {
            "type": "table", "id": uuid.uuid4().hex,
            "title": _("%(model)s: changes of %(field)s", model=label, field=field.string),
            "model": model, "model_label": label, "columns": columns, "rows": rows, "total": len(records),
            "domain": [["id", "in", records.ids]],
            "subtitle": self._scope_text(model, [], None) or "",
        })
        if not records:
            return f"[{ref}] No {label} records that this user can access had {field.string} changed like that in the period."
        return (f"[{ref}] {len(records)} {label} record(s) that this user can access had {field.string} changed "
                f"(columns: record, changed on, from, to, changed by). Record IDs: {records.ids[:60]}.")

    @api.model
    def _filter_domain(self, model, names):
        """Domain terms and labels of the named filters (Odoo's own definitions, evaluated for this user)."""
        if isinstance(names, str):
            names = [names]
        filters = self._named_filters(model)
        unknown = [n for n in names if n not in filters]
        if unknown:
            raise UserError(_("Unknown filter(s) %(names)s for %(model)s. Available: %(available)s",
                              names=", ".join(map(str, unknown)), model=model, available=", ".join(filters) or "none"))
        context = self._domain_eval_context()
        domain, labels = [], []
        for name in names:
            try:
                value = safe_eval(filters[name]["domain"], context)
            except Exception as err:  # noqa: BLE001
                raise UserError(_("The filter %s cannot be used here; write the conditions in 'domain'.", name)) from err
            domain += self._plain_domain(value)
            labels.append(filters[name]["label"])
        return domain, labels

    @api.model
    def _plain_domain(self, value):
        if isinstance(value, (list, tuple)):
            return [self._plain_domain(v) if isinstance(v, (list, tuple)) else self._plain_value(v) for v in value]
        return value

    @staticmethod
    def _plain_value(value):
        if isinstance(value, datetime.datetime):
            return fields.Datetime.to_string(value)
        if isinstance(value, datetime.date):
            return fields.Date.to_string(value)
        return value

    @api.model
    def _scope_text(self, model, domain, filter_labels=None, top=None):
        """What a result covers, written by Odoo from the real filter: named filters, date ranges, top N."""
        parts = []
        if filter_labels:
            parts.extend(filter_labels)
        Model = self.env[model] if model in self.env else None
        ranges = {}
        for term in domain or []:
            if not (Model is not None and isinstance(term, (list, tuple)) and len(term) == 3):
                continue
            name, operator, value = term
            field = Model._fields.get(name) if isinstance(name, str) else None
            if not field or field.type not in ("date", "datetime") or operator not in (">=", ">", "<=", "<"):
                continue
            try:
                if field.type == "datetime" and len(str(value)) > 10:
                    moment = fields.Datetime.to_datetime(str(value)[:19])
                    day = fields.Datetime.context_timestamp(
                        self.with_context(tz=self.env.user.tz or "UTC"), moment).date()
                else:
                    day = fields.Date.to_date(str(value)[:10])
            except (TypeError, ValueError):
                continue
            if not day:
                continue
            bounds = ranges.setdefault(field.string or name, [None, None])
            if operator in (">=", ">"):
                bounds[0] = day + datetime.timedelta(days=1) if operator == ">" else day
            else:
                bounds[1] = day - datetime.timedelta(days=1) if operator == "<" else day
        for label, (low, high) in ranges.items():
            show = lambda d: d.strftime("%d %b %Y")  # noqa: E731
            if low and high:
                parts.append(f"{label}: {show(low)} – {show(high)}")
            elif low:
                parts.append(_("%(label)s: from %(day)s", label=label, day=show(low)))
            else:
                parts.append(_("%(label)s: until %(day)s", label=label, day=show(high)))
        if top:
            parts.append(_("Top %s only", top))
        return " · ".join(parts)

    # ------------------------------------------------------------------
    # Presenting the answer
    # ------------------------------------------------------------------
    @api.model
    def _tool_present(self, arguments, state):
        """Build what the user sees from named results, in Odoo; the AI only gives names and labels."""
        results = state.get("results", {})
        items = [item for item in arguments.get("items") or [] if isinstance(item, dict)]
        if not items:
            raise UserError(_("items needs at least one result to show."))
        shown, notes = [], []
        for item in items:
            view = item.get("view") or "table"
            labels = {str(k).lower(): str(v) for k, v in (item.get("labels") or {}).items() if v}
            if item.get("combine"):
                block = None
                if view in ("bar", "line"):
                    block = self._present_period_chart(item["combine"], results, view, labels, item.get("title"),
                                                       item.get("measure"))
                if block is None:
                    block = self._present_combine(item["combine"], results, labels, item.get("title"))
            else:
                ref = item.get("result")
                if ref not in results:
                    raise UserError(_("Unknown result %(ref)s. Available results: %(refs)s. Results of earlier "
                                      "answers are not kept: run the query again in this answer.",
                                      ref=ref, refs=", ".join(results) or "none"))
                source = results[ref]
                spec_groupby = (source.get("spec") or {}).get("groupby") or []
                if view != "table" and any("." in g.split(":")[0] for g in spec_groupby):
                    # Odoo's graph and pivot cannot group through a relation: show the table, and say how to chart it.
                    view = "table"
                    notes.append(_("%(ref)s is grouped through a related record (%(groupby)s), which Odoo charts cannot "
                                   "draw, so it is shown as a table. To chart it, group by a field of the model itself "
                                   "(a reporting model such as sale.report has many of them).",
                                   ref=ref, groupby=", ".join(spec_groupby)))
                if view in ("bar", "line", "pie", "pivot"):
                    block = self._present_chart(source, view, labels, item.get("title"), item.get("measure"))
                else:
                    block = copy.deepcopy(source)
                    block.pop("ref", None)
                    block["id"] = uuid.uuid4().hex
                    if item.get("title"):
                        block["title"] = item["title"]
                    if block["type"] == "table":
                        self._present_columns(block, labels, item.get("columns"))
            block["presented"] = True
            state["blocks"].append(block)
            kind = view if not item.get("combine") else ("combined table" if block["type"] == "table" else f"{view} chart")
            shown.append(f"{block.get('title') or block['type']} ({kind})")
        state["presented"] = True
        return ("Shown to the user: " + "; ".join(shown) + ". Do not repeat these tables in your text."
                + "".join(" " + note for note in notes))

    @staticmethod
    def _present_columns(block, labels, columns):
        for column in block["columns"]:
            new = labels.get(column["name"].lower()) or labels.get(str(column["label"]).lower())
            if new:
                column["label"] = new
        if columns:
            wanted = [str(c).lower() for c in columns]
            kept = []
            for wanted_name in wanted:
                for column in block["columns"]:
                    if wanted_name in (column["name"].lower(), str(column["label"]).lower()) and column not in kept:
                        kept.append(column)
            if kept:
                block["columns"] = kept

    @api.model
    def _present_combine(self, refs, results, labels, title):
        refs = [r for r in refs if isinstance(r, str)]
        missing = [r for r in refs if r not in results]
        if len(refs) < 2 or missing:
            raise UserError(_("combine needs two or more existing results (unknown: %s).", ", ".join(missing) or "-"))
        sources = [results[r] for r in refs]
        groupings = []
        for ref, source in zip(refs, sources):
            if source["type"] != "table" or not source.get("grouped"):
                raise UserError(_("%s is not a grouped result; combine works on aggregate_records results.", ref))
            groupings.append([c["name"] for c in source["columns"] if c["type"] == "group"])
        if any(g != groupings[0] for g in groupings):
            raise UserError(_("These results are not grouped the same way: %s.",
                              "; ".join(f"{r} by {', '.join(g)}" for r, g in zip(refs, groupings))))
        group_names = groupings[0]
        names = [c["name"] for s in sources for c in s["columns"] if c["type"] != "group"]

        def key(row):
            return tuple(json.dumps(v[0] if isinstance(v, list) and len(v) == 2 else v, default=str)
                         for v in (row["values"].get(g) for g in group_names))

        columns = [dict(c) for c in sources[0]["columns"] if c["type"] == "group"]
        for column in columns:
            column["label"] = labels.get(column["name"].lower()) or column["label"]
        rows = {}
        for ref, source in zip(refs, sources):
            for column in source["columns"]:
                if column["type"] == "group":
                    continue
                name = f"{ref}.{column['name']}"
                label = (labels.get(name.lower())
                         or (labels.get(column["name"].lower()) if names.count(column["name"]) == 1 else None)
                         or f"{column['label']} – {source.get('title') or ref}")
                columns.append(dict(column, name=name, label=label))
            for row in source["rows"]:
                entry = rows.setdefault(key(row), {g: row["values"].get(g) for g in group_names})
                for column in source["columns"]:
                    if column["type"] != "group":
                        entry[f"{ref}.{column['name']}"] = row["values"].get(column["name"])
        for entry in rows.values():
            for column in columns:
                if column["type"] in ("integer", "float", "monetary") and entry.get(column["name"]) is None:
                    entry[column["name"]] = 0
        return {
            "type": "table",
            "id": uuid.uuid4().hex,
            "title": title or sources[0].get("title"),
            "model": sources[0].get("model"),
            "model_label": sources[0].get("model_label"),
            "columns": columns,
            "rows": [{"values": values} for values in rows.values()],
            "total": len(rows),
            "grouped": True,
        }

    @api.model
    def _present_period_chart(self, refs, results, view, labels, title, measure):
        """Grouped results that differ only by a date period (this month vs last month...) as one native Odoo chart
        with one series per period. None when they do not fit that shape (the caller then shows a combined table)."""
        sources = [results.get(r) for r in refs if isinstance(r, str)]
        specs = [source.get("spec") if source else None for source in sources]
        if len(specs) < 2 or not all(specs):
            return None
        model = specs[0]["model"]
        if any(s["model"] != model or s["groupby"] != specs[0]["groupby"] for s in specs) or len(specs[0]["groupby"]) != 1:
            return None
        if "." in specs[0]["groupby"][0].split(":")[0]:
            return None  # Odoo's graph cannot group through a relation
        def field_at(path):
            """The field a (dotted) path ends on, e.g. order_id.date_order on sale.order.line."""
            current, field = self.env[model], None
            for name in str(path).split("."):
                field = current._fields.get(name)
                if field is None:
                    return None
                if field.relational:
                    current = self.env[field.comodel_name]
            return field

        parts = []
        for spec in specs:
            dated, rest = {}, []
            for term in spec["domain"]:
                field = field_at(term[0]) if isinstance(term, (list, tuple)) and len(term) == 3 else None
                if field is not None and field.type in ("date", "datetime") and term[1] in (">=", ">", "<", "<="):
                    dated.setdefault(term[0], []).append(term)
                else:
                    rest.append(term)
            parts.append((dated, json.dumps(rest, default=str)))
        date_fields = {name for dated, _rest in parts for name in dated}
        if len(date_fields) != 1 or len({rest for _dated, rest in parts}) != 1:
            return None
        date_field = date_fields.pop()
        if "." in date_field:
            return None  # Odoo's chart cannot group by a date through a relation: keep the combined table
        spans = []
        for dated, _rest in parts:
            try:
                bounds = [fields.Datetime.to_datetime(str(term[2])[:19]) for term in dated.get(date_field, [])]
            except (TypeError, ValueError):
                return None
            if len(bounds) != 2:
                return None
            spans.append((max(bounds) - min(bounds)).days)
        days = max(spans)
        granularity = ("day" if days <= 1 else "week" if days <= 7 else "month" if days <= 31
                       else "quarter" if days <= 92 else "year")
        block = self._present_chart(sources[0], view, labels, title, measure)
        block["groupby"] = [specs[0]["groupby"][0], f"{date_field}:{granularity}"]
        block["group_labels"] = [block["group_labels"][0],
                                 field_at(date_field)._description_string(self.env)]
        block["domain"] = [list(t) if isinstance(t, tuple) else t
                           for t in Domain.OR([spec["domain"] for spec in specs])]
        block["subtitle"] = " vs ".join(s.get("subtitle") or "" for s in sources if s.get("subtitle")) or None
        return block

    @api.model
    def _present_chart(self, source, view, labels, title, measure):
        spec = source.get("spec")
        if source["type"] != "table" or not spec:
            raise UserError(_("Charts need a grouped result from aggregate_records (or use show_chart)."))
        self._check_model_read(spec["model"])
        groupby = spec["groupby"][:2]
        aggregates = spec["aggregates"] or ["__count"]
        chosen = measure if measure in aggregates else next((a for a in aggregates if a != "__count"), aggregates[0])
        by_name = {c["name"]: c for c in source["columns"]}

        def field_of(aggregate):
            return "__count" if aggregate == "__count" else aggregate.split(":")[0]

        def label_of(aggregate):
            return labels.get(aggregate.lower()) or by_name.get(aggregate, {}).get("label") or aggregate

        return {
            "type": "chart",
            "id": uuid.uuid4().hex,
            "title": title or source.get("title"),
            "model": spec["model"],
            "model_label": source.get("model_label"),
            "chart_type": view,
            "groupby": groupby,
            "group_labels": [labels.get(g.lower()) or by_name.get(g, {}).get("label") or g for g in groupby],
            "measure": field_of(chosen),
            "measure_label": label_of(chosen),
            # A pivot shows every measure of the result side by side.
            "measures": [field_of(a) for a in aggregates] if view == "pivot" else [field_of(chosen)],
            "measure_labels": [label_of(a) for a in aggregates] if view == "pivot" else [label_of(chosen)],
            "domain": spec["domain"],
            "order": None,
            "stacked": False,
            "cumulative": False,
            "subtitle": source.get("subtitle"),
        }

    # ------------------------------------------------------------------
    # Conversation history
    # ------------------------------------------------------------------
    @api.model
    def _tool_history(self, arguments, config, state):
        Model = self._check_model_read(arguments.get("model"))
        model = Model._name
        if "message_ids" not in Model._fields:
            raise UserError(_("%s records have no conversation history.", self._model_label(model)))
        ids = []
        for value in arguments.get("record_ids") or []:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        if not ids:
            raise UserError(_("record_ids needs at least one record ID; find them with search_records first."))
        records = Model.browse(ids[:HISTORY_RECORD_CAP]).exists()._filtered_access("read")
        if not records:
            raise MissingError(_("No %s found that you may access with these IDs.", self._model_label(model)))
        domain = [("model", "=", model), ("res_id", "in", records.ids), ("message_type", "in", HISTORY_MESSAGE_TYPES)]
        if arguments.get("include_notes") is False:
            domain.append(("subtype_id.internal", "=", False))
        # As the user: mail.message follows the access rights of the records it belongs to.
        messages = self.env["mail.message"].search(domain, order="date asc, id asc")
        in_user_tz = self.with_context(tz=self.env.user.tz or "UTC")
        by_record = {record.id: [] for record in records}
        for message in messages:
            by_record[message.res_id].append(message)
        entries = []
        for record in records:
            kept = by_record[record.id][-HISTORY_MESSAGE_CAP:]
            responsible = record.user_id.name if "user_id" in record._fields and record.user_id else ""
            entries.append({
                "id": record.id,
                "name": record.display_name,
                "responsible": responsible,
                "total": len(by_record[record.id]),
                "messages": [{
                    "date": fields.Datetime.context_timestamp(in_user_tz, m.date).strftime("%Y-%m-%d %H:%M"),
                    "author": m.author_id.name or m.email_from or _("Unknown"),
                    "kind": _("Note") if m.subtype_id.internal else (_("Email") if m.message_type == "email" else _("Message")),
                    "text": html2plaintext(m.body or "").strip()[:HISTORY_MESSAGE_TEXT_CAP],
                } for m in kept],
            })
        label = self._model_label(model)
        ref = self._ai_add_result(state, {"type": "history", "id": uuid.uuid4().hex, "title": _("Conversation history"),
                                     "model": model, "model_label": label, "records": entries})
        counts = f"[{ref}] " + ", ".join(f"{e['name']} (ID {e['id']}): {e['total']} messages" for e in entries)
        if not config["share_data"]:
            if self.env.user.has_group("odoo_ai_agent.group_ai_agent_manager"):
                notice = _("The AI can show conversations but not read them, so it cannot summarise or rate them. To "
                           "allow it, turn on \"Share record values with the AI provider\" in AI Agent > "
                           "Configuration > Settings.")
            else:
                notice = _("Your administrator has not enabled this for you.")
            self._add_notice(state, notice)
            return (f"NOT ENABLED (already shown to the user): the conversation history of {len(entries)} {label} "
                    f"records is shown to the user ({counts}), but sharing record values is off, so you cannot read, "
                    f"summarise or rate it.")
        lines = []
        for entry in entries:
            responsible = f", responsible: {entry['responsible']}" if entry["responsible"] else ""
            lines.append(f"## {entry['name']} (ID {entry['id']}{responsible}, {entry['total']} messages)")
            lines.extend(f"[{m['date']}] {m['author']} ({m['kind']}): {m['text']}" for m in entry["messages"])
            lines.append("")
        text = "\n".join(lines)
        if len(text) > HISTORY_TEXT_CAP:
            text = text[:HISTORY_TEXT_CAP] + "\n[… cut: ask about fewer records for the rest]"
        return f"{counts}\nConversation history:\n{text}"

    # ------------------------------------------------------------------
    # Charts
    # ------------------------------------------------------------------
    @api.model
    def _check_model_read(self, model):
        if not model or model not in self.env:
            raise UserError(_("Unknown model: %s", model))
        if not mcp_utils.check_model_operation_allowed(self.env, model, "read"):
            raise AccessError(self._not_enabled_message(model, "read"))
        self.env[model].check_access("read")
        return self.env[model]

    @api.model
    def _tool_chart(self, arguments, state):
        Model = self._check_model_read(arguments.get("model"))
        model = Model._name
        groupby = arguments.get("groupby") or []
        if isinstance(groupby, str):
            groupby = [groupby]
        groupby = [g.strip() for g in groupby if isinstance(g, str) and g.strip()][:2]
        if not groupby:
            raise UserError(_("groupby needs at least one field."))
        measure = (arguments.get("measure") or "__count").strip()
        chart_type = arguments.get("chart_type") or "bar"
        if chart_type not in ("bar", "line", "pie", "pivot"):
            chart_type = "bar"
        field_names = [g.split(":")[0] for g in groupby] + ([measure] if measure != "__count" else [])
        meta = Model.fields_get(field_names, attributes=["string", "type", "groupable", "aggregator", "store"])
        for spec in groupby:
            field, _sep, granularity = spec.partition(":")
            info = meta.get(field)
            if not info:
                raise UserError(_("%(model)s has no field %(field)s. Check the names with get_fields.",
                                  model=model, field=field))
            if info.get("groupable") is False:
                raise UserError(_("Records cannot be grouped by %s.", field))
            if granularity and info["type"] not in ("date", "datetime"):
                raise UserError(_("Only date fields take a granularity like ':month' (%s is not a date).", field))
        aggregate = "__count"
        if measure != "__count":
            info = meta.get(measure)
            if not info or info["type"] not in ("integer", "float", "monetary"):
                raise UserError(_("The measure must be a number field of %(model)s or '__count' (got %(measure)s).",
                                  model=model, measure=measure))
            aggregate = f"{measure}:{info.get('aggregator') or 'sum'}"
        domain = [list(t) if isinstance(t, tuple) else t
                  for t in self.env["mcp.mixin"]._coerce_domain(arguments.get("domain")) or []]
        extra, filter_labels = self._scope_terms(model, dict(arguments))
        domain += extra
        scope_domain = list(domain)
        order = (arguments.get("order") or "").lower()
        limit = arguments.get("limit")
        first, first_type = groupby[0].split(":")[0], meta[groupby[0].split(":")[0]]["type"]
        if limit and ":" not in groupby[0] and first_type in ("many2one", "selection", "char", "integer", "boolean"):
            top = Model.formatted_read_group(
                domain, [first], [aggregate], order=f"{aggregate} {order or 'desc'}", limit=int(limit),
            )
            keys = [g[first][0] if isinstance(g[first], (list, tuple)) else g[first] for g in top]
            domain = list(domain) + [(first, "in", keys)]
        groups = Model.formatted_read_group(domain, groupby[:1], [aggregate], limit=500)
        label = self._model_label(model)
        if not groups:
            return f"No {label} records that this user can access match these filters, so no chart was drawn."
        measure_label = _("Count") if measure == "__count" else meta[measure]["string"]
        group_labels = [meta[g.split(":")[0]]["string"] for g in groupby]
        title = arguments.get("title") or _("%(measure)s by %(groups)s", measure=measure_label,
                                            groups=" / ".join(group_labels))
        state["blocks"].append({
            "type": "chart",
            "id": uuid.uuid4().hex,
            "title": title,
            "model": model,
            "model_label": label,
            "chart_type": chart_type,
            "groupby": groupby,
            "group_labels": group_labels,
            "measure": measure,
            "measure_label": measure_label,
            "domain": self._safe_domain([list(term) if isinstance(term, tuple) else term for term in domain]),
            "order": order.upper() if order in ("asc", "desc") else None,
            "stacked": bool(arguments.get("stacked")),
            "cumulative": bool(arguments.get("cumulative")),
            "subtitle": self._scope_text(model, scope_domain, filter_labels,
                                         int(limit) if limit and ":" not in groupby[0] else None),
        })
        shared = ""
        if state.get("share_data"):
            # The groups the chart shows, so the answer names the right months, customers... (no guessing).
            values = []
            for group in groups[:24]:
                key = group.get(groupby[0])
                key = key[1] if isinstance(key, (list, tuple)) and len(key) == 2 else key
                values.append(f"{key if key not in (None, False) else _('None')}: {group.get(aggregate)}")
            more = f" (+{len(groups) - 24} more)" if len(groups) > 24 else ""
            shared = f" Groups ({measure_label}): " + "; ".join(values) + more + "."
        return (f"A {chart_type} chart of {measure_label} by {', '.join(group_labels)} on {label} "
                f"({len(groups)} groups) is shown to the user.{shared}")

    # ------------------------------------------------------------------
    # Changes (previews the user confirms)
    # ------------------------------------------------------------------
    @api.model
    def _check_write_allowed(self, tool_name, model):
        operation = WRITE_TOOLS.get(tool_name, "method")
        label = self._model_label(model)
        if not model or model not in self.env:
            raise UserError(_("Unknown model: %s", model))
        if operation == "method":
            allowed = mcp_utils.check_model_method_allowed(self.env, model)
            what = _("pressing buttons")
            acl = "write"
        else:
            allowed = mcp_utils.check_model_operation_allowed(self.env, model, operation)
            what = {"create": _("creating"), "write": _("updating"), "unlink": _("deleting")}[operation]
            acl = operation
        if not allowed:
            raise AccessError(self._not_enabled_message(model, operation))
        if not self.env[model].has_access(acl):
            raise AccessError(_("Your Odoo access rights do not allow %(what)s %(label)s records.",
                                what=what, label=label))

    @api.model
    def _tool_propose(self, tool, arguments, state):
        name = tool["name"]
        if tool.get("custom_tool_id"):
            model, model_label = False, _("Custom action")
            title = tool["description"]
            lines = [{"label": key, "value": self._preview_value(value)} for key, value in arguments.items()]
            records = []
        else:
            self._check_arguments(tool["raw_schema"], arguments)
            model = arguments.get("model")
            self._check_write_allowed(name, model)
            model_label = self._model_label(model)
            title, lines, records = self._preview(name, model, arguments)
        block = {
            "type": "action",
            "id": uuid.uuid4().hex,
            "tool": name,
            "arguments": arguments,
            "model": model,
            "model_label": model_label,
            "title": title,
            "lines": lines,
            "records": records,
            "status": "pending",
            "expires": fields.Datetime.to_string(fields.Datetime.now() + ACTION_TTL),
        }
        state["blocks"].append(block)
        return ("PREVIEW SHOWN, NOT APPLIED: the user now sees this change with Confirm and Cancel buttons. Tell the "
                "user briefly what will happen and ask them to confirm. Do not propose it again.")

    @api.model
    def _preview_value(self, value):
        if isinstance(value, bool):
            return _("Yes") if value else _("No")
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False, default=str)[:300]
        return str(value)[:300] if value not in (None, "") else _("(empty)")

    @api.model
    def _preview_fields(self, model, values):
        """Field label: value lines, with related records and options shown by name."""
        Model = self.env[model]
        meta = Model.fields_get(list(values), attributes=["string", "type", "selection", "relation"])
        lines = []
        for field, value in values.items():
            info = meta.get(field)
            if not info:
                raise UserError(_("%(model)s has no field %(field)s. Check the names with get_fields.",
                                  model=model, field=field))
            shown = value
            if info["type"] == "many2one" and isinstance(value, int) and info.get("relation"):
                related = self.env[info["relation"]].browse(value).exists()
                if not related:
                    raise UserError(_("No %(label)s with ID %(id)s exists.",
                                      label=self._model_label(info["relation"]), id=value))
                shown = related.display_name
            elif info["type"] == "selection" and info.get("selection"):
                shown = dict(info["selection"]).get(value, value)
            elif info["type"] in ("many2many", "one2many") and isinstance(value, list) and info.get("relation") \
                    and all(isinstance(v, int) for v in value):
                shown = ", ".join(self.env[info["relation"]].browse(value).exists().mapped("display_name"))
            lines.append({"label": info.get("string") or field, "value": self._preview_value(shown)})
        return lines

    @api.model
    def _preview_records(self, model, ids):
        ids = [int(i) for i in ids or [] if str(i).lstrip("-").isdigit()]
        records = self.env[model].browse(ids).exists()
        missing = set(ids) - set(records.ids)
        if missing:
            raise MissingError(_("No %(label)s found that you may access with ID(s) %(ids)s.",
                                 label=self._model_label(model), ids=sorted(missing)))
        records.check_access("read")
        return [{"id": r.id, "name": r.display_name} for r in records]

    @api.model
    def _button_label(self, model, method):
        try:
            arch = self.env[model].get_view(view_type="form")["arch"]
        except Exception:  # noqa: BLE001 - a missing form view only costs us the nicer label
            arch = ""
        match = re.search(r'<button[^>]*name="%s"[^>]*string="([^"]+)"' % re.escape(method), arch) or re.search(
            r'<button[^>]*string="([^"]+)"[^>]*name="%s"' % re.escape(method), arch)
        return match.group(1) if match else method.replace("action_", "").replace("button_", "").replace("_", " ").title()

    @api.model
    def _preview(self, name, model, arguments):
        label = self._model_label(model)
        if name == "create_record":
            return (_("Create a %s", label), self._preview_fields(model, arguments.get("values") or {}), [])
        if name == "create_records":
            items = arguments.get("records") or []
            lines = []
            for index, values in enumerate(items[:10], 1):
                for line in self._preview_fields(model, values or {}):
                    lines.append({"label": f"#{index} {line['label']}", "value": line["value"]})
            if len(items) > 10:
                lines.append({"label": "…", "value": _("%s more", len(items) - 10)})
            return (_("Create %(count)s %(label)s records", count=len(items), label=label), lines, [])
        if name == "update_record":
            records = self._preview_records(model, [arguments.get("record_id")])
            return (_("Update %s", label), self._preview_fields(model, arguments.get("values") or {}), records)
        if name == "update_records":
            if arguments.get("updates"):
                ids = [u.get("record_id") for u in arguments["updates"]]
                records = self._preview_records(model, ids)
                names = {r["id"]: r["name"] for r in records}
                lines = []
                for update in arguments["updates"]:
                    for line in self._preview_fields(model, update.get("values") or {}):
                        lines.append({"label": f"{names.get(update.get('record_id'))}: {line['label']}",
                                      "value": line["value"]})
                return (_("Update %(count)s %(label)s records", count=len(records), label=label), lines, records)
            records = self._preview_records(model, arguments.get("record_ids"))
            return (_("Update %(count)s %(label)s records", count=len(records), label=label),
                    self._preview_fields(model, arguments.get("values") or {}), records)
        if name == "delete_record":
            records = self._preview_records(model, [arguments.get("record_id")])
            return (_("Delete %s", label), [{"label": _("This cannot be undone"), "value": records[0]["name"]}], records)
        if name == "call_model_method":
            method = arguments.get("method") or ""
            if method.startswith("_"):
                raise AccessError(_("Private methods cannot be called."))
            records = self._preview_records(model, arguments.get("record_ids")) if arguments.get("record_ids") else []
            lines = []
            if arguments.get("args"):
                lines.append({"label": _("Arguments"), "value": self._preview_value(arguments["args"])})
            if arguments.get("kwargs"):
                lines.append({"label": _("Options"), "value": self._preview_value(arguments["kwargs"])})
            return (_("Press “%(button)s” on %(label)s", button=self._button_label(model, method), label=label),
                    lines, records)
        if name == "post_message":
            records = self._preview_records(model, [arguments.get("record_id")])
            kind = _("Log note") if (arguments.get("subtype") or "note") == "note" else _("Send message to followers")
            body = html2plaintext(str(arguments.get("body") or ""))
            return (kind, [{"label": _("Message"), "value": body[:600]}], records)
        return (name, [{"label": key, "value": self._preview_value(value)} for key, value in arguments.items()], [])

    @api.model
    def _ai_agent_execute_action(self, message, block_id, confirm):
        """Run (or cancel) a previewed change after the user's click, then update the message."""
        channel = self.env["discuss.channel"].browse(message.res_id)
        if message.model != "discuss.channel" or message.author_id != channel._ai_agent_partner():
            raise AccessError(_("This change preview is not available."))
        channel._ai_agent_check_conversation()
        payload = copy.deepcopy(message.sudo().ai_agent_payload or {})
        block = next((b for b in payload.get("blocks", []) if b.get("id") == block_id and b.get("type") == "action"),
                     None)
        if not block:
            raise UserError(_("This change preview no longer exists."))
        if block["status"] != "pending":
            return block
        if fields.Datetime.now() > fields.Datetime.to_datetime(block["expires"]):
            block["status"] = "expired"
            block["result"] = _("This preview expired after 30 minutes. Ask the AI again.")
        elif not confirm:
            block["status"] = "cancelled"
            block["result"] = _("Cancelled. Nothing was changed.")
        else:
            self._apply_action(block)
        trace = copy.deepcopy(message.sudo().ai_agent_trace or {})
        trace.setdefault("notes", []).append(
            f"The user {'confirmed' if confirm else 'cancelled'} '{block['title']}': {block['status']}"
            + (f" ({block.get('result')})" if block.get("result") else "")
        )
        message.sudo().write({"ai_agent_payload": payload, "ai_agent_trace": trace})
        from odoo.addons.mail.tools.discuss import Store

        Store(bus_channel=message._bus_channel()).add(message.sudo(), ["ai_agent_payload"]).bus_send()
        return block

    @api.model
    def _apply_action(self, block):
        tool = next((t for t in self._ai_tools(False) if t["name"] == block["tool"]), None)
        if not tool:
            block.update(status="failed", result=_("This kind of change is no longer available."))
            return
        try:
            if not tool.get("custom_tool_id"):
                self._check_write_allowed(block["tool"], block["model"])
            result = self._call_mcp(tool, copy.deepcopy(block["arguments"]))
        except (UserError, AccessError, ValidationError, MissingError, ValueError, TypeError) as err:
            block.update(status="failed", result=error_sanitizer.sanitize_message(str(err.args[0] if err.args else err)))
            return
        except Exception as err:  # noqa: BLE001
            _logger.warning("AI Agent: confirmed change %s failed", block["tool"], exc_info=True)
            block.update(status="failed", result=error_sanitizer.sanitize_exception(err, "AI Agent confirmed change"))
            return
        structured = result.get("structuredContent") or {}
        block["status"] = "done"
        record = structured.get("record") or {}
        if block["tool"] in ("create_record", "create_records"):
            names = [record.get("display_name")] if record else [
                r.get("display_name") for r in structured.get("records") or []]
            block["result"] = _("Created: %s", ", ".join(n for n in names if n)) if any(names) else _("Created.")
        elif block["tool"] == "delete_record":
            block["result"] = _("Deleted.")
        elif block["tool"] in ("update_record", "update_records"):
            block["result"] = _("Saved.")
        else:
            block["result"] = _("Done.")
        ids = []
        if structured.get("record", {}).get("id"):
            ids = [structured["record"]["id"]]
        elif structured.get("records"):
            ids = [r.get("id") for r in structured["records"] if r.get("id")]
        elif block["tool"] in ("call_model_method", "update_records", "post_message") and block.get("records"):
            ids = [r["id"] for r in block["records"]]
        if block["tool"] != "delete_record" and ids and block.get("model"):
            block["result_ids"] = ids
        returned = structured.get("result")
        if isinstance(returned, dict) and returned.get("type", "").startswith("ir.actions"):
            # e.g. a button that opens a wizard: the browser opens it for the user.
            block["client_action"] = returned

    # ------------------------------------------------------------------
    # Reply
    # ------------------------------------------------------------------
    @api.model
    def _markdown(self, text):
        text = (text or "").strip()
        if not text:
            return Markup("")
        if markdown2:
            html = markdown2.markdown(text, extras=["tables", "fenced-code-blocks", "cuddled-lists", "break-on-newline",
                                                    "strike", "target-blank-links", "link-patterns"],
                                      link_patterns=[(BARE_URL, r"\g<0>")])  # bare web addresses become links
        else:
            html = "".join(f"<p>{Markup.escape(p).replace(chr(10), Markup('<br/>'))}</p>"
                           for p in text.split("\n\n") if p.strip())
        return Markup(html_sanitize(html))

    @api.model
    def _post_answer(self, channel, text, error, state, config):
        if state.get("presented"):
            # The AI chose what to show; the other results stay available under "Data used".
            for block in state["blocks"]:
                if block.get("ref"):
                    block["collapsed"] = True
        else:
            # Nothing was presented: show each distinct result once and fold away the empty ones.
            seen = set()
            for block in state["blocks"]:
                if not block.get("ref"):
                    continue
                key = json.dumps([block.get("type"), block.get("columns"), block.get("rows")], default=str)
                if (block["type"] == "table" and not block.get("rows")) or key in seen:
                    block["collapsed"] = True
                seen.add(key)
        body = self._markdown(text)
        if error and not body:
            body = Markup("<p>%s</p>") % error
        elif not body and not state["blocks"]:
            body = Markup("<p>%s</p>") % _("I don't have an answer for that. Could you rephrase your question?")
        payload = {
            "blocks": state["blocks"],
            "steps": state["steps"],
            "error": error or False,
            "model": config["model"].name if config else False,
        }
        return channel.sudo().message_post(
            author_id=channel._ai_agent_partner().id,
            body=body,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            ai_agent_payload=payload,
            ai_agent_trace={"notes": state["notes"][-12:]},
        )

    @api.model
    def _log_usage(self, channel, message, config, state, error, duration):
        model = config["model"] if config else self.env["ai_agent.model"]
        self.env["ai_agent.usage"].sudo().create({
            "user_id": self.env.uid,
            "channel_id": channel.id,
            "message_id": message.id,
            "provider_id": model.provider_id.id,
            "model_id": model.id,
            "model_code": state.get("answered_by") or model.code,
            "request_count": state["requests"],
            "input_tokens": state["input"],
            "output_tokens": state["output"],
            "cached_tokens": state["cached"],
            "cost": state["cost"],
            "duration": duration,
            "tool_call_count": len(state["tools_used"]),
            "tool_error_count": state["tool_errors"],
            "tools_used": ",".join(dict.fromkeys(state["tools_used"]))[:500],
            "state": "error" if error else "success",
            "error_message": error or False,
        })
