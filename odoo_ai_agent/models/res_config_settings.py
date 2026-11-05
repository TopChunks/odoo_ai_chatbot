from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.mcp_server.controllers import utils as mcp_utils


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    ai_agent_provider_id = fields.Many2one(
        "ai_agent.provider", string="AI Provider", config_parameter="odoo_ai_agent.provider_id"
    )
    ai_agent_provider_code = fields.Selection(related="ai_agent_provider_id.code")
    ai_agent_api_key = fields.Char(
        string="API Key", related="ai_agent_provider_id.api_key", readonly=False,
        groups="odoo_ai_agent.group_ai_agent_manager",
    )
    ai_agent_api_key_url = fields.Char(related="ai_agent_provider_id.api_key_url")
    ai_agent_model_id = fields.Many2one(
        "ai_agent.model", string="AI Model", config_parameter="odoo_ai_agent.model_id",
        domain="[('provider_id', '=', ai_agent_provider_id)]",
    )
    ai_agent_share_data = fields.Boolean(
        string="Share record values with the AI provider",
        config_parameter="odoo_ai_agent.share_data",
        help="Off (recommended): the AI only receives the tool list, field definitions, record counts and IDs; "
        "record values are shown to the user but never sent to the provider. On: results are also sent to "
        "the provider so the AI can summarise and compare values in its own words.",
    )
    ai_agent_error_resolution_enabled = fields.Boolean(
        string="Offer AI help for validation errors",
        config_parameter="odoo_ai_agent.error_resolution_enabled",
        help="Users must click the button. The displayed validation message and model name are sent to the configured "
        "AI provider; no traceback or form payload is sent (the message itself may mention a value).",
    )
    ai_agent_business_flow_enabled = fields.Boolean(
        string="Answer from custom business-flow code",
        config_parameter="odoo_ai_agent.business_flow_enabled",
        help="Allows the AI to use relevant excerpts from the administrator-built index of installed custom modules, "
        "Studio views and automation rules. Relevant excerpts are sent to the configured AI provider when users ask "
        "about instance workflows.",
    )
    ai_agent_max_steps = fields.Integer(
        string="Maximum tool steps per answer", config_parameter="odoo_ai_agent.max_steps", default=8
    )
    ai_agent_max_output_tokens = fields.Integer(
        string="Maximum answer length (tokens)", config_parameter="odoo_ai_agent.max_output_tokens", default=2048
    )
    ai_agent_timeout = fields.Integer(
        string="Provider timeout (seconds)", config_parameter="odoo_ai_agent.timeout", default=60
    )
    ai_agent_mcp_enabled = fields.Boolean(compute="_compute_ai_agent_mcp_status")
    ai_agent_mcp_model_count = fields.Integer(compute="_compute_ai_agent_mcp_status")
    ai_agent_flow_index_count = fields.Integer(compute="_compute_ai_agent_flow_status")
    ai_agent_flow_indexed_at = fields.Char(compute="_compute_ai_agent_flow_status")

    @api.depends("company_id")
    def _compute_ai_agent_mcp_status(self):
        enabled = self.env["ir.config_parameter"].sudo().get_param("mcp_server.enabled") == "True"
        count = self.env["mcp.enabled.model"].sudo().search_count([("active", "=", True)])
        for settings in self:
            settings.ai_agent_mcp_enabled = enabled
            settings.ai_agent_mcp_model_count = count

    @api.depends("ai_agent_business_flow_enabled")
    def _compute_ai_agent_flow_status(self):
        params = self.env["ir.config_parameter"].sudo()
        count = self.env["ai_agent.flow.index"].sudo().search_count([])
        indexed_at = params.get_param("odoo_ai_agent.business_flow_indexed_at") or ""
        for settings in self:
            settings.ai_agent_flow_index_count = count
            settings.ai_agent_flow_indexed_at = indexed_at

    def action_ai_agent_rebuild_flow_index(self):
        self.ensure_one()
        self.execute()
        self.env["ai_agent.flow.index"].action_rebuild()
        return {"type": "ir.actions.client", "tag": "reload"}

    @api.onchange("ai_agent_provider_id")
    def _onchange_ai_agent_provider_id(self):
        if self.ai_agent_model_id.provider_id != self.ai_agent_provider_id:
            self.ai_agent_model_id = False

    def set_values(self):
        for settings in self:
            if settings.ai_agent_max_steps and not 1 <= settings.ai_agent_max_steps <= 25:
                raise UserError(_("The maximum tool steps must be between 1 and 25."))
            if settings.ai_agent_max_output_tokens and not 256 <= settings.ai_agent_max_output_tokens <= 65536:
                raise UserError(_("The maximum answer length must be between 256 and 65,536 tokens."))
            if settings.ai_agent_timeout and not 10 <= settings.ai_agent_timeout <= 600:
                raise UserError(_("The provider timeout must be between 10 and 600 seconds."))
        result = super().set_values()
        for settings in self:
            if not settings.ai_agent_business_flow_enabled:
                self.env["ai_agent.flow.index"].sudo().search([]).unlink()
                self.env["ir.config_parameter"].sudo().set_param("odoo_ai_agent.business_flow_indexed_at", False)
        return result

    def action_ai_agent_sync_models(self):
        self.ensure_one()
        if not self.ai_agent_provider_id:
            raise UserError(_("Choose an AI provider first."))
        # Save first so a key typed in just now is used.
        self.execute()
        return self.ai_agent_provider_id.action_sync_models()

    def action_ai_agent_enable_mcp(self):
        self.env["ir.config_parameter"].sudo().set_param("mcp_server.enabled", "True")
        mcp_utils.clear_mcp_caches()
        self.env.registry.clear_cache()
        return {"type": "ir.actions.client", "tag": "reload"}
