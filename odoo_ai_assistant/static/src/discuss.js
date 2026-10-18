import { Component, useEffect, useState } from "@odoo/owl";
import { Discuss } from "@mail/core/public_web/discuss";
import { DiscussClientAction } from "@mail/core/public_web/discuss_client_action";
import { discussSidebarItemsRegistry } from "@mail/core/public_web/discuss_sidebar";
import { DiscussSidebarCategories } from "@mail/discuss/core/public_web/discuss_sidebar_categories";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { router } from "@web/core/browser/router";
import { _t } from "@web/core/l10n/translation";
import { deserializeDateTime } from "@web/core/l10n/dates";
import { Thread } from "@mail/core/common/thread_model";
import { MessagingMenu } from "@mail/core/public_web/messaging_menu";
import { DiscussCoreCommon } from "@mail/discuss/core/common/discuss_core_common_service";
import { AI_WORKSPACE_ID, AssistantPanel } from "./assistant";

/** AI conversations are Discuss group chats that only the AI panel can display (its cards, charts and tables). */
function isAIConversation(aiState, thread) {
    return thread.model === "discuss.channel" && aiState.channel_ids.includes(thread.id);
}

/** AI conversations with unread answers, counted like Odoo counts chats with unread messages. */
function unreadConversations(aiState) {
    return aiState.conversations.filter((conversation) => conversation.unread).length;
}

patch(Thread.prototype, {
    get inChathubOnNewMessage() {
        // Keep AI replies out of Odoo's chat bubbles, which would show them as plain text.
        return super.inChathubOnNewMessage && !isAIConversation(this.store.env.services.odoo_ai.state, this);
    },
});

patch(DiscussCoreCommon.prototype, {
    async _handleNotificationNewMessage(payload) {
        await super._handleNotificationNewMessage(...arguments);
        // The server marks AI replies as read (the AI panel tracks them), so undo the browser's unread count.
        const channel = this.store.Thread.get({ model: "discuss.channel", id: payload.id });
        if (channel?.self_member_id && isAIConversation(this.store.env.services.odoo_ai.state, channel)) {
            channel.self_member_id.message_unread_counter = 0;
        }
    },
});

// The chat menu's own AI tab, as Odoo adds tabs (see WhatsApp): it lists the AI conversations and opens them in
// the AI panel, never as plain chat windows.
const AI_TAB = "odoo_ai";

patch(MessagingMenu.prototype, {
    setup() {
        super.setup();
        this.ai = useService("odoo_ai");
        this.aiState = useState(this.ai.state);
        this.aiTab = AI_TAB;
    },
    get threads() {
        // As in the Discuss sidebar, AI conversations are listed under their own tab, not with the chats.
        return super.threads.filter((thread) => !isAIConversation(this.aiState, thread));
    },
    get aiUnread() {
        return unreadConversations(this.aiState);
    },
    get _tabs() {
        const items = super._tabs;
        if (this.aiState.enabled) {
            items.push({ counter: this.aiUnread, icon: "fa fa-magic", id: AI_TAB, label: _t("AI"), sequence: 50 });
        }
        return items;
    },
    get counter() {
        // Unread AI answers count on the chat icon, like unread chats.
        return super.counter + this.aiUnread;
    },
    get hasPreviews() {
        return super.hasPreviews || (this.store.discuss.activeTab === AI_TAB && this.aiState.conversations.length > 0);
    },
    aiDate(conversation) {
        return deserializeDateTime(conversation.date);
    },
    openAIConversation(conversation) {
        this.dropdown.close();
        return this.ai.open(conversation);
    },
});

patch(Discuss, {
    components: { ...Discuss.components, AssistantPanel },
});

patch(Discuss.prototype, {
    setup() {
        super.setup();
        this.ai = useService("odoo_ai");
        this.aiState = useState(this.ai.state);
        useEffect(() => {
            if (this.store.discuss.thread) {
                this.aiState.discussActive = false;
            }
        }, () => [this.store.discuss.thread]);
        useEffect(() => {
            if (this.aiState.discussActive) {
                this.env.config?.setDisplayName(this.aiState.assistant_name);
            }
        }, () => [this.aiState.discussActive]);
    },
});

patch(DiscussClientAction.prototype, {
    setup() {
        this.ai = useService("odoo_ai");
        super.setup();
    },
    async restoreDiscussThread(props) {
        if (this.getActiveId(props) === AI_WORKSPACE_ID) {
            // Load the sidebar's channels and direct messages, as Odoo's own Discuss start-up does.
            await this.store.channels.fetch();
            this.store.discuss.thread = undefined;
            this.store.discuss.hasRestoredThread = true;
            try {
                await this.ai.showWorkspace();
            } catch (error) {
                this.ai.report(error);
            }
            return;
        }
        this.ai.state.discussActive = false;
        return super.restoreDiscussThread(props);
    },
});

patch(DiscussSidebarCategories.prototype, {
    setup() {
        super.setup();
        this.aiState = useState(useService("odoo_ai").state);
    },
    filteredThreads(threads) {
        const visible = super.filteredThreads(threads);
        return visible.filter((thread) => this.isAIVisibleThread(thread));
    },
    isAIVisibleThread(thread) {
        // Show AI conversations only under "AI conversations"; every other channel, direct message
        // and group chat stays as in standard Odoo.
        return !isAIConversation(this.aiState, thread);
    },
});

class AISidebarButton extends Component {
    static template = "odoo_ai_assistant.DiscussSidebarButton";
    static props = {};

    setup() {
        this.ai = useService("odoo_ai");
        this.state = useState(this.ai.state);
        this.category = useState({ open: true });
        this.store = useService("mail.store");
        this.action = useService("action");
    }

    get unread() {
        return unreadConversations(this.state);
    }

    async open() {
        try {
            this.store.discuss.thread = undefined;
            router.pushState({ active_id: AI_WORKSPACE_ID });
            // Keep the action stack up to date (used by breadcrumbs), as Discuss does when opening a thread.
            const discussAction = this.action.currentController?.action;
            if (discussAction?.id === this.store.action_discuss_id) {
                discussAction.context.active_id = AI_WORKSPACE_ID;
            }
            await this.ai.showWorkspace();
        } catch (error) {
            this.ai.report(error);
        }
    }
}

discussSidebarItemsRegistry.add("odoo_ai_assistant.ai", AISidebarButton, { sequence: 31 });
