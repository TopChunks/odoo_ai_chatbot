import { Thread } from "@mail/core/common/thread";
import { prettifyMessageContent } from "@mail/utils/common/format";

import { Component, onWillStart, useState } from "@odoo/owl";

import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";

import { AI_ICON_URL } from "../core/ai_agent_utils";

/** Shown in an empty AI conversation: what the AI can do, example questions, resume previous chat. */
export class AiAgentWelcome extends Component {
    static template = "odoo_ai_agent.Welcome";
    static props = ["thread", "compact?"];

    setup() {
        this.store = useService("mail.store");
        this.iconUrl = AI_ICON_URL;
        this.state = useState({ previous: false, suggestions: [], hideBanner: false });
        onWillStart(async () => {
            const data = await rpc("/ai_agent/welcome", { channel_id: this.props.thread.id }, { silent: true });
            Object.assign(this.state, data);
        });
    }

    async ask(text) {
        await this.props.thread.post(await prettifyMessageContent(text));
    }

    async resume() {
        const previous = await this.store.Thread.getOrFetch({
            model: "discuss.channel",
            id: this.state.previous.id,
        });
        if (!previous) {
            return;
        }
        if (!this.store.discuss.isActive) {
            await this.props.thread.closeChatWindow();
        }
        previous.open({ focus: true });
    }
}

/** "AI Agent is thinking…" with the live step the server reports. */
export class AiAgentThinking extends Component {
    static template = "odoo_ai_agent.Thinking";
    static props = ["thread"];

    setup() {
        this.iconUrl = AI_ICON_URL;
    }
}

Thread.components = { ...Thread.components, AiAgentWelcome, AiAgentThinking };
