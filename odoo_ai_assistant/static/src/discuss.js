import { Component, useEffect, useState } from "@odoo/owl";
import { Discuss } from "@mail/core/public_web/discuss";
import { DiscussClientAction } from "@mail/core/public_web/discuss_client_action";
import { discussSidebarItemsRegistry } from "@mail/core/public_web/discuss_sidebar";
import { DiscussSidebarCategories } from "@mail/discuss/core/public_web/discuss_sidebar_categories";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { router } from "@web/core/browser/router";
import { AssistantPanel } from "./assistant";

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
        if (this.getActiveId(props) === "odoo_ai.workspace") {
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
        // AI conversations are Discuss group chats; show them only under "AI conversations",
        // and keep every other channel, direct message and group chat as in standard Odoo.
        return !this.aiState.channel_ids.includes(thread.id);
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
    }

    async open() {
        try {
            this.store.discuss.thread = undefined;
            router.pushState({ active_id: "odoo_ai.workspace" });
            await this.ai.showWorkspace();
        } catch (error) {
            this.ai.report(error);
        }
    }
}

discussSidebarItemsRegistry.add("odoo_ai_assistant.ai", AISidebarButton, { sequence: 31 });
