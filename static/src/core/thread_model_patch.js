import { Thread } from "@mail/core/common/thread_model";

import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { patch } from "@web/core/utils/patch";

import { AI_CHANNEL_TYPE, AI_ICON_URL } from "./ai_agent_utils";

patch(Thread.prototype, {
    setup() {
        super.setup(...arguments);
        /** The AI is preparing an answer in this conversation. */
        this.aiAgentPending = false;
        /** Live status line pushed by the server ("Searching Sales Orders…"). */
        this.aiAgentStatus = "";
        /** The user wrote again while the AI was answering: answer once more afterwards. */
        this.aiAgentQueued = false;
    },
    get isAiAgent() {
        return this.channel_type === AI_CHANNEL_TYPE;
    },
    get avatarUrl() {
        return this.isAiAgent ? AI_ICON_URL : super.avatarUrl;
    },
    /** Unread AI answers count like messages in a direct chat (green badge, like OdooBot). */
    get needactionCounter() {
        return this.isAiAgent ? this.self_member_id?.message_unread_counter ?? 0 : super.needactionCounter;
    },
    get importantCounter() {
        return this.isAiAgent ? this.needactionCounter : super.importantCounter;
    },
    /** No chat bubble pops up for a new AI answer: the green badge in the AI Agent list and menu is enough. */
    get inChathubOnNewMessage() {
        return this.isAiAgent ? false : super.inChathubOnNewMessage;
    },
    _computeDiscussAppCategory() {
        if (this.isAiAgent) {
            return this.store.discuss.aiConversations ?? undefined;
        }
        return super._computeDiscussAppCategory(...arguments);
    },
    async rename(name) {
        if (!this.isAiAgent) {
            return super.rename(...arguments);
        }
        const newName = (name || "").trim();
        if (!newName || newName === this.name) {
            return;
        }
        this.name = newName;
        await this.store.env.services.orm.call("discuss.channel", "ai_agent_rename", [[this.id]], {
            name: newName,
        });
    },
    async post() {
        const message = await super.post(...arguments);
        if (this.isAiAgent && message) {
            this.aiAgentAsk();
        }
        return message;
    },
    /** Ask the server to answer the latest message; the answer arrives as a normal message. */
    async aiAgentAsk() {
        if (this.aiAgentPending) {
            this.aiAgentQueued = true;
            return;
        }
        this.aiAgentPending = true;
        this.aiAgentStatus = _t("Thinking…");
        try {
            const result = await rpc("/ai_agent/answer", { channel_id: this.id }, { silent: true });
            if (result?.status === "busy") {
                this.aiAgentQueued = true;
            }
            if (result?.store_data) {
                this.store.insert(result.store_data);
                const message = this.store["mail.message"].get(result.message_id);
                if (message && !message.in(this.messages)) {
                    this.messages.add(message);
                    // The unread count (green badge) comes from the server with the answer. On screen,
                    // the answer is read right away.
                    if (this.isDisplayed && document.visibilityState === "visible") {
                        this.markAsRead();
                    }
                }
            }
        } catch (error) {
            this.store.env.services.notification.add(
                error?.data?.message || _t("The AI Agent could not answer. Please try again."),
                { type: "danger", title: _t("AI Agent") }
            );
        } finally {
            this.aiAgentPending = false;
            this.aiAgentStatus = "";
        }
        if (this.aiAgentQueued) {
            this.aiAgentQueued = false;
            this.aiAgentAsk();
        }
    },
});
