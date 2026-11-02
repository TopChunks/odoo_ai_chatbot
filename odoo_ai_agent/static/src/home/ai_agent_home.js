import { DiscussClientAction } from "@mail/core/public_web/discuss_client_action";

import { xml } from "@odoo/owl";

import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";

/**
 * The AI Agent app: Odoo's own Discuss screen without the Discuss sidebar, so it shows the
 * AI conversation list and the chat, opened on the latest conversation (a new one if none).
 */
export class AiAgentHome extends DiscussClientAction {
    static template = xml`<Discuss hasSidebar="false"/>`;

    async restoreDiscussThread(props) {
        let activeId = props.action.context?.active_id ?? props.action.params?.active_id;
        const current = this.store.discuss.thread;
        if (!activeId && current?.isAiAgent) {
            activeId = current.id;
        }
        if (!activeId) {
            activeId = await rpc("/ai_agent/latest", {});
        }
        if (!activeId) {
            const { channel } = await this.store.fetchStoreData(
                "/ai_agent/new_conversation",
                {},
                { readonly: false, requestData: true }
            );
            activeId = channel.id;
        }
        const action = { ...props.action, context: { ...props.action.context, active_id: activeId } };
        return super.restoreDiscussThread({ ...props, action });
    }
}

registry.category("actions").add("odoo_ai_agent.home", AiAgentHome);
