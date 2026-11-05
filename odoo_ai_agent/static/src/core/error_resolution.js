/** Let users request an explanation for supported server-side validation errors. */

import { FormErrorDialog } from "@web/views/form/form_error_dialog/form_error_dialog";
import { WarningDialog } from "@web/core/errors/error_dialogs";
import { patch } from "@web/core/utils/patch";
import { rpc } from "@web/core/network/rpc";
import { onWillStart, useState } from "@odoo/owl";

const SUPPORTED_ERRORS = new Set([
    "odoo.exceptions.ValidationError",
    "odoo.exceptions.UserError",
    "odoo.exceptions.Warning",
    "odoo.addons.base.models.ir_actions.ServerActionWithWarningsError",
]);

function setupErrorResolution(component, message, exceptionName, model) {
    component.aiResolution = useState({ enabled: false, loading: false, answer: "", error: "" });
    component.canResolveWithAI = () =>
        component.aiResolution.enabled && SUPPORTED_ERRORS.has(exceptionName()) && !component.aiResolution.loading;
    component.resolveWithAI = async () => {
        if (!component.canResolveWithAI()) {
            return;
        }
        component.aiResolution.loading = true;
        component.aiResolution.answer = "";
        component.aiResolution.error = "";
        try {
            const result = await rpc("/ai_agent/resolve_error", {
                message: String(message() || "").slice(0, 2400),
                exception_name: exceptionName(),
                model: model() || "",
            });
            component.aiResolution.answer = result.answer || "The AI returned no explanation.";
        } catch {
            component.aiResolution.error = "The AI could not analyze this error. Check the AI Agent settings and try again.";
        } finally {
            component.aiResolution.loading = false;
        }
    };
    if (!SUPPORTED_ERRORS.has(exceptionName())) {
        return;
    }
    onWillStart(async () => {
        try {
            component.aiResolution.enabled = await rpc("/ai_agent/error_resolution_enabled", {});
        } catch {
            component.aiResolution.enabled = false;
        }
    });
}

const warningSetup = WarningDialog.prototype.setup;
patch(WarningDialog.prototype, {
    setup(...args) {
        warningSetup.apply(this, args);
        setupErrorResolution(
            this,
            () => this.message,
            () => this.props.exceptionName || this.props.data?.name,
            () => this.props.model || ""
        );
    },
});

const formErrorSetup = FormErrorDialog.prototype.setup;
patch(FormErrorDialog.prototype, {
    setup(...args) {
        formErrorSetup.apply(this, args);
        setupErrorResolution(
            this,
            () => this.message,
            () => this.props.data?.name,
            () => this.props.data?.model || ""
        );
    },
});
