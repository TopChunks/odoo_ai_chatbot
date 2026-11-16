from odoo import api, fields, models


class AiAgentModel(models.Model):
    _name = "ai_agent.model"
    _description = "AI Model"
    _order = "provider_sequence, name"
    _rec_names_search = ["name", "code"]

    name = fields.Char(required=True)
    code = fields.Char("Model ID", required=True, help="Technical model name sent to the provider.")
    provider_id = fields.Many2one("ai_agent.provider", required=True, ondelete="cascade", index=True)
    provider_sequence = fields.Integer(related="provider_id.sequence", store=True)
    active = fields.Boolean(default=True)
    archived_manually = fields.Boolean(
        copy=False, help="Archived by an administrator: the next model sync keeps it archived."
    )
    description = fields.Text()
    context_window = fields.Integer("Context window (tokens)")
    max_output_tokens = fields.Integer("Max output (tokens)")
    input_price = fields.Float("Input price ($ / 1M tokens)", digits=(16, 4))
    output_price = fields.Float("Output price ($ / 1M tokens)", digits=(16, 4))
    cache_read_price = fields.Float("Cached input price ($ / 1M tokens)", digits=(16, 4))
    price_source = fields.Selection(
        [("provider", "Provider"), ("openrouter", "OpenRouter list"), ("manual", "Manual")],
        help="Where the prices come from. Prices typed in by hand are kept on the next sync.",
    )
    is_free = fields.Boolean("Free")
    last_seen = fields.Datetime("Last listed by provider", readonly=True)

    _code_provider_unique = models.Constraint(
        "UNIQUE(provider_id, code)", "This model is already listed for this provider."
    )

    @api.depends("name", "provider_id")
    def _compute_display_name(self):
        for model in self:
            model.display_name = f"{model.provider_id.name} · {model.name}" if model.provider_id else model.name

    def write(self, vals):
        if "active" in vals and "archived_manually" not in vals:
            vals["archived_manually"] = not vals["active"]
        if {"input_price", "output_price", "cache_read_price"} & set(vals) and "price_source" not in vals:
            vals["price_source"] = "manual"
        return super().write(vals)

    def _cost(self, usage):
        """USD cost of one provider call from its token usage."""
        self.ensure_one()
        if usage.get("cost") is not None:
            return float(usage["cost"])
        cached = usage.get("cached") or 0
        cache_write = usage.get("cache_write") or 0
        fresh = max((usage.get("input") or 0) - cached - cache_write, 0)
        cache_price = self.cache_read_price or self.input_price
        return (
            fresh * self.input_price
            + cached * cache_price
            # Anthropic bills cache writes at 1.25x the input price.
            + cache_write * self.input_price * 1.25
            + (usage.get("output") or 0) * self.output_price
        ) / 1_000_000
