from odoo import api, models, _
from odoo.exceptions import AccessError, UserError

from .conversation import _BOT_POST


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    def message_post(self, **kwargs):
        conversation = self.env["odoo.ai.assistant.conversation"].sudo().search(
            [("channel_id", "in", self.ids)], limit=1,
        ).with_user(self.env.user)
        bot = self.env.ref("odoo_ai_assistant.ai_partner")
        is_bot = kwargs.get("author_id") == bot.id
        if conversation:
            conversation._owned()
            if is_bot and self.env.context.get("odoo_ai_assistant_bot") is not _BOT_POST:
                raise AccessError(_("Only the AI service can post as %s.") % bot.name)
            if kwargs.get("partner_ids"):
                raise AccessError(_("AI conversations cannot notify external recipients."))
            if not is_bot and (kwargs.get("attachment_ids") or kwargs.get("attachments")):
                raise UserError(_("AI conversations support text and local queries only, not attachments."))
        message = super().message_post(**kwargs)
        if conversation and not is_bot and message.message_type == "comment":
            conversation._reply_to_message(message)
        return message

    def write(self, vals):
        if {"channel_type", "group_public_id", "parent_channel_id"} & vals.keys():
            if self.env["odoo.ai.assistant.conversation"].sudo().search_count(
                [("channel_id", "in", self.ids)], limit=1,
            ):
                raise AccessError(_("AI conversation privacy cannot be changed."))
        return super().write(vals)


class DiscussChannelMember(models.Model):
    _inherit = "discuss.channel.member"

    @api.constrains("channel_id", "partner_id", "guest_id")
    def _check_ai_membership(self):
        for member in self:
            conversation = self.env["odoo.ai.assistant.conversation"].sudo().search(
                [("channel_id", "=", member.channel_id.id)], limit=1,
            )
            if conversation:
                allowed = (conversation.user_id.partner_id | self.env.ref("odoo_ai_assistant.ai_partner"))
                if member.guest_id or member.partner_id not in allowed:
                    raise AccessError(_("AI conversations are private and cannot have additional members."))

    def unlink(self):
        if self.env["odoo.ai.assistant.conversation"].sudo().search_count(
            [("channel_id", "in", self.channel_id.ids)], limit=1,
        ):
            raise AccessError(_("Delete the AI conversation instead of removing its members."))
        return super().unlink()
