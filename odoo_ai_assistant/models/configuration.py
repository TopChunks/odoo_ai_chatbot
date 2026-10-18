import logging

import requests

from odoo import Command, api, fields, models, _
from odoo.exceptions import AccessError, ValidationError

from .limits import (
    DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_NATIVE_RATE_LIMIT, DEFAULT_REQUEST_TIMEOUT, MAX_OUTPUT_TOKENS_RANGE, NATIVE_RATE_LIMIT_RANGE,
    REQUEST_TIMEOUT_RANGE,
)
from .providers import PROVIDERS, ProviderClient

_logger = logging.getLogger(__name__)


class AIProviderModel(models.Model):
    _name = "odoo.ai.assistant.provider.model"
    _description = "AI Provider Model"
    _order = "name"

    name = fields.Char(required=True)
    identifier = fields.Char(required=True)
    provider = fields.Selection(PROVIDERS, required=True, index=True)
    active = fields.Boolean(default=True)
    # Set from the provider's token prices on refresh; empty when the provider does not publish them.
    pricing = fields.Selection([("free", "Free"), ("commercial", "Commercial")], readonly=True)
    # USD per million tokens, set on refresh from the provider's catalogue (OpenRouter) or OpenRouter's public
    # price list for a direct provider's model; they estimate each request's cost on the Usage dashboard.
    price_input = fields.Float(string="Input price (USD per 1M tokens)", digits=(16, 4))
    price_output = fields.Float(string="Output price (USD per 1M tokens)", digits=(16, 4))
    price_cache_read = fields.Float(string="Cached input price (USD per 1M tokens)", digits=(16, 4))
    price_cache_write = fields.Float(string="Cache write price (USD per 1M tokens)", digits=(16, 4))
    _unique_model = models.Constraint("UNIQUE(provider, identifier)", "Model already exists.")

    @api.model
    def _refresh(self, provider, key):
        self.env["odoo.ai.assistant.config"]._require_admin()
        observer = self.env["odoo.ai.assistant.usage"]._observer(provider, "", "catalogue")
        timeout = self.env["odoo.ai.assistant.config"]._configuration().request_timeout
        entries = ProviderClient(provider, key, observer=observer, timeout=timeout).models()
        if not entries:
            raise ValidationError(_("No usable models were returned by this provider."))
        public = {}
        if not any(prices for _identifier, _name, prices in entries):
            # Direct providers list no prices: estimate from OpenRouter's public list; costs stay unknown without it.
            try:
                public = ProviderClient.public_prices(provider, [identifier for identifier, *_rest in entries], timeout)
            except (requests.RequestException, ValueError):
                _logger.info("AI model prices for %s could not be read from OpenRouter's public list.", provider)
        existing = self.with_context(active_test=False).search([("provider", "=", provider)])
        by_id = {record.identifier: record for record in existing}
        existing.write({"active": False})
        for identifier, name, prices in entries:
            values = {"name": name, "active": True}
            if prices := prices or public.get(identifier):
                charged = prices["price_input"] or prices["price_output"]
                values.update(prices, pricing="commercial" if charged else "free")
            if identifier in by_id:
                by_id[identifier].write(values)
            else:
                self.create({"provider": provider, "identifier": identifier, **values})
        # Requests recorded before these prices were known get their estimated cost now.
        models = {record.identifier: record for record in self.with_context(active_test=False).search(
            [("provider", "=", provider)])}
        for usage in self.env["odoo.ai.assistant.usage"].sudo().search(
                [("provider", "=", provider), ("tokens_known", "=", True), ("cost_source", "=", False)]):
            model = models.get(usage.model_name)
            if model and (cost := model._cost(usage)) is not None:
                usage.write({"cost": cost, "cost_source": "estimate"})

    def _cost(self, tokens):
        """Estimated USD cost of a request's token counts (a dict or a usage record) at this model's prices;
        None without prices."""
        self.ensure_one()
        if not (self.price_input or self.price_output):
            return None
        fresh = tokens["input_tokens"] - tokens["cached_tokens"]
        return (fresh * self.price_input + tokens["output_tokens"] * self.price_output
                + tokens["cached_tokens"] * (self.price_cache_read or self.price_input)
                + tokens["cache_write_tokens"] * (self.price_cache_write or self.price_input)) / 1_000_000


class AIConfig(models.Model):
    _name = "odoo.ai.assistant.config"
    _description = "AI App Configuration"

    name = fields.Char(default="AI Settings", required=True)
    enabled = fields.Boolean(string="Enable AI Agent")
    provider = fields.Selection(PROVIDERS, default="openai", required=True)
    api_key = fields.Char(string="API Key", copy=False)
    model_id = fields.Many2one("odoo.ai.assistant.provider.model", string="AI Model")
    local_tools = fields.Boolean(string="Enable native MCP tools", default=True)
    screen_metadata = fields.Boolean(string="Explain accessible app screens", default=False)
    native_actions = fields.Boolean(string="Allow confirmed MCP actions", default=False)
    business_data_sharing = fields.Boolean(string="Allow approved business data sharing", default=False)
    native_rate_limit = fields.Integer(string="Native tool attempts per minute per user", default=DEFAULT_NATIVE_RATE_LIMIT)
    max_output_tokens = fields.Integer(string="Maximum answer tokens", default=DEFAULT_MAX_OUTPUT_TOKENS)
    request_timeout = fields.Integer(string="Provider timeout (seconds)", default=DEFAULT_REQUEST_TIMEOUT)

    @api.constrains("native_rate_limit")
    def _check_native_rate_limit(self):
        for config in self:
            low, high = NATIVE_RATE_LIMIT_RANGE
            if not low <= config.native_rate_limit <= high:
                raise ValidationError(_("Choose a native tool rate limit between %(low)s and %(high)s.",
                                        low=low, high=high))

    @api.constrains("max_output_tokens", "request_timeout")
    def _check_request_limits(self):
        for config in self:
            low, high = MAX_OUTPUT_TOKENS_RANGE
            if not low <= config.max_output_tokens <= high:
                raise ValidationError(_("Choose a maximum answer length between %(low)s and %(high)s tokens.",
                                        low=f"{low:,}", high=f"{high:,}"))
            low, high = REQUEST_TIMEOUT_RANGE
            if not low <= config.request_timeout <= high:
                raise ValidationError(_("Choose a provider timeout between %(low)s and %(high)s seconds.",
                                        low=low, high=high))

    def _client(self, purpose, model=None):
        """Provider client recording usage for `purpose` under `model` (a conversation's), else the configured one."""
        self.ensure_one()
        observer = self.env["odoo.ai.assistant.usage"]._observer(
            self.provider, (model or self.model_id).identifier, purpose)
        return ProviderClient(
            self.provider, self.api_key, observer=observer,
            max_output_tokens=self.max_output_tokens, timeout=self.request_timeout,
        )

    @api.model
    def _is_admin(self):
        return self.env.user.has_group("base.group_system")

    @api.model
    def _require_admin(self):
        if not self._is_admin():
            raise AccessError(_("Only administrators can configure AI."))

    @api.model
    def _configuration(self):
        return self.env.ref("odoo_ai_assistant.default_config").sudo()

    @api.constrains("model_id", "provider")
    def _check_provider(self):
        for record in self:
            if record.model_id and record.model_id.provider != record.provider:
                raise ValidationError(_("Select a model belonging to the chosen provider."))


# AI Settings fields shown on the Settings page as ai_assistant_<name>.
_SETTINGS_FIELDS = (
    "enabled", "provider", "api_key", "model_id", "local_tools", "screen_metadata", "native_actions",
    "business_data_sharing", "native_rate_limit", "max_output_tokens", "request_timeout",
)


def _plain(value):
    """A field value as settings store it: a record becomes its ID, anything else is kept as is."""
    return value.id if isinstance(value, models.BaseModel) else value


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ai_assistant_enabled = fields.Boolean(string="Enable AI Agent")
    ai_assistant_provider = fields.Selection(PROVIDERS, string="AI provider")
    ai_assistant_api_key = fields.Char(string="API Key")
    ai_assistant_key_provider = fields.Char()
    ai_assistant_model_id = fields.Many2one("odoo.ai.assistant.provider.model", string="AI Model")
    ai_assistant_local_tools = fields.Boolean(string="Local MCP tools")
    ai_assistant_screen_metadata = fields.Boolean(string="Explain accessible app screens")
    ai_assistant_native_actions = fields.Boolean(string="Allow confirmed MCP actions")
    ai_assistant_business_data_sharing = fields.Boolean(string="Allow approved business data sharing")
    ai_assistant_native_rate_limit = fields.Integer(string="Native tool attempts per minute per user")
    ai_assistant_max_output_tokens = fields.Integer(string="Maximum answer tokens")
    ai_assistant_request_timeout = fields.Integer(string="Provider timeout (seconds)")

    @api.model
    def get_values(self):
        values = super().get_values()
        self.env["odoo.ai.assistant.config"]._require_admin()
        config = self.env["odoo.ai.assistant.config"]._configuration()
        values.update({f"ai_assistant_{name}": _plain(config[name]) for name in _SETTINGS_FIELDS},
                      ai_assistant_key_provider=config.provider)
        return values

    def set_values(self):
        super().set_values()
        self.env["odoo.ai.assistant.config"]._require_admin()
        self.env["odoo.ai.assistant.config"]._configuration().write({
            name: _plain(self[f"ai_assistant_{name}"]) for name in _SETTINGS_FIELDS
        })

    @api.onchange("ai_assistant_provider")
    def _onchange_ai_provider(self):
        if self.ai_assistant_provider != self.ai_assistant_key_provider:
            self.ai_assistant_model_id = False
            self.ai_assistant_api_key = False
            self.ai_assistant_key_provider = self.ai_assistant_provider

    @api.onchange("ai_assistant_api_key")
    def _onchange_ai_key(self):
        if self.ai_assistant_api_key and self.ai_assistant_provider:
            config = self.env["odoo.ai.assistant.config"]._configuration()
            if (self.ai_assistant_api_key != config.api_key
                    or self.ai_assistant_provider != config.provider):
                self.ai_assistant_model_id = False
                self.env["odoo.ai.assistant.provider.model"]._refresh(
                    self.ai_assistant_provider, self.ai_assistant_api_key,
                )

    def action_refresh_ai_models(self):
        self.ensure_one()
        self.env["odoo.ai.assistant.provider.model"]._refresh(
            self.ai_assistant_provider, self.ai_assistant_api_key,
        )
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("AI models refreshed"), "type": "success",
                       "message": _("Select a model and save your settings."), "sticky": False},
        }


class ResUsers(models.Model):
    _inherit = "res.users"

    # Access is the AI Assistant group (No / Yes) on the user's Access Rights tab;
    # this flag mirrors the group for code and tests.
    ai_assistant_enabled = fields.Boolean(
        string="AI Agent access", compute="_compute_ai_access", inverse="_inverse_ai_access",
    )

    @api.depends("all_group_ids")
    def _compute_ai_access(self):
        group = self.env.ref("odoo_ai_assistant.group_ai_user")
        for user in self:
            user.ai_assistant_enabled = group in user.all_group_ids

    def _inverse_ai_access(self):
        group = self.env.ref("odoo_ai_assistant.group_ai_user")
        for user in self:
            user.group_ids = [Command.link(group.id) if user.ai_assistant_enabled else Command.unlink(group.id)]

    def write(self, vals):
        if "ai_assistant_enabled" in vals and not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only administrators can change AI access."))
        return super().write(vals)

    @api.model_create_multi
    def create(self, values_list):
        grants_ai = any(values.get("ai_assistant_enabled") for values in values_list)
        if grants_ai and not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only administrators can grant AI access."))
        return super().create(values_list)
