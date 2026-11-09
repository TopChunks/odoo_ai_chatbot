"""Changes: previews the user confirms, then runs through the same MCP tool. Part of the ai_agent.engine model (see
ai_agent_engine.py).
"""

import copy
import json
import logging
import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.tools import html2plaintext

from odoo.addons.mcp_server.controllers import error_sanitizer
from odoo.addons.mcp_server.controllers import utils as mcp_utils

from .ai_agent_engine_constants import (
    ACTION_TTL, WRITE_TOOLS,
)

_logger = logging.getLogger(__name__)


class AiAgentEngineWrite(models.AbstractModel):
    _inherit = "ai_agent.engine"

    # ------------------------------------------------------------------
    # Changes (previews the user confirms)
    # ------------------------------------------------------------------
    @api.model
    def _check_write_allowed(self, tool_name, model):
        operation = WRITE_TOOLS.get(tool_name, "method")
        label = self._model_label(model)
        if not model or model not in self.env:
            raise UserError(_("Unknown model: %s", model))
        if operation == "method":
            allowed = mcp_utils.check_model_method_allowed(self.env, model)
            what = _("pressing buttons")
            acl = "write"
        else:
            allowed = mcp_utils.check_model_operation_allowed(self.env, model, operation)
            what = {"create": _("creating"), "write": _("updating"), "unlink": _("deleting")}[operation]
            acl = operation
        if not allowed:
            raise AccessError(self._not_enabled_message(model, operation))
        if not self.env[model].has_access(acl):
            raise AccessError(_("Your Odoo access rights do not allow %(what)s %(label)s records.",
                                what=what, label=label))

    @api.model
    def _tool_propose(self, tool, arguments, state):
        name = tool["name"]
        if tool.get("custom_tool_id"):
            model, model_label = False, _("Custom action")
            title = tool["description"]
            lines = [{"label": key, "value": self._preview_value(value)} for key, value in arguments.items()]
            records = []
        else:
            self._check_arguments(tool["raw_schema"], arguments)
            model = arguments.get("model")
            self._check_write_allowed(name, model)
            refused = self._cost_refused(model, list(arguments.get("values") or {}))
            if refused:
                self._add_notice(state, refused)
                raise UserError(refused)  # setting a cost the user may not see
            if name == "call_model_method":
                # MCP's own check, before a preview exists: reading (read, search...) is not a button to press.
                self.env["mcp.mixin"]._validate_method_call(model, self.env[model], str(arguments.get("method") or ""))
            model_label = self._model_label(model)
            title, lines, records = self._preview(name, model, arguments)
        block = {
            "type": "action",
            "id": uuid.uuid4().hex,
            "tool": name,
            "arguments": arguments,
            "model": model,
            "model_label": model_label,
            "title": title,
            "lines": lines,
            "records": records,
            "status": "pending",
            "expires": fields.Datetime.to_string(fields.Datetime.now() + ACTION_TTL),
        }
        state["blocks"].append(block)
        return ("PREVIEW SHOWN, NOT APPLIED: the user now sees this change with Confirm and Cancel buttons. Tell the "
                "user briefly what will happen and ask them to confirm. Do not propose it again.")

    @api.model
    def _preview_value(self, value):
        if isinstance(value, bool):
            return _("Yes") if value else _("No")
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False, default=str)[:300]
        return str(value)[:300] if value not in (None, "") else _("(empty)")

    @api.model
    def _preview_fields(self, model, values):
        """Field label: value lines, with related records and options shown by name."""
        Model = self.env[model]
        meta = Model.fields_get(list(values), attributes=["string", "type", "selection", "relation"])
        lines = []
        for field, value in values.items():
            info = meta.get(field)
            if not info:
                raise UserError(_("%(model)s has no field %(field)s. Check the names with get_fields.",
                                  model=model, field=field))
            shown = value
            if info["type"] == "many2one" and isinstance(value, int) and info.get("relation"):
                related = self.env[info["relation"]].browse(value).exists()
                if not related:
                    raise UserError(_("No %(label)s with ID %(id)s exists.",
                                      label=self._model_label(info["relation"]), id=value))
                shown = related.display_name
            elif info["type"] == "selection" and info.get("selection"):
                shown = dict(info["selection"]).get(value, value)
            elif info["type"] in ("many2many", "one2many") and isinstance(value, list) and info.get("relation") \
                    and all(isinstance(v, int) for v in value):
                shown = ", ".join(self.env[info["relation"]].browse(value).exists().mapped("display_name"))
            lines.append({"label": info.get("string") or field, "value": self._preview_value(shown)})
        return lines

    @api.model
    def _preview_records(self, model, ids):
        ids = [int(i) for i in ids or [] if str(i).lstrip("-").isdigit()]
        records = self.env[model].browse(ids).exists()
        missing = set(ids) - set(records.ids)
        if missing:
            raise MissingError(_("No %(label)s found that you may access with ID(s) %(ids)s.",
                                 label=self._model_label(model), ids=sorted(missing)))
        records.check_access("read")
        return [{"id": r.id, "name": r.display_name} for r in records]

    @api.model
    def _button_label(self, model, method):
        try:
            arch = self.env[model].get_view(view_type="form")["arch"]
        except Exception:  # noqa: BLE001 - a missing form view only costs us the nicer label
            _logger.debug("AI Agent: skipped (%s)", 'a missing form view only costs us the nicer label', exc_info=True)
            arch = ""
        match = re.search(r'<button[^>]*name="%s"[^>]*string="([^"]+)"' % re.escape(method), arch) or re.search(
            r'<button[^>]*string="([^"]+)"[^>]*name="%s"' % re.escape(method), arch)
        return match.group(1) if match else method.replace("action_", "").replace("button_", "").replace("_", " ").title()

    @api.model
    def _preview(self, name, model, arguments):
        label = self._model_label(model)
        if name == "create_record":
            return (_("Create a %s", label), self._preview_fields(model, arguments.get("values") or {}), [])
        if name == "create_records":
            items = arguments.get("records") or []
            lines = []
            for index, values in enumerate(items[:10], 1):
                for line in self._preview_fields(model, values or {}):
                    lines.append({"label": f"#{index} {line['label']}", "value": line["value"]})
            if len(items) > 10:
                lines.append({"label": "…", "value": _("%s more", len(items) - 10)})
            return (_("Create %(count)s %(label)s records", count=len(items), label=label), lines, [])
        if name == "update_record":
            records = self._preview_records(model, [arguments.get("record_id")])
            return (_("Update %s", label), self._preview_fields(model, arguments.get("values") or {}), records)
        if name == "update_records":
            if arguments.get("updates"):
                ids = [u.get("record_id") for u in arguments["updates"]]
                records = self._preview_records(model, ids)
                names = {r["id"]: r["name"] for r in records}
                lines = []
                for update in arguments["updates"]:
                    for line in self._preview_fields(model, update.get("values") or {}):
                        lines.append({"label": f"{names.get(update.get('record_id'))}: {line['label']}",
                                      "value": line["value"]})
                return (_("Update %(count)s %(label)s records", count=len(records), label=label), lines, records)
            records = self._preview_records(model, arguments.get("record_ids"))
            return (_("Update %(count)s %(label)s records", count=len(records), label=label),
                    self._preview_fields(model, arguments.get("values") or {}), records)
        if name == "delete_record":
            records = self._preview_records(model, [arguments.get("record_id")])
            return (_("Delete %s", label), [{"label": _("This cannot be undone"), "value": records[0]["name"]}], records)
        if name == "call_model_method":
            method = arguments.get("method") or ""
            if method.startswith("_"):
                raise AccessError(_("Private methods cannot be called."))
            records = self._preview_records(model, arguments.get("record_ids")) if arguments.get("record_ids") else []
            lines = []
            if arguments.get("args"):
                lines.append({"label": _("Arguments"), "value": self._preview_value(arguments["args"])})
            if arguments.get("kwargs"):
                lines.append({"label": _("Options"), "value": self._preview_value(arguments["kwargs"])})
            return (_("Press “%(button)s” on %(label)s", button=self._button_label(model, method), label=label),
                    lines, records)
        if name == "post_message":
            records = self._preview_records(model, [arguments.get("record_id")])
            kind = _("Log note") if (arguments.get("subtype") or "note") == "note" else _("Send message to followers")
            body = html2plaintext(str(arguments.get("body") or ""))
            return (kind, [{"label": _("Message"), "value": body[:600]}], records)
        return (name, [{"label": key, "value": self._preview_value(value)} for key, value in arguments.items()], [])

    @api.model
    def _ai_agent_execute_action(self, message, block_id, confirm):
        """Run (or cancel) a previewed change after the user's click, then update the message."""
        channel = self.env["discuss.channel"].browse(message.res_id)
        if message.model != "discuss.channel" or message.author_id != channel._ai_agent_partner():
            raise AccessError(_("This change preview is not available."))
        channel._ai_agent_check_conversation()
        payload = copy.deepcopy(message.sudo().ai_agent_payload or {})
        block = next((b for b in payload.get("blocks", []) if b.get("id") == block_id and b.get("type") == "action"),
                     None)
        if not block:
            raise UserError(_("This change preview no longer exists."))
        if block["status"] != "pending":
            return block
        if fields.Datetime.now() > fields.Datetime.to_datetime(block["expires"]):
            block["status"] = "expired"
            block["result"] = _("This preview expired after 30 minutes. Ask the AI again.")
        elif not confirm:
            block["status"] = "cancelled"
            block["result"] = _("Cancelled. Nothing was changed.")
        else:
            self._apply_action(block)
        trace = copy.deepcopy(message.sudo().ai_agent_trace or {})
        trace.setdefault("notes", []).append(
            f"The user {'confirmed' if confirm else 'cancelled'} '{block['title']}': {block['status']}"
            + (f" ({block.get('result')})" if block.get("result") else "")
        )
        message.sudo().write({"ai_agent_payload": payload, "ai_agent_trace": trace})
        from odoo.addons.mail.tools.discuss import Store

        Store(bus_channel=message._bus_channel()).add(message.sudo(), ["ai_agent_payload"]).bus_send()
        return block

    @api.model
    def _apply_action(self, block):
        tool = next((t for t in self._ai_tools(False) if t["name"] == block["tool"]), None)
        if not tool:
            block.update(status="failed", result=_("This kind of change is no longer available."))
            return
        try:
            if not tool.get("custom_tool_id"):
                self._check_write_allowed(block["tool"], block["model"])
            result = self._call_mcp(tool, copy.deepcopy(block["arguments"]))
        except (UserError, AccessError, ValidationError, MissingError, ValueError, TypeError) as err:
            block.update(status="failed", result=error_sanitizer.sanitize_message(str(err.args[0] if err.args else err)))
            return
        except Exception as err:  # noqa: BLE001
            _logger.warning("AI Agent: confirmed change %s failed", block["tool"], exc_info=True)
            block.update(status="failed", result=error_sanitizer.sanitize_exception(err, "AI Agent confirmed change"))
            return
        structured = result.get("structuredContent") or {}
        block["status"] = "done"
        record = structured.get("record") or {}
        if block["tool"] in ("create_record", "create_records"):
            names = [record.get("display_name")] if record else [
                r.get("display_name") for r in structured.get("records") or []]
            block["result"] = _("Created: %s", ", ".join(n for n in names if n)) if any(names) else _("Created.")
        elif block["tool"] == "delete_record":
            block["result"] = _("Deleted.")
        elif block["tool"] in ("update_record", "update_records"):
            block["result"] = _("Saved.")
        else:
            block["result"] = _("Done.")
        ids = []
        if structured.get("record", {}).get("id"):
            ids = [structured["record"]["id"]]
        elif structured.get("records"):
            ids = [r.get("id") for r in structured["records"] if r.get("id")]
        elif block["tool"] in ("call_model_method", "update_records", "post_message") and block.get("records"):
            ids = [r["id"] for r in block["records"]]
        if block["tool"] != "delete_record" and ids and block.get("model"):
            block["result_ids"] = ids
        returned = structured.get("result")
        if isinstance(returned, dict) and returned.get("type", "").startswith("ir.actions"):
            # e.g. a button that opens a wizard: the browser opens it for the user.
            block["client_action"] = returned
