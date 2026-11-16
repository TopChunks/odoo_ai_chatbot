import { compareDatetime } from "@mail/utils/common/misc";

import { session } from "@web/session";

export const AI_CHANNEL_TYPE = "ai_agent";
export const AI_ICON_URL = "/odoo_ai_agent/static/description/icon.svg";

/** Whether the current user may use the AI Agent (AI Agent > User access right). */
export function hasAiAgentAccess() {
    return Boolean(session.ai_agent_access);
}

/**
 * The user's AI conversations, newest first. Empty ones are hidden unless open.
 *
 * @param {import("models").Store} store
 */
export function aiConversations(store) {
    return Object.values(store.Thread.records)
        .filter(
            (thread) =>
                thread.channel_type === AI_CHANNEL_TYPE &&
                thread.self_member_id &&
                (thread.newestPersistentOfAllMessage || thread.eq(store.discuss.thread))
        )
        .sort((a, b) => compareDatetime(b.lastInterestDt, a.lastInterestDt) || b.id - a.id);
}

/**
 * Open an empty AI conversation (an existing empty one is reused) in a chat window, or in
 * Discuss when Discuss is the current app.
 *
 * @param {import("models").Store} store
 * @param {{ replace?: import("models").Thread }} [options] chat window to replace
 */
export async function openNewAiConversation(store, { replace } = {}) {
    const { channel } = await store.fetchStoreData(
        "/ai_agent/new_conversation",
        {},
        { readonly: false, requestData: true }
    );
    if (replace && replace.notEq(channel) && !store.discuss.isActive) {
        await replace.closeChatWindow();
    }
    await channel.open({ focus: true });
    return channel;
}
