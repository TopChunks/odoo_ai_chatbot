from datetime import date, datetime
from typing import TYPE_CHECKING

from odoo import api, models, _
from odoo.exceptions import AccessError, UserError
from odoo.orm.models import READ_GROUP_DISPLAY_FORMAT
from odoo.tools.misc import format_date

from .limits import MAX_CHART_GROUPS

if TYPE_CHECKING:
    from mcp_server.models.mcp_mixin import mcp_tool
else:
    from odoo.addons.mcp_server.models.mcp_mixin import mcp_tool


_LOCAL_MCP = object()


class AILocalMCP(models.AbstractModel):
    _name = "odoo.ai.assistant.mcp"
    _inherit = "mcp.mixin"
    _description = "Local AI adapter for the supplied MCP Server"

    def _resolve_model(self, model):
        if self.env.context.get("odoo_ai_local_mcp") is not _LOCAL_MCP:
            raise AccessError(_("Use the AI app's approved local tools."))
        # Internal access uses AI opt-in; the separate external MCP switch stays off.
        config = self.env["odoo.ai.assistant.conversation"]._check_enabled()
        if not config.local_tools:
            raise AccessError(_("Local MCP tools are disabled."))
        if not isinstance(model, str) or model not in self.env:
            raise UserError(_("Unknown Odoo model."))
        gates = self.env["mcp.enabled.model"].sudo()
        if not gates.is_model_enabled(model):
            raise AccessError(_("Model '%s' is not enabled in MCP Server.") % model)
        return self.env[model]

    def _check_op(self, model, operation):
        recordset = self._resolve_model(model)
        if operation != "read":
            raise AccessError(_("Only approved MCP reads are available through the AI app."))
        if not self.env["mcp.enabled.model"].sudo().check_model_operation_enabled(model, operation):
            raise AccessError(_("MCP Server does not allow this operation on '%s'.") % model)
        return recordset

    @mcp_tool(
        name="ai_group_records", description="Group records by one field locally for a chart.",
        input_schema={"type": "object", "properties": {
            "model": {"type": "string"}, "domain": {"type": "array"},
            "group": {"type": "string"}, "measure": {"type": "string"},
        }, "required": ["model", "domain", "group", "measure"], "additionalProperties": False},
        operation="read", readOnlyHint=True,
    )
    @api.model
    def _ai_group_records(self, model, domain, group, measure):
        model_rs = self._check_op(model, "read")
        grouped = model_rs._read_group(
            self._coerce_domain(domain), groupby=[group], aggregates=[measure], limit=MAX_CHART_GROUPS + 1,
        )
        field = model_rs._fields[group.split(":")[0]]
        # Show selection labels (e.g. "Sales Order"), not stored keys (e.g. "sale").
        options = dict(field._description_selection(self.env)) if field.type == "selection" else {}
        rows = []
        for label, value in grouped:
            if label in options:
                label = options[label]
            elif isinstance(label, models.BaseModel):
                label = label.display_name if label else _("Unassigned")
            elif isinstance(label, date) and ":" in group:
                # Same labels as Odoo's own graph view ("October 2026", "W41 2026"); read_group already
                # returns the period start in the user's timezone, so only the date part is formatted.
                label = format_date(self.env, label.date() if isinstance(label, datetime) else label,
                                    date_format=READ_GROUP_DISPLAY_FORMAT[group.split(":")[1]])
            elif label is False:
                label = _("Unassigned")
            rows.append({"label": str(label), "value": value})
        return {"structuredContent": {"rows": rows[:MAX_CHART_GROUPS], "truncated": len(rows) > MAX_CHART_GROUPS}}

    def _execute(self, tool_name, arguments):
        allowed = {"ai_group_records"}
        meta = self._get_mcp_tools().get(tool_name)
        if tool_name not in allowed or not meta or meta["operation"] != "read":
            raise AccessError(_("Only approved MCP read tools may execute in the AI app."))
        executor = self.with_context(odoo_ai_local_mcp=_LOCAL_MCP)
        result = getattr(executor, meta["method_name"])(**arguments)
        if not isinstance(result, dict) or not isinstance(result.get("structuredContent"), dict):
            raise UserError(_("MCP Server returned an invalid tool result."))
        return result["structuredContent"]
