from odoo import Command, api, fields, models, _
from odoo.exceptions import AccessError, ValidationError

from .limits import (
    DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_NATIVE_RATE_LIMIT, DEFAULT_REQUEST_TIMEOUT, MAX_OUTPUT_TOKENS_RANGE, NATIVE_RATE_LIMIT_RANGE,
    REQUEST_TIMEOUT_RANGE,
)
from .providers import PROVIDERS, ProviderClient


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
    _unique_model = models.Constraint("UNIQUE(provider, identifier)", "Model already exists.")

    @api.model
    def _refresh(self, provider, key):
        self.env["odoo.ai.assistant.config"]._require_admin()
        observer = self.env["odoo.ai.assistant.usage"]._observer(provider, "", "catalogue")
        timeout = self.env["odoo.ai.assistant.config"]._configuration().request_timeout
        entries = ProviderClient(provider, key, observer=observer, timeout=timeout).models()
        if not entries:
            raise ValidationError(_("No usable models were returned by this provider."))
        existing = self.with_context(active_test=False).search([("provider", "=", provider)])
        by_id = {record.identifier: record for record in existing}
        existing.write({"active": False})
        for identifier, name, pricing in entries:
            if identifier in by_id:
                by_id[identifier].write({"name": name, "pricing": pricing, "active": True})
            else:
                self.create({"provider": provider, "identifier": identifier, "name": name, "pricing": pricing})


class AIConfig(models.Model):
    _name = "odoo.ai.assistant.config"
    _description = "AI App Configuration"

    name = fields.Char(default="AI Settings", required=True)
    enabled = fields.Boolean(string="Enable AI Assistant")
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
    # Sales rating rules used by the rate_sales_executives tool; a target of 0 leaves that measure unscored.
    rating_win_rate = fields.Integer(string="Target win rate (%)", default=30)
    rating_revenue = fields.Float(string="Monthly won revenue target per salesperson")
    rating_close_days = fields.Integer(string="Target days to close", default=30)
    rating_activities = fields.Integer(string="Activities on time target (%)", default=90)
    rating_contact_days = fields.Integer(string="Customer contact window (days)", default=14)
    rating_min_deals = fields.Integer(string="Minimum closed deals for a score", default=3)
    rating_revenue_source = fields.Selection([
        ("setting", "The monthly target above"),
        ("team", "Each sales team's monthly invoicing target, shared by its members"),
    ], string="Revenue target from", default="setting", required=True)
    rating_owner_at_close = fields.Boolean(string="Credit closed deals to their salesperson at closing")
    # Share of each measure in the overall score; 0 leaves it out.
    rating_weight_win_rate = fields.Integer(string="Win rate weight", default=1)
    rating_weight_revenue = fields.Integer(string="Won revenue weight", default=1)
    rating_weight_close_days = fields.Integer(string="Days to close weight", default=1)
    rating_weight_activities = fields.Integer(string="Activities on time weight", default=1)
    rating_weight_contact = fields.Integer(string="Recent contact weight", default=1)

    @api.constrains("rating_win_rate", "rating_revenue", "rating_close_days", "rating_activities",
                    "rating_contact_days", "rating_min_deals", "rating_weight_win_rate", "rating_weight_revenue",
                    "rating_weight_close_days", "rating_weight_activities", "rating_weight_contact")
    def _check_rating_rules(self):
        for config in self:
            if min(config.rating_revenue, config.rating_close_days, config.rating_contact_days,
                   config.rating_min_deals, config.rating_weight_win_rate, config.rating_weight_revenue,
                   config.rating_weight_close_days, config.rating_weight_activities,
                   config.rating_weight_contact) < 0 or not (0 <= config.rating_win_rate <= 100
                                                            and 0 <= config.rating_activities <= 100):
                raise ValidationError(_("Sales rating targets and weights cannot be negative, and percentages must be "
                                        "0 to 100."))

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

    def _client(self, purpose):
        """Provider client for the configured model, recording usage for `purpose`."""
        self.ensure_one()
        observer = self.env["odoo.ai.assistant.usage"]._observer(self.provider, self.model_id.identifier, purpose)
        return ProviderClient(
            self.provider, self.api_key, observer=observer,
            max_output_tokens=self.max_output_tokens, timeout=self.request_timeout,
        )

    @api.model
    def _require_admin(self):
        if not self.env.user.has_group("base.group_system"):
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
    "rating_win_rate", "rating_revenue", "rating_close_days", "rating_activities", "rating_contact_days",
    "rating_min_deals", "rating_revenue_source", "rating_owner_at_close", "rating_weight_win_rate",
    "rating_weight_revenue", "rating_weight_close_days", "rating_weight_activities", "rating_weight_contact",
)


def _plain(value):
    return value.id if isinstance(value, models.BaseModel) else value


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ai_assistant_enabled = fields.Boolean(string="Enable AI Assistant")
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
    ai_assistant_rating_win_rate = fields.Integer(string="Target win rate (%)")
    ai_assistant_rating_revenue = fields.Float(string="Monthly won revenue target")
    ai_assistant_rating_close_days = fields.Integer(string="Target days to close")
    ai_assistant_rating_activities = fields.Integer(string="Activities on time target (%)")
    ai_assistant_rating_contact_days = fields.Integer(string="Customer contact window (days)")
    ai_assistant_rating_min_deals = fields.Integer(string="Minimum closed deals")
    ai_assistant_rating_revenue_source = fields.Selection(
        lambda self: self.env["odoo.ai.assistant.config"]._fields["rating_revenue_source"].selection,
        string="Revenue target from")
    ai_assistant_rating_owner_at_close = fields.Boolean(string="Credit closed deals to their salesperson at closing")
    ai_assistant_rating_weight_win_rate = fields.Integer(string="Win rate")
    ai_assistant_rating_weight_revenue = fields.Integer(string="Won revenue")
    ai_assistant_rating_weight_close_days = fields.Integer(string="Days to close")
    ai_assistant_rating_weight_activities = fields.Integer(string="Activities on time")
    ai_assistant_rating_weight_contact = fields.Integer(string="Recent contact")

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
        string="AI Assistant access", compute="_compute_ai_access", inverse="_inverse_ai_access",
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
