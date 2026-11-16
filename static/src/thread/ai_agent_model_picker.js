import { ChatWindow } from "@mail/core/common/chat_window";
import { DiscussContent } from "@mail/core/public_web/discuss_content";

import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";

/**
 * Choose the AI model of one conversation, among the models of the provider set in the
 * settings. The choice is saved on the conversation and used for its next answers.
 */
export class AiAgentModelPicker extends Component {
    static template = "odoo_ai_agent.ModelPicker";
    static props = ["thread"];

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.state = useState({ models: [], selected: false });
        onWillStart(() => this.load(this.props.thread));
        onWillUpdateProps((next) => {
            if (next.thread.id !== this.props.thread.id) {
                return this.load(next.thread);
            }
        });
    }

    get title() {
        return _t("AI model for this conversation");
    }

    async load(thread) {
        try {
            const info = await this.orm.call("discuss.channel", "ai_agent_model_info", [[thread.id]]);
            this.state.models = info.models;
            this.state.selected = info.selected_id;
        } catch {
            this.state.models = [];
        }
    }

    async onChange(ev) {
        const previous = this.state.selected;
        this.state.selected = parseInt(ev.target.value);
        try {
            await this.orm.call("discuss.channel", "ai_agent_set_model", [[this.props.thread.id], this.state.selected]);
        } catch {
            this.state.selected = previous;
            ev.target.value = previous;
            this.notification.add(_t("This AI model could not be selected."), { type: "danger" });
        }
    }
}

DiscussContent.components = { ...DiscussContent.components, AiAgentModelPicker };
ChatWindow.components = { ...ChatWindow.components, AiAgentModelPicker };
