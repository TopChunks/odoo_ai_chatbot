import logging

_logger = logging.getLogger(__name__)

# Data the AI may use right after installation (only models that exist in this database).
# Read access everywhere; changes only where they are everyday work. Every change is still
# a preview the user confirms, and runs with the user's own access rights.
DEFAULT_MODELS = {
    "res.partner": {"allow_create": True, "allow_write": True},
    "crm.lead": {"allow_create": True, "allow_write": True, "allow_method_calls": True},
    "crm.stage": {},
    "crm.team": {},
    "sale.order": {"allow_method_calls": True},
    "sale.order.line": {},
    "account.move": {},
    "account.move.line": {},
    "product.product": {},
    "product.template": {},
    "product.category": {},
    "purchase.order": {},
    "purchase.order.line": {},
    "stock.picking": {},
    "stock.quant": {},
    "project.project": {},
    "project.task": {"allow_create": True, "allow_write": True},
    "hr.employee": {},
    "hr.department": {},
    "calendar.event": {"allow_create": True, "allow_write": True},
    "mail.activity": {"allow_create": True, "allow_write": True},
    "res.country": {},
    "res.currency": {},
    "res.users": {},
    "res.groups": {},
    "res.groups.privilege": {},
    "res.company": {},
    "account.payment": {},
    "account.journal": {},
    "product.pricelist": {},
    "product.pricelist.item": {},
    "product.supplierinfo": {},
    "purchase.requisition": {},
    "purchase.requisition.line": {},
    "stock.move": {},
    "stock.move.line": {},
    "stock.location": {},
    "stock.warehouse": {},
    "stock.warehouse.orderpoint": {},
    "stock.lot": {},
    "stock.scrap": {},
    "mrp.production": {},
    "mrp.bom": {},
    "mrp.bom.line": {},
    "mrp.workorder": {},
    "mrp.workcenter": {},
    "link.tracker": {},
    "link.tracker.click": {},
    "utm.campaign": {},
    "utm.source": {},
    "utm.medium": {},
    "hr.job": {},
    "project.task.type": {},
    "crm.lost.reason": {},
}


def post_init_hook(env):
    params = env["ir.config_parameter"].sudo()
    if params.get_param("mcp_server.enabled") != "True":
        # The AI Agent runs the MCP Server's tools inside Odoo. Switching MCP on does not by itself
        # open the external /mcp endpoint to anyone: that still needs the "MCP User" group and a key.
        params.set_param("mcp_server.enabled", "True")
        _logger.info("AI Agent: MCP Server switched on.")
    Enabled = env["mcp.enabled.model"].sudo().with_context(active_test=False)
    if Enabled.search_count([]):
        return  # an administrator already chose the data
    for model_name, flags in DEFAULT_MODELS.items():
        ir_model = env["ir.model"]._get(model_name)
        if ir_model:
            Enabled.create({"model_id": ir_model.id, "allow_read": True, **flags})
    env.registry.clear_cache()
