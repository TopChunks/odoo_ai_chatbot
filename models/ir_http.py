from odoo import models


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    def session_info(self):
        result = super().session_info()
        if self.env.user._is_internal():
            # Read by the web client to show the top-bar icon and the Discuss category.
            result["ai_agent_access"] = self.env.user.has_group("odoo_ai_agent.group_ai_agent_user")
        return result
