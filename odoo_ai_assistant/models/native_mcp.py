import base64
import contextlib
import copy
import hashlib
import io
import json
import logging
import re
from urllib.parse import urlsplit
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from jsonschema.validators import validator_for
from jsonschema.exceptions import SchemaError, ValidationError as SchemaValidationError
from lxml import etree

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.service.model import get_public_method
from odoo.tools import SQL, config as server_config, formatLang, html2plaintext
from odoo.tools.image import image_process
from odoo.tools.mimetypes import guess_mimetype
from odoo.tools.translate import LazyTranslate

if TYPE_CHECKING:
    from mcp_server.controllers import utils
    from mcp_server.models.mcp_mixin import MAX_EXTRACT_BYTES, MAX_INLINE_BLOB_BYTES, mcp_tool
    from mcp_server.models.mcp_tools_read import _tool_result
    from mcp_server.models.mcp_tools_write import (
        MAX_UPLOAD_BYTES as MAX_UPLOAD_BYTES, _BLOCKED_METHOD_CALLS, _METHOD_CALL_BLOCKED_MODELS, _json_safe,
    )
else:
    from odoo.addons.mcp_server.controllers import utils
    from odoo.addons.mcp_server.models.mcp_mixin import MAX_EXTRACT_BYTES, MAX_INLINE_BLOB_BYTES, mcp_tool
    from odoo.addons.mcp_server.models.mcp_tools_read import _tool_result
    from odoo.addons.mcp_server.models.mcp_tools_write import (
        MAX_UPLOAD_BYTES as MAX_UPLOAD_BYTES, _BLOCKED_METHOD_CALLS, _METHOD_CALL_BLOCKED_MODELS, _json_safe,
    )

from .tools import cell_value, data_access_menus
from .navigation import button_label, eval_view_expression, visible_for
from .providers import _vocabulary_pattern
from .limits import (
    CREDENTIAL_FIELD, CURRENT_USER, MAX_ATTACHMENT_TEXT_CHARS, MAX_FIELD_SUGGESTIONS, MAX_AUDIT_TOOL_NAME_CHARS, MAX_IMAGE_CHARS, MAX_LINE_FIELD_EXAMPLES,
    MAX_PLAN_DEPTH, MAX_RECORD_MESSAGES, MAX_REFERENCE_TERMS, MAX_SUMMARY_NAMES, MAX_REQUEST_DETAIL_CHARS, MAX_SCHEMA_ERROR_CHARS, MAX_SHARED_EVIDENCE_CHARS,
    MAX_SHARED_IMAGES, MAX_TOOL_ARGUMENT_CHARS, NATIVE_RATE_LOCK_NAMESPACE, STAGED_FILE_LIFETIME,
    TOOL_REQUEST_LIFETIME, is_restricted_model,
)


def _decoded_marker(value):
    """Some providers send a local reference or label as JSON text; decode only those markers."""
    if (isinstance(value, str) and value.lstrip().startswith("{")
            and ("$odoo_ref" in value or "$odoo_selection" in value)):
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        if isinstance(decoded, dict) and {"$odoo_ref", "$odoo_selection"} & set(decoded):
            return decoded
    return value


_NATIVE = object()
_CONFIRMED = object()
# Technical models the native attachment and activity tools need.
_NATIVE_ALLOWED_MODELS = frozenset({"ir.attachment", "mail.activity", "mail.activity.type"})
_BUILTINS = {
    "list_models", "get_record", "get_fields", "search_records", "aggregate_records",
    "read_attachment", "list_record_attachments", "list_resource_templates", "get_current_context",
    "create_record", "create_records", "update_record", "update_records", "delete_record",
    "call_model_method", "upload_attachment", "post_message", "list_record_messages", "rate_sales_executives",
    "press_button",
}
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
# Native MCP tools are offered to the AI provider as mcp_<tool name>.
NATIVE_TOOL_PREFIX = "mcp_"
# Pressing a screen button may create or change anything, so it is offered with every kind of change request.
PRESS_TOOL = NATIVE_TOOL_PREFIX + "press_button"
_lt = LazyTranslate(__name__)
# What a confirmed change shows, by its tool's MCP operation tag (other actions: "Successfully completed").
_CHANGE_DONE = {"create": _lt("Successfully created"), "write": _lt("Successfully updated"),
                "unlink": _lt("Successfully deleted")}
# Tool arguments that name target records; _resolve_plan accepts a record's exact name in them.
RECORD_KEYS = ("record_id", "record_ids")


def _accepting_record_names(schema):
    """A data-changing tool's schema where record IDs may also be given as the record's exact name.
    _resolve_plan turns names into IDs first, so the arguments validated against it are IDs."""
    schema = copy.deepcopy(schema)
    properties = schema.get("properties", {})
    update_id = properties.get("updates", {}).get("items", {}).get("properties", {}).get("id")
    targets = [properties[key] for key in RECORD_KEYS if key in properties] + ([update_id] if update_id else [])
    for node in targets:
        node = node.get("items", node) if node.get("type") == "array" else node
        kinds = node.get("type") if isinstance(node.get("type"), list) else [node.get("type")]
        if "integer" in kinds and "string" not in kinds:
            node["type"] = [kind for kind in kinds if kind] + ["string"]
            node["description"] = (node.get("description", "") + " Or the record's exact name.").strip()
    return schema


def _base_mimetype(mimetype):
    """``application/pdf; charset=...`` -> ``application/pdf``."""
    return (mimetype or "").split(";", 1)[0].strip().lower()
# Reference to a file staged locally in the conversation (see stage_upload).
_UPLOAD_REFERENCE = re.compile(r"upload:([1-9]\d*)")


_logger = logging.getLogger(__name__)


def _pdf_text_rows(data):
    """PDF text with each visual row on one line, so table cells stay together.

    pdfminer.six (the library Odoo's document indexing uses) returns each table cell as its own
    text block; lines whose vertical extents overlap are joined left to right instead.
    Returns None when pdfminer.six is unavailable."""
    try:
        from pdfminer.high_level import extract_pages  # noqa: PLC0415 - optional, as in attachment_indexation
        from pdfminer.layout import LTTextContainer, LTTextLine  # noqa: PLC0415
    except ImportError:
        return None

    def text_lines(element):
        for child in element:
            if isinstance(child, LTTextLine):
                yield child
            elif isinstance(child, LTTextContainer):
                yield from text_lines(child)

    pages = []
    for page in extract_pages(io.BytesIO(data)):
        rows = []  # [top, bottom, [(x, text), ...]]
        for line in sorted(text_lines(page), key=lambda line: -line.y1):
            content = line.get_text().strip()
            if not content:
                continue
            row = next((row for row in rows if line.y1 > row[1] and line.y0 < row[0]), None)
            if row:
                row[2].append((line.x0, content))
            else:
                rows.append([line.y1, line.y0, [(line.x0, content)]])
        pages.append("\n".join(" ".join(content for _x, content in sorted(cells)) for _top, _bottom, cells in rows))
    return "\n\n".join(page for page in pages if page)


def _option_key(field, text):
    """Selection key for ``text``: the key itself, or the one option whose label matches (any case)."""
    text = text.strip().strip(",.;:").strip()
    options = field.get("selection") or []
    if text in [key for key, _label in options]:
        return text
    keys = [key for key, label in options if str(label).strip().casefold() == text.casefold()]
    return keys[0] if len(keys) == 1 else None


def _check_settable(name, field):
    """Refuse a value Odoo derives and users cannot edit (an Amount, an Invoiced quantity): it is never set directly.
    ``field`` is the fields_get description, with its string, readonly, related and depends."""
    if field.get("readonly") and field.get("related"):
        source = field["related"].split(".")[0] if isinstance(field["related"], str) else field["related"][0]
        raise ValidationError(_("'%(field)s' is derived from '%(source)s' and cannot be set directly. "
                                "Set '%(source)s' instead.") % {"field": name, "source": source})
    if field.get("readonly") and field.get("depends"):
        raise ValidationError(_("'%(label)s' is calculated by Odoo, so it cannot be set directly. Change the "
                                "values it is calculated from instead.") % {"label": field["string"]})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, default=str).encode()).hexdigest()


def _history_summary(entry):
    """What happened in one list_record_messages entry: its subject, full text and tracked changes."""
    return " · ".join(filter(None, [entry["subject"], entry["text"], "; ".join(entry["changes"])]))


def _check_schema(schema, arguments):
    def check_references(node):
        if isinstance(node, dict):
            if "$ref" in node and not str(node["$ref"]).startswith("#"):
                raise ValidationError(_("Remote references are not allowed in native tool schemas."))
            for value in node.values():
                check_references(value)
        elif isinstance(node, list):
            for value in node:
                check_references(value)
    check_references(schema)
    try:
        validator = validator_for(schema)
        validator.check_schema(schema)
        validator(schema).validate(arguments)
    except SchemaError as error:
        raise ValidationError(_("The MCP tool has an invalid input schema. Contact your administrator.")) from error
    except SchemaValidationError as error:
        path = ".".join(str(item) for item in error.absolute_path) or _("arguments")
        detail = (error.message if len(error.message) <= MAX_SCHEMA_ERROR_CHARS
                  else error.message[:MAX_SCHEMA_ERROR_CHARS - 3] + "...")
        if error.validator == "additionalProperties" and isinstance(error.schema, dict):
            # Name the accepted arguments, so the AI can correct a misspelt one when it retries.
            detail += " " + _("Allowed: %s.") % ", ".join(sorted(error.schema.get("properties", {})))
        raise ValidationError(_("Invalid MCP arguments at %(path)s: %(detail)s") % {
            "path": path, "detail": detail,
        }) from error


class DirectReply(UserError):
    """An outcome the chat states as its answer instead of asking the AI to try another tool: a permission only an
    administrator can grant (or never can), or a value only the user can give."""


class NativeMCP(models.AbstractModel):
    _name = "odoo.ai.assistant.native.mcp"
    _inherit = "mcp.mixin"
    _description = "Confirmed native MCP execution"

    def _native(self, confirmed=False):
        """This bridge, allowed to run native tools; data-changing ones only when ``confirmed``."""
        return self.with_context(odoo_ai_native=_NATIVE, odoo_ai_confirmed=_CONFIRMED if confirmed else None)

    def _access(self, mutation=False):
        if self.env.context.get("odoo_ai_native") is not _NATIVE:
            raise AccessError(_("Use the native AI conversation tools."))
        config = self.env["odoo.ai.assistant.conversation"]._check_enabled()
        if not config.local_tools:
            raise AccessError(_("Odoo data access for the AI is disabled in AI Settings."))
        if mutation:
            if not config.native_actions:
                raise AccessError(_("AI changes are disabled in AI Settings."))
            if self.env.context.get("odoo_ai_confirmed") is not _CONFIRMED:
                raise AccessError(_("This action requires explicit confirmation in the AI conversation."))
        return config

    def _allowed_model(self, model):
        return not is_restricted_model(self.env, model, allowed=_NATIVE_ALLOWED_MODELS)

    def _refusal(self, model, operation="read"):
        """Why the AI may not use ``model`` for ``operation`` (read, create, write, unlink or method_calls), in plain
        words: the model with its technical name and the MCP Server checkbox an administrator ticks, or why the
        model is never available."""
        if not isinstance(model, str) or model not in self.env:
            return DirectReply(_("This Odoo has no %s records: the app they belong to is not installed.") % model)
        label = "%s (%s)" % (self.env["ir.model"].sudo()._get(model).name, model)
        if not self._allowed_model(model):
            return DirectReply(_("The AI cannot use %s records: Odoo keeps technical, security and settings "
                                       "data away from the AI.") % label)
        gates = self.env["mcp.enabled.model"]
        values = {"label": label, "menu": data_access_menus(self.env, settings=False),
                  "checkbox": gates.fields_get(["allow_" + operation], ["string"])["allow_" + operation]["string"]}
        if not gates.sudo().is_model_enabled(model):
            return DirectReply(_("The AI cannot do this yet: %(label)s is not in %(menu)s. An administrator "
                                       "can add it there and tick %(checkbox)s.") % values)
        return DirectReply(_("The AI cannot do this yet: in %(menu)s, %(label)s does not have %(checkbox)s "
                                   "ticked. An administrator can tick it there.") % values)

    def _resolve_model(self, model, operation="read"):
        self._access()
        if not self._allowed_model(model) or not self.env["mcp.enabled.model"].sudo().is_model_enabled(model):
            raise self._refusal(model, operation)
        return self.env[model]

    def _check_op(self, model, operation):
        recordset = self._resolve_model(model, operation)
        self._access(mutation=operation != "read")
        if not self.env["mcp.enabled.model"].sudo().check_model_operation_enabled(model, operation):
            raise self._refusal(model, operation)
        recordset.check_access(operation)
        return recordset

    def _attachment_read_allowed(self, res_model):
        self._access()
        gates = self.env["mcp.enabled.model"].sudo()
        return any(self._allowed_model(model) and gates.check_model_operation_enabled(model, "read")
                   for model in ("ir.attachment", res_model) if model)

    def _resolve_fields(self, requested, metadata):
        safe = {name: value for name, value in metadata.items() if not CREDENTIAL_FIELD.search(name)}
        if requested and any(CREDENTIAL_FIELD.search(name) for name in requested):
            raise AccessError(_("Credential fields cannot be used by AI tools."))
        return super()._resolve_fields(requested, safe)

    def _resolve_binary_field(self, ref):
        if CREDENTIAL_FIELD.search(ref.field):
            raise AccessError(_("Credential fields cannot be used by AI tools."))
        return super()._resolve_binary_field(ref)

    def list_models(self) -> Mapping[str, object]:
        self._access()
        entries = []
        for gate in self.env["mcp.enabled.model"].sudo().search([("active", "=", True)]):
            model = gate.model_name
            if not self._allowed_model(model):
                continue
            operations = {op: bool(getattr(gate, "allow_" + op) and self.env[model].has_access(op))
                          for op in ("read", "create", "write", "unlink")}
            if any(operations.values()):
                entries.append({"model": model, "name": gate.model_id.name, "operations": operations,
                                "allow_method_calls": gate.allow_method_calls})
        return _tool_result(json.dumps(entries, ensure_ascii=False), {"models": entries})

    def get_current_context(self):
        self._access()
        return super().get_current_context()

    def list_resource_templates(self):
        self._access()
        entries = self._model_entries()
        data = {
            "templates": [{"uri_template": "odoo://record/{model}/{id}/{field}"},
                          {"uri_template": "odoo://attachment/{id}"}],
            "enabled_models": [item["model"] for item in entries], "total_models": len(entries),
            "note": _("Use read_attachment to read these resources under your Odoo permissions."),
        }
        return _tool_result(json.dumps(data), data)

    def _model_entries(self):
        structured = self.list_models().get("structuredContent")
        if not isinstance(structured, dict) or not isinstance(structured.get("models"), list):
            raise UserError(_("MCP Server returned invalid model metadata."))
        return structured["models"]

    def _staged_upload(self, reference, conversation=None):
        """The current user's staged file for an ``upload:<id>`` reference."""
        match = _UPLOAD_REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
        upload = self.env["odoo.ai.assistant.upload"].sudo().browse(int(match[1]) if match else 0).exists()
        if (not upload or upload.user_id.id != self.env.uid
                or (conversation is not None and upload.conversation_id.id != conversation.id)):
            raise AccessError(_("This upload does not belong to your conversation."))
        return upload

    def _resolve_read_attachment_target(self, uri, attachment_id):
        if attachment_id is not None or not isinstance(uri, str) or not _UPLOAD_REFERENCE.fullmatch(uri):
            return super()._resolve_read_attachment_target(uri, attachment_id)
        upload = self._staged_upload(uri)
        # The bytes are pre-loaded, so the shared text/image/blob builders never read the record field.
        return {"uri": uri, "attachment": None, "record": upload, "field": "data", "name": upload.name,
                "mimetype": upload.mimetype or None, "file_size": upload.size, "type": "binary", "url": None,
                "raw": base64.b64decode(upload.data), "staged": True}

    def _attachment_download_link(self, target):
        self._access()
        if target.get("staged"):
            return {}
        if target["attachment"] is not None:
            path = "/web/content/%s?download=true" % target["attachment"].id
        else:
            path = "/web/content/%s/%s/%s?download=true" % (
                target["record"]._name, target["record"].id, target["field"],
            )
        return {"download_path": path, "download_url": None, "expires_at": None, "requires_login": True}

    def _attachment_link_result(self, target, notice=None):
        if target.get("staged"):
            return _tool_result(
                (notice + "\n" if notice else "") + _("A staged file has no download link; upload it to keep it."),
                self._attachment_structured(target, "staged"),
            )
        link = self._attachment_download_link(target)
        return _tool_result(
            (notice + "\n" if notice else "") + _("Download requires your Odoo login: %s") % link["download_path"],
            self._attachment_structured(target, "link", **link),
        )

    def _attachment_inline_text(self, target, text, format_used):
        if len(text) <= MAX_ATTACHMENT_TEXT_CHARS:
            return super()._attachment_inline_text(target, text, format_used)
        link = self._attachment_download_link(target)
        text = text[:MAX_ATTACHMENT_TEXT_CHARS]
        return _tool_result(
            text + "\n" + (_("Text truncated.") if target.get("staged")
                            else _("Text truncated. Download the full file using your Odoo login.")),
            self._attachment_structured(target, format_used, text=text, truncated=True, **link),
        )

    def _extract_attachment_text(self, target):
        """For PDFs, keep each table row on one line; any other file, or a PDF this cannot read
        (pdfminer.six missing, encrypted or scanned), uses MCP Server's standard extraction."""
        mimetype = _base_mimetype(target["mimetype"])
        if mimetype == "application/pdf":
            try:
                text = _pdf_text_rows(self._load_target_bytes(target))
            except Exception:  # noqa: BLE001 - parser failure falls back to the standard extraction
                _logger.warning("AI read_attachment: row-preserving PDF extraction failed for %s", target["uri"],
                                exc_info=True)
            else:
                if text:
                    return text
        return super()._extract_attachment_text(target)

    def _attachment_auto_result(self, target):
        """Show an image above MCP Server's inline limit as the largest Odoo preview size that fits,
        instead of only a download link. Other files, and images that cannot be reduced, keep the
        standard result."""
        if not target["mimetype"] or target["file_size"] is None:
            self._load_target_bytes(target)
        mimetype = _base_mimetype(target["mimetype"])
        if (mimetype not in _IMAGE_TYPES or target["file_size"] <= MAX_INLINE_BLOB_BYTES
                or target["file_size"] > MAX_EXTRACT_BYTES):
            return super()._attachment_auto_result(target)
        raw = self._load_target_bytes(target)
        # Odoo's own image sizes (image_1920, image_1024, ...), largest first.
        sides = sorted({field.max_width for field in self.env["image.mixin"]._fields.values()
                        if isinstance(field, fields.Image) and field.max_width}, reverse=True)
        for side in sides:
            try:
                preview = image_process(raw, size=(side, side), verify_resolution=True)
            except (UserError, ValueError, OSError):
                break  # unreadable, or more pixels than Odoo processes: keep the standard result
            preview_type = guess_mimetype(preview)
            if len(preview) <= MAX_INLINE_BLOB_BYTES and preview_type in _IMAGE_TYPES:
                result = _tool_result(
                    _("Attachment: %s, shown as a smaller preview.") % self._attachment_headline(target),
                    self._attachment_structured(target, "image", preview_max_side=side),
                )
                result["content"].append({"type": "image", "data": base64.b64encode(preview).decode("ascii"),
                                          "mimeType": preview_type})
                return result
        return super()._attachment_auto_result(target)

    @mcp_tool(
        name="list_record_messages",
        title="List Record Messages",
        description=(
            "List the history logged on one record, such as an opportunity: notes, messages and emails with "
            "their author and date, and tracked changes such as stage or salesperson, oldest first. Use it to "
            "review or analyse what happened on that record and who did what. Whenever the user asks for the "
            "history, timeline, log, notes, messages or chatter of a record, call this tool directly with the "
            "record's exact name as record_id; never search for the record first, as only one tool runs per request."
        ),
        input_schema={"type": "object", "properties": {
            "model": {"type": "string", "description": "Technical model of the record, for example crm.lead."},
            "record_id": {"type": ["integer", "string"],
                          "description": "The record's ID, or its exact name as the user wrote it (no lookup needed)."},
        }, "required": ["model", "record_id"], "additionalProperties": False},
        operation="read",
        readOnlyHint=True,
    )
    def _list_record_messages(self, model, record_id):
        recordset = self._check_op(model, "read")
        record = self._resolve_record_ids_or_raise(model, recordset, [record_id], 1)
        record.check_access("read")  # the resolver only checks existence, not record rules
        if "message_ids" not in record._fields:
            raise UserError(_("%s records have no history.") % recordset._description)
        # Searched as the user: Odoo only returns messages they may read on a record they may read.
        messages = self.env["mail.message"].search([("model", "=", model), ("res_id", "=", record.id)],
                                                   order="id desc", limit=MAX_RECORD_MESSAGES + 1)
        entries = []
        for message in reversed(messages[:MAX_RECORD_MESSAGES]):
            # sudo: mail.message - tracking values are admin-only; keep those the user may read, as the chatter does.
            changes = message.sudo().tracking_value_ids._filter_has_field_access(self.env)._tracking_value_format()
            entries.append({
                "date": fields.Datetime.context_timestamp(self, message.date).strftime("%Y-%m-%d %H:%M"),
                "author": message.author_id.display_name or message.email_from or "",
                "kind": message.subtype_id.name or message.message_type,
                "subject": message.subject or "",
                "text": html2plaintext(message.body or "").strip(),
                "changes": ["%s: %s → %s" % (change["fieldInfo"]["changedField"], change["oldValue"], change["newValue"])
                            for change in changes],
            })
        lines = [_("History of %(record)s, oldest first:", record=record.display_name)] + [
            " · ".join(filter(None, [entry["date"], entry["author"], entry["kind"], _history_summary(entry)]))
            for entry in entries
        ]
        truncated = len(messages) > MAX_RECORD_MESSAGES
        if truncated:
            lines.append(_("Only the latest %s entries are listed.") % MAX_RECORD_MESSAGES)
        return _tool_result("\n".join(lines), {"model": model, "record_id": record.id, "record": record.display_name,
                                               "messages": entries, "truncated": truncated})

    @mcp_tool(
        name="rate_sales_executives",
        title="Rate Sales Executives",
        description=(
            "Rate salespeople for a period against the user's targets: weighted score, misses and who needs "
            "coaching. Use it for any sales performance or coaching question instead of computing ratings yourself. "
            "Targets come only from the user; if they gave none, ask which measures and targets to use."
        ),
        input_schema={"type": "object", "properties": {
            "date_from": {"type": "string", "description": "YYYY-MM-DD; default first day of this month."},
            "date_to": {"type": "string", "description": "YYYY-MM-DD; default today."},
            "salesperson": {"type": ["string", "array"], "items": {"type": "string"},
                            "description": "Exact name(s); omit for everyone."},
            "team": {"type": "string", "description": "Exact sales team name; omit for all."},
            "targets": {"type": "object", "description": "Only the measures the user named.", "properties": {
                "win_rate": {"type": "number", "minimum": 0, "maximum": 100, "description": "% of closed deals won"},
                "revenue": {"type": "number", "minimum": 0, "description": "won per salesperson per month"},
                "close_days": {"type": "number", "minimum": 0, "description": "average days to win; fewer is better"},
                "activities": {"type": "number", "minimum": 0, "maximum": 100, "description": "% done by deadline"},
                "contact": {"type": "number", "exclusiveMinimum": 0,
                            "description": "days within which open deals had an email, note or done activity"},
            }, "additionalProperties": False},
            "weights": {"type": "object", "additionalProperties": {"type": "number", "minimum": 0},
                        "description": "By target key; default 1."},
            "min_deals": {"type": "integer", "minimum": 0,
                          "description": "Closed deals needed to score win rate, revenue and close days."},
            "revenue_from_teams": {"type": "boolean",
                                   "description": "Revenue target from each team's invoicing target."},
            "owner_at_close": {"type": "boolean", "description": "Credit deals to their owner at closing."},
        }, "additionalProperties": False},
        operation="read",
        readOnlyHint=True,
    )
    def _rate_sales_executives(self, date_from=None, date_to=None, salesperson=None, team=None, targets=None,
                               weights=None, min_deals=0, revenue_from_teams=False, owner_at_close=False):
        if "crm.lead" not in self.env:
            raise UserError(_("Sales ratings need the CRM app."))
        today = fields.Date.context_today(self)
        start, end = fields.Date.to_date(date_from) or today.replace(day=1), fields.Date.to_date(date_to) or today
        if start > end:
            raise UserError(_("The period must start before it ends."))
        # The rules come from the user's request: the AI asks for them rather than inventing targets.
        targets, team_targets = targets or {}, bool(revenue_from_teams)
        if not (targets or team_targets):
            raise UserError(_("Ask which measures and targets to rate on: win rate (%), monthly won revenue, days to "
                              "close, activities on time (%) or a customer contact window (days)."))
        requested = [name.strip() for name in ([salesperson] if isinstance(salesperson, str) else salesperson or [])
                     if name.strip()]
        names = {name.casefold() for name in requested}
        domain = [("type", "=", "opportunity"), ("user_id", "!=", False)]
        if team:
            domain.append(("team_id.name", "=ilike", team.strip()))
        # Every model read here must be allowed in MCP Server; sales teams only when a team filter or target uses them.
        # The opportunities' own chatter (messages, tracked changes) is covered by the opportunity permission.
        if team or team_targets:
            self._check_op("crm.team", "read")
        # Searched as the user: CRM's own rules decide whose deals each user may see.
        opportunities = self._check_op("crm.lead", "read").with_context(active_test=False).search(domain)
        # Measured within the period: up to its end, or up to today for a period still running.
        period_end = min(end, today)
        midnight = lambda day: datetime.combine(day, datetime.min.time())
        end_moment = midnight(period_end) + timedelta(days=1)
        closed = opportunities.filtered(lambda deal: deal.won_status in ("won", "lost") and deal.date_closed and (
            midnight(start) <= deal.date_closed < midnight(end) + timedelta(days=1)))
        owners = {deal: deal.user_id for deal in closed}
        if owner_at_close and closed:
            # Odoo tracks salesperson changes: the first change after closing still holds the owner at closing.
            # sudo: tracking values are readable by administrators only; these are the salesperson changes of
            # opportunities this user may read.
            settled = set()
            for change in self.env["mail.tracking.value"].sudo().search([
                ("field_id", "=", self.env["ir.model.fields"]._get("crm.lead", "user_id").id),
                ("mail_message_id.model", "=", "crm.lead"), ("mail_message_id.res_id", "in", closed.ids),
            ], order="id"):
                deal = closed.browse(change.mail_message_id.res_id)
                if deal not in settled and change.mail_message_id.date > deal.date_closed:
                    owners[deal] = self.env["res.users"].browse(change.old_value_integer).exists() or deal.user_id
                    settled.add(deal)
        open_at_end = opportunities.filtered(lambda deal: deal.create_date < end_moment and (
            (deal.won_status == "pending" and deal.active) or (deal.date_closed and deal.date_closed >= end_moment)))
        contacted = set(self.env["mail.message"].search([
            ("model", "=", "crm.lead"), ("res_id", "in", open_at_end.ids), ("date", "<", end_moment),
            ("date", ">=", end_moment - timedelta(days=targets["contact"])),
            # Emails, logged notes and completed activities such as calls and meetings.
            "|", ("message_type", "in", ("email", "comment")), ("mail_activity_type_id", "!=", False),
        ]).mapped("res_id")) if "contact" in targets else set()
        due = self.env["mail.activity"]
        if "activities" in targets:
            # Activities due in the period, done or not (Odoo keeps done ones archived);
            # one due today may still be done.
            due = self._check_op("mail.activity", "read").with_context(active_test=False).search([
                ("res_model", "=", "crm.lead"), ("res_id", "in", opportunities.ids),
                ("date_deadline", ">=", start), ("date_deadline", "<=", period_end),
            ]).filtered(lambda activity: activity.date_done or activity.date_deadline < today)
        # Revenue targets cover only the part of the period that has happened: future months cannot be earned yet.
        months = ((period_end - start).days + 1) / (365.25 / 12)

        def revenue_target(user):
            sales_team = user.sale_team_id
            if team_targets and getattr(sales_team, "invoiced_target", 0):
                return sales_team.invoiced_target / len(sales_team.member_ids or user) * months
            return targets.get("revenue", 0) * months

        percent = lambda value: "%d%%" % round(value * 100)
        money = lambda value: formatLang(self.env, value, currency_obj=self.env.company.currency_id, rounding_unit="units")
        # (key, label, target or 0 when unscored, lower is better, format); a contact window is met when every open
        # opportunity was contacted within it.
        available = (
            ("win_rate", _("Win rate"), targets.get("win_rate", 0) / 100, False, percent),
            ("revenue", _("Won revenue"), targets.get("revenue", 0) * months, False, money),
            ("close_days", _("Days to close"), targets.get("close_days", 0), True, round),
            ("activities", _("Activities on time"), targets.get("activities", 0) / 100, False, percent),
            ("contact", _("Recent contact"), 1.0, False, percent),
        )
        # Only the measures the user asked for are shown and scored, each weighted as they said (default 1).
        measures = [(key, label, target, (weights or {}).get(key, 1), lower, show)
                    for key, label, target, lower, show in available
                    if key in targets or (key == "revenue" and team_targets)]
        # Results of closed deals: unfair below the minimum, so shown but not scored.
        needs_deals = {"win_rate", "revenue", "close_days"}
        # Each target is shown once, in its column header; team revenue targets differ per row, so they go there.
        columns = {"salesperson": _("Salesperson"), "closed": _("Closed deals"), **{
            key: _("%(measure)s (target %(target)s)", measure=label, target=show(target))
            if target and not (key == "revenue" and team_targets) else label
            for key, label, target, _weight, _lower, show in measures
        }, "score": _("Score (0–100)"), "below": _("Needs attention")}
        rows, attention = [], {}
        users = (self.env["res.users"].union(*owners.values()) | open_at_end.user_id).sorted("name")
        for user in users.filtered(lambda user: not names or user.name.casefold() in names):
            mine = [deal for deal, owner in owners.items() if owner == user]
            won = [deal for deal in mine if deal.won_status == "won"]
            deals = open_at_end.filtered(lambda deal: deal.user_id == user)
            assigned = due.filtered(lambda activity: activity.user_id == user)
            # Measured values; None without data.
            values = {
                "win_rate": len(won) / len(mine) if mine else None,
                "revenue": sum(deal.expected_revenue for deal in won),
                "close_days": sum(deal.day_close for deal in won) / len(won) if won else None,
                "activities": len(assigned.filtered(lambda activity: activity.date_done
                                                   and activity.date_done <= activity.date_deadline)) / len(assigned)
                if assigned else None,
                "contact": len(set(deals.ids) & contacted) / len(deals) if deals else None,
            }
            targets = {"revenue": revenue_target(user)} if team_targets else {}
            cells, scored, below = {}, [], []
            for key, label, target, weight, lower, show in measures:
                target = targets.get(key, target)
                value = values[key]
                if value is None:
                    cells[key] = "–"
                elif key in targets:
                    cells[key] = _("%(value)s of %(target)s", value=show(value), target=show(target))
                else:
                    cells[key] = show(value)
                if value is None or not target or not weight or (key in needs_deals and len(mine) < min_deals):
                    continue
                ratio = min((target / value if value else 1) if lower else value / target, 1)
                scored.append((weight, ratio))
                if ratio < 1:
                    below.append(label)
            rows.append({
                "salesperson": user.name, "closed": len(mine), **cells,
                "score": round(100 * sum(weight * ratio for weight, ratio in scored) / sum(weight for weight, _r in scored))
                if scored else _("Not enough data"),
                "below": ", ".join(below) or (_("On target") if scored else "–"),
            })
            attention[user.name] = below
        # A plain-language verdict written by Odoo from the scores above, so nothing is sent to the AI provider.
        ranked = [row for row in rows if isinstance(row["score"], int)]
        lowest = [row for row in ranked if row["score"] == min((row["score"] for row in ranked), default=None)]
        highest = [row for row in ranked if row["score"] == max((row["score"] for row in ranked), default=None)]
        listed = lambda group: ", ".join(row["salesperson"] for row in group[:MAX_SUMMARY_NAMES]) + (
            _(" and %s more", len(group) - MAX_SUMMARY_NAMES) if len(group) > MAX_SUMMARY_NAMES else "")
        notes = []
        if lowest:
            notes.append(_("Needs coaching most: %(names)s (score %(score)s).", names=listed(lowest), score=lowest[0]["score"])
                         if len(ranked) > 1 else
                         _("%(name)s scores %(score)s.", name=lowest[0]["salesperson"], score=lowest[0]["score"]))
            if len(lowest) == 1 and attention[lowest[0]["salesperson"]]:
                notes.append(_("Below target: %s.") % ", ".join(attention[lowest[0]["salesperson"]]))
        if highest and highest[0]["score"] > lowest[0]["score"]:
            notes.append(_("Strongest: %(names)s (score %(score)s).", names=listed(highest), score=highest[0]["score"]))
        if len(rows) > len(ranked):
            notes.append(_("Not enough data to score: %s.") % listed([row for row in rows if row not in ranked]))
        found = {row["salesperson"].casefold() for row in rows}
        if missing := [name for name in requested if name.casefold() not in found]:
            notes.append(_("No opportunities found for: %s.") % ", ".join(missing))
        note = " ".join(notes)
        title = _("Sales ratings %(start)s to %(end)s", start=start, end=end)
        if min_deals:
            title += _(" · win rate, revenue and days to close count from %s closed deals", min_deals)
        lines = [title, note] + ["%s: %s. %s" % (row["salesperson"], row["score"], row["below"]) for row in rows]
        return _tool_result("\n".join(lines) if rows else _("No salesperson has opportunities in this period."), {
            "title": title, "note": note,
            "columns": [{"key": key, "label": label} for key, label in columns.items()], "rows": rows,
        })

    def _validate_native_method(self, model, recordset, method):
        if not isinstance(method, str) or not method.strip():
            raise ValidationError(_("Specify a public business method."))
        method = method.strip()
        mapped = utils.map_method_to_operation(method)
        if mapped:
            if (method.startswith(("_", "web_")) or method in _BLOCKED_METHOD_CALLS
                    or any(model == prefix or model.startswith(prefix + ".") for prefix in _METHOD_CALL_BLOCKED_MODELS)
                    or not callable(getattr(recordset, method, None))):
                raise AccessError(_("This is not an allowed business method."))
            self._check_op(model, mapped)
        else:
            self._validate_method_call(model, recordset, method)
        return method

    def call_model_method(self, model, method, record_ids=None, args=None, kwargs=None):
        self._access(mutation=True)
        recordset = self._resolve_model(model)
        gates = self.env["mcp.enabled.model"].sudo()
        if not gates.is_method_call_enabled(model):
            raise self._refusal(model, "method_calls")
        method = self._validate_native_method(model, recordset, method)
        target = (self._resolve_record_ids_or_raise(model, recordset, record_ids, self._limit_bounds()[1])
                  if record_ids else recordset)
        if record_ids:
            target.check_access("write")
        raw = getattr(target, method)(*(args or []), **(kwargs or {}))
        return _tool_result(_("Business method completed."), {"success": True, "result": _json_safe(raw)})

    def upload_attachment(self, name, data, model=None, record_id=None, mimetype=None):
        if model is not None or record_id is not None:
            return super().upload_attachment(name, data, model, record_id, mimetype)
        self._check_op("ir.attachment", "create")
        raw = base64.b64decode(data, validate=True)
        if not name.strip() or len(raw) > MAX_UPLOAD_BYTES:
            raise ValidationError(_("Choose a named file within the MCP upload limit."))
        attachment = self.env["ir.attachment"].create({
            "name": name.strip(), "raw": raw, "type": "binary", **({"mimetype": mimetype} if mimetype else {}),
        })
        return _tool_result(_("Attachment uploaded."), {
            "attachment_id": attachment.id, "uri": "odoo://attachment/%s" % attachment.id,
            "name": attachment.name, "mimetype": attachment.mimetype, "file_size": attachment.file_size,
        })

    @mcp_tool(
        name="press_button",
        title="Press Button",
        description=(
            "Press a button on a record's screen as the user would in Odoo, for a workflow step that no record tool "
            "does: Create Invoice, Send PRO-FORMA Invoice, Register Payment, Mark as Lost, Validate. Give the button's "
            "label as Odoo shows it. When it opens a form, Odoo fills it with its own values and presses that form's "
            "main button; give wizard_values only for choices the user asked for."
        ),
        input_schema={"type": "object", "properties": {
            "model": {"type": "string", "description": "Technical model of the record, for example sale.order."},
            "record_id": {"type": "integer", "description": "The record's ID."},
            "button": {"type": "string", "description": "The button's label, for example Create Invoice."},
            "wizard_values": {"type": "object", "description": "Values for the form the button opens, by field name."},
            "wizard_button": {"type": "string", "description": "Label of that form's button to press; default its main one."},
        }, "required": ["model", "record_id", "button"], "additionalProperties": False},
    )
    def _press_button(self, model, record_id, button, wizard_values=None, wizard_button=None):
        self._access(mutation=True)
        recordset = self._resolve_model(model, "method_calls")
        if not self.env["mcp.enabled.model"].sudo().is_method_call_enabled(model):
            raise self._refusal(model, "method_calls")
        record = self._resolve_record_ids_or_raise(model, recordset, [record_id], 1)
        record.check_access("write")
        action = self._run_button(record, self._find_button(record, button), wizard_values, wizard_button)
        # Link to what the button opened when that is one record (the new invoice), else to the pressed record.
        target = record
        if (isinstance(action, dict) and action.get("res_model") in self.env
                and isinstance(action.get("res_id"), int) and action["res_id"]):
            target = self.env[action["res_model"]].browse(action["res_id"])
        return _tool_result(_("Pressed %(button)s on %(record)s.") % {"button": button, "record": record.display_name},
                            {"record": {"display_name": target.display_name},
                             "url": "/odoo/%s/%s" % (target._name, target.id)})

    def _find_button(self, record, label=None, view_id=None):
        """The button labelled ``label`` (any case) that ``record``'s form shows for it now, or without a label the
        form's main button. Odoo has already left out the buttons of groups the user is not in."""
        arch = etree.fromstring(record.get_view(view_id, "form")["arch"].encode())
        buttons = [node for node in arch.iter("button") if node.get("type") in {"object", "action"}
                   and not any(parent.tag == "field" for parent in node.iterancestors()) and visible_for(node, record)]
        found = ([node for node in buttons if button_label(node).casefold() == label.strip().casefold()] if label else
                 [node for node in buttons if {"btn-primary", "oe_highlight"} & set((node.get("class") or "").split())]
                 or buttons)
        if not found:
            raise UserError(_("%(record)s has no '%(button)s' button it can use now. Its buttons now: %(buttons)s.") % {
                "record": record.display_name or record._description, "button": label or _("main"),
                "buttons": ", ".join(dict.fromkeys(filter(None, map(button_label, buttons)))) or _("none")})
        return found[0]

    def _run_button(self, record, node, wizard_values=None, wizard_button=None, preview=False):
        """Press ``node`` on ``record`` as Odoo's web client does and return Odoo's resulting action. When it opens a
        form (a wizard), that form gets Odoo's defaults and ``wizard_values``, then its ``wizard_button`` (or main
        button) is pressed. With ``preview``, describe what pressing does instead, running no business method."""
        context = {"active_id": record.id, "active_ids": record.ids, "active_model": record._name}
        if node.get("context"):
            context.update(eval_view_expression(node.get("context"), record, context))
        description = {"button": button_label(node), "warning": node.get("confirm") or "", "form": "", "values": []}
        if node.get("type") == "action":
            action = self.env["ir.actions.actions"].browse(int(node.get("name")))
            action = self.env[action.type].browse(action.id).read()[0]
        elif preview:
            return description
        else:
            get_public_method(record, node.get("name"))  # Odoo's own rule for buttons: public methods only
            action = getattr(record.with_context(**context), node.get("name"))()
        if not (isinstance(action, dict) and action.get("target") == "new" and action.get("res_model") in self.env
                and self.env[action["res_model"]]._transient):
            return description if preview else action
        form_context = action.get("context") or {}
        if isinstance(form_context, str):
            form_context = eval_view_expression(form_context, record, context)
        form = self.env[action["res_model"]].with_context(**{**context, **form_context})
        values = self._wizard_values(form, wizard_values)
        view = action.get("view_id")
        view = view[0] if isinstance(view, (list, tuple)) else view or None
        if preview:
            wizard = form.new({**form.default_get(list(form._fields)), **values})
            return dict(description, form=action.get("name") or form._description,
                        values=self._form_values(wizard, view)
                        + [{"label": _("Then press"), "value": button_label(self._find_button(wizard, wizard_button, view))}])
        wizard = form.create(values)
        wizard_node = self._find_button(wizard, wizard_button, view)
        if wizard_node.get("type") != "object":
            raise UserError(_("'%s' opens another screen, which the AI cannot use; finish this step in Odoo.")
                            % button_label(wizard_node))
        get_public_method(wizard, wizard_node.get("name"))
        wizard_context = eval_view_expression(wizard_node.get("context"), wizard) if wizard_node.get("context") else {}
        result = getattr(wizard.with_context(**wizard_context), wizard_node.get("name"))()
        if isinstance(result, dict) and result.get("target") == "new":
            raise UserError(_("Odoo opened another form after '%s'; finish this step in Odoo.") % button_label(wizard_node))
        return result

    def _check_given(self, arguments, conversation):
        """A new record's name and required text come from the conversation, never from the AI alone: Odoo asks for
        a value nobody wrote (a placeholder such as "New Contact") instead of creating it, whatever the model.
        Words naming the kind of record ("a new contact") are not values; a direct answer to the question always is."""
        model = self.env[arguments["model"]]
        definitions = model.fields_get(attributes=["string", "type", "required"])
        names = [name for name, field in definitions.items()
                 if field["type"] in {"char", "text"} and (field["required"] or name == model._rec_name)]
        kind = _vocabulary_pattern([word for word in re.split(r"\W+", model._description) if word])
        # What was written, and the Odoo results the user shared with the AI (values may come from those).
        text = " ".join([entry["content"] for entry in conversation._provider_history()] + [
            json.dumps(result, ensure_ascii=False) for result in
            self.env["odoo.ai.assistant.tool.request"].sudo().search(
                [("conversation_id", "=", conversation.id), ("state", "=", "shared")]).mapped("result")])

        def squash(value):
            return re.sub(r"\W+", "", value).casefold()

        written, answer = squash(kind.sub(" ", text) if kind else text), squash(conversation._latest_question())
        missing = {definitions[name]["string"] for values in [arguments.get("values"), *(arguments.get("records") or [])]
                   if isinstance(values, dict) for name in names if isinstance(values.get(name), str)
                   and squash(values[name]) not in written and squash(values[name]) != answer}
        if missing:
            raise DirectReply(_("What %(fields)s should the new %(model)s have?") % {
                "fields": ", ".join(sorted(missing)), "model": model._description})

    def _wizard_values(self, form, values):
        """``values`` for the form a button opens, checked against its fields: never a credential or a value Odoo
        calculates; a selection may be given by its label."""
        definitions = form.fields_get(attributes=["string", "type", "selection", "readonly", "related", "depends"])
        checked = {}
        for name, value in (values or {}).items():
            field = definitions.get(name)
            if not field or CREDENTIAL_FIELD.search(name):
                raise ValidationError(_("The form %(form)s has no field '%(name)s'. Fields it can take: %(fields)s.") % {
                    "form": form._description, "name": name, "fields": ", ".join(sorted(
                        "%s (%s)" % (key, item["string"]) for key, item in definitions.items()
                        if not item.get("readonly") and not CREDENTIAL_FIELD.search(key))[:MAX_FIELD_SUGGESTIONS])})
            _check_settable(name, field)
            checked[name] = (_option_key(field, value) or value) if field["type"] == "selection" and isinstance(
                value, str) else value
        return checked

    def _form_values(self, record, view_id=None):
        """Label and value of the fields ``record``'s form shows, without long texts, files or credentials, for the
        confirmation card."""
        arch = etree.fromstring(record.get_view(view_id, "form")["arch"].encode())
        rows = {}
        for node in arch.iter("field"):
            name = node.get("name")
            field = record._fields.get(name)
            if (name in rows or not field or field.type in {"html", "binary", "one2many"} or CREDENTIAL_FIELD.search(name)
                    or any(parent.tag == "field" for parent in node.iterancestors()) or not visible_for(node, record)):
                continue
            value = record[name]
            if field.relational:
                value = ", ".join(value.mapped("display_name"))
            elif field.type == "selection":
                value = dict(field._description_selection(self.env)).get(value, value)
            elif field.type == "boolean":
                value = value and _("Yes")
            if value not in (False, None, ""):
                rows[name] = {"label": node.get("string") or field.string, "value": str(value)}
        return list(rows.values())

    def _catalogue(self):
        executor = self._native()
        config = executor._access()
        can_act = config.native_actions
        entries = {}
        for name, meta in executor._get_mcp_tools().items():
            if name not in _BUILTINS:
                continue
            mutation = not meta["annotations"].get("readOnlyHint", False)
            if mutation and not can_act:
                continue
            schema = executor._mcp_live_input_schema(meta["input_schema"])
            entries[NATIVE_TOOL_PREFIX + name] = {
                "name": name, "schema": _accepting_record_names(schema) if mutation else schema,
                "description": meta["description"], "mutation": mutation, "custom_id": None,
                "operation": meta.get("operation"),
            }
        if can_act:
            for custom in self.env["mcp.custom.tool"]._visible_tools():
                schema = custom.sudo()._parsed_input_schema()
                if schema is None:
                    raise ValidationError(_("An accessible custom MCP tool has an invalid schema."))
                entries[NATIVE_TOOL_PREFIX + "custom_" + str(custom.id)] = {
                    "name": custom.sudo().name, "schema": schema,
                    "description": custom.sudo().description, "mutation": True, "custom_id": custom.id,
                    "operation": None,
                }
        return entries

    def _provider_tools(self):
        return [{
            "name": name,
            # Sharing, confirmation, argument format, record names and upload references are explained once
            # in the system instructions, which are always sent with these tools.
            "description": entry["description"]
            + (" It describes the signed-in user, companies and timezone, not the app or screen the user has open. "
               "Its result is not shared with you; to filter by the signed-in user, use %s instead." % CURRENT_USER
               if entry["name"] == "get_current_context" else ""),
            "inputSchema": entry["schema"], "strict": False,
        } for name, entry in self._catalogue().items()]

    def _planning_context(self, request=None, all_fields=False):
        executor = self._native()
        # Model and field names only (no records): always available when Odoo data access is on,
        # so the AI plans with real field names instead of guessing them.
        executor._access()
        # One short line per model ("Contact (read, create, methods)"): the same facts as MCP's entries in a
        # fraction of the tokens, as this list goes with every request.
        metadata = {"models": {
            entry["model"]: "%s (%s)" % (entry["name"], ", ".join(
                [operation for operation, allowed in entry["operations"].items() if allowed]
                + (["methods"] if entry["allow_method_calls"] else [])))
            for entry in executor._model_entries()
        }}
        if request and request.tool == "mcp_get_fields":
            model = (request.arguments or {}).get("model")
            recordset = executor._check_op(model, "read")
            names = (request.arguments or {}).get("field_names")
            definitions = recordset.fields_get(
                None if all_fields or not names or "__all__" in names else names,
                attributes=["string", "type", "relation", "required", "readonly"],
            )
            metadata["schema"] = {"model": model, "fields": [
                {"name": name, **definition} for name, definition in sorted(definitions.items())
                if not CREDENTIAL_FIELD.search(name)
            ]}
            for definition in metadata["schema"]["fields"]:
                selection = (recordset._fields[definition["name"]].selection
                             if definition.get("type") == "selection" else None)
                if isinstance(selection, (list, tuple)):
                    definition["selection"] = [[key, str(label)] for key, label in selection]
        return metadata

    def _record_fingerprint(self, records):
        metadata = records.fields_get(attributes=["type", "store"])
        names = [name for name, definition in metadata.items()
                 if definition.get("store") and definition.get("type") not in {
                     "binary", "html", "one2many", "many2many",
                 } and not CREDENTIAL_FIELD.search(name)]
        return _digest(records.read(names))

    def _resolve_plan(self, tool, plan):
        entries = self._catalogue()
        if tool not in entries or not isinstance(plan, dict):
            raise AccessError(_("This native MCP tool is not available to your user."))
        entry = entries[tool]
        executor = self._native()
        if entry["mutation"]:
            self._native(confirmed=True)._access(mutation=True)
        dependencies = []

        def depend(model, record):
            """Record ``record`` as a plan dependency, so confirming fails if it changes meanwhile."""
            dependencies.append({"model": model, "id": record.id, "name": record.display_name,
                                 "version": self._record_fingerprint(record)})
            return record.id

        def record_id(relation, named):
            """ID of the one accessible ``relation`` record named ``named``, recorded as a plan dependency.
            Users are never read by the AI, yet a record may name one (a salesperson, an assignee): the exact name
            is matched under the user's own access, outside MCP Server's model list, and nothing else is read."""
            source = (executor.env[relation] if relation == self.env.user._name
                      else executor._check_op(relation, "read"))
            matches = source.name_search(named.strip(), operator="=ilike", limit=2)
            if len(matches) != 1:
                raise UserError((_("More than one %(model)s is named '%(name)s', so no action was prepared. "
                                   "Describe it more precisely, with another of its fields, so that only one "
                                   "matches.") if matches else
                                 _("No %(model)s named '%(name)s' was found, so no action was prepared.")) % {
                                     "name": named, "model": executor.env[relation]._description})
            return depend(relation, executor.env[relation].browse(matches[0][0]))

        def resolve(value, field=None, depth=0):
            if depth > MAX_PLAN_DEPTH:
                raise ValidationError(_("The action plan is nested too deeply."))
            value = _decoded_marker(value)
            if value == CURRENT_USER:
                return self.env.uid
            if isinstance(value, dict) and "$odoo_ref" in value:
                if not entry["mutation"] or entry["custom_id"] or set(value) != {"$odoo_ref"}:
                    raise ValidationError(_("Local record references are only supported in built-in action plans."))
                reference = value["$odoo_ref"]
                if (not isinstance(reference, dict) or set(reference) != {"model", "domain"}
                        or not isinstance(reference["model"], str)
                        or not isinstance(reference["domain"], list) or not reference["domain"]
                        or len(reference["domain"]) > MAX_REFERENCE_TERMS):
                    raise ValidationError(_("A local reference requires a model and a nonempty domain "
                                            "of at most %s terms.") % MAX_REFERENCE_TERMS)
                model = reference["model"]
                if model == "ir.model":
                    # The model registry is never searched; resolve only "model = <business model name>".
                    names = [term[2] for term in reference["domain"]
                             if isinstance(term, (list, tuple)) and len(term) == 3 and term[0] == "model"
                             and term[1] in {"=", "=ilike", "ilike"} and isinstance(term[2], str)]
                    if len(names) != 1 or names[0] not in self.env or is_restricted_model(self.env, names[0]):
                        raise ValidationError(_("For a link to a model, give the business model's technical name "
                                                "as text, for example \"crm.lead\"."))
                    return self.env["ir.model"]._get_id(names[0])
                # A many2one_reference (such as res_id) names its model in a sibling field.
                if field and field["type"] != "many2one_reference" and (
                        field["type"] != "many2one" or field.get("relation") != model):
                    if field.get("relation") == "ir.model":
                        raise ValidationError(_("This field links to a model, not a record. Give the model's "
                                                "technical name as text instead, for example \"%s\".") % model)
                    raise ValidationError(_("The lookup model '%(model)s' does not match this field, which links to "
                                            "'%(expected)s'. Repeat the lookup with model '%(expected)s'.") % {
                                                "model": model, "expected": field.get("relation")})
                if len(dependencies) >= executor._limit_bounds()[1]:
                    raise ValidationError(_("This plan exceeds the MCP record-reference limit. "
                                            "Prepare a smaller batch."))
                recordset = executor._check_op(model, "read")
                for term in reference["domain"]:
                    if isinstance(term, (list, tuple)) and term:
                        if not isinstance(term[0], str) or CREDENTIAL_FIELD.search(term[0]):
                            raise AccessError(_("Credential fields cannot be used in local lookups."))
                records = recordset.search(executor._coerce_domain(reference["domain"]), limit=2)
                if len(records) != 1:
                    raise UserError(_("%(count)s %(model)s records match this description, so no action was "
                                      "prepared. Describe it so that exactly one matches.") % {
                                          "count": _("Several") if records else _("No"),
                                          "model": recordset._description})
                records.check_access("read")
                return depend(model, records)
            # A linked record given by name, as plain text or {"$odoo_selection": name}.
            named = value if isinstance(value, str) else (
                value["$odoo_selection"] if isinstance(value, dict) and set(value) == {"$odoo_selection"} else None)
            if (isinstance(named, str) and field and field["type"] == "many2one" and field.get("relation")
                    and not entry["custom_id"]):
                return record_id(field["relation"], named)
            if isinstance(value, dict) and "$odoo_selection" in value:
                if (not entry["mutation"] or entry["custom_id"] or set(value) != {"$odoo_selection"}
                        or not field or field["type"] != "selection"
                        or not isinstance(value["$odoo_selection"], str)):
                    raise ValidationError(_("Selection labels can only be used for a selection field "
                                            "in an action's values."))
                key = _option_key(field, value["$odoo_selection"])
                if key is None:
                    raise ValidationError(_("The selection label must match exactly one available option."))
                return key
            if isinstance(value, dict):
                return {key: resolve(item, depth=depth + 1) for key, item in value.items()}
            if isinstance(value, list):
                return [resolve(item, depth=depth + 1) for item in value]
            return value

        model = plan.get("model")
        definitions = {}
        # What checked_value needs of a field; "depends" marks a value Odoo calculates.
        attributes = ["string", "type", "relation", "selection", "related", "readonly", "depends"]
        if isinstance(model, str) and entry["mutation"] and not entry["custom_id"]:
            # A refusal names the permission this action needs (a business method when it has no operation tag).
            definitions = executor._resolve_model(model, entry["operation"] or "method_calls").fields_get(
                attributes=attributes)
        # Plans from the AI provider never see record values or IDs (results stay local), so a raw
        # numeric record ID is a guess unless approved results were shared with the provider.
        guarded = entry["mutation"] and bool(self.env.context.get("odoo_ai_provider_plan")) and not (
            self.env["odoo.ai.assistant.config"]._configuration().business_data_sharing
        )

        def reject_guessed_id(key, value):
            if guarded and (type(value) is int or (
                    isinstance(value, list) and any(type(item) is int for item in value))):
                raise ValidationError(_("'%s' uses a numeric record ID, which you cannot know because Odoo results "
                                        "are not shared with you. Give the record's exact name as text instead.")
                                      % key)

        def checked_value(key, value, known=None):
            """Validate one provider value against the real field definition (of ``known`` fields for a line)."""
            field = (definitions if known is None else known).get(key)
            if not field:
                return value
            _check_settable(key, field)
            if field["type"] == "many2one" and field.get("relation") == "ir.model" and isinstance(value, str):
                if value not in self.env or is_restricted_model(self.env, value):
                    raise ValidationError(_("'%s' is not an available business model.") % value)
                return self.env["ir.model"]._get_id(value)
            if field["type"] in {"many2one", "many2one_reference"}:
                reject_guessed_id(key, value)
                # A link takes a local reference, a label or an empty value; any other object is a mistake.
                if guarded and isinstance(value, dict) and not {"$odoo_ref", "$odoo_selection"} & set(value):
                    raise ValidationError(
                        _("'%s' must be the model's technical name as text, for example \"crm.lead\".") % key
                        if field.get("relation") == "ir.model" else
                        _("'%(field)s' must be the exact name of a %(model)s record, as text.")
                        % {"field": key, "model": field.get("relation")})
            # Tags and other linked records given by name ("Demo" or ["Demo", "VIP"]) are added to the field.
            if field["type"] == "many2many" and (isinstance(value, str) or (
                    isinstance(value, list) and value and all(isinstance(item, str) for item in value))):
                return [[4, record_id(field["relation"], name)] for name in ([value] if isinstance(value, str) else value)]
            if field["type"] in {"one2many", "many2many"} and isinstance(value, list) and value:
                comodel = self.env[field["relation"]]
                # x2many values must be ORM commands; a plain list of records means "create these".
                if all(isinstance(item, dict) and not {"$odoo_ref", "$odoo_selection"} & set(item) for item in value):
                    unknown = sorted({name for item in value for name in item} - set(comodel._fields))
                    if unknown:
                        raise ValidationError(_("'%(field)s' lines use unknown fields %(unknown)s. Each line must be "
                                                "an object of %(model)s fields, for example: %(examples)s.") % {
                                                    "field": key, "unknown": ", ".join(unknown),
                                                    "model": comodel._name,
                                                    "examples": ", ".join(sorted(comodel.fields_get(
                                                        attributes=["required"]))[:MAX_LINE_FIELD_EXAMPLES])})
                    value = [[0, 0, item] for item in value]
                # Only new lines, and for tags links to existing records: the AI may not edit, remove or move other
                # records through a field. Each record touched gets the checks of a direct action on its model.
                line_fields = comodel.fields_get(attributes=attributes)
                commands = []
                for command in value:
                    if isinstance(command, list) and len(command) == 3 and command[0] == 0 and isinstance(command[2], dict):
                        if any(CREDENTIAL_FIELD.search(name) for name in command[2]):
                            raise AccessError(_("Credential fields cannot be changed by AI tools."))
                        self._native(confirmed=True)._check_op(comodel._name, "create")
                        # New lines get the record's own checks: option labels, linked records and tags by name.
                        commands.append([0, 0, {name: resolve(checked_value(name, line_value, line_fields),
                                                              line_fields.get(name))
                                                for name, line_value in command[2].items()}])
                    elif field["type"] == "many2many" and (not isinstance(command, list) or (
                            len(command) == 2 and command[0] == 4)):
                        # A link is [4, record] or the record alone (a name or a local reference).
                        target = _decoded_marker(command[1] if isinstance(command, list) else command)
                        reject_guessed_id(key, target)
                        if type(target) is int:
                            record = executor._check_op(comodel._name, "read").browse(target).exists()
                            if not record:
                                raise UserError(_("No %s with that ID was found, so no action was prepared.")
                                                % comodel._description)
                            record.check_access("read")
                            commands.append([4, depend(comodel._name, record)])
                        elif isinstance(target, str) or (
                                isinstance(target, dict) and {"$odoo_ref", "$odoo_selection"} & set(target)):
                            commands.append([4, resolve(target, {"type": "many2one", "relation": comodel._name})])
                        else:
                            raise ValidationError(_("Link '%s' records by their exact name.") % key)
                    else:
                        raise ValidationError(_("'%s' can only get new lines or links to existing records by name; "
                                                "the AI cannot edit, remove or replace linked records.") % key)
                return commands
            if field["type"] == "selection" and isinstance(value, str):
                keys = [option[0] for option in field.get("selection") or []]
                if keys and value not in keys:
                    # An option given by its label ("High") means its key ("2").
                    option = _option_key(field, value)
                    if option is None:
                        raise ValidationError(_("'%(value)s' is not a valid option for '%(field)s'. "
                                                "Valid options: %(options)s.") % {
                                                    "value": value, "field": key, "options": ", ".join(keys)})
                    return option
            return value

        def values(mapping):
            if not isinstance(mapping, dict):
                return resolve(mapping)
            if entry["mutation"] and not entry["custom_id"]:
                for key, value in mapping.items():
                    value = _decoded_marker(value)
                    if (isinstance(value, dict) and {"$odoo_ref", "$odoo_selection"} & set(value)
                            and (key not in definitions or CREDENTIAL_FIELD.search(key))):
                        raise AccessError(_("An action value refers to an unavailable or credential field."))
            return {key: resolve(checked_value(key, _decoded_marker(value)), definitions.get(key))
                    for key, value in mapping.items()}

        arguments = {}
        for key, value in plan.items():
            if key == "values":
                arguments[key] = values(value)
            elif key == "records" and isinstance(value, list):
                arguments[key] = [values(item) for item in value]
            elif key == "updates" and isinstance(value, list):
                target = {"type": "many2one", "relation": model}
                arguments[key] = [
                    {name: (values(item) if name == "values" else
                            resolve(item, target if name == "id" else None)) for name, item in update.items()}
                    if isinstance(update, dict) else resolve(update) for update in value
                ]
            elif key in RECORD_KEYS and model:
                reject_guessed_id(key, _decoded_marker(value))
                target = {"type": "many2one", "relation": model}
                arguments[key] = ([resolve(item, target) for item in value]
                                  if isinstance(value, list) else resolve(value, target))
            else:
                arguments[key] = resolve(value)
        # Stored plans are jsonb, which reorders keys; keep the snapshot independent of resolution order.
        dependencies.sort(key=lambda item: (item["model"], item["id"]))
        return arguments, dependencies

    def _entry(self, tool, arguments):
        entries = self._catalogue()
        if tool not in entries or not isinstance(arguments, dict):
            raise AccessError(_("This native MCP tool is not available to your user."))
        entry = entries[tool]
        _check_schema(entry["schema"], arguments)
        if entry["custom_id"]:
            return entry
        if entry["name"] == "upload_attachment" and not _UPLOAD_REFERENCE.fullmatch(arguments["data"]):
            raise ValidationError(_("Upload a local file first and use its upload reference."))
        for key in ("fields", "field_names"):
            if any(CREDENTIAL_FIELD.search(name) for name in arguments.get(key) or []):
                raise AccessError(_("Credential fields cannot be used by AI tools."))
        for values in [arguments.get("values", {})] + (arguments.get("records") or []) + [
            item.get("values", {}) for item in arguments.get("updates", []) or []
        ]:
            if isinstance(values, dict) and any(CREDENTIAL_FIELD.search(name) for name in values):
                raise AccessError(_("Credential fields cannot be changed by AI tools."))
        if entry["name"] == "aggregate_records":
            for expression in (arguments.get("groupby") or []) + (arguments.get("aggregates") or []):
                if CREDENTIAL_FIELD.search(expression):
                    raise AccessError(_("Credential fields cannot be used by AI tools."))
        return entry

    def _snapshot(self, entry, arguments):
        executor = self._native(confirmed=True)
        executor._access(mutation=entry["mutation"])
        if entry["custom_id"]:
            custom = self.env["mcp.custom.tool"].browse(entry["custom_id"])
            if not custom._user_can_run():
                raise AccessError(_("This custom tool is no longer available."))
            definition = custom.sudo()
            return {"custom": _digest([definition.name, definition.input_schema, definition.action_id.code,
                                      definition.action_id.write_date.isoformat(),
                                      definition.write_date.isoformat()])}
        model = arguments.get("model")
        if not model:
            if entry["name"] == "upload_attachment":
                executor._check_op("ir.attachment", "create")
            return {}
        recordset = executor._resolve_model(model)
        meta = executor._get_mcp_tools()[entry["name"]]
        operation = meta["operation"]
        if entry["name"] == "upload_attachment":
            operation = "write"
        if entry["name"] in {"call_model_method", "press_button"} and not self.env[
                "mcp.enabled.model"].sudo().is_method_call_enabled(model):
            raise self._refusal(model, "method_calls")
        if entry["name"] == "call_model_method":
            method = executor._validate_native_method(model, recordset, arguments["method"])
            operation = utils.map_method_to_operation(method)
            recordset.check_access(operation or "write")
        if operation:
            executor._check_op(model, operation)
        ids = arguments.get("record_ids") or (
            [arguments["record_id"]] if arguments.get("record_id") else []
        )
        if entry["name"] == "update_records" and arguments.get("updates"):
            ids = [item["id"] for item in arguments["updates"]]
        if not ids:
            return {}
        records = executor._resolve_record_ids_or_raise(model, recordset, ids, executor._limit_bounds()[1])
        for access in {operation or ("write" if entry["mutation"] else "read"), "read"}:
            records.check_access(access)
        summary_fields = ["display_name"] + (["write_date"] if "write_date" in recordset._fields else [])
        snapshot = {"records": _json_safe(records.read(summary_fields)), "version": self._record_fingerprint(records)}
        if entry["name"] == "press_button":
            # What pressing does, for the card; it is checked again when the user confirms.
            snapshot["press"] = executor._run_button(records, executor._find_button(records, arguments["button"]),
                                                     arguments.get("wizard_values"), arguments.get("wizard_button"),
                                                     preview=True)
        return snapshot

    def _execute_native(self, tool, arguments, confirmed=False, upload_data=None):
        event = self.env["odoo.ai.assistant.tool.event"]._begin(tool)
        try:
            entry = self._entry(tool, arguments)
            executor = self._native(confirmed)
            executor._access(mutation=entry["mutation"])
            if entry["custom_id"]:
                result = self.env["mcp.custom.tool"].browse(entry["custom_id"])._run_tool(arguments)
                result = _tool_result(_("Custom tool completed."), {"result": _json_safe(result)})
            else:
                meta = executor._get_mcp_tools()[entry["name"]]
                execution_arguments = dict(arguments)
                if entry["name"] == "upload_attachment":
                    if not upload_data:
                        raise ValidationError(_("The locally staged upload is unavailable."))
                    execution_arguments["data"] = upload_data
                result = getattr(executor, meta["method_name"])(**execution_arguments)
            if not isinstance(result, dict) or result.get("isError"):
                raise UserError(_("MCP did not return a successful tool result."))
            # Neither tool content nor argument values enter the operation audit.
            self.env["odoo.ai.assistant.tool.event"]._finish(event, "done")
            return result
        except Exception:
            self.env["odoo.ai.assistant.tool.event"]._finish(event, "failed")
            raise


class NativeToolEvent(models.Model):
    _name = "odoo.ai.assistant.tool.event"
    _description = "Native AI MCP audit"
    _order = "id desc"

    user_id = fields.Many2one("res.users", ondelete="set null", index=True)
    tool = fields.Char()
    state = fields.Selection([("started", "Started"), ("done", "Completed"), ("failed", "Failed"),
                              ("shared", "Shared with provider"), ("denied", "Rate limit denied")])

    @contextlib.contextmanager
    def _audit_env(self):
        """Audit rows outlive a rolled-back request: a separate cursor, except inside tests."""
        if server_config["test_enable"]:
            yield self.env
            return
        with self.env.registry.cursor() as cr:
            yield api.Environment(cr, self.env.uid, {})

    @api.model
    def _begin(self, tool, state="started"):
        limit = self.env["odoo.ai.assistant.config"]._configuration().native_rate_limit
        with self._audit_env() as env:
            event, denied = self._insert_attempt(env, tool, state, limit)
        if denied:
            raise UserError(_("Native MCP rate limit reached. Wait one minute and retry."))
        return event

    @api.model
    def _insert_attempt(self, env, tool, state, limit):
        env.cr.execute("SELECT pg_advisory_xact_lock(%s, %s)", (NATIVE_RATE_LOCK_NAMESPACE, env.uid))
        ledger = env[self._name].sudo()
        count = ledger.search_count([("user_id", "=", env.uid),
                                     ("create_date", ">=", fields.Datetime.now() - timedelta(minutes=1))])
        denied = count >= limit
        return ledger.create({"user_id": env.uid, "tool": tool[:MAX_AUDIT_TOOL_NAME_CHARS],
                              "state": "denied" if denied else state}).id, denied

    @api.model
    def _finish(self, event_id, state):
        with self._audit_env() as env:
            env[self._name].sudo().browse(event_id).write({"state": state})


class NativeUpload(models.Model):
    _name = "odoo.ai.assistant.upload"
    _description = "Locally staged AI file"

    conversation_id = fields.Many2one("odoo.ai.assistant.conversation", required=True, ondelete="cascade")
    user_id = fields.Many2one("res.users", required=True, ondelete="cascade")
    name = fields.Char(required=True)
    data = fields.Text(required=True, groups="base.group_system")
    mimetype = fields.Char()
    size = fields.Integer()
    # Base64 preview image shown on the file card (images: reduced copy; PDFs: first page, drawn by the browser).
    thumbnail = fields.Text(groups="base.group_system")

    def _card(self):
        """What the chat shows for this file, and the reference the AI uses to read it."""
        self.ensure_one()
        upload = self.sudo()
        return {"reference": "upload:%s" % upload.id, "name": upload.name, "mimetype": upload.mimetype or "",
                "size": upload.size, "thumbnail": upload.thumbnail or False}

    @api.autovacuum
    def _gc_staged_files(self):
        self.sudo().search([("create_date", "<", fields.Datetime.now() - STAGED_FILE_LIFETIME)]).unlink()


class NativeToolRequest(models.Model):
    _name = "odoo.ai.assistant.tool.request"
    _description = "Native AI tool confirmation"
    _order = "id desc"

    conversation_id = fields.Many2one("odoo.ai.assistant.conversation", ondelete="set null")
    user_id = fields.Many2one("res.users", required=True, ondelete="restrict")
    company_id = fields.Many2one("res.company", required=True)
    allowed_company_ids = fields.Json(required=True)
    tool = fields.Char(required=True)
    tool_label = fields.Char()
    arguments = fields.Json(groups="base.group_system")
    resolution_plan = fields.Json(groups="base.group_system")
    resolution_snapshot = fields.Json(groups="base.group_system")
    definition_digest = fields.Char(required=True)
    snapshot = fields.Json(groups="base.group_system")
    result = fields.Json(groups="base.group_system")
    upload_id = fields.Many2one("odoo.ai.assistant.upload", ondelete="set null")
    mutation = fields.Boolean()
    operation = fields.Char()  # MCP Server operation tag of the tool
    state = fields.Selection([
        ("pending", "Awaiting confirmation"), ("done", "Completed"),
        ("cancelled", "Cancelled"), ("shared", "Shared with provider"),
    ], default="pending", required=True)
    provider = fields.Char()
    provider_model = fields.Char()
    expires_at = fields.Datetime(required=True)

    @api.model
    def _prepare(self, conversation, tool, wrapper):
        conversation._owned()
        event = self.env["odoo.ai.assistant.tool.event"]._begin(tool)
        try:
            result = self._prepare_request(conversation, tool, wrapper)
        except Exception:
            self.env["odoo.ai.assistant.tool.event"]._finish(event, "failed")
            raise
        self.env["odoo.ai.assistant.tool.event"]._finish(event, "done")
        return result

    @api.model
    def _prepare_request(self, conversation, tool, wrapper):
        if not isinstance(wrapper, dict):
            raise ValidationError(_("Native MCP tool arguments must be an object."))
        if set(wrapper) == {"arguments_json"} and isinstance(wrapper["arguments_json"], str):
            if len(wrapper["arguments_json"]) > MAX_TOOL_ARGUMENT_CHARS:
                raise ValidationError(_("MCP arguments exceed the native request limit."))
            try:
                arguments = json.loads(wrapper["arguments_json"])
            except ValueError as error:
                raise ValidationError(_("MCP arguments must be a valid JSON object.")) from error
        else:
            arguments = wrapper
            if len(json.dumps(arguments, ensure_ascii=True)) > MAX_TOOL_ARGUMENT_CHARS:
                raise ValidationError(_("MCP arguments exceed the native request limit."))
        if not isinstance(arguments, dict):
            raise ValidationError(_("MCP arguments must be a JSON object."))
        # Some providers serialize a requested field list as named objects even
        # though the MCP contract is a list of field-name strings. Normalize only
        # this unambiguous representation; all other malformed input still fails
        # the published schema validation below.
        if tool == "mcp_get_fields":
            for key in ("field_names", "attributes"):
                value = arguments.get(key)
                if isinstance(value, list) and all(
                    isinstance(item, dict) and set(item) == {"name"}
                    and isinstance(item["name"], str) for item in value
                ):
                    arguments[key] = [item["name"] for item in value]
        bridge = conversation.env["odoo.ai.assistant.native.mcp"]
        plan = arguments
        arguments, dependencies = bridge._resolve_plan(tool, plan)
        entry = bridge._entry(tool, arguments)
        context = conversation.env.context
        if (entry["operation"] == "create" and not entry["custom_id"] and context.get("odoo_ai_provider_plan")
                and not context.get("odoo_ai_sample_data")):
            bridge._check_given(arguments, conversation)
        snapshot = bridge._snapshot(entry, arguments)
        upload = self.env["odoo.ai.assistant.upload"]
        if entry["name"] == "upload_attachment":
            upload = bridge._staged_upload(arguments["data"], conversation)
            if arguments["name"] != upload.name:
                raise ValidationError(_("Use the staged file's exact name."))
        if entry["name"] == "read_attachment" and _UPLOAD_REFERENCE.fullmatch(arguments.get("uri") or ""):
            bridge._staged_upload(arguments["uri"], conversation)
        config = conversation._check_enabled()
        request = self.sudo().create({
            "conversation_id": conversation.id, "user_id": self.env.uid,
            "company_id": self.env.company.id, "allowed_company_ids": sorted(self.env.companies.ids),
            "tool": tool, "arguments": arguments, "definition_digest": _digest(entry),
            "resolution_plan": plan if dependencies or plan != arguments else None,
            "resolution_snapshot": dependencies,
            "tool_label": entry["name"].replace("_", " "),
            "snapshot": snapshot, "mutation": entry["mutation"], "operation": entry["operation"], "upload_id": upload.id,
            "provider": config.provider, "provider_model": conversation._model(config).identifier,
            "expires_at": fields.Datetime.now() + TOOL_REQUEST_LIFETIME,
        })
        if not entry["mutation"]:
            result = bridge._execute_native(tool, arguments)
            request.write({"result": _json_safe(result), "state": "done"})
        return request

    def _validate_current(self, conversation, check_snapshot=True):
        self.ensure_one()
        conversation._owned()
        if self.user_id.id != self.env.uid or self.conversation_id.id != conversation.id:
            raise AccessError(_("This tool request belongs to another conversation or user."))
        if fields.Datetime.now() > self.expires_at:
            raise UserError(_("This tool request expired. Ask again for a fresh preview."))
        if (self.company_id != conversation.env.company
                or self.allowed_company_ids != sorted(conversation.env.companies.ids)):
            raise AccessError(_("Return to the original active company selection before continuing."))
        bridge = conversation.env["odoo.ai.assistant.native.mcp"]
        arguments = self.arguments or {}
        if check_snapshot and self.resolution_plan:
            resolved, dependencies = bridge._resolve_plan(self.tool, self.resolution_plan)
            if (_digest(resolved) != _digest(arguments)
                    or _digest(dependencies) != _digest(self.resolution_snapshot or [])):
                raise UserError(_("A referenced record or selection changed. Ask again for a fresh preview."))
        entry = bridge._entry(self.tool, arguments)
        if _digest(entry) != self.definition_digest:
            raise UserError(_("The tool definition changed. Ask again for a fresh preview."))
        snapshot_arguments = arguments
        if not check_snapshot and entry["name"] == "delete_record":
            snapshot_arguments = {key: value for key, value in arguments.items() if key != "record_id"}
        snapshot = bridge._snapshot(entry, snapshot_arguments)
        if check_snapshot and _digest(snapshot) != _digest(self.snapshot or {}):
            raise UserError(_("The target records or custom action changed. Ask again for a fresh preview."))
        return bridge

    def _lock(self):
        """Lock this request row until the transaction ends, then reload its state."""
        self.ensure_one()
        self.env.cr.execute(SQL("SELECT id FROM %s WHERE id = %s FOR UPDATE", SQL.identifier(self._table), self.id))
        self.invalidate_recordset()

    def _confirm(self, conversation):
        self._lock()
        if self.state != "pending" or not self.mutation:
            raise UserError(_("This action is no longer awaiting confirmation."))
        bridge = self._validate_current(conversation)
        data = None
        if self.upload_id:
            data = bridge._staged_upload(self.arguments["data"], conversation).data
        result = bridge._execute_native(self.tool, self.arguments or {}, confirmed=True, upload_data=data)
        self.sudo().write({"result": _json_safe(result), "state": "done"})
        return result

    def _sharing_context(self, conversation):
        self.ensure_one()
        if self.state != "done":
            raise UserError(_("This result is not available for sharing."))
        bridge = self._validate_current(conversation, check_snapshot=not self.mutation)
        config = conversation._check_enabled()
        if (not config.local_tools or not config.business_data_sharing
                or self.provider != config.provider
                or self.provider_model != conversation._model(config).identifier):
            raise AccessError(_("Business-data sharing is disabled or the provider changed. Ask again."))
        if not self.mutation:
            fresh = bridge._execute_native(self.tool, self.arguments or {})
            if _digest(fresh) != _digest(self.result):
                raise UserError(_("The result changed. Ask again before sharing the updated result."))
        else:
            arguments = self.arguments or {}
            model = arguments.get("model")
            structured = self.result.get("structuredContent") or {}
            executor = bridge._native()
            if model:
                recordset = executor._check_op(model, "read")
                ids = [row["id"] for row in structured.get("records", []) if isinstance(row, dict) and row.get("id")]
                record = structured.get("record") or {}
                if isinstance(record, dict) and record.get("id"):
                    ids.append(record["id"])
                if ids:
                    records = executor._resolve_record_ids_or_raise(model, recordset, ids, executor._limit_bounds()[1])
                    records.check_access("read")
            if structured.get("attachment_id"):
                executor._resolve_attachment_for_tool(
                    "odoo://attachment/%s" % structured["attachment_id"], structured["attachment_id"],
                )
        if fields.Datetime.now() > self.expires_at:
            raise UserError(_("This result expired. Retrieve a fresh result before sharing."))
        texts, resource_texts, images = self._result_parts()
        texts += resource_texts
        evidence = {"tool": self.tool, "structured": self.result.get("structuredContent"), "text": texts}
        if len(json.dumps(evidence, default=str)) > MAX_SHARED_EVIDENCE_CHARS:
            raise UserError(_("This result is too large to share. Request fewer records or fields."))
        if len(images) > MAX_SHARED_IMAGES or any(len(image["data"]) > MAX_IMAGE_CHARS for image in images):
            raise UserError(_("Image content exceeds the provider-sharing limit."))
        return evidence, images

    def _result_parts(self):
        """Text blocks, embedded-resource texts and supported images of the stored tool result."""
        texts, resource_texts, images, resource_images = [], [], [], []
        for part in (self.result or {}).get("content", []):
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif part.get("type") == "image" and part.get("mimeType") in _IMAGE_TYPES:
                images.append(part)
            elif part.get("type") == "resource":
                resource = part.get("resource", {})
                if isinstance(resource.get("text"), str):
                    resource_texts.append(resource["text"])
                elif resource.get("blob") and resource.get("mimeType") in _IMAGE_TYPES:
                    resource_images.append({"type": "image", "mimeType": resource["mimeType"],
                                            "data": resource["blob"]})
        return texts, resource_texts, images + resource_images

    def _payload(self):
        self.ensure_one()
        config = self.env["odoo.ai.assistant.config"]._configuration()
        title = self.tool_label or self.tool.removeprefix(NATIVE_TOOL_PREFIX).replace("_", " ")
        title = title[:1].upper() + title[1:]
        # Built-in tool descriptions are written for the AI provider, not for the user.
        description = ""
        details = ""
        summary = None
        images = []
        links = []
        if self.result:
            texts, resource_texts, result_images = self._result_parts()
            details = "\n\n".join(texts) + "".join("\n" + text for text in resource_texts if text)
            # Oversized images are left out of the card; the result stays downloadable.
            images = [image for image in result_images
                      if isinstance(image.get("data"), str) and 0 < len(image["data"]) <= MAX_IMAGE_CHARS]
            structured = self.result.get("structuredContent") or {}
            if structured.get("format_used") == "extracted_text":
                # PDF/Office extraction separates layout blocks with blank lines.
                details = re.sub(r"\n\s*\n", "\n", details)
            if self.tool == "mcp_read_attachment" and isinstance(structured.get("name"), str):
                description = structured["name"]
            path = structured.get("download_path")
            if isinstance(path, str) and path.startswith("/web/content/") and not path.startswith("//"):
                links.append({"label": _("Download attachment (Odoo login required)"), "url": path})
            if self.mutation:
                # A confirmed change: a short outcome and links to the changed records, not MCP's technical text.
                details = self.env._(_CHANGE_DONE.get(self.operation) or _lt("Successfully completed"))
                # A single record's URL sits beside it; batch results carry one per record.
                records = list(structured.get("records") or []) + (
                    [dict(structured.get("record") or {}, url=structured["url"])] if structured.get("url") else [])
                for record in records:
                    url = urlsplit(record["url"]).path if isinstance(record, dict) and isinstance(record.get("url"), str) else ""
                    if re.fullmatch(r"/odoo/[a-z0-9_.]+/\d+", url):  # the changed record's page in Odoo
                        links.append({"label": record.get("display_name") or url, "url": url})
            if not details:
                details = json.dumps(structured, ensure_ascii=False, indent=2)
        else:
            display_values = {}
            arguments = self.arguments or {}
            model_name = arguments.get("model")
            definitions = {}
            model_label = model_name or ""
            if isinstance(model_name, str):
                model = self.env[model_name].with_user(self.user_id)
                model_label = model._description
                definitions = model.fields_get(attributes=["string", "type", "selection", "relation"])

            def record_name(relation, record_id):
                """A linked record's name, from the names checked with the plan when available."""
                reference = next((item for item in self.resolution_snapshot or []
                                  if item["model"] == relation and item["id"] == record_id), None)
                if reference:
                    return reference["name"]
                related = self.env[relation].with_user(self.user_id).browse(record_id).exists()
                if related:
                    related.check_access("read")
                    return related.display_name
                return record_id

            def readable_value(name, value):
                definition = definitions.get(name, {})
                field_type = definition.get("type")
                if field_type == "selection":
                    value = next((label for key, label in definition.get("selection", []) if key == value), value)
                elif field_type == "many2one" and isinstance(value, int) and value:
                    value = record_name(definition["relation"], value)
                elif field_type == "many2many" and isinstance(value, list) and value and all(
                        isinstance(command, list) and len(command) == 2 and command[0] == 4 for command in value):
                    value = ", ".join(str(record_name(definition["relation"], command[1])) for command in value)
                if isinstance(value, bool):
                    return _("Yes") if value else _("No")
                if value is None or value is False:
                    return _("Not set")
                if isinstance(value, (dict, list)):
                    return json.dumps(value, ensure_ascii=False, indent=2)
                return str(value)

            def value_rows(values):
                rows = []
                if not isinstance(values, dict):
                    return rows
                for name, value in values.items():
                    definition = definitions.get(name)
                    if not definition:
                        continue
                    label = definition["string"]
                    rendered = readable_value(name, value)
                    display_values[label] = rendered
                    rows.append({"label": label, "value": rendered})
                return rows

            action_names = {
                "create_record": _("Create"), "create_records": _("Create"),
                "update_record": _("Update"), "update_records": _("Update"),
                "delete_record": _("Delete"),
                "call_model_method": _("Run"), "upload_attachment": _("Upload"),
            }
            verb = action_names.get(self.tool.removeprefix(NATIVE_TOOL_PREFIX), self.tool_label or self.tool)
            headline = _("%(action)s %(model)s", action=verb, model=model_label or self.tool_label or self.tool)
            target_records = (self.snapshot or {}).get("records", [])
            target_names = [record["display_name"] for record in target_records
                            if isinstance(record, dict) and record.get("display_name")]
            change_groups = []
            if isinstance(arguments.get("values"), dict):
                rows = value_rows(arguments["values"])
                if rows:
                    change_groups.append({"title": _("Values"), "items": rows})
            for index, record_values in enumerate(arguments.get("records") or [], start=1):
                rows = value_rows(record_values)
                if rows:
                    change_groups.append({"title": _("Record %(number)s", number=index), "items": rows})
            for index, update in enumerate(arguments.get("updates") or [], start=1):
                if isinstance(update, dict):
                    rows = value_rows(update.get("values"))
                    if rows:
                        change_groups.append({"title": _("Update %(number)s", number=index), "items": rows})
            operation = self.tool.removeprefix(NATIVE_TOOL_PREFIX)
            if operation in {"create_records", "update_records"}:
                count = len(arguments.get("records") or arguments.get("updates") or [])
                subject = _("%(count)s records", count=count)
            elif target_names:
                subject = ", ".join(target_names[:5])
                if len(target_names) > 5:
                    subject += _(" and %(count)s more", count=len(target_names) - 5)
            elif arguments.get("record_id"):
                subject = _("the selected record")
            elif operation in {"create_record", "create_records"}:
                subject = _("Review the proposed values")
            else:
                subject = model_label or self.tool_label or self.tool
            press = (self.snapshot or {}).get("press")
            if press:
                headline = _("Press '%(button)s'", button=press["button"])
                if press["warning"]:
                    change_groups.append({"title": _("Odoo asks"), "items": [{"label": _("Warning"),
                                                                              "value": press["warning"]}]})
                change_groups.append({"title": press["form"] or _("Next step"), "items": press["values"] or [
                    {"label": _("If Odoo opens a form"), "value": _("it is completed with Odoo's own values")}]})
            summary = {"headline": headline, "subject": subject, "targets": target_names,
                       "groups": change_groups}
            details = json.dumps({"arguments": self.arguments or {}, "targets": self.snapshot or {},
                                  "proposed_values": display_values,
                                  "local_references": self.resolution_snapshot or []},
                                 ensure_ascii=False, indent=2)
        table = None
        if self.tool in {"mcp_search_records", "mcp_get_record", "mcp_aggregate_records"} and self.result:
            structured = self.result.get("structuredContent") or {}
            rows = {"mcp_search_records": structured.get("records"), "mcp_aggregate_records": structured.get("groups")}.get(
                self.tool, [structured.get("record")])
            if isinstance(rows, list) and all(isinstance(row, dict) for row in rows):
                model = (self.arguments or {})["model"]
                definitions = self.env[model].with_user(self.user_id).fields_get(attributes=["string"])

                def label(key):
                    # Aggregate keys are "field", "field:sum" or "field:month", and Odoo's "__count".
                    name, _sep, operator = key.partition(":")
                    text = definitions.get(name, {}).get("string") or (_("Count") if name == "__count" else name)
                    return "%s (%s)" % (text, operator) if operator else text

                # Aggregates list their groups before their measures.
                keys = list(dict.fromkeys(structured.get("groupby", []) + structured.get("aggregates", [])
                                          + [key for row in rows if row for key in row]))
                if not keys:
                    keys = [name for name in (self.arguments or {}).get("fields", []) if name != "__all__"] or ["id"]
                total = structured.get("total", len(rows))
                offset = structured.get("offset", 0)
                # Fewer records than MCP Server's page size is a count the user asked for ("the last five"):
                # show exactly those, without paging.
                limit = (self.arguments or {}).get("limit")
                requested = type(limit) is int and limit < self.env["odoo.ai.assistant.native.mcp"]._limit_bounds()[0]
                table = {
                    "kind": "table", "title": self.env[model]._description,
                    "columns": [{"key": key, "label": label(key)} for key in keys],
                    "rows": [{key: cell_value(self.env[model], key.partition(":")[0], value) for key, value in row.items()}
                             for row in rows if row],
                    "total": total, "offset": offset,
                    "has_more": not requested and offset + len(rows) < total,
                }
        elif self.result and {"columns", "rows"} <= set(self.result.get("structuredContent") or {}):
            # A tool that already returns a ready table, such as rate_sales_executives, with an optional note above it.
            structured = self.result["structuredContent"]
            description = structured.get("note") or description
            table = {"kind": "table", "title": structured.get("title", ""), "columns": structured["columns"],
                     "rows": structured["rows"], "total": len(structured["rows"]), "offset": 0, "has_more": False}
        elif self.tool == "mcp_list_record_messages" and self.result:
            # One row per entry, in the scrolling record table instead of one long block of text.
            structured = self.result.get("structuredContent") or {}
            entries = structured.get("messages") or []
            labels = {"date": _("Date"), "author": _("By"), "kind": _("Type"), "summary": _("What happened")}
            table = {
                "kind": "table", "title": structured.get("record", ""),
                "columns": [{"key": key, "label": label} for key, label in labels.items()],
                "rows": [{"date": entry["date"], "author": entry["author"], "kind": entry["kind"],
                          "summary": _history_summary(entry)} for entry in entries],
                "total": len(entries), "offset": 0, "has_more": False,
            }
        return {
            "kind": "mcp_request", "request_id": self.id, "title": title, "state": self.state,
            "description": description,
            "mutation": self.mutation, "summary": summary, "details": details[:MAX_REQUEST_DETAIL_CHARS],
            "table": table,
            "images": images, "links": links, "provider": self.provider,
            "sharing_allowed": bool(config.business_data_sharing and config.enabled and config.local_tools
                                    and self.state == "done"
                                    and config.provider == self.provider
                                    and self.conversation_id._model(config).identifier == self.provider_model
                                    and fields.Datetime.now() <= self.expires_at),
            "expires_at": fields.Datetime.to_string(self.expires_at),
        }
