/** "Resolve with AI" on the error dialogs Odoo shows for server errors. */

import { FormErrorDialog } from "@web/views/form/form_error_dialog/form_error_dialog";
import { RedirectWarningDialog, RPCErrorDialog, WarningDialog } from "@web/core/errors/error_dialogs";
import { _t } from "@web/core/l10n/translation";
import { rpc, rpcBus } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { patch } from "@web/core/utils/patch";
import { markup, onWillStart, useState } from "@odoo/owl";

import { hasAiAgentAccess } from "./ai_agent_utils";

const RESOLVABLE = new Set([
    "odoo.exceptions.ValidationError",
    "odoo.exceptions.UserError",
    "odoo.exceptions.AccessError",
    "odoo.exceptions.MissingError",
    "odoo.exceptions.RedirectWarning",
    "odoo.addons.base.models.ir_actions.ServerActionWithWarningsError",
]);

// The last server call that failed: which model, action and records (never the values the user typed).
let lastFailedCall = null;
rpcBus.addEventListener("RPC:RESPONSE", (ev) => {
    const { data, error } = ev.detail || {};
    if (!error || !data?.params) {
        return;
    }
    const { model, method, args } = data.params;
    let ids = Array.isArray(args) ? args[0] : null;
    if (Number.isInteger(ids)) {
        ids = [ids];
    }
    lastFailedCall = {
        model: typeof model === "string" ? model : "",
        method: typeof method === "string" ? method : "",
        ids: Array.isArray(ids) ? ids.filter((id) => Number.isInteger(id) && id > 0).slice(0, 5) : [],
        at: Date.now(),
    };
});

function failedCall(model) {
    const call = lastFailedCall;
    if (!call || Date.now() - call.at > 15000 || (model && call.model && call.model !== model)) {
        return { model: model || "", method: "", ids: [] };
    }
    return { model: model || call.model, method: call.method, ids: call.ids };
}

/**
 * @param {Object} component the error dialog
 * @param {Object} getters functions returning the message, exception name, model and traceback
 */
function setupErrorResolution(component, { message, exceptionName, model, traceback }) {
    component.aiResolution = useState({
        enabled: false, loading: false, answer: "", answerHtml: "", error: "", sources: [], codeChecked: false,
    });
    const store = useService("mail.store");
    const name = exceptionName() || "";
    const crash = !RESOLVABLE.has(name) && Boolean(traceback());
    const call = failedCall(model());
    component.canResolveWithAI = () =>
        component.aiResolution.enabled && !component.aiResolution.loading && !component.aiResolution.answer;
    component.resolveWithAI = async () => {
        if (!component.canResolveWithAI()) {
            return;
        }
        Object.assign(component.aiResolution, { loading: true, answer: "", answerHtml: "", error: "" });
        try {
            const result = await rpc("/ai_agent/resolve_error", {
                message: String(message() || "").slice(0, 2400),
                exception_name: name,
                model: call.model,
                method: call.method,
                record_ids: call.ids,
                traceback: crash ? String(traceback() || "").slice(-20000) : false,
            });
            component.aiResolution.answer = result.answer || _t("The AI returned no explanation.");
            component.aiResolution.answerHtml = markup(result.answer_html || "");
            component.aiResolution.sources = result.sources || [];
            component.aiResolution.codeChecked = Boolean(result.code_checked);
        } catch {
            component.aiResolution.error = _t(
                "The AI could not analyse this error. Check the AI Agent settings and try again."
            );
        } finally {
            component.aiResolution.loading = false;
        }
    };
    component.continueInAiAgent = async () => {
        const { channel } = await store.fetchStoreData(
            "/ai_agent/error_conversation",
            { message: String(message() || "").slice(0, 1500), answer: component.aiResolution.answer },
            { readonly: false, requestData: true }
        );
        component.props.close?.();
        await channel.open({ focus: true });
    };
    if (!hasAiAgentAccess() || (!RESOLVABLE.has(name) && !crash)) {
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

// Where each error dialog keeps its message, error type, model and (for server crashes) traceback.
const dialogs = [
    // Warnings, validation, access and missing-record errors.
    [WarningDialog, (d) => d.message, (d) => d.props.exceptionName || d.props.data?.name, (d) => d.props.model, () => ""],
    // Warnings with a button to another screen.
    [RedirectWarningDialog, (d) => d.message, (d) => d.props.exceptionName || d.props.data?.name, (d) => d.props.model, () => ""],
    // "Oh snap!" when saving a form fails.
    [FormErrorDialog, (d) => d.message, (d) => d.props.data?.name, () => "", () => ""],
    // Server crashes (the "Oops!" dialog with a traceback). Client-side JavaScript errors are not sent.
    [
        RPCErrorDialog,
        (d) => d.props.data?.message || d.props.message || "",
        (d) => d.props.exceptionName || d.props.data?.name,
        (d) => d.props.model,
        (d) => d.props.data?.debug || "",
    ],
];
for (const [Dialog, message, exceptionName, model, traceback] of dialogs) {
    patch(Dialog.prototype, {
        setup() {
            super.setup(...arguments);
            setupErrorResolution(this, {
                message: () => message(this),
                exceptionName: () => exceptionName(this),
                model: () => model(this) || "",
                traceback: () => traceback(this),
            });
        },
    });
}
