import { fields } from "@mail/core/common/record";
import { DiscussApp } from "@mail/core/public_web/discuss_app_model";
import { DiscussAppCategory } from "@mail/discuss/core/public_web/discuss_app_category_model";
import { DiscussSidebarCategory } from "@mail/discuss/core/public_web/discuss_sidebar_categories";
import { compareDatetime } from "@mail/utils/common/misc";

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

import { hasAiAgentAccess } from "./ai_agent_utils";

export const AI_CATEGORY_ID = "odoo_ai_agent.conversations";

patch(DiscussApp.prototype, {
    setup() {
        super.setup(...arguments);
        // "AI Conversations", right below "Direct messages" (sequence 30).
        this.aiConversations = fields.One("DiscussAppCategory", {
            compute() {
                if (!hasAiAgentAccess()) {
                    return null;
                }
                return {
                    addTitle: _t("New AI conversation"),
                    extraClass: "o-ai-agent-category",
                    icon: "o-ai-agent-glyph",
                    id: AI_CATEGORY_ID,
                    name: _t("AI Conversations"),
                    sequence: 35,
                };
            },
            eager: true,
        });
    },
});

patch(DiscussAppCategory.prototype, {
    sortThreads(t1, t2) {
        if (this.id === AI_CATEGORY_ID) {
            return compareDatetime(t2.lastInterestDt, t1.lastInterestDt) || t2.id - t1.id;
        }
        return super.sortThreads(t1, t2);
    },
});

patch(DiscussSidebarCategory.prototype, {
    get actions() {
        const actions = super.actions;
        if (this.category.id === AI_CATEGORY_ID) {
            actions.push({
                icon: "fa fa-plus",
                label: _t("New AI conversation"),
                onSelect: () => this.store.aiAgentNewConversation(),
            });
        }
        return actions;
    },
});
