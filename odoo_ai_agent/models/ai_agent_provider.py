"""AI providers: API keys, model catalogue sync and one chat adapter per provider.

Every adapter takes and returns the same provider-neutral conversation format so the
agent loop (``ai_agent.engine``) never deals with provider specifics:

* ``{"role": "user", "content": str}``
* ``{"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}],
  "raw": <provider data that must be replayed verbatim, e.g. Gemini thought signatures>}``
* ``{"role": "tool", "tool_call_id": str, "name": str, "content": str}``
"""

import json
import logging
import re
import time
import uuid

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PROVIDERS = [
    ("openai", "OpenAI"),
    ("gemini", "Google Gemini"),
    ("anthropic", "Anthropic Claude"),
    ("openrouter", "OpenRouter"),
]
BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "anthropic": "https://api.anthropic.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
API_KEY_URLS = {
    "openai": "https://platform.openai.com/api-keys",
    "gemini": "https://aistudio.google.com/app/apikey",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openrouter": "https://openrouter.ai/settings/keys",
}
OPENROUTER_CATALOG_URL = "https://openrouter.ai/api/v1/models"
ANTHROPIC_VERSION = "2023-06-01"

# HTTP statuses worth one more try (overload / transient gateway errors). 429 is not
# retried: it usually means a quota, and retrying only burns the next minute's budget.
RETRY_STATUSES = (500, 502, 503, 504, 529)
RETRY_DELAYS = (2, 4)

# Model-list filters. Providers list every model they serve; the agent needs current
# models that generate text and can call tools.
NON_TEXT_MARKERS = (
    "embedding", "embed", "tts", "whisper", "dall-e", "image", "imagen", "audio",
    "realtime", "transcribe", "moderation", "veo", "sora", "live", "aqa", "robotics",
    "computer-use", "native-audio", "learnlm", "gemma", "search", "deep-research",
)
# Families the providers have deprecated or retired, or that cannot call tools.
DEPRECATED_MARKERS = {
    "openai": (
        "gpt-3.5", "gpt-4-", "gpt-4.5", "o1-preview", "o1-mini", "chatgpt-", "davinci",
        "babbage", "instruct", "codex", "-chat-latest", "-pro", "gpt-oss",
    ),
    "gemini": ("gemini-1.", "gemini-2.0", "gemini-exp", "-exp-"),
    "anthropic": ("claude-2", "claude-instant", "claude-3"),
    # OpenRouter also resells the legacy models of other providers.
    "openrouter": (
        "gpt-3.5", "gpt-4-", "gpt-4.5", "o1-preview", "o1-mini", "chatgpt-", "davinci",
        "gemini-1.", "gemini-2.0", "claude-2", "claude-instant", "claude-3",
    ),
}
DATED_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")
REASONING_OPENAI = re.compile(r"^(o\d|gpt-5)")


class ProviderError(Exception):
    """A provider call failed; the message is safe to show to users."""


def _norm_model_key(code):
    """Comparable key for one model across providers' and OpenRouter's naming."""
    key = code.lower().split("/")[-1].split(":")[0]
    key = re.sub(r"-(\d{8}|\d{4}-\d{2}-\d{2})$", "", key)
    key = key.replace(".", "-")
    return re.sub(r"-latest$", "", key)


def _per_million(value):
    """OpenRouter prices are USD per token, as strings; store USD per 1M tokens."""
    try:
        return round(float(value or 0.0) * 1_000_000, 6)
    except (TypeError, ValueError):
        return 0.0


class AiAgentProvider(models.Model):
    _name = "ai_agent.provider"
    _description = "AI Provider"
    _order = "sequence, id"

    name = fields.Char(required=True)
    code = fields.Selection(PROVIDERS, required=True, readonly=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    api_key = fields.Char(
        string="API Key",
        groups="odoo_ai_agent.group_ai_agent_manager",
        copy=False,
        help="Secret key from the provider's console. Stored in the database and readable by AI Agent "
        "administrators only.",
    )
    has_api_key = fields.Boolean(compute="_compute_has_api_key", string="Key set")
    api_key_url = fields.Char(compute="_compute_api_key_url")
    model_ids = fields.One2many("ai_agent.model", "provider_id", string="Models")
    model_count = fields.Integer(compute="_compute_model_count")
    last_sync = fields.Datetime(readonly=True, copy=False)

    _code_unique = models.Constraint("UNIQUE(code)", "Each provider can be configured only once.")

    @api.depends_context("uid")
    def _compute_has_api_key(self):
        for provider in self:
            provider.has_api_key = bool(provider.sudo().api_key)

    def _compute_api_key_url(self):
        for provider in self:
            provider.api_key_url = API_KEY_URLS.get(provider.code)

    def _compute_model_count(self):
        counts = dict(
            self.env["ai_agent.model"]._read_group(
                [("provider_id", "in", self.ids)], ["provider_id"], ["__count"]
            )
        )
        for provider in self:
            provider.model_count = counts.get(provider, 0)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _headers(self):
        self.ensure_one()
        key = (self.sudo().api_key or "").strip()
        headers = {"Content-Type": "application/json"}
        if self.code in ("openai", "openrouter"):
            if key:
                headers["Authorization"] = f"Bearer {key}"
            if self.code == "openrouter":
                headers["HTTP-Referer"] = self.env["ir.config_parameter"].sudo().get_param(
                    "web.base.url", "https://www.odoo.com"
                )
                headers["X-Title"] = "Odoo AI Agent"
        elif self.code == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = ANTHROPIC_VERSION
        elif self.code == "gemini":
            # Header rather than ?key=, so the key never appears in URLs or logs.
            headers["x-goog-api-key"] = key
        return headers

    def _request(self, method, path, payload=None, timeout=60, params=None, deadline=None):
        """Call the provider API and return its JSON, raising ProviderError with a clear message."""
        self.ensure_one()
        url = BASE_URLS[self.code] + path
        response = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            started = time.monotonic()
            try:
                response = requests.request(
                    method, url, json=payload, params=params, headers=self._headers(), timeout=timeout
                )
            except requests.Timeout as err:
                raise ProviderError(
                    _("%(provider)s did not answer within %(seconds)s seconds. Try again, or choose a "
                      "faster model.", provider=self.name, seconds=timeout)
                ) from err
            except requests.RequestException as err:
                raise ProviderError(
                    _("Odoo could not reach %(provider)s (%(error)s).", provider=self.name, error=type(err).__name__)
                ) from err
            _logger.info(
                "AI Agent: %s %s -> HTTP %s in %.1fs", self.code, path.split("?")[0], response.status_code,
                time.monotonic() - started,
            )
            retry = response.status_code in RETRY_STATUSES
            if not retry and response.status_code == 200 and self.code == "openrouter":
                # OpenRouter reports upstream failures inside a 200 response.
                body = self._json(response)
                code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
                retry = isinstance(code, int) and code >= 500
            if retry and attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                if deadline is None or time.monotonic() + delay + 5 < deadline:
                    time.sleep(delay)
                    continue
            break
        body = self._json(response)
        error = body.get("error") if isinstance(body, dict) else None
        if response.status_code >= 400 or error:
            raise ProviderError(self._error_message(response.status_code, error, body))
        return body

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError:
            return {}

    def _error_message(self, status, error, body):
        detail = ""
        if isinstance(error, dict):
            detail = error.get("message") or ""
            metadata = error.get("metadata") or {}
            if isinstance(metadata, dict) and metadata.get("raw"):
                detail = f"{detail} ({str(metadata['raw'])[:200]})"
        elif isinstance(error, str):
            detail = error
        elif isinstance(body, list) and body and isinstance(body[0], dict):
            detail = (body[0].get("error") or {}).get("message", "")
        detail = str(detail)[:300]
        if status in (401, 403):
            text = _("%(provider)s rejected the API key (HTTP %(status)s). Check the key in AI Agent > "
                     "Configuration > AI Providers.", provider=self.name, status=status)
        elif status == 404:
            text = _("%(provider)s does not offer this model to your API key (HTTP 404). Refresh the model list "
                     "and choose another model.", provider=self.name)
        elif status == 429:
            text = _("%(provider)s refused the request: rate limit or usage quota reached (HTTP 429). Wait a "
                     "moment, or check your plan and credit.", provider=self.name)
        elif status == 402:
            text = _("%(provider)s refused the request: not enough credit (HTTP 402).", provider=self.name)
        elif status in RETRY_STATUSES:
            text = _("%(provider)s is temporarily unavailable (HTTP %(status)s). Try again shortly or choose "
                     "another model.", provider=self.name, status=status)
        else:
            text = _("%(provider)s returned an error (HTTP %(status)s).", provider=self.name, status=status)
        return f"{text} {detail}".strip() if detail else text

    # ------------------------------------------------------------------
    # Model catalogue
    # ------------------------------------------------------------------
    def action_sync_models(self):
        catalog = self._openrouter_catalog()
        total = 0
        for provider in self:
            if provider.code != "openrouter" and not provider.sudo().api_key:
                raise UserError(_("Enter the %s API key first.", provider.name))
            entries = provider._fetch_models(catalog)
            provider._store_models(entries)
            total += len(entries)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "message": _("%s current text models found.", total),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def action_view_models(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("%s models", self.name),
            "res_model": "ai_agent.model",
            "view_mode": "list,form",
            "domain": [("provider_id", "=", self.id)],
            "context": {"default_provider_id": self.id},
        }

    def _openrouter_catalog(self):
        """OpenRouter's public model list (no key needed): prices, modalities, expiry dates."""
        try:
            response = requests.get(OPENROUTER_CATALOG_URL, timeout=30)
            response.raise_for_status()
            return response.json().get("data") or []
        except (requests.RequestException, ValueError):
            _logger.warning("AI Agent: OpenRouter catalogue unavailable; models are stored without prices.")
            return []

    def _catalog_index(self, catalog, prefix):
        index = {}
        for entry in catalog:
            model_id = entry.get("id") or ""
            if prefix and not model_id.startswith(prefix + "/"):
                continue
            index.setdefault(_norm_model_key(model_id), entry)
        return index

    @staticmethod
    def _catalog_values(entry):
        if not entry:
            return {}
        pricing = entry.get("pricing") or {}
        return {
            "input_price": _per_million(pricing.get("prompt")),
            "output_price": _per_million(pricing.get("completion")),
            "cache_read_price": _per_million(pricing.get("input_cache_read")),
            "context_window": entry.get("context_length") or 0,
            "price_source": "openrouter",
        }

    @staticmethod
    def _catalog_is_text_tool_model(entry):
        architecture = entry.get("architecture") or {}
        outputs = architecture.get("output_modalities") or ["text"]
        return outputs == ["text"] and not entry.get("expiration_date")

    def _is_excluded(self, code, description=""):
        lowered = code.lower()
        if any(marker in lowered for marker in NON_TEXT_MARKERS):
            return True
        if any(marker in lowered for marker in DEPRECATED_MARKERS[self.code]):
            return True
        if lowered.split("/")[-1] == "gpt-4":
            return True
        description = (description or "").lower()
        return "deprecat" in description or "discontinu" in description

    def _fetch_models(self, catalog):
        self.ensure_one()
        return getattr(self, f"_fetch_models_{self.code}")(catalog)

    def _fetch_models_openai(self, catalog):
        index = self._catalog_index(catalog, "openai")
        codes = [m.get("id", "") for m in self._request("GET", "/models").get("data", [])]
        undated = {code for code in codes if not DATED_SNAPSHOT.search(code)}
        entries = []
        for code in codes:
            if not re.match(r"^(gpt-|o\d)", code) or self._is_excluded(code):
                continue
            if DATED_SNAPSHOT.search(code) and DATED_SNAPSHOT.sub("", code) in undated:
                continue
            match = index.get(_norm_model_key(code))
            if match and not self._catalog_is_text_tool_model(match):
                continue
            name = (match or {}).get("name", "").split(": ", 1)[-1] or code
            entries.append({"code": code, "name": name, **self._catalog_values(match)})
        return entries

    def _fetch_models_gemini(self, catalog):
        index = self._catalog_index(catalog, "google")
        entries, page_token = [], None
        while True:
            params = {"pageSize": 1000, **({"pageToken": page_token} if page_token else {})}
            data = self._request("GET", "/models", params=params)
            for model in data.get("models", []):
                code = (model.get("name") or "").removeprefix("models/")
                if not code.startswith("gemini-"):
                    continue
                if "generateContent" not in (model.get("supportedGenerationMethods") or []):
                    continue
                if self._is_excluded(code, model.get("description")):
                    continue
                match = index.get(_norm_model_key(code))
                if match and not self._catalog_is_text_tool_model(match):
                    continue
                values = self._catalog_values(match)
                values["context_window"] = model.get("inputTokenLimit") or values.get("context_window", 0)
                entries.append({
                    "code": code,
                    "name": model.get("displayName") or code,
                    "max_output_tokens": model.get("outputTokenLimit") or 0,
                    "description": (model.get("description") or "")[:500],
                    **values,
                })
            page_token = data.get("nextPageToken")
            if not page_token:
                return entries

    def _fetch_models_anthropic(self, catalog):
        index = self._catalog_index(catalog, "anthropic")
        entries = []
        for model in self._request("GET", "/models", params={"limit": 1000}).get("data", []):
            code = model.get("id") or ""
            if not code.startswith("claude-") or self._is_excluded(code):
                continue
            values = self._catalog_values(index.get(_norm_model_key(code)))
            if model.get("max_input_tokens"):
                values["context_window"] = model["max_input_tokens"]
            entries.append({
                "code": code,
                "name": model.get("display_name") or code,
                "max_output_tokens": model.get("max_tokens") or 0,
                **values,
            })
        return entries

    def _fetch_models_openrouter(self, catalog):
        catalog = catalog or self._request("GET", "/models").get("data", [])
        undated = {e.get("id") for e in catalog if not DATED_SNAPSHOT.search(e.get("id") or "")}
        entries = []
        for entry in catalog:
            code = entry.get("id") or ""
            if code.endswith(":batch") or (DATED_SNAPSHOT.search(code) and DATED_SNAPSHOT.sub("", code) in undated):
                continue  # batch-API variants and dated snapshots of a listed model
            if "tools" not in (entry.get("supported_parameters") or []):
                continue
            if not self._catalog_is_text_tool_model(entry) or self._is_excluded(code, entry.get("description")):
                continue
            values = self._catalog_values(entry)
            values["price_source"] = "provider"
            entries.append({
                "code": code,
                "name": entry.get("name") or code,
                "max_output_tokens": (entry.get("top_provider") or {}).get("max_completion_tokens") or 0,
                "is_free": code.endswith(":free") or (not values["input_price"] and not values["output_price"]),
                **values,
            })
        return entries

    def _store_models(self, entries):
        """Create/update the listed models; archive models the provider no longer offers."""
        self.ensure_one()
        Model = self.env["ai_agent.model"].with_context(active_test=False)
        existing = {m.code: m for m in Model.search([("provider_id", "=", self.id)])}
        seen = set()
        now = fields.Datetime.now()
        for entry in entries:
            code = entry["code"]
            if code in seen:
                continue
            seen.add(code)
            values = {k: v for k, v in entry.items() if v not in (None, "")}
            values.update(provider_id=self.id, last_seen=now)
            record = existing.get(code)
            if record:
                if record.price_source == "manual":
                    # Keep prices an administrator typed in.
                    for key in ("input_price", "output_price", "cache_read_price", "price_source"):
                        values.pop(key, None)
                if not record.active and not record.archived_manually:
                    values["active"] = True
                record.write(values)
            else:
                Model.create(values)
        stale = [m for code, m in existing.items() if code not in seen and m.active]
        for record in stale:
            record.write({"active": False, "archived_manually": False})
        self.last_sync = now

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------
    def _chat(self, model_code, system, messages, tools, max_tokens, timeout, deadline=None, allow_tools=True):
        """One model turn. Returns {text, tool_calls, assistant, usage, model}.

        ``allow_tools=False`` keeps the tool definitions (Anthropic requires them when the history holds
        tool calls) but forbids new calls, to force a final written answer.
        """
        self.ensure_one()
        return getattr(self, f"_chat_{self.code}")(
            model_code, system, messages, tools, max_tokens, timeout, deadline, allow_tools
        )

    # OpenAI and OpenRouter (OpenAI-compatible Chat Completions)
    def _chat_openai(self, model_code, system, messages, tools, max_tokens, timeout, deadline, allow_tools=True):
        payload = {"model": model_code, "messages": self._openai_messages(system, messages)}
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {
                    "name": t["name"], "description": t["description"], "parameters": t["schema"],
                }}
                for t in tools
            ]
            if not allow_tools:
                payload["tool_choice"] = "none"
        if self.code == "openai":
            payload["max_completion_tokens"] = max_tokens
            if REASONING_OPENAI.match(model_code):
                payload["reasoning_effort"] = "low"
        else:
            payload["max_tokens"] = max_tokens
            payload["usage"] = {"include": True}
        data = self._request("POST", "/chat/completions", payload, timeout=timeout, deadline=deadline)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(_("%s returned an empty answer.", self.name))
        message = choices[0].get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            calls.append({
                "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "name": function.get("name") or "",
                "arguments": self._parse_arguments(function.get("arguments")),
            })
        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        raw = {}
        if message.get("reasoning_details"):
            # OpenRouter: Gemini/Claude reasoning must be replayed with tool calls.
            raw["reasoning_details"] = message["reasoning_details"]
        text = message.get("content") or ""
        if isinstance(text, list):
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        return {
            "text": text,
            "tool_calls": calls,
            "assistant": {"role": "assistant", "content": text, "tool_calls": calls, "raw": raw},
            "usage": {
                "input": usage.get("prompt_tokens") or 0,
                "output": usage.get("completion_tokens") or 0,
                "cached": details.get("cached_tokens") or 0,
                "cost": usage.get("cost") if self.code == "openrouter" else None,
            },
            "model": data.get("model") or model_code,
        }

    _chat_openrouter = _chat_openai

    @staticmethod
    def _openai_messages(system, messages):
        result = [{"role": "system", "content": system}]
        for message in messages:
            role = message["role"]
            if role == "user":
                result.append({"role": "user", "content": message["content"]})
            elif role == "assistant":
                entry = {"role": "assistant", "content": message.get("content") or ""}
                if message.get("tool_calls"):
                    entry["tool_calls"] = [
                        {"id": c["id"], "type": "function",
                         "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                        for c in message["tool_calls"]
                    ]
                    if not entry["content"]:
                        entry["content"] = None
                if (message.get("raw") or {}).get("reasoning_details"):
                    entry["reasoning_details"] = message["raw"]["reasoning_details"]
                result.append(entry)
            elif role == "tool":
                result.append({
                    "role": "tool", "tool_call_id": message["tool_call_id"], "content": message["content"],
                })
        return result

    # Anthropic Messages API
    def _chat_anthropic(self, model_code, system, messages, tools, max_tokens, timeout, deadline, allow_tools=True):
        payload = {
            "model": model_code,
            "max_tokens": max_tokens,
            # The instructions and tools are identical on every call: cache them.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": self._anthropic_messages(messages),
        }
        if tools:
            payload["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools
            ]
            payload["tools"][-1]["cache_control"] = {"type": "ephemeral"}
            if not allow_tools:
                payload["tool_choice"] = {"type": "none"}
        data = self._request("POST", "/messages", payload, timeout=timeout, deadline=deadline)
        text, calls = [], []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                calls.append({"id": block["id"], "name": block.get("name") or "", "arguments": block.get("input") or {}})
        usage = data.get("usage") or {}
        cache_read = usage.get("cache_read_input_tokens") or 0
        cache_write = usage.get("cache_creation_input_tokens") or 0
        text = "\n".join(t for t in text if t)
        return {
            "text": text,
            "tool_calls": calls,
            "assistant": {"role": "assistant", "content": text, "tool_calls": calls, "raw": {}},
            "usage": {
                "input": (usage.get("input_tokens") or 0) + cache_read + cache_write,
                "output": usage.get("output_tokens") or 0,
                "cached": cache_read,
                "cache_write": cache_write,
                "cost": None,
            },
            "model": data.get("model") or model_code,
        }

    @staticmethod
    def _anthropic_messages(messages):
        result = []

        def push(role, blocks):
            if result and result[-1]["role"] == role:
                result[-1]["content"].extend(blocks)
            else:
                result.append({"role": role, "content": list(blocks)})

        for message in messages:
            role = message["role"]
            if role == "user":
                push("user", [{"type": "text", "text": message["content"] or "…"}])
            elif role == "assistant":
                blocks = []
                if message.get("content"):
                    blocks.append({"type": "text", "text": message["content"]})
                for call in message.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"],
                                   "input": call["arguments"] or {}})
                if blocks:
                    push("assistant", blocks)
            elif role == "tool":
                push("user", [{"type": "tool_result", "tool_use_id": message["tool_call_id"],
                               "content": message["content"]}])
        return result

    # Google Gemini generateContent
    def _chat_gemini(self, model_code, system, messages, tools, max_tokens, timeout, deadline, allow_tools=True):
        generation = {"maxOutputTokens": max_tokens}
        if model_code.startswith("gemini-3"):
            generation["thinkingConfig"] = {"thinkingLevel": "low"}
        elif model_code.startswith("gemini-2.5"):
            generation["thinkingConfig"] = {"thinkingBudget": 1024 if "pro" in model_code else 0}
            generation["maxOutputTokens"] = max_tokens + (1024 if "pro" in model_code else 0)
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": self._gemini_contents(messages),
            "generationConfig": generation,
        }
        if tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": t["name"], "description": t["description"], "parametersJsonSchema": t["schema"]}
                for t in tools
            ]}]
            if not allow_tools:
                payload["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
        data = self._request(
            "POST", f"/models/{model_code}:generateContent", payload, timeout=timeout, deadline=deadline
        )
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason") or "empty answer"
            raise ProviderError(_("%(provider)s did not answer (%(reason)s).", provider=self.name, reason=reason))
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text, calls = [], []
        for index, part in enumerate(parts):
            if part.get("functionCall"):
                call = part["functionCall"]
                calls.append({
                    "id": call.get("id") or f"call_{index}_{uuid.uuid4().hex[:8]}",
                    "name": call.get("name") or "",
                    "arguments": call.get("args") or {},
                    "gemini_id": call.get("id"),
                })
            elif part.get("text") and not part.get("thought"):
                text.append(part["text"])
        usage = data.get("usageMetadata") or {}
        text = "".join(text)
        return {
            "text": text,
            "tool_calls": calls,
            # Replay the model's parts verbatim: Gemini checks its thought signatures.
            "assistant": {"role": "assistant", "content": text, "tool_calls": calls, "raw": {"parts": parts}},
            "usage": {
                "input": usage.get("promptTokenCount") or 0,
                "output": (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0),
                "cached": usage.get("cachedContentTokenCount") or 0,
                "cost": None,
            },
            "model": data.get("modelVersion") or model_code,
        }

    @staticmethod
    def _gemini_contents(messages):
        contents = []

        def push(role, parts):
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": list(parts)})

        calls_by_id = {}
        for message in messages:
            role = message["role"]
            if role == "user":
                push("user", [{"text": message["content"] or "…"}])
            elif role == "assistant":
                for call in message.get("tool_calls") or []:
                    calls_by_id[call["id"]] = call
                parts = (message.get("raw") or {}).get("parts")
                if not parts:
                    parts = ([{"text": message["content"]}] if message.get("content") else []) + [
                        {"functionCall": {"name": c["name"], "args": c["arguments"] or {}}}
                        for c in message.get("tool_calls") or []
                    ]
                if parts:
                    push("model", parts)
            elif role == "tool":
                response = {"name": message["name"], "response": {"result": message["content"]}}
                gemini_id = (calls_by_id.get(message["tool_call_id"]) or {}).get("gemini_id")
                if gemini_id:
                    response["id"] = gemini_id
                push("user", [{"functionResponse": response}])
        return contents

    @staticmethod
    def _parse_arguments(raw):
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {"__invalid_json__": str(raw)[:500]}
        return value if isinstance(value, dict) else {"__invalid_json__": str(raw)[:500]}
