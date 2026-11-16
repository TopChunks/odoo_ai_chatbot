import { MessagingMenu } from "@mail/core/public_web/messaging_menu";

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

import { AI_CHANNEL_TYPE, hasAiAgentAccess } from "./ai_agent_utils";

patch(MessagingMenu.prototype, {
    get hasAiAgentAccess() {
        return hasAiAgentAccess();
    },
    get aiAgentUnreadCount() {
        return Object.values(this.store.Thread.records).filter(
            (thread) =>
                thread.channel_type === AI_CHANNEL_TYPE && thread.needactionCounter > 0
        ).length;
    },
    /** Mobile bottom tabs. */
    get _tabs() {
        const tabs = super._tabs;
        if (hasAiAgentAccess()) {
            tabs.push({
                counter: this.aiAgentUnreadCount,
                icon: "o-ai-agent-glyph",
                id: AI_CHANNEL_TYPE,
                label: _t("AI Agent"),
                sequence: 50,
            });
        }
        return tabs;
    },
    onClickNewMessage() {
        if (this.store.discuss.activeTab === AI_CHANNEL_TYPE) {
            this.dropdown?.close();
            this.store.aiAgentNewConversation();
            return;
        }
        return super.onClickNewMessage(...arguments);
    },
});
