import base64

from odoo import _, api, fields, models
from odoo.exceptions import AccessError
from odoo.fields import Command
from odoo.tools import file_open

AI_CHANNEL_TYPE = "ai_agent"


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    channel_type = fields.Selection(
        selection_add=[(AI_CHANNEL_TYPE, "AI Agent Conversation")], ondelete={AI_CHANNEL_TYPE: "cascade"}
    )
    # The model the user picked for this conversation; empty means the one chosen in the settings.
    ai_agent_model_id = fields.Many2one("ai_agent.model", string="AI model", ondelete="set null", copy=False)

    def _generate_avatar(self):
        if self.channel_type == AI_CHANNEL_TYPE:
            with file_open("odoo_ai_agent/static/description/icon.svg", "rb") as icon:
                return base64.b64encode(icon.read())
        return super()._generate_avatar()

    def _compute_is_editable(self):
        super()._compute_is_editable()
        for channel in self.filtered(lambda c: c.channel_type == AI_CHANNEL_TYPE):
            # Members rename their own conversations (through ai_agent_rename).
            channel.is_editable = bool(channel.self_member_id)

    def _get_message_create_valid_field_names(self):
        return super()._get_message_create_valid_field_names() | {"ai_agent_payload", "ai_agent_trace"}

    # ------------------------------------------------------------------
    # AI conversations
    # ------------------------------------------------------------------
    @api.model
    def _ai_agent_check_user(self):
        if not self.env.user.has_group("odoo_ai_agent.group_ai_agent_user"):
            raise AccessError(_("You do not have access to the AI Agent. Ask an administrator to give you "
                                "AI Agent access in Settings > Users."))

    def _ai_agent_check_conversation(self):
        """Raise unless self is an AI conversation of the current user."""
        self._ai_agent_check_user()
        for channel in self:
            if channel.channel_type != AI_CHANNEL_TYPE or not channel.self_member_id:
                raise AccessError(_("This AI conversation is not available."))

    @api.model
    def _ai_agent_partner(self):
        return self.env.ref("odoo_ai_agent.partner_ai_agent")

    @api.model
    def _ai_agent_get_or_create_conversation(self):
        """An empty AI conversation of the current user, reused when one already exists."""
        self._ai_agent_check_user()
        empty = self.search([
            ("channel_type", "=", AI_CHANNEL_TYPE),
            ("is_member", "=", True),
            ("message_ids", "=", False),
        ], limit=1, order="id desc")
        if empty:
            return empty
        # sudo: the AI partner is added as the second member, like OdooBot in a direct chat.
        channel = self.sudo().create({
            "name": _("New conversation"),
            "channel_type": AI_CHANNEL_TYPE,
            "channel_member_ids": [
                Command.create({"partner_id": self.env.user.partner_id.id}),
                Command.create({"partner_id": self._ai_agent_partner().id}),
            ],
        })
        return channel.sudo(False)

    def ai_agent_rename(self, name):
        self._ai_agent_check_conversation()
        name = (name or "").strip()[:120]
        if name:
            # sudo: the member renames their own private AI conversation.
            self.sudo().name = name
        return True

    def _ai_agent_model_choices(self):
        """The settings' model and the other active models of the same provider (what the user may pick)."""
        default = self.env["ai_agent.engine"]._ai_default_model()
        if not default:
            return default, self.env["ai_agent.model"]
        # sudo: users pick a model by name; prices and keys are not shared.
        models = self.env["ai_agent.model"].sudo().search([("provider_id", "=", default.provider_id.id)])
        return default, models

    def ai_agent_model_info(self):
        self._ai_agent_check_conversation()
        default, models = self._ai_agent_model_choices()
        chosen = self.sudo().ai_agent_model_id
        return {
            "models": [{"id": m.id, "name": m.name} for m in models],
            "default_id": default.id or False,
            "selected_id": (chosen if chosen in models else default).id or False,
        }

    def ai_agent_set_model(self, model_id):
        self._ai_agent_check_conversation()
        default, models = self._ai_agent_model_choices()
        model = models.filtered(lambda m: m.id == int(model_id or 0))
        if not model:
            raise AccessError(_("This AI model is not available."))
        # sudo: the member chooses the model of their own AI conversation.
        self.sudo().ai_agent_model_id = False if model == default else model
        return True

    def ai_agent_delete(self):
        self._ai_agent_check_conversation()
        # sudo: a user may delete their own private AI conversations.
        self.sudo().unlink()
        return True

    def _ai_agent_set_title_from(self, question):
        """Name a new conversation after its first question (no AI call needed)."""
        self.ensure_one()
        if self.name != _("New conversation"):
            return
        title = " ".join((question or "").split())
        if len(title) > 60:
            title = title[:57].rsplit(" ", 1)[0] + "…"
        if title:
            self.sudo().name = title
