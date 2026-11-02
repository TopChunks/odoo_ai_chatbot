import { Component, useState } from "@odoo/owl";

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

import { hasAiAgentAccess } from "../core/ai_agent_utils";

export class AiAgentSystray extends Component {
    static template = "odoo_ai_agent.Systray";
    static props = {};

    setup() {
        super.setup();
        this.store = useService("mail.store");
        this.state = useState({ opening: false });
    }

    async onClick() {
        if (this.state.opening) {
            return;
        }
        this.state.opening = true;
        try {
            await this.store.aiAgentNewConversation();
        } finally {
            this.state.opening = false;
        }
    }
}

// Higher sequence = further left: just before the messaging (chat) icon.
registry.category("systray").add(
    "odoo_ai_agent.systray",
    { Component: AiAgentSystray, isDisplayed: () => hasAiAgentAccess() },
    { sequence: 30 }
);
