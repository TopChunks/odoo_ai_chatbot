import base64
import json
import logging
import re
from functools import partial

from markupsafe import Markup, escape

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import SQL, html2plaintext
from odoo.tools.image import image_process
from odoo.tools.mimetypes import guess_mimetype

from .providers import GREETING_INSTRUCTIONS, SCOPE_REFUSAL, ProviderClient, ProviderError, latest_user_index
from .formatting import format_reply
from . import chart_export
from .configuration import _SETTINGS_FIELDS
from .tools import (
    CHART_TOOL, available_tools, chart_models, execute_chart, execute_tool, parse_tool_arguments, requested_chart_tool,
)
from .limits import (
    CHANGE_OPERATIONS, CONVERSATION_LOCK_NAMESPACE, CONVERSATION_PREVIEW_CHARS, CREDENTIAL_FIELD, FILE_THUMBNAIL_SIDE,
    MAX_CHART_IMAGE_CHARS, MAX_DISCOVERY_ROUNDS,
    MAX_FIELD_HINT_NAMES, MAX_FILE_NAME_CHARS, MAX_HISTORY_CHARS, MAX_HISTORY_MESSAGES, MAX_LISTED_CONVERSATIONS,
    MAX_IMAGE_CHARS, MAX_LOADED_MESSAGES, MAX_MESSAGE_CHARS, MAX_MIMETYPE_CHARS, MAX_TITLE_PROMPTS, MAX_TOOL_CALLS,
    WELCOME_SUGGESTIONS, is_restricted_model,
)
from .navigation import (
    accessible_apps, accessible_menu_names, app_screens, app_structure, apps_named_in, apps_using_models, filters_named_in,
    buttons_named_in, current_screen, menus_named_in, screen_context, screen_details, screen_details_for_ai,
    unknown_model_names,
)
from .native_mcp import (
    _IMAGE_TYPES, _UPLOAD_REFERENCE, MAX_UPLOAD_BYTES, NATIVE_TOOL_PREFIX, PRESS_TOOL, DirectReply,
)


_logger = logging.getLogger(__name__)
_BOT_POST = object()


class AIConversation(models.Model):
    _name = "odoo.ai.assistant.conversation"
    _description = "Private AI Conversation"
    _order = "write_date desc, id desc"

    name = fields.Char(required=True, default=lambda self: self.env.ref("odoo_ai_assistant.ai_partner").name)
    # True until a topic title replaces the default name; renames also clear it.
    title_pending = fields.Boolean(default=True)
    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    channel_id = fields.Many2one("discuss.channel", required=True, ondelete="cascade")
    message_ids = fields.One2many("odoo.ai.assistant.message", "conversation_id")
    last_read_message_id = fields.Integer(default=0)
    # Chosen by an administrator from the chat for this conversation only; empty uses the AI Settings model.
    model_id = fields.Many2one("odoo.ai.assistant.provider.model", ondelete="set null")
    _unique_channel = models.Constraint("UNIQUE(channel_id)", "Channel already linked.")

    @api.model
    def _enabled_for_user(self, config):
        return self.env.user.has_group("base.group_user") and self.env.user.ai_assistant_enabled and config.enabled

    @api.model
    def _check_enabled(self):
        config = self.env["odoo.ai.assistant.config"]._configuration()
        if not self._enabled_for_user(config):
            raise AccessError(_("AI Agent is disabled for your user. Contact your administrator."))
        return config

    def _owned(self):
        self.ensure_one()
        self._check_enabled()
        if not self.exists() or self.sudo().user_id != self.env.user:
            raise AccessError(_("This AI conversation belongs to another user."))
        self.check_access("read")
        return self

    def write(self, vals):
        if "name" in vals and "title_pending" not in vals:
            vals = dict(vals, title_pending=False)
        return super().write(vals)

    @api.model
    def bootstrap(self):
        config = self.env["odoo.ai.assistant.config"]._configuration()
        enabled = self._enabled_for_user(config)
        common = {
            "assistant_name": self.env.ref("odoo_ai_assistant.ai_partner").sudo().name,
            "max_message_chars": MAX_MESSAGE_CHARS, "max_upload_bytes": MAX_UPLOAD_BYTES,
        }
        if not enabled:
            return {**common, "enabled": False, "conversations": [], "suggestions": [], "channel_ids": []}
        # Runs on every page load: a fixed number of queries, however many conversations exist.
        all_conversations = self.search([("user_id", "=", self.env.uid)])
        conversations = all_conversations[:MAX_LISTED_CONVERSATIONS]
        Message = self.env["odoo.ai.assistant.message"]
        latest_ids = [message_id for _conversation, message_id in Message._read_group(
            [("conversation_id", "in", conversations.ids)], ["conversation_id"], ["id:max"],
        )]
        latest_by_conversation = {message.conversation_id.id: message for message in Message.browse(latest_ids)}
        # Unread answers per conversation: the AI's messages after the last one the user saw, in one query.
        Message.flush_model(["conversation_id", "role"])
        self.flush_model(["last_read_message_id"])
        self.env.cr.execute(SQL(
            "SELECT message.conversation_id, COUNT(*) FROM %s message JOIN %s conversation"
            " ON conversation.id = message.conversation_id WHERE message.conversation_id = ANY(%s)"
            " AND message.role = 'assistant' AND message.id > conversation.last_read_message_id"
            " GROUP BY message.conversation_id",
            SQL.identifier(Message._table), SQL.identifier(self._table), conversations.ids,
        ))
        unread = dict(self.env.cr.fetchall())
        entries = []
        for conversation in conversations:
            latest = latest_by_conversation.get(conversation.id, Message)
            model = conversation._model(config)
            entries.append({
                "id": conversation.id, "name": conversation.name,
                "model_id": model.id, "model": model.name or _("Not configured"),
                "preview": latest.content[:CONVERSATION_PREVIEW_CHARS] if latest else _("This is the start of your conversation"),
                "date": fields.Datetime.to_string(latest.create_date or conversation.create_date),
                "unread": unread.get(conversation.id, 0),
            })
        return {
            **common,
            "enabled": True, "provider": config.provider,
            "suggestions": [_("What can I do in %s?") % app for app in self._suggested_apps(config)],
            "model": config.model_id.name or _("Not configured"), "model_id": config.model_id.id,
            "can_change_model": config._is_admin(),
            "can_stage_files": config.local_tools and config.native_actions,
            "conversations": sorted(entries, key=lambda entry: entry["date"], reverse=True),
            # Every AI channel (not only the listed ones), so Discuss can keep them out of Direct messages.
            "channel_ids": all_conversations.channel_id.ids,
        }

    @api.model
    def _available_models(self, domain=()):
        """The AI Settings provider's models, as the Settings page offers them (administrators only)."""
        config = self.env["odoo.ai.assistant.config"]
        config._require_admin()
        return self.env["odoo.ai.assistant.provider.model"].search(
            [("provider", "=", config._configuration().provider), *domain],
        )

    @api.model
    def model_choices(self):
        return [{"value": model.id, "label": model.name} for model in self._available_models()]

    def set_model(self, model_id):
        """Change the AI model of this conversation only (administrators); AI Settings keeps the default model
        of every other conversation."""
        self._owned()
        model = (not isinstance(model_id, bool) and isinstance(model_id, int)
                 and self._available_models([("id", "=", model_id)]))
        if not model:
            raise ValidationError(_("Select an available model."))
        self.sudo().model_id = model

    def _model(self, config):
        """The model this conversation uses: the one chosen for it while it still belongs to the AI Settings provider
        and is available (only that provider's key is stored), otherwise the AI Settings model."""
        chosen = self.sudo().model_id  # AI models are readable by administrators only, as AI Settings is
        return chosen if chosen.active and chosen.provider == config.provider else config.model_id

    @api.model
    def _suggested_apps(self, config):
        """Apps to suggest on the welcome screen: those whose data the AI may read, in menu order;
        any accessible app when the AI cannot read Odoo data."""
        models = set(chart_models(self.env)) if config.local_tools else set()
        apps = (apps_using_models(self.env, models) if models else []) or accessible_apps(self.env)
        return apps[:WELCOME_SUGGESTIONS]

    @api.model
    def start_conversation(self, record_model=None, record_id=None):
        self._check_enabled()
        partner = self.env.ref("odoo_ai_assistant.ai_partner")
        name = partner.name
        if record_model is not None or record_id is not None:
            if (not isinstance(record_model, str) or record_model not in self.env
                    or isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0):
                raise ValidationError(_("Select a saved Odoo record to name this conversation."))
            record = self.env[record_model].browse(record_id).exists()
            if not record:
                raise UserError(_("The record used to name this conversation no longer exists."))
            record.check_access("read")
            name = _("AI: %s") % record.display_name
        title_pending = record_model is None and record_id is None
        channel = self.env["discuss.channel"].create({
            "name": name, "channel_type": "group",
            "channel_partner_ids": [fields.Command.link(partner.id)],
        })
        conversation = self.sudo().create({
            "name": name, "user_id": self.env.uid, "channel_id": channel.id,
            "title_pending": title_pending,
        })
        return {"id": conversation.id, "name": conversation.name}

    def get_messages(self, mark_read=True):
        """The conversation's latest messages; ``mark_read`` records that the user has seen them."""
        self._owned()
        messages = self.env["odoo.ai.assistant.message"].search_read(
            [("conversation_id", "=", self.id)],
            ["role", "content", "local_only", "payload", "execution_steps", "create_date", "provider_sensitive"],
            order="id desc", limit=MAX_LOADED_MESSAGES,
        )
        for message in messages:
            if message["payload"] and message["payload"].get("kind") == "mcp_request":
                request = self._tool_request(message["payload"]["request_id"])
                message["payload"] = request._payload()
            if message["role"] == "assistant" and not message["local_only"]:
                message["content_html"] = format_reply(message["content"])
        if mark_read and messages:
            self.sudo().write({"last_read_message_id": messages[0]["id"]})
            latest_channel_message = self.channel_id.message_ids[:1]
            if latest_channel_message:
                self.channel_id.self_member_id._set_last_seen_message(latest_channel_message)
        return list(reversed(messages))

    def _topic_title(self, config):
        if not self.title_pending:
            return ""
        prompts = self.env["odoo.ai.assistant.message"].search(
            [("conversation_id", "=", self.id), ("role", "=", "user")], order="id", limit=MAX_TITLE_PROMPTS,
        ).mapped("content")
        if not prompts:
            return ""
        return config._client("title", self._model(config)).conversation_title(self._model(config).identifier, prompts)

    def _apply_title(self, title):
        self.sudo().write({"name": title, "title_pending": False})
        self.channel_id.sudo().write({"name": title})

    def _lock_conversation(self, error_message):
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", (CONVERSATION_LOCK_NAMESPACE, self.id))
        result = self.env.cr.fetchone()
        if not result or not result[0]:
            raise UserError(error_message)

    def send_message(self, content, files=None, screen=None):
        """Post the user's text, with the staged files (``upload:<id>`` references) it is about and the
        app and screen the user has open (see ``navigation.current_screen``). The answer is returned unread:
        the chat marks it read only if the user still has this conversation on screen."""
        self._owned()
        files = files or []
        if (not isinstance(content, str) or len(content) > MAX_MESSAGE_CHARS
                or not isinstance(files, list) or not (content.strip() or files)):
            raise ValidationError(_("Enter a message of 1 to %s characters.") % f"{MAX_MESSAGE_CHARS:,}")
        self.channel_id.with_context(odoo_ai_staged_files=files, odoo_ai_screen=screen).message_post(
            body=escape(content.strip()), message_type="comment", subtype_xmlid="mail.mt_comment",
        )
        return self.get_messages(mark_read=False)

    def _store_answer(self, content, local_only=False, payload=None, execution_steps=None, provider_sensitive=False):
        # The AI may name models from memory; say which ones this database does not have.
        invented = [] if local_only else unknown_model_names(self.env, content)
        if invented:
            content += "\n\n" + _("Not found in this Odoo: %s.") % ", ".join(f"`{name}`" for name in invented)
        self.env["odoo.ai.assistant.message"].sudo().create({
            "conversation_id": self.id, "role": "assistant",
            "content": content, "local_only": local_only, "payload": payload,
            "execution_steps": execution_steps, "provider_sensitive": provider_sensitive,
        })
        self.channel_id.with_context(odoo_ai_assistant_bot=_BOT_POST).message_post(
            body=(Markup("<pre style='white-space:pre-wrap'>{}</pre>").format(content)
                  if local_only else Markup(format_reply(content))),
            author_id=self.env.ref("odoo_ai_assistant.ai_partner").id,
            message_type="comment", subtype_xmlid="mail.mt_comment",
        )
        self._mark_channel_read()

    def _mark_channel_read(self):
        """The AI panel tracks unread replies itself, so they never count as unread Discuss chat messages."""
        for conversation in self:
            member = conversation.channel_id.channel_member_ids.filtered(
                lambda member: member.partner_id == conversation.user_id.partner_id,
            )
            latest = conversation.channel_id.message_ids[:1]
            if member and latest:
                member._mark_as_read(latest.id)

    def _progress(self, label):
        self.ensure_one()
        # An independent transaction makes progress visible before the chat commits.
        with self.env.registry.cursor() as cr:
            env = api.Environment(cr, self.env.uid, {})
            env.user.partner_id._bus_send("odoo_ai_progress", {
                "conversation_id": self.id, "label": label,
            })

    def load_record_page(self, message_id, previous=False):
        """Show the next (or previous) page of a record search in the same card."""
        self._owned()
        if type(message_id) is not int or message_id <= 0:
            raise ValidationError(_("Choose a valid record-list message."))
        self._lock_conversation(_("This conversation is already being updated."))
        message = self.env["odoo.ai.assistant.message"].search([
            ("id", "=", message_id), ("conversation_id", "=", self.id), ("local_only", "=", True),
        ])
        payload = message.payload or {}
        current = payload.get("table") if payload.get("kind") == "mcp_request" else None
        if not current or not (current["offset"] if previous else current.get("has_more")):
            raise UserError(_("This message has no other record page to show."))
        request = self._tool_request(payload["request_id"])
        request._validate_current(self)
        if request.tool != "mcp_search_records":
            raise ValidationError(_("Only a record search can load another page."))
        page_size = (request.result.get("structuredContent") or {}).get("limit") or len(current["rows"])
        arguments = dict(request.arguments or {})
        arguments["offset"] = (max(0, current["offset"] - page_size) if previous
                               else current["offset"] + len(current["rows"]))
        page = self.env["odoo.ai.assistant.tool.request"]._prepare(
            self.with_context(odoo_ai_provider_plan=True), request.tool, {"arguments_json": json.dumps(arguments)},
        )
        message.sudo().write({"payload": page._payload()})
        return self.get_messages()

    def _add_step(self, steps, label):
        """Record an execution step and show it as live progress."""
        steps.append(label)
        self._progress(label)

    def _native_request(self, name, arguments, steps=None):
        request = self.env["odoo.ai.assistant.tool.request"]._prepare(
            self.with_context(odoo_ai_provider_plan=True), name, arguments,
        )
        self._store_answer(
            _("Review this action. No change will occur until you confirm. Business automation, notifications "
              "and related-record changes may run when confirmed.") if request.mutation else
            _("MCP result retrieved locally. Sharing with your external AI provider requires separate approval."),
            local_only=True, payload=request._payload(), execution_steps=steps,
        )
        return request

    def _tool_request(self, request_id):
        self._owned()
        if type(request_id) is not int or request_id <= 0:
            raise ValidationError(_("Choose a valid native tool request."))
        request = self.env["odoo.ai.assistant.tool.request"].sudo().browse(request_id).exists()
        if not request or request.user_id.id != self.env.uid or request.conversation_id.id != self.id:
            raise AccessError(_("This tool request belongs to another conversation or user."))
        return request

    def _update_tool_message(self, request):
        for message in self.env["odoo.ai.assistant.message"].sudo().search([("conversation_id", "=", self.id)]):
            if message.payload and message.payload.get("request_id") == request.id:
                message.write({"payload": request._payload()})

    def confirm_tool(self, request_id):
        request = self._tool_request(request_id)
        with self.env.cr.savepoint():
            request._confirm(self)
            self._update_tool_message(request)
        return self.get_messages()

    def cancel_tool(self, request_id):
        request = self._tool_request(request_id)
        request._lock()
        if request.state != "pending":
            raise UserError(_("This tool request is no longer awaiting confirmation."))
        request.write({"state": "cancelled"})
        self._update_tool_message(request)
        return self.get_messages()

    def share_tool_result(self, request_id):
        request = self._tool_request(request_id)
        self._lock_conversation(_("An answer is already being generated for this conversation."))
        request._lock()
        evidence, images = request._sharing_context(self)
        config = self._check_enabled()
        event = self.env["odoo.ai.assistant.tool.event"]._begin(request.tool, state="shared")
        history = self._provider_history()
        client = config._client("reply", self._model(config))
        options = {"native_tools": True, "tool_context": evidence, "tool_images": images}
        if config.screen_metadata:
            options["screen_context"] = screen_context(self.env, menus=False)
        try:
            tools = [tool for tool in available_tools(self.env) if tool["name"].startswith(NATIVE_TOOL_PREFIX)]
            text, calls = client.reply(self._model(config).identifier, history, tools, **options)
        except Exception:
            self.env["odoo.ai.assistant.tool.event"]._finish(event, "failed")
            raise
        request.write({"state": "shared"})
        self._update_tool_message(request)
        if calls:
            self._dispatch_calls(client, config, history, calls, [], allow_native_only=True)
        else:
            self._store_answer(text, provider_sensitive=True)
        return self.get_messages()

    def stage_upload(self, name, data, mimetype=None, thumbnail=None):
        self._owned()
        config = self._check_enabled()
        if not (config.local_tools and config.native_actions):
            raise AccessError(_("Enable confirmed business actions before staging files."))
        if (not isinstance(name, str) or not name.strip() or len(name) > MAX_FILE_NAME_CHARS
                or not isinstance(data, str) or len(data) > (MAX_UPLOAD_BYTES * 4 // 3 + 4)
                or (mimetype is not None and (not isinstance(mimetype, str) or len(mimetype) > MAX_MIMETYPE_CHARS))):
            raise ValidationError(_("Choose a named file within the MCP upload limit."))
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError as error:
            raise ValidationError(_("The selected file is not valid base64.")) from error
        if not raw or len(raw) > MAX_UPLOAD_BYTES:
            raise ValidationError(_("The selected file is empty or exceeds the MCP upload limit."))
        self.env["odoo.ai.assistant.tool.event"]._begin("stage_upload", state="done")
        upload = self.env["odoo.ai.assistant.upload"].sudo().create({
            "conversation_id": self.id, "user_id": self.env.uid, "name": name.strip(),
            "data": data, "mimetype": mimetype, "size": len(raw),
            "thumbnail": self._file_thumbnail(raw, thumbnail),
        })
        return upload._card()

    @api.model
    def _raster(self, data, side=0):
        """``data`` (bytes, or base64 text from a browser) re-encoded by Odoo as a raster image, at most
        ``side`` pixels wide and high (0: unchanged), or None when it is not a PNG/JPEG/GIF/WebP image.
        image_process passes SVG/XML through unchanged, hence the type check."""
        try:
            raw = base64.b64decode(data, validate=True) if isinstance(data, str) else data
            image = image_process(raw, size=(side, side), verify_resolution=True)
        except (UserError, ValueError, OSError):
            return None
        return image if guess_mimetype(image) in _IMAGE_TYPES else None

    @api.model
    def _file_thumbnail(self, raw, thumbnail=None):
        """Preview for a file card: an image is reduced directly; for other files (PDFs) only a
        browser-drawn preview is accepted, and only once Odoo has re-encoded it as a small image."""
        browser_preview = thumbnail if isinstance(thumbnail, str) and len(thumbnail) <= MAX_IMAGE_CHARS else None
        preview = (browser_preview and self._raster(browser_preview, FILE_THUMBNAIL_SIDE)) or \
            self._raster(raw, FILE_THUMBNAIL_SIDE)
        if not preview:
            return False  # not an image: the card shows the file-type icon
        return "data:%s;base64,%s" % (guess_mimetype(preview), base64.b64encode(preview).decode("ascii"))

    def export_chart(self, message_id, file_format, image=None):
        """A chart or grouped table of this conversation as a PDF or Excel file. ``image`` is the chart as
        drawn in the browser (base64), placed in the PDF once Odoo has re-encoded it."""
        self._owned()
        message = self.env["odoo.ai.assistant.message"].search(
            [("id", "=", message_id), ("conversation_id", "=", self.id)]) if type(message_id) is int else None
        payload = (message and message.payload) or {}
        if payload.get("kind") not in {"chart", "table"} or file_format not in chart_export.EXPORT_FORMATS:
            raise ValidationError(_("Choose a chart or table of this conversation, exported as PDF or Excel."))
        title, headers, rows = chart_export.export_table(payload)
        if file_format == "xlsx":
            data = chart_export.to_xlsx(title, headers, rows, payload.get("chart_type"))
        else:
            picture = self._raster(image) if isinstance(image, str) and len(image) <= MAX_CHART_IMAGE_CHARS else None
            data = chart_export.to_pdf(self.env, title, headers, rows, picture)
        return {"name": chart_export.file_name(title, file_format), "mimetype": chart_export.EXPORT_FORMATS[file_format],
                "data": base64.b64encode(data).decode("ascii")}

    def _staged_files(self, references):
        """The current user's staged files of this conversation, for ``upload:<id>`` references."""
        if not isinstance(references, list):
            raise ValidationError(_("Choose staged files from this conversation."))
        bridge = self.env["odoo.ai.assistant.native.mcp"]
        uploads = self.env["odoo.ai.assistant.upload"]
        for reference in dict.fromkeys(references):
            uploads |= bridge._staged_upload(reference, self)
        return uploads

    def _mentions_staged_upload(self, history):
        """Whether the latest request names a file staged in this conversation (Odoo data, no topic check)."""
        index = latest_user_index(history)
        latest = history[index]["content"] if index is not None else ""
        ids = {int(match[1]) for match in _UPLOAD_REFERENCE.finditer(latest)}
        return bool(ids) and bool(self.env["odoo.ai.assistant.upload"].sudo().search_count([
            ("id", "in", list(ids)), ("conversation_id", "=", self.id), ("user_id", "=", self.env.uid),
        ]))

    def _provider_history(self):
        history = self.env["odoo.ai.assistant.message"].search(
            [("conversation_id", "=", self.id)], order="id desc", limit=MAX_HISTORY_MESSAGES,
        )
        entries = [{
            "role": message.role,
            # A bracketed system note, so models do not echo it as their own reply.
            "content": ("[System note, not a reply: an earlier request was completed and its result was "
                        "shown to the user inside Odoo; the values are not shared with you. Answer the "
                        "user's latest message; do not repeat this note or the earlier request.]"
                        if message.local_only or message.provider_sensitive else self._provider_text(message)),
        } for message in reversed(history)]
        while entries and entries[0]["role"] != "user":
            entries.pop(0)
        # Bound history by both message count and text size. A count-only window
        # can exceed smaller providers' context windows because prompts may be long.
        bounded = []
        used = 0
        for entry in reversed(entries):
            size = len(entry["content"])
            if bounded and used + size > MAX_HISTORY_CHARS:
                break
            bounded.append(entry)
            used += size
        bounded.reverse()
        while bounded and bounded[0]["role"] != "user":
            bounded.pop(0)
        return bounded

    @api.model
    def _provider_text(self, message):
        """A message as the AI provider reads it: the user's text plus a reference to each attached file."""
        files = message.payload.get("files", []) if message.payload and message.payload.get("kind") == "files" else []
        return "\n".join(([message.content] if message.content else []) + [
            _("Use staged file %(name)s, reference %(reference)s.", name=file["name"], reference=file["reference"])
            for file in files
        ])

    def _reply_to_message(self, message):
        self._owned()
        self._lock_conversation(_("An answer is already being generated for this conversation."))
        content = html2plaintext(message.body).strip()
        uploads = self._staged_files(self.env.context.get("odoo_ai_staged_files") or [])
        if not (content or uploads) or len(content) > MAX_MESSAGE_CHARS:
            raise ValidationError(_("AI messages must contain 1 to %s characters of text.") % f"{MAX_MESSAGE_CHARS:,}")
        config = self.env["odoo.ai.assistant.config"]._configuration()
        if not self._model(config).active:
            raise UserError(_("Select an available model in AI Settings first."))
        self.env["odoo.ai.assistant.message"].sudo().create({
            "conversation_id": self.id, "role": "user", "content": content,
            "payload": {"kind": "files", "files": [upload._card() for upload in uploads]} if uploads else False,
        })
        # Latest activity first in the conversation list, whatever the outcome below.
        self.sudo().write({"write_date": fields.Datetime.now()})
        if not uploads and ProviderClient.is_standalone_greeting(content):
            self._progress(_("Responding to your greeting"))
            text, calls = config._client("reply", self._model(config)).reply(
                self._model(config).identifier, [{"role": "user", "content": content}], [],
                system_override=GREETING_INSTRUCTIONS,
            )
            if calls or not text.strip():
                raise UserError(_("The AI provider did not return a valid greeting. Please retry."))
            self._store_answer(text, execution_steps=[_("Responding to your greeting")])
            return
        history = self._provider_history()
        tools = available_tools(self.env) if config.local_tools else []
        steps = []
        progress = partial(self._add_step, steps)
        progress(_("Checking your request"))
        intent = config._client("scope", self._model(config)).classify_request(
            self._model(config).identifier, history, app_names=accessible_apps(self.env),
            vocabulary=accessible_menu_names(self.env), intent=config.local_tools,
            topic_known=self._mentions_staged_upload(history),
        )
        if intent == "OUTSIDE":
            self._store_answer(self.env._(SCOPE_REFUSAL), execution_steps=steps)
            return
        action_request, chart_request = intent in CHANGE_OPERATIONS, intent == "CHART"
        help_request = intent == "HELP"
        # Made-up values were asked for: new records may then carry names the user never wrote (see _prepare_request).
        self = self.with_context(odoo_ai_sample_data=intent == "GENERATE")
        change_tools = []
        if config.local_tools:
            change_tools = self._tools_for(tools, action=True)
            tools = self._tools_for(tools, action=action_request, operation=CHANGE_OPERATIONS.get(intent))
        if help_request:
            tools = []  # general Odoo know-how: no tools or model list, a fraction of a data request's size
        progress(_("Answering from Odoo know-how") if help_request else _("Selecting approved local tools"))
        client = config._client("reply", self._model(config))
        # Reads are offered no data-changing tools, so their rules are left out of the instructions.
        context_options = dict(self._reply_context(config, data=not help_request), change_request=action_request)
        selected_view = requested_chart_tool(tools) if chart_request else None
        reply_tools = [selected_view] if selected_view else tools
        try:
            text, calls = client.reply(
                self._model(config).identifier, history, reply_tools,
                tool_choice=selected_view["name"] if selected_view else None, **context_options,
            )
        except ProviderError as error:
            if not selected_view or error.kind != "tool_not_called":
                raise
            # The model ignored the chart instruction; let it choose freely among all tools once.
            reply_tools = tools
            text, calls = client.reply(self._model(config).identifier, history, tools, **context_options)
        try:
            title = self._topic_title(config)
        except UserError:
            # A title is cosmetic; never lose the answer because naming the chat failed.
            _logger.info("AI conversation %s: automatic title skipped after an invalid provider result.", self.id)
            title = ""
        if calls:
            # A retry after a refused change may use any data-changing tool, not only the matched ones.
            retry_tools = change_tools if action_request else reply_tools
            self._dispatch_with_recovery(client, config, history, calls, steps, retry_tools,
                                         context_options, action_request)
        else:
            self._store_answer(text, execution_steps=steps, payload=self._named_apps())
        if title:
            self._apply_title(title)

    def _reply_context(self, config, request=None, action_schema=False, data=True):
        """Context for a reply; without ``data`` (a question general Odoo knowledge answers) the model list and
        named filters are left out, as no record tool is offered."""
        context = {}
        if config.screen_metadata:
            context = screen_context(self.env, menus=False)
            screen = current_screen(self.env, self.env.context.get("odoo_ai_screen"))
            if screen:
                context["current_screen"] = screen
            context.update(self._menus_named())
        if config.local_tools:
            context["calendar"] = {
                "today": fields.Date.to_string(fields.Date.context_today(self)),
                "timezone": self.env.user.tz or "UTC",
            }
            metadata = data and self.env["odoo.ai.assistant.native.mcp"]._planning_context(
                request, all_fields=action_schema)
            if metadata:
                context["native_schema"] = metadata
            named = data and filters_named_in(self.env, self._latest_question(), chart_models(self.env))
            if named:
                context["named_filters"] = named
            # The AI Settings' own labels, so the AI knows these are settings an administrator changes, not records.
            context["ai_settings"] = [field["string"] for field in config.fields_get(_SETTINGS_FIELDS, ["string"]).values()]
        options = {"screen_context": context} if context else {}
        if config.local_tools and data:
            options["native_tools"] = True
        return options

    def _store_explanation(self, client, config, history, context, steps, screens=()):
        """Ask the provider to explain screen metadata (no tools) and store the answer, with previews of the
        buttons of the inspected ``screens`` that the question names."""
        explanation, extra_calls = client.reply(self._model(config).identifier, history, [],
                                                screen_context=dict(context, **self._menus_named()))
        if extra_calls or not explanation.strip():
            raise UserError(_("The AI provider did not return an explanation. Please retry."))
        self._store_answer(explanation, execution_steps=list(steps), payload=self._named_apps(screens))

    def _latest_question(self):
        """The text of the user's latest message in this conversation."""
        return self.env["odoo.ai.assistant.message"].search(
            [("conversation_id", "=", self.id), ("role", "=", "user")], order="id desc", limit=1,
        ).content or ""

    def _menus_named(self):
        """Screen context naming where each screen in the latest question is, its model and whether the AI may read
        that model's records ({} when the question names no screen)."""
        named = menus_named_in(self.env, self._latest_question())
        readable = set(chart_models(self.env)) if named and self.env[
            "odoo.ai.assistant.config"]._configuration().local_tools else set()
        for menu in named:
            if "model" in menu:
                menu["ai_can_read"] = menu["model"] in readable
        return {"named_menus": named} if named else {}

    def _named_apps(self, screens=()):
        """The apps and buttons the latest question names, so the chat shows their icons and what the buttons
        look like under a written answer. Buttons come from the screens the AI inspected and from the screens of
        the named apps (or the app open now), so the previews do not depend on which screen the AI chose."""
        question = self._latest_question()
        apps, buttons = apps_named_in(self.env, question), []
        if self.env["odoo.ai.assistant.config"]._configuration().screen_metadata:
            current = (current_screen(self.env, self.env.context.get("odoo_ai_screen")) or {}).get("app")
            searched = apps or apps_named_in(self.env, current or "")
            buttons = buttons_named_in(self.env, [*screens, *app_screens(self.env, searched)], question)
        return {"kind": "apps", "apps": apps, "buttons": buttons} if apps or buttons else None

    def _field_hint(self, reason):
        """For an invented field (Odoo: "Invalid field <model>.<field>"), list the model's real field names."""
        match = re.search(r"Invalid field ([a-z0-9_.]+)\.([a-z0-9_]+)", reason)
        model = match and match.group(1)
        if not model or is_restricted_model(self.env, model) or not self.env[model].has_access("read"):
            return ""
        names = sorted(name for name in self.env[model].fields_get(attributes=["type"])
                       if not CREDENTIAL_FIELD.search(name))
        return " Fields available on %s: %s." % (model, ", ".join(names[:MAX_FIELD_HINT_NAMES]))

    def _tools_for(self, tools, action, operation=None):
        """Tools for an action: only native tools that change data (the native request validates the
        target and stages a preview, so no schema discovery is needed first), narrowed to MCP Server's
        ``operation`` tag when given (and when any such tool exists). For a question: all others."""
        catalogue = self.env["odoo.ai.assistant.native.mcp"]._catalogue()
        selected = [tool for tool in tools
                    if (tool["name"] in catalogue and catalogue[tool["name"]]["mutation"]) == action]
        matching = [tool for tool in selected if operation and (
            catalogue[tool["name"]]["operation"] == operation or tool["name"] == PRESS_TOOL)]
        return matching or selected

    def _try_dispatch(self, client, config, history, calls, steps, action_request):
        """Run tool calls in a savepoint. Return why Odoo refused them, or None when they ran or Odoo has answered
        instead (a missing MCP Server permission, a value only the user can give)."""
        try:
            with self.env.cr.savepoint():
                self._dispatch_calls(client, config, history, calls, steps, action_request=action_request)
        except ProviderError:
            raise
        except DirectReply as error:
            # Another tool would not help: say what is needed, in plain words.
            self._store_answer(str(error.args[0]), execution_steps=steps)
            return None
        # ValueError covers Odoo domain errors such as an invented field name.
        except (AccessError, ValidationError, UserError, ValueError) as error:
            return str(error.args[0]) if error.args else str(error)
        return None

    def _dispatch_with_recovery(self, client, config, history, calls, steps, tools, options, action_request):
        """Run the provider's tool calls. If Odoo refuses or rejects a call, tell the provider once
        so it can choose another tool; if that also fails, answer with the reason instead of an error."""
        reason = self._try_dispatch(client, config, history, calls, steps, action_request)
        if reason is None:
            return
        self._add_step(steps, _("Choosing another approach after: %s") % reason)
        latest = history[-1]
        retry_history = history[:-1] + [dict(latest, content=latest["content"] + (
            "\n\n[Odoo note: the tool call you chose could not run: %s.%s Choose a different available tool "
            "that fits, or answer without tools. Do not repeat the same call. If you explain the reason, keep "
            "Odoo's menu paths and names exactly as written.]" % (reason, self._field_hint(reason))
        ))]
        text, retry_calls = client.reply(self._model(config).identifier, retry_history, tools, **options)
        if retry_calls:
            reason = self._try_dispatch(client, config, retry_history, retry_calls, steps, action_request)
            if reason is None:
                return
        elif text.strip():
            self._store_answer(text, execution_steps=steps, payload=self._named_apps())
            return
        self._store_answer(_("I couldn't complete this request in Odoo: %s") % reason,
                           local_only=True, execution_steps=steps)

    def _dispatch_calls(self, client, config, history, calls, steps, allow_native_only=False, discovery_depth=0,
                        action_request=False):
        tools = available_tools(self.env) if config.local_tools else []
        if action_request:
            tools = self._tools_for(tools, action=True)
        discovery = []
        terminal_result = False
        progress = partial(self._add_step, steps)
        if len(calls) > MAX_TOOL_CALLS:
            raise UserError(_("The provider requested too many tools. Ask a narrower question."))
        if calls:
            if not config.local_tools:
                raise AccessError(_("The provider requested a tool while local tools are disabled."))
            for call in calls:
                if not isinstance(call.get("name"), str) or call["name"] not in {tool["name"] for tool in tools}:
                    raise AccessError(_("The provider requested a tool unavailable to your user."))
                arguments = parse_tool_arguments(call["arguments"])
                if call["name"].startswith(NATIVE_TOOL_PREFIX):
                    progress(_("Preparing confirmed MCP action") if self.env[
                        "odoo.ai.assistant.native.mcp"
                    ]._catalogue()[call["name"]]["mutation"] else _("Executing native MCP read"))
                    if call["name"] in {"mcp_list_models", "mcp_get_fields"}:
                        request = self.env["odoo.ai.assistant.tool.request"]._prepare(self, call["name"], arguments)
                        discovery.append(self._reply_context(config, request, action_schema=action_request)[
                            "screen_context"
                        ]["native_schema"])
                    else:
                        self._native_request(call["name"], arguments, list(steps))
                        terminal_result = True
                elif allow_native_only:
                    raise UserError(_("Use a native MCP tool when continuing an approved shared result."))
                elif call["name"] == "odoo_screen_details":
                    terminal_result = True
                    progress(_("Inspecting permitted fields in the requested Odoo screen"))
                    details = screen_details(self.env, arguments)
                    context = screen_context(self.env, arguments["app"])
                    context["screen_details"] = screen_details_for_ai(details)
                    progress(_("Explaining the screen fields"))
                    self._store_explanation(client, config, history, context, steps, screens=details["screens"])
                elif call["name"] == "odoo_app_structure":
                    terminal_result = True
                    progress(_("Inspecting accessible Odoo app menus"))
                    payload = app_structure(self.env, arguments)
                    if config.screen_metadata:
                        progress(_("Explaining the requested Odoo screens"))
                        self._store_explanation(
                            client, config, history, screen_context(self.env, arguments["app"]), steps,
                        )
                    else:
                        self._store_answer(
                            _("These are the app screens available to your user. To see their records, "
                              "an administrator must allow the AI to use that Odoo data in AI Settings."),
                            local_only=True, payload=payload, execution_steps=list(steps),
                        )
                elif call["name"] == CHART_TOOL:
                    terminal_result = True
                    progress(_("Grouping the records and drawing the chart locally"))
                    payload = execute_chart(self.env, arguments)
                    self._store_answer(_("Local Odoo chart: %s") % payload["title"],
                                       local_only=True, payload=payload, execution_steps=list(steps))
                else:
                    terminal_result = True
                    progress(_("Reading running Odoo instance information") if call["name"] == "odoo_instance_info"
                             else _("Executing approved local MCP query"))
                    result = execute_tool(self.env, call["name"], arguments)
                    if call["name"] == "odoo_instance_info" and config.screen_metadata:
                        progress(_("Explaining Odoo version information"))
                        self._store_explanation(client, config, history, screen_context(self.env, menus=False),
                                                steps)
                    else:
                        self._store_answer(
                            _("Local Odoo results (not sent to any AI provider):\n%s") % result,
                            local_only=True, execution_steps=list(steps),
                        )
        if discovery and not terminal_result:
            if discovery_depth >= MAX_DISCOVERY_ROUNDS:
                raise UserError(_("The AI kept discovering schemas without completing your query. "
                                  "Please retry with a narrower request."))
            progress(_("Using discovered fields to complete your request"))
            options = self._reply_context(config)
            options["screen_context"]["discovered_schemas"] = discovery
            next_tools = [tool for tool in tools if not allow_native_only or tool["name"].startswith(NATIVE_TOOL_PREFIX)]
            text, next_calls = client.reply(self._model(config).identifier, history, next_tools, **options)
            if next_calls:
                self._dispatch_calls(client, config, history, next_calls, steps,
                                     allow_native_only=allow_native_only, discovery_depth=discovery_depth + 1,
                                     action_request=action_request)
            elif text.strip():
                self._store_answer(text, execution_steps=list(steps))
            else:
                raise UserError(_("The AI did not complete your query after schema discovery. Please retry."))

    def unlink(self):
        for conversation in self:
            conversation._owned()
        channels = self.channel_id
        result = super().unlink()
        channels.sudo().unlink()
        return result


class AIMessage(models.Model):
    _name = "odoo.ai.assistant.message"
    _description = "AI Conversation Message"
    _order = "id"

    conversation_id = fields.Many2one("odoo.ai.assistant.conversation", required=True, ondelete="cascade", index=True)
    role = fields.Selection([("user", "User"), ("assistant", "Assistant")], required=True)
    content = fields.Text(required=True)
    local_only = fields.Boolean(default=False)
    payload = fields.Json()
    execution_steps = fields.Json()
    provider_sensitive = fields.Boolean(default=False)
