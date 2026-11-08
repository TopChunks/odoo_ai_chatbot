"""Shared by the ai_agent.engine files: tool schemas, answer filters, limits and schema helpers."""

import re
from datetime import timedelta

from odoo.tools import LazyTranslate

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
HISTORY_SALESPERSON_CAP = 10
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
        "salesperson": {
            "type": "string",
            "description": "Instead of record_ids: one or more salesperson names (comma-separated), to read the "
            f"conversations of their records (at most {HISTORY_SALESPERSON_CAP} records, the latest first).",
        },
        "most_active": {"type": "integer", "description": "Instead of names, for 'any two salespeople': the N "
                        "salespeople (1-5) with the most messages, chosen by Odoo."},
    },
    "required": ["model"],
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
                    "share": {
                        "type": "boolean",
                        "description": "Grouped table: add a '% of total' column for each number, e.g. win rate "
                        "from won and lost counts, or each salesperson's share of sales.",
                    },
                    "change": {
                        "type": "boolean",
                        "description": "Table grouped by a period (e.g. date:month): add the difference and % change "
                        "versus the previous row, e.g. this month vs last month.",
                    },
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
BUSINESS_FLOW_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "A short description of the instance workflow or behavior to find, "
                  "with the business words and, if known, field or button names (e.g. 'purchase order confirm "
                  "budget code approval')."},
        "model": {"type": "string", "description": "Optional technical model the flow is about, e.g. purchase.order."},
    },
    "required": ["query"],
    "additionalProperties": False,
}

CALCULATE_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string", "enum": ["column", "filter", "total"],
            "description": "column: add the formula as a new column of a table result. filter: keep only the rows "
            "where the formula is true. total: one figure from one or more results.",
        },
        "result": {"type": "string", "description": "column and filter: the table result to work on, e.g. 'r2'."},
        "inputs": {
            "type": "object", "additionalProperties": {"type": "string"},
            "description": "Variable name -> what it stands for. column/filter: a column of that result, e.g. "
            "{\"price\": \"list_price\", \"cost\": \"standard_price\"}. total: '<result>.<column>', e.g. "
            "{\"won\": \"r1.__count\", \"lost\": \"r2.__count\"}; a result with several rows gives a list (use "
            "sum, avg, min, max or len on it), or pick one row by its name with @: "
            "{\"alpha\": \"r1.amount_total:sum@TEST-Alpha Cycles\", \"beta\": \"r1.amount_total:sum@TEST-Beta Riders\"}.",
        },
        "formula": {
            "type": "string",
            "description": "One Python expression over the inputs: numbers, + - * / // % **, comparisons, and/or/"
            "not, x if c else y, min, max, sum, avg, len, abs, round, days(later, earlier); 'today' is today's "
            "date. E.g. 'won / (won + lost) * 100', 'on_hand < min_qty', 'days(today, due_date)'.",
        },
        "label": {"type": "string", "description": "Name of the computed column or figure, in the user's language, "
                  "e.g. 'Win rate (%)'."},
        "order": {"type": "string", "enum": ["desc", "asc"],
                  "description": "column mode: sort the rows by the new column (e.g. a score, highest first)."},
    },
    "required": ["mode", "inputs", "formula", "label"],
    "additionalProperties": False,
}

SETTINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What the option does, in the user's words, e.g. 'lock confirmed "
                  "orders', 'purchase approval', 'product variants', 'multi currency'."},
        "app": {"type": "string", "description": "Optional app whose settings to search, e.g. Purchase, Sales."},
    },
    "required": ["query"],
}
# Settings whose value is never reported (keys, passwords, tokens...), whatever their type.
SETTING_SECRET = re.compile(r"key|secret|token|password|passwd|pwd|credential|auth", re.IGNORECASE)
SETTING_STOPWORDS = {"setting", "settings", "option", "options", "enable", "enabled", "turn", "where", "how", "can",
                     "the", "and", "for", "this", "that", "they", "them", "is", "are", "be", "on", "off", "find",
                     "activate", "activated", "configure", "configuration", "feature", "features", "does", "what",
                     "there", "have", "our", "my", "in", "of", "to", "it", "do", "we", "you", "odoo", "app", "apps"}
ACCESS_RIGHTS_SCHEMA = {
    "type": "object",
    "properties": {
        "user": {"type": "string", "description": "Name or login of the user. Leave out for the current user "
                 "('what access rights do I have')."},
        "application": {"type": "string", "description": "For 'which users have ... access': the application as "
                        "on the user form (Sales, Inventory, Employees...), or 'Role' for Settings / Administrator "
                        "access. Lists every active internal user with their level for it."},
        "level": {"type": "string", "description": "With application: only users with this level, e.g. "
                  "'Administrator' or 'User: All Documents'."},
    },
}
CONVERSATION_STATS_SCHEMA = {
    "type": "object",
    "properties": {
        "salespeople": {
            "type": "array", "items": {"type": "string"},
            "description": "Names of the salespeople to compare. Leave out for all salespeople with opportunities.",
        },
        "date_from": {"type": "string", "description": "Optional start date 'YYYY-MM-DD' for messages and closings."},
        "date_to": {"type": "string", "description": "Optional end date 'YYYY-MM-DD' (inclusive)."},
        "top": {"type": "integer", "description": "Optional: only the N salespeople with the most opportunities."},
        "all_salespeople": {"type": "boolean", "description": "True only when the user explicitly asks for all "
                            "salespeople. Without names, top or this, you get a pick list to ask the user from."},
        "provisional": {"type": "boolean", "description": "True only when the user explicitly asks to rate the "
                        "salespeople with too few emails too: they get a provisional score, ranked after the others."},
    },
    "additionalProperties": False,
}
CONVERSATION_MIN_MESSAGES = 3  # fewer customer emails than this: not enough to rate
CONVERSATION_SCORE_TEXT = ("score: reply rate 30%, reply speed 25% (full within 4 hours, none after 72), customers left "
                           "waiting 15%, recent contact on open deals 15% (full within 7 days, none after 60), overdue "
                           "activities 15%")

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
CHANGES_CAP = 5000  # tracked changes read by find_changes, newest first
SCREEN_OUTLINE_CAP = 3000  # the screen outline added to get_fields
MODEL_LIST_CAP = 150
ACTION_TTL = timedelta(minutes=30)
TIME_MARGIN = 8  # seconds kept free before the request deadline
# A web address written as plain text in an answer (closing punctuation is not part of it).
# Where a name that matched nothing is looked up instead (counts only), in order.
NAME_HINT_MODELS = {"res.partner": "name", "product.product": "name", "hr.employee": "name", "res.users": "name",
                    "crm.lead": "name", "project.project": "name", "res.company": "name", "link.tracker": "title",
                    "utm.campaign": "name"}
BARE_URL = re.compile(r"""https?://[^\s<>"'\])]*[^\s<>"'\]).,;:!?]""")
# "shown in the table above": the tables and charts of an answer always come after its text.
SHOWN_ABOVE = re.compile(r"\b(tables?|charts?|lists?|results?|records?|cards?|fields?|details|shown|listed|displayed|"
                         r"(?:I've|I have) (?:listed|shown) [^.\n]{1,40}?)(\s+)above\b(?! \d)", re.IGNORECASE)
# "shown in the table below" with nothing looked up in this answer: earlier answers' tables are not shown again.
REFERS_SHOWN = re.compile(r"\b(?:table|chart|list|card)s?\b[^.\n]{0,40}\b(?:below|above)\b|\bshown (?:below|above)\b",
                          re.IGNORECASE)
# "the earlier score table": tables of earlier answers are not shown again.
REFERS_EARLIER = re.compile(r"\b(?:earlier|previous|last|above)\s+(?:\w+\s+){0,2}(?:table|chart|list|result|ranking)s?\b",
                            re.IGNORECASE)
# Rating or comparing people: the measurable comparison answers it even with sharing off.
RATING_INTENT = re.compile(r"\b(?:compar\w*|rate|rating|rank\w*|best|worst|score|how (?:they|well)|handle)\b",
                           re.IGNORECASE)
# Tools that read no records of a model (field lists, menus, calculations): not limited by the user's apps.
APP_GATE_FREE_KINDS = ("schema", "app", "calculate", "present", "conversation_stats",
                       "access_rights", "settings")  # access_rights checks itself: own rights, or an administrator
# Directory data everyone sees in Odoo (names in salesperson fields...): only these fields need the app.
APP_GATE_DIRECTORY = {"res.users": ("group_ids", "all_group_ids", "groups_id", "role", "login_date", "log_ids",
                                    "share", "api_key_ids"),
                      "res.company": ()}
APP_GATE_NOT_PARENTS = ("res.users", "res.partner", "res.company", "ir.model", "ir.model.fields", "res.currency",
                        "uom.uom", "mail.message", "mail.activity.type")
# Archived records worth a note when a result links to them (not users: OdooBot is archived by design).
ARCHIVED_NOTE_MODELS = ("product.product", "product.template", "res.partner")
# "...so you'll need to compare the figures in the table yourself": handing the reading back (sharing off).
YOURSELF = re.compile(r"\s*[^.!?\n]*\b(?:compare|check|read|look at|sort|find|work out)\b[^.!?\n]*\byourself\b[^.!?\n]*[.!?]",
                      re.IGNORECASE)
HANDBACK = re.compile(r"\s*[^.!?\n]*(?:\bI can(?:'|no)t name\b|\bthere for you to (?:read|check|see)\b|"
                      r"\b(?:check|look at|see)\b[^.!?\n]*\b(?:column|dates)\b[^.!?\n]*"
                      r"\bto (?:find|see|spot)\b|\b(?:calculated|computed)\b[^.!?\n]*\b(?:isn't|is not|wasn't|not) "
                      r"shown\b)[^.!?\n]*[.!?]", re.IGNORECASE)
COMPARE_INTENT = re.compile(r"\b(?:compare|comparison|versus|vs\.?|difference between)\b", re.IGNORECASE)
CHART_INTENT = re.compile(r"\b(?:chart|graph|plot|visuali[sz]ation|visuali[sz]e)\b", re.IGNORECASE)
HIGHLIGHT_CLAIM = re.compile(r"\bhighlight", re.IGNORECASE)
# "Which product sold the most?", "lowest price", "top 5 customers": Odoo names the top row.
SUPERLATIVE = re.compile(r"(?<!\bat )\b(?:most(?! recent)(?! recently)|top(?! of)|best|highest|largest|biggest|"
                         r"maximum|lowest|least|cheapest|fewest|smallest|minimum)\b", re.IGNORECASE)
LOWEST = re.compile(r"\b(?:lowest|least|cheapest|fewest|smallest|minimum)\b", re.IGNORECASE)
# "Is INV/2026/00046 fully paid?" and an answer that starts with Yes or No.
PAID_QUESTION = re.compile(r"^\s*(?:is|are|has|have|was|were)\b.*\bpaid\b", re.IGNORECASE)
YES_NO = re.compile(r"\s*\**\s*(yes|no)\b", re.IGNORECASE)
# "So I can't confirm whether it is fully paid": the AI could not read its own count.
PAID_UNSURE = re.compile(r"\b(?:can't|cannot|can not|unable to|couldn't|could not)\s+(?:confirm|tell|say|determine|"
                         r"decide|verify|check)\b|\bcorrect my (?:previous|last|earlier)\b|\bplease check\b|"
                         r"\bcheck (?:those|these|the) (?:figures|values|columns)\b|\bre-?run the check\b|"
                         r"\bdefinite answer\b", re.IGNORECASE)
# How answers name the custom code they are based on: technical details for administrators, plain words otherwise.
FLOW_SOURCE_TECHNICAL = ("End with one short line 'Technical details:' citing the module and file with line number "
                         "in backticks, e.g. `my_module/models/sale_order.py:42`.")
FLOW_SOURCE_PLAIN = ("Say in one plain sentence that this is a rule your company added to Odoo, naming the module by "
                     "its title (e.g. 'Sales Approval Rules'); do not mention file names, methods, line numbers or "
                     "technical module names.")
ERROR_SOURCE_TECHNICAL = ("End this part with one short line 'Technical details:' citing the code as module/file:line "
                          "in backticks.")
ERROR_SOURCE_PLAIN = ("If the check comes from the custom code, say it is a rule your company added to Odoo, naming "
                      "the module by its title; do not mention file names, methods, line numbers or technical module "
                      "names. Only for a crash in the code, end with one line 'For your administrator:' and the "
                      "location as module/file:line in backticks, so the user can pass it on.")
# Odoo refusing a read because of one field: "The fields "wage", which you are trying to read, are not available
# for employee public profiles." or "You do not have enough rights to access the field "version_id" on ...".
FIELD_ACCESS_ERROR = re.compile(r"which you are trying to read|enough rights to access the field", re.IGNORECASE)
# Errors Odoo shows in a dialog that the user can get AI help for (a crash needs its traceback as well).
ERROR_HELP_EXCEPTIONS = {
    "odoo.exceptions.ValidationError", "odoo.exceptions.UserError", "odoo.exceptions.AccessError",
    "odoo.exceptions.MissingError", "odoo.exceptions.RedirectWarning",
    "odoo.addons.base.models.ir_actions.ServerActionWithWarningsError",
}
# An HTML tag in the answer text that is not simple formatting (script, style, iframe...).
RAW_TAG = re.compile(r"<(?=/?[A-Za-z!])(?!/?(?:b|i|u|s|em|strong|br|code|p|ul|ol|li|a)\b)")
# "There is nothing to chart": no chart belongs to this answer.
NO_CHART = re.compile(r"\bnothing to (?:chart|plot|draw)\b", re.IGNORECASE)
# "I accidentally displayed a first chart...": about the AI's own tool calls, not the user's data.
MISTAKE = re.compile(r"\s*[^.!?\n]*\b(?:accidentally|by mistake|mistakenly)\b[^.!?\n]*[.!?]", re.IGNORECASE)
# "The list is capped at 100 rows, so if you have more...": removed when every table shown is complete.
CAPPED = re.compile(r"\s*[^.!?\n]*\b(?:capped|limited|truncated|cut off)\s+(?:at|to)\s+(?:the\s+first\s+)?\d+[^.!?\n]*[.!?]",
                    re.IGNORECASE)
# "The order's card is shown below": the answer promises a record card.
CARD_CLAIM = re.compile(r"\bcard\b", re.IGNORECASE)
REFERS_SHOWN_NUDGE = ("(Nothing is shown in this answer yet: tables and charts of earlier answers are not shown again. "
                      "Run the lookup with the tools now, then answer.)")
# "...listed under the data used for this answer": that section is folded, so what the answer talks about is presented.
DATA_USED = re.compile(r"\b(?:under|in|see)\s+(?:the\s+)?[\"'\u201c]?data used\b", re.IGNORECASE)
DATA_USED_NUDGE = ("(The 'data used' section is folded and only lets the user check the answer; it is not part of it. "
                   "Present every result your answer talks about with the present tool (one item per result), then "
                   "answer again and refer to them as shown below.)")
# "The rule's price figures are in that table.": says nothing the table under the answer does not already show.
IN_TABLE = re.compile(r"\s*[^.!?\n]*\b(?:figures|values|amounts|numbers|prices?|details)\s+(?:are|is)\s+"
                      r"(?:shown\s+|listed\s+)?in\s+(?:that|this|the)\s+table\s*[.!?]", re.IGNORECASE)
# With sharing off the AI must not hand the reading back to the user ("I can't see the figures, check the columns").
CANT_SEE = re.compile(r"\s*[^.!?\n]*\bI (?:can't|cannot|can not|don't|do not) (?:see|read|view|state|tell|give|quote|"
                      r"say)\b[^.!?\n]*\b(?:figures?|values?|amounts?|numbers?|prices?|totals?)\b[^.!?\n]*[.!?]",
                      re.IGNORECASE)
# Sentences that hand the reading back to the user, removed when data sharing is off (in this order).
HANDBACK_FILTERS = (CANT_SEE, YOURSELF, HANDBACK, IN_TABLE)
# The reason of a refusal: such a sentence answers the question and is never removed as a hand-back.
REFUSAL_REASON = re.compile(r"\b(?:access|rights?|permission|allowed|private|restricted|confidential|not enabled)\b",
                            re.IGNORECASE)
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
    "calculate": _lt("Calculating…"),
    "conversation_stats": _lt("Comparing salespeople…"),
    "user_access_rights": _lt("Reading access rights…"),
    "find_setting": _lt("Checking the settings…"),
    "find_changes": _lt("Checking the change history of %s…"),
    "search_business_flow": _lt("Checking the custom business flow…"),
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
