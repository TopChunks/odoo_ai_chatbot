import { registerThreadAction, threadActionsRegistry } from "@mail/core/common/thread_actions";

import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";

registerThreadAction("ai-agent-new-chat", {
    // Chat windows only: in Discuss, the conversation list has its own "+ Chat".
    condition: ({ owner, thread }) =>
        thread?.isAiAgent && !owner.isDiscussSidebarChannelActions && owner.props.chatWindow?.isOpen,
    icon: "fa fa-fw fa-plus",
    name: _t("New conversation"),
    open: ({ store, thread }) => store.aiAgentNewConversation({ replace: thread }),
    // Header order, like the other quick actions: + | Open in Discuss | Fold | Close
    sequenceQuick: 40,
    sequence: 5,
});

registerThreadAction("ai-agent-delete", {
    // Chat windows only: in Discuss, conversations are deleted from the conversation list.
    condition: ({ owner, thread }) => thread?.isAiAgent && thread.self_member_id && owner.props.chatWindow,
    icon: "fa fa-fw fa-trash",
    name: _t("Delete conversation"),
    open: ({ store, thread }) => {
        store.env.services.dialog.add(ConfirmationDialog, {
            title: _t("Delete conversation"),
            body: _t('Delete "%s" and all its messages? This cannot be undone.', thread.displayName),
            confirmLabel: _t("Delete"),
            confirm: () => store.aiAgentDeleteConversation(thread),
            cancel: () => {},
        });
    },
    sequence: 90,
    sequenceGroup: 40,
});

/**
 * Adjust stock actions for AI conversations: "Open in Discuss" becomes a header icon, and
 * actions that make no sense with an AI (inviting people, notification settings, message
 * search, attachments, pinned messages) are hidden.
 */
function adapt(id, changes) {
    const definition = threadActionsRegistry.get(id, null);
    if (definition) {
        threadActionsRegistry.add(id, { ...definition, ...changes(definition) }, { force: true });
    }
}

function call(value, args) {
    return typeof value === "function" ? value.call(this, args) : value;
}

adapt("expand-discuss", (definition) => ({
    sequenceQuick(args) {
        return args.thread?.isAiAgent ? 30 : call.call(this, definition.sequenceQuick, args);
    },
}));
for (const id of ["invite-people", "notification-settings", "search-messages", "attachments", "pinned-messages"]) {
    adapt(id, (definition) => ({
        condition(args) {
            return !args.thread?.isAiAgent && (call.call(this, definition.condition, args) ?? true);
        },
    }));
}
