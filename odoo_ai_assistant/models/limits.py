"""Shared limits and model policy for the AI assistant."""

import re
from datetime import timedelta

# Chat sizes, shared with the web client through bootstrap().
MAX_MESSAGE_CHARS = 12000
MAX_HISTORY_MESSAGES = 30
MAX_HISTORY_CHARS = 12000
MAX_LISTED_CONVERSATIONS = 100
MAX_LOADED_MESSAGES = 500
MAX_TITLE_PROMPTS = 20
# Longest automatic conversation title (told to the AI and checked on its answer).
MAX_TITLE_CHARS = 60
WELCOME_SUGGESTIONS = 3

# pg advisory lock namespaces: replies within one conversation, native tool rate limit per user.
CONVERSATION_LOCK_NAMESPACE = 190019
NATIVE_RATE_LOCK_NAMESPACE = 190020

# Native MCP payload sizes (characters).
MAX_TOOL_ARGUMENT_CHARS = 200000
MAX_ATTACHMENT_TEXT_CHARS = 100000
MAX_SHARED_EVIDENCE_CHARS = 100000
MAX_REQUEST_DETAIL_CHARS = 120000
# Images shown on a tool request card or shared with the provider.
MAX_SHARED_IMAGES = 4
MAX_IMAGE_CHARS = 350000

# Earlier messages are shortened to this length when a request must shrink to fit the model's context.
COMPACT_MESSAGE_CHARS = 2000
# Characters after an action verb in which an app or menu name marks a direct Odoo request.
DIRECT_REQUEST_WINDOW_CHARS = 160
# Messages kept at the first shortening level.
COMPACT_RECENT_MESSAGES = 5
# Tools kept, most relevant first, at the second shortening level.
COMPACT_TOOL_COUNT = 8
# Characters of the latest message shown under a conversation in the chat list.
CONVERSATION_PREVIEW_CHARS = 160

# Written by the AI wherever the signed-in user's ID goes ("my deals"); Odoo puts in the ID, which the AI never sees.
CURRENT_USER = "$current_user"

# Request-check labels for a data change, and the MCP Server operation tag of the tools that perform it.
# ACTION (a workflow, several kinds of change, or unclear) is offered every data-changing tool. GENERATE is a CREATE
# whose values the user asked the AI to make up: only then may a new record's name be one the user never wrote.
CHANGE_OPERATIONS = {"CREATE": "create", "GENERATE": "create", "UPDATE": "write", "DELETE": "unlink", "ACTION": None}

# Tool calls in one provider answer, and follow-up rounds after schema discovery.
MAX_TOOL_CALLS = 5
MAX_DISCOVERY_ROUNDS = 3

# Charts drawn in the chat, and the chart picture a browser may send for a PDF export (base64 characters).
MAX_CHART_GROUPS = 50
MAX_CHART_TITLE_CHARS = 120
MAX_CHART_IMAGE_CHARS = 4_000_000

# App and screen discovery: names the AI may look up, screens and fields it receives.
MAX_LOOKUP_NAME_CHARS = 200
MAX_CONTEXT_SCREENS = 600
MAX_SCREEN_MATCHES = 8
MAX_SCREEN_FIELDS = 80
MAX_SCREEN_BUTTONS = 40

# Field (or screen) names listed back to the AI after it used an unknown or unsuitable one.
MAX_FIELD_SUGGESTIONS = 80
MAX_FIELD_HINT_NAMES = 150
MAX_LINE_FIELD_EXAMPLES = 40
# Newest chatter entries of one record returned by list_record_messages.
MAX_RECORD_MESSAGES = 100
# Names listed in the sales rating summary before "and N more".
MAX_SUMMARY_NAMES = 3

# Native tool requests: preview lifetime, staged-file lifetime and plan validation.
TOOL_REQUEST_LIFETIME = timedelta(minutes=30)
STAGED_FILE_LIFETIME = timedelta(days=1)
MAX_PLAN_DEPTH = 20
MAX_REFERENCE_TERMS = 20
MAX_SCHEMA_ERROR_CHARS = 300
MAX_AUDIT_TOOL_NAME_CHARS = 200
MAX_FILE_NAME_CHARS = 255
MAX_MIMETYPE_CHARS = 120
# Preview size of a file card in the chat, as Discuss draws its PDF thumbnails.
FILE_THUMBNAIL_SIDE = 256

# Provider request defaults; administrators can override them in AI Settings.
DEFAULT_MAX_OUTPUT_TOKENS = 2048
DEFAULT_REQUEST_TIMEOUT = 60
DEFAULT_NATIVE_RATE_LIMIT = 60
PROVIDER_CONNECT_TIMEOUT = 10
# Seconds an AI call leaves free before Odoo's own limit for a browser request (limit_time_real), to save the
# answer or show the error: past that limit Odoo restarts the whole server.
ODOO_TIME_MARGIN = 10
# Allowed (minimum, maximum) values for the AI Settings above and the native tool rate limit.
MAX_OUTPUT_TOKENS_RANGE = (256, 65536)
REQUEST_TIMEOUT_RANGE = (10, 600)
NATIVE_RATE_LIMIT_RANGE = (1, 600)

# Field names AI tools never read, change, filter or group by.
CREDENTIAL_FIELD = re.compile(r"password|passwd|api.?key|token|secret|credential|private.?key", re.IGNORECASE)

# Technical, security and credential models never exposed to AI tools.
RESTRICTED_MODEL_PREFIXES = (
    "ir.", "mail.", "discuss.", "bus.", "mcp.", "odoo.ai.", "auth.", "payment.", "res.users.",
)
RESTRICTED_MODELS = frozenset({
    "res.users", "res.users.apikeys", "res.groups", "res.config", "res.config.settings",
})


def is_restricted_model(env, model, allowed=()):
    """Return True for models that AI tools must not read or change.

    `allowed` lists explicit exceptions (for example attachments and activities
    used by native MCP tools)."""
    if not isinstance(model, str) or model not in env or env[model]._transient:
        return True
    if model in allowed:
        return False
    return model in RESTRICTED_MODELS or model.startswith(RESTRICTED_MODEL_PREFIXES)
