import { Discuss } from "@mail/core/public_web/discuss";
import {
    DiscussSidebarCategories,
    DiscussSidebarCategory,
} from "@mail/discuss/core/public_web/discuss_sidebar_categories";

import { Component, useState } from "@odoo/owl";

import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";
import { useAutofocus, useService } from "@web/core/utils/hooks";

import { AI_ICON_URL, aiConversations } from "../core/ai_agent_utils";

const { DateTime } = luxon;

class RenameInput extends Component {
    static template = "odoo_ai_agent.RenameInput";
    static props = ["value", "onDone"];

    setup() {
        this.state = useState({ value: this.props.value });
        useAutofocus();
    }

    onKeydown(ev) {
        if (ev.key === "Enter") {
            this.props.onDone(this.state.value);
        } else if (ev.key === "Escape") {
            this.props.onDone(null);
        }
    }
}

/** Panel next to the chat: + Chat, search, All / Unread, and the conversations. */
export class AiAgentConversationList extends Component {
    static template = "odoo_ai_agent.ConversationList";
    static components = { RenameInput };
    static props = {};

    setup() {
        this.store = useService("mail.store");
        this.dialog = useService("dialog");
        this.iconUrl = AI_ICON_URL;
        this.state = useState({ search: "", tab: "all", renamingId: null });
    }

    get threads() {
        const term = this.state.search.trim().toLowerCase();
        return aiConversations(this.store).filter(
            (thread) =>
                (!term || thread.displayName.toLowerCase().includes(term)) &&
                (this.state.tab === "all" || thread.needactionCounter > 0)
        );
    }

    lastMessage(thread) {
        return thread.newestPersistentOfAllMessage;
    }

    time(thread) {
        const message = this.lastMessage(thread);
        const date = message?.datetime || thread.lastInterestDt;
        if (!date) {
            return "";
        }
        if (date.hasSame(DateTime.now(), "day")) {
            return date.toLocaleString(DateTime.TIME_SIMPLE);
        }
        return date.toLocaleString({ month: "short", day: "numeric" });
    }

    isUnread(thread) {
        return thread.needactionCounter > 0;
    }

    open(thread) {
        if (this.state.renamingId !== thread.id) {
            thread.setAsDiscussThread();
        }
    }

    newChat() {
        this.store.aiAgentNewConversation();
    }

    startRename(thread) {
        this.state.renamingId = thread.id;
    }

    async endRename(thread, value) {
        this.state.renamingId = null;
        if (value !== null) {
            await thread.rename(value);
        }
    }

    delete(thread) {
        this.dialog.add(ConfirmationDialog, {
            title: _t("Delete conversation"),
            body: _t('Delete "%s" and all its messages? This cannot be undone.', thread.displayName),
            confirmLabel: _t("Delete"),
            confirm: () => this.store.aiAgentDeleteConversation(thread),
            cancel: () => {},
        });
    }
}

/** The single "AI Agent" entry of the "AI Conversations" sidebar category. */
export class AiAgentSidebarEntry extends Component {
    static template = "odoo_ai_agent.SidebarEntry";
    static components = { DiscussSidebarCategory };
    static props = ["category"];

    setup() {
        this.store = useService("mail.store");
        this.iconUrl = AI_ICON_URL;
    }

    get isActive() {
        return Boolean(this.store.discuss.thread?.isAiAgent);
    }

    get unreadCount() {
        return aiConversations(this.store).filter(
            (thread) => thread.needactionCounter > 0
        ).length;
    }

    open() {
        this.store.aiAgentOpen();
    }
}

Discuss.components = { ...Discuss.components, AiAgentConversationList };
DiscussSidebarCategories.components = { ...DiscussSidebarCategories.components, AiAgentSidebarEntry };
