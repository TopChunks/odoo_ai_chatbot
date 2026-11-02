from odoo import fields, models

from odoo.addons.mail.tools.discuss import Store


class MailMessage(models.Model):
    _inherit = "mail.message"

    # What the user sees under an AI answer: tables, records, charts, change previews.
    ai_agent_payload = fields.Json(copy=False)
    # What the AI itself was told about this answer (tool calls and their redacted results),
    # replayed as context for follow-up questions. Never sent to the browser.
    ai_agent_trace = fields.Json(copy=False, groups="base.group_system")

    def _to_store_defaults(self, target):
        return super()._to_store_defaults(target) + [
            Store.Attr("ai_agent_payload", predicate=lambda m: bool(m.ai_agent_payload)),
        ]
