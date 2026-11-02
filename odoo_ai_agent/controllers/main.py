from werkzeug.exceptions import NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import content_disposition, request

from odoo.addons.mail.controllers.webclient import WebclientController
from odoo.addons.mail.tools.discuss import Store

# pg advisory lock namespace ("AIAG"): one answer at a time per conversation.
ANSWER_LOCK = 0x41494147


class AiAgentWebclient(WebclientController):
    @classmethod
    def _process_request_for_internal_user(cls, store: Store, name, params):
        super()._process_request_for_internal_user(store, name, params)
        if name == "/ai_agent/new_conversation":
            channel = request.env["discuss.channel"]._ai_agent_get_or_create_conversation()
            store.add(channel).resolve_data_request(channel=Store.One(channel, []))


class AiAgentController(http.Controller):
    def _channel(self, channel_id):
        channel = request.env["discuss.channel"].browse(int(channel_id)).exists()
        if not channel:
            raise NotFound()
        channel._ai_agent_check_conversation()
        return channel

    @http.route("/ai_agent/answer", type="jsonrpc", auth="user")
    def answer(self, channel_id):
        channel = self._channel(channel_id)
        request.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", (ANSWER_LOCK, channel.id))
        if not request.env.cr.fetchone()[0]:
            return {"status": "busy"}
        result = request.env["ai_agent.engine"]._ai_agent_answer(channel)
        if result.get("message_id"):
            # Also returned here, so the answer shows even when the bus connection is slow or down.
            message = request.env["mail.message"].browse(result["message_id"])
            store = Store().add(message).add(channel, ["name"])
            member = channel.self_member_id
            if member:
                # The server's unread count (the green badge), stamped like Odoo's own member updates so
                # the browser keeps it in step with later "read" updates.
                # sudo: bus.bus - reading the non-sensitive last notification id
                bus_last_id = request.env["bus.bus"].sudo()._bus_last_id()
                store.add(member, [
                    Store.One("channel_id", [], as_thread=True),
                    "message_unread_counter",
                    {"message_unread_counter_bus_id": bus_last_id},
                    "new_message_separator",
                    *request.env["discuss.channel.member"]._to_store_persona([]),
                ])
            result["store_data"] = store.get_result()
        return result

    @http.route("/ai_agent/action", type="jsonrpc", auth="user")
    def action(self, message_id, block_id, confirm):
        message = request.env["mail.message"].browse(int(message_id)).exists()
        if not message:
            raise NotFound()
        return request.env["ai_agent.engine"]._ai_agent_execute_action(message, block_id, bool(confirm))

    @http.route("/ai_agent/welcome", type="jsonrpc", auth="user", readonly=True)
    def welcome(self, channel_id=False):
        """Data for an empty conversation: the previous chat to resume and example questions."""
        Channel = request.env["discuss.channel"]
        Channel._ai_agent_check_user()
        previous = Channel.search([
            ("channel_type", "=", "ai_agent"), ("is_member", "=", True), ("message_ids", "!=", False),
            ("id", "!=", int(channel_id or 0)),
        ], order="last_interest_dt desc, id desc", limit=1)
        return {
            "previous": {"id": previous.id, "name": previous.name} if previous else False,
            "suggestions": request.env["ai_agent.engine"]._suggestions(),
        }

    @http.route("/ai_agent/latest", type="jsonrpc", auth="user", readonly=True)
    def latest(self):
        """The conversation the AI Agent app opens on: the most recent one, if any."""
        request.env["discuss.channel"]._ai_agent_check_user()
        channel = request.env["discuss.channel"].search(
            [("channel_type", "=", "ai_agent"), ("is_member", "=", True)],
            order="last_interest_dt desc, id desc", limit=1,
        )
        return channel.id or False

    @http.route("/ai_agent/export", type="http", auth="user", methods=["POST"])
    def export(self, message_id, block_id, file_format, image=None, **kwargs):
        exporter = request.env["ai_agent.export"]
        try:
            if file_format == "xlsx":
                filename, content = exporter.export_xlsx(message_id, block_id)
                mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            elif file_format == "pdf":
                filename, content = exporter.export_pdf(message_id, block_id, image)
                mimetype = "application/pdf"
            else:
                raise NotFound()
        except (AccessError, UserError) as err:
            return request.make_response(
                str(err.args[0] if err.args else err), headers=[("Content-Type", "text/plain; charset=utf-8")],
                status=400,
            )
        return request.make_response(content, headers=[
            ("Content-Type", mimetype),
            ("Content-Disposition", content_disposition(filename)),
        ])

    @http.route("/ai_agent/dashboard", type="jsonrpc", auth="user", readonly=True)
    def dashboard(self, days=30, provider_id=False, model_id=False, user_id=False):
        return request.env["ai_agent.usage"].get_dashboard_data(days, provider_id, model_id, user_id)
