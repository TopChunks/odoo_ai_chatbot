import { Store } from "@mail/core/common/store_service";

import { patch } from "@web/core/utils/patch";

import { aiConversations, hasAiAgentAccess, openNewAiConversation } from "./ai_agent_utils";

patch(Store.prototype, {
    onStarted() {
        super.onStarted(...arguments);
        if (!hasAiAgentAccess()) {
            return;
        }
        this.env.services.bus_service.subscribe("odoo_ai_agent/progress", ({ channel_id, text }) => {
            const thread = this.Thread.get({ model: "discuss.channel", id: channel_id });
            if (thread?.aiAgentPending) {
                thread.aiAgentStatus = text;
            }
        });
    },
    /** @param {{ replace?: import("models").Thread }} [options] */
    aiAgentNewConversation(options) {
        return openNewAiConversation(this, options);
    },
    /** Show the AI conversations in Discuss, on the latest one (a new one when there is none). */
    async aiAgentOpen() {
        const latest = aiConversations(this)[0];
        if (latest) {
            latest.setAsDiscussThread();
        } else {
            await this.aiAgentNewConversation();
        }
    },
    /** @param {import("models").Thread} thread */
    async aiAgentDeleteConversation(thread) {
        const wasOpen = thread.eq(this.discuss.thread);
        await thread.closeChatWindow();
        await this.env.services.orm.call("discuss.channel", "ai_agent_delete", [[thread.id]]);
        thread.delete();
        if (wasOpen) {
            this.discuss.thread = undefined;
            // Stay on the AI conversations: open the next one.
            await this.aiAgentOpen();
        }
    },
});
