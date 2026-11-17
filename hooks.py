import json
import logging

_logger = logging.getLogger(__name__)

# Data the AI may use (only models that exist in this database). Read access everywhere; changes only
# where they are everyday work. Every change is still a preview the user confirms, and runs with the
# user's own access rights. Offered at installation, and again whenever a model or flag is added here
# or an app adds one of these models (see offer_default_models).
DEFAULT_MODELS = {
    "res.partner": {"allow_create": True, "allow_write": True},
    "crm.lead": {"allow_create": True, "allow_write": True, "allow_method_calls": True},
    "crm.stage": {},
    "crm.team": {},
    "sale.order": {"allow_write": True, "allow_method_calls": True},
    "sale.order.line": {},
    "account.move": {"allow_create": True, "allow_write": True},
    "account.move.line": {},
    "product.product": {},
    "product.template": {},
    "product.attribute": {},
    "product.attribute.value": {},
    "product.template.attribute.line": {"allow_create": True, "allow_write": True},  # variants
    "product.template.attribute.value": {},
    "product.category": {},
    "purchase.order": {},
    "purchase.order.line": {},
    "stock.picking": {},
    "stock.quant": {"allow_write": True, "allow_method_calls": True},  # count + Apply
    "project.project": {},
    "project.task": {"allow_create": True, "allow_write": True},
    "hr.employee": {"allow_create": True, "allow_write": True},
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
    "link.tracker.code": {},
    "utm.campaign": {},
    "utm.source": {},
    "utm.medium": {},
    "hr.job": {},
    "project.task.type": {},
    "project.milestone": {"allow_create": True, "allow_write": True},
    "project.tags": {},
    "crm.lost.reason": {},
    "crm.tag": {},
    "res.partner.category": {},
    "hr.employee.category": {},
    "account.payment.term": {},
    "mail.activity.type": {},
    "mail.activity.plan": {},
    "stock.picking.type": {},
    "uom.uom": {},
}


OFFERED_KEY = "odoo_ai_agent.default_models_offered"


def offer_default_models(env):
    """Enable the DEFAULT_MODELS that are missing, and the flags added to them since the last offer.

    Remembers what was offered, so an administrator's later choice is kept: a model they removed or
    archived is not brought back, and a flag they untick stays off. Runs at installation and at every
    start (cheap when nothing is new), so a new app's models and new defaults reach existing databases.
    """
    if "mcp.enabled.model" not in env:
        return
    params = env["ir.config_parameter"].sudo()
    saved = params.get_param(OFFERED_KEY)
    try:
        offered = json.loads(saved or "{}")
    except ValueError:
        offered = {}
    Enabled = env["mcp.enabled.model"].sudo().with_context(active_test=False)
    added, extended = [], []
    for model_name, flags in DEFAULT_MODELS.items():
        ir_model = env["ir.model"]._get(model_name)
        if not ir_model:
            continue  # its app is not installed: offered once it is
        wanted = {"allow_read", *(flag for flag, on in flags.items() if on)}
        before = set(offered.get(model_name, []))
        new = wanted - before
        if not new:
            continue
        record = Enabled.search([("model_id", "=", ir_model.id)], limit=1)
        if not record and model_name not in offered:
            Enabled.create({"model_id": ir_model.id, **{flag: True for flag in wanted}})
            added.append(model_name)
        elif record.active:
            turn_on = {flag: True for flag in new if not record[flag]}
            if turn_on:
                record.write(turn_on)
                extended.append(model_name)
        offered[model_name] = sorted(wanted)
    text = json.dumps(offered, sort_keys=True)
    if text != saved:
        params.set_param(OFFERED_KEY, text)
    if added or extended:
        _logger.info("AI Agent: MCP models enabled %s, permissions added on %s.", added or "-", extended or "-")


def post_init_hook(env):
    params = env["ir.config_parameter"].sudo()
    if params.get_param("mcp_server.enabled") != "True":
        # The AI Agent runs the MCP Server's tools inside Odoo. Switching MCP on does not by itself
        # open the external /mcp endpoint to anyone: that still needs the "MCP User" group and a key.
        params.set_param("mcp_server.enabled", "True")
        _logger.info("AI Agent: MCP Server switched on.")
    offer_default_models(env)
