import { Component, markRaw, onWillStart, useRef, useState } from "@odoo/owl";

import { deserializeDate, deserializeDateTime } from "@web/core/l10n/dates";
import { _t } from "@web/core/l10n/translation";
import { download } from "@web/core/network/download";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import {
    formatDate,
    formatDateTime,
    formatFloat,
    formatInteger,
    formatMonetary,
} from "@web/views/fields/formatters";
import { View } from "@web/views/view";

const ROWS_PREVIEW = 10;

/**
 * Display text for one cell, reusing Odoo's own field formatters.
 *
 * @param {any} value raw value as Odoo's search_read returns it
 * @param {{ type: string, selection?: [string, string][] }} column
 * @param {{ currency?: [number, string] }} [row]
 */
export function formatCell(value, column, row = {}) {
    if (Array.isArray(value)) {
        if (value.length === 2 && typeof value[0] !== "object" && typeof value[1] === "string") {
            return value[1];
        }
        if (column.type === "many2many" || column.type === "one2many") {
            return value.length ? _t("%s records", value.length) : "";
        }
        return value.join(", ");
    }
    if (value === false || value === null || value === undefined) {
        if (column.type === "boolean") {
            return _t("No");
        }
        return column.type === "group" ? _t("None") : "";
    }
    if (column.selection) {
        const option = column.selection.find(([key]) => key === value);
        if (option) {
            return option[1];
        }
    }
    try {
        switch (column.type) {
            case "boolean":
                return value ? _t("Yes") : _t("No");
            case "date":
                return formatDate(deserializeDate(value));
            case "datetime":
                return formatDateTime(deserializeDateTime(value));
            case "monetary":
                return formatMonetary(value, { currencyId: row.currency?.[0] });
            case "float":
                return formatFloat(value, { digits: [false, 2] });
            case "integer":
                return formatInteger(value);
            case "html":
                return new DOMParser().parseFromString(value, "text/html").body.textContent || "";
        }
    } catch {
        // Fall back to the raw value below.
    }
    return String(value);
}

function isNumeric(column) {
    return ["integer", "float", "monetary"].includes(column.type);
}

class BlockExport extends Component {
    static template = "odoo_ai_agent.BlockExport";
    static props = ["message", "block", "getImage?"];

    async export(fileFormat) {
        await download({
            url: "/ai_agent/export",
            data: {
                message_id: this.props.message.id,
                block_id: this.props.block.id,
                file_format: fileFormat,
                image: (fileFormat === "pdf" && this.props.getImage?.()) || "",
            },
        });
    }
}

export class AiAgentTable extends Component {
    static template = "odoo_ai_agent.Table";
    static components = { BlockExport };
    static props = ["message", "block"];

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
        this.state = useState({ expanded: false, drill: {} });
    }

    canDrill(row) {
        return Boolean(this.props.block.grouped && row.domain && this.props.block.model);
    }

    /** Show the records behind a grouped row, read with the user's own rights. */
    async toggleDrill(row, index) {
        if (this.state.drill[index]) {
            delete this.state.drill[index];
            return;
        }
        const records = await this.orm.searchRead(this.props.block.model, row.domain, ["display_name"], {
            limit: 21,
        });
        this.state.drill[index] = { records: records.slice(0, 20), more: records.length > 20, domain: row.domain };
    }

    openDrillRecord(record) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: this.props.block.model,
            res_id: record.id,
            views: [[false, "form"]],
        });
    }

    openDrillAll(domain) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: this.props.block.title,
            res_model: this.props.block.model,
            domain,
            views: [
                [false, "list"],
                [false, "form"],
            ],
        });
    }

    onRowClick(row, index) {
        if (this.canDrill(row)) {
            this.toggleDrill(row, index);
        } else {
            this.openRecord(row);
        }
    }

    get rows() {
        const rows = this.props.block.rows || [];
        return this.state.expanded ? rows : rows.slice(0, ROWS_PREVIEW);
    }

    get hiddenCount() {
        return (this.props.block.rows || []).length - this.rows.length;
    }

    get canOpen() {
        const block = this.props.block;
        return Boolean(block.model && !block.grouped && block.model !== "ir.attachment");
    }

    cell(row, column) {
        return formatCell(row.values[column.name], column, row);
    }

    isNumeric(column) {
        return isNumeric(column);
    }

    openRecord(row) {
        if (!this.canOpen || !row.id) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: this.props.block.model,
            res_id: row.id,
            views: [[false, "form"]],
        });
    }

    openAll() {
        const block = this.props.block;
        this.action.doAction({
            type: "ir.actions.act_window",
            name: block.title,
            res_model: block.model,
            domain: block.domain || [],
            views: [
                [false, "list"],
                [false, "form"],
            ],
        });
    }
}

export class AiAgentRecord extends Component {
    static template = "odoo_ai_agent.Record";
    static components = { BlockExport };
    static props = ["message", "block"];

    setup() {
        this.action = useService("action");
    }

    get fields() {
        return (this.props.block.fields || [])
            .map((field) => ({ ...field, text: formatCell(field.value, field) }))
            .filter((field) => field.text !== "");
    }

    open() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: this.props.block.model,
            res_id: this.props.block.record_id,
            views: [[false, "form"]],
        });
    }
}

// Field attributes the web client needs to load a view (ir.ui.view._get_view_field_attributes).
const VIEW_FIELD_ATTRIBUTES = [
    "change_default", "context", "currency_field", "digits", "domain", "aggregator", "groups", "help",
    "name", "readonly", "related", "relation", "relation_field", "required", "searchable", "selection",
    "size", "sortable", "store", "string", "translate", "trim", "type", "groupable", "falsy_value_label",
];

function escapeAttr(value) {
    return String(value).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

/**
 * A native Odoo graph or pivot view of the chart's records. Odoo's own toolbar switches
 * between bar, line and pie, stacks, sorts and changes the measure.
 */
export class AiAgentChart extends Component {
    static template = "odoo_ai_agent.Chart";
    static components = { BlockExport, View };
    static props = ["message", "block"];

    setup() {
        this.action = useService("action");
        this.orm = useService("orm");
        this.root = useRef("root");
        this.state = useState({
            view: this.props.block.chart_type === "pivot" ? "pivot" : "graph",
            fields: null,
            error: false,
        });
        onWillStart(async () => {
            try {
                // get_views only describes the fields used in the arch; the graph's own
                // toolbar (measures, group-bys) needs them all.
                this.state.fields = markRaw(
                    await this.orm.call(this.props.block.model, "fields_get", [], {
                        attributes: VIEW_FIELD_ATTRIBUTES,
                    })
                );
            } catch {
                this.state.error = true;
            }
        });
    }

    get graphMode() {
        const type = this.props.block.chart_type;
        return ["bar", "line", "pie"].includes(type) ? type : "bar";
    }

    groupField(spec, attrs = "") {
        const [name, interval] = spec.split(":");
        const intervalAttr = interval ? ` interval="${escapeAttr(interval)}"` : "";
        return `<field name="${escapeAttr(name)}"${intervalAttr}${attrs}/>`;
    }

    get arch() {
        const block = this.props.block;
        const measures = this.state.view === "pivot" && block.measures?.length ? block.measures : [block.measure];
        // A graph counts records by default; a pivot lists "__count" like any other measure.
        const measure = measures
            .filter((m) => m && (this.state.view === "pivot" || m !== "__count"))
            .map((m) => `<field name="${escapeAttr(m)}" type="measure"/>`)
            .join("");
        if (this.state.view === "pivot") {
            const [row, col] = block.groupby;
            const colField = col ? this.groupField(col, ' type="col"') : "";
            return `<pivot>${this.groupField(row, ' type="row"')}${colField}${measure}</pivot>`;
        }
        const order = block.order ? ` order="${escapeAttr(block.order)}"` : "";
        const groups = block.groupby.map((g) => this.groupField(g)).join("");
        const stacked = block.stacked ? 1 : 0;
        const cumulated = block.cumulative ? 1 : 0;
        return `<graph type="${this.graphMode}" stacked="${stacked}" cumulated="${cumulated}"${order}>${groups}${measure}</graph>`;
    }

    get viewProps() {
        return {
            resModel: this.props.block.model,
            type: this.state.view,
            arch: this.arch,
            fields: this.state.fields,
            domain: this.props.block.domain || [],
            // The pivot's own way to show several measures, including the record count.
            context:
                this.state.view === "pivot" && this.props.block.measures?.length > 1
                    ? { pivot_measures: this.props.block.measures }
                    : {},
            display: { controlPanel: false },
            searchMenuTypes: [],
            loadIrFilters: false,
            noBreadcrumbs: true,
            useSampleModel: false,
            className: "o-ai-agent-chartView",
        };
    }

    setView(view) {
        this.state.view = view;
    }

    getImage() {
        const canvas = this.root.el?.querySelector("canvas");
        if (!canvas) {
            return "";
        }
        // Paint the transparent chart on white so it prints well.
        const copy = document.createElement("canvas");
        copy.width = canvas.width;
        copy.height = canvas.height;
        const context = copy.getContext("2d");
        context.fillStyle = "#ffffff";
        context.fillRect(0, 0, copy.width, copy.height);
        context.drawImage(canvas, 0, 0);
        return copy.toDataURL("image/png");
    }

    openInOdoo() {
        const block = this.props.block;
        const [row, col] = block.groupby;
        const views =
            this.state.view === "pivot"
                ? [[false, "pivot"], [false, "graph"], [false, "list"]]
                : [[false, "graph"], [false, "pivot"], [false, "list"]];
        this.action.doAction({
            type: "ir.actions.act_window",
            name: block.title,
            res_model: block.model,
            domain: block.domain || [],
            context: {
                graph_measure: block.measure,
                graph_mode: this.graphMode,
                graph_groupbys: block.groupby,
                graph_order: block.order || null,
                graph_stacked: Boolean(block.stacked),
                pivot_measures: [block.measure],
                pivot_row_groupby: [row],
                pivot_column_groupby: col ? [col] : [],
                group_by: block.groupby,
            },
            views: [...views, [false, "form"]],
        });
    }
}

export class AiAgentAction extends Component {
    static template = "odoo_ai_agent.Action";
    static props = ["message", "block"];

    setup() {
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({ busy: false });
    }

    get expired() {
        const block = this.props.block;
        return block.status === "pending" && new Date(block.expires.replace(" ", "T") + "Z") < new Date();
    }

    async decide(confirm) {
        if (this.state.busy) {
            return;
        }
        this.state.busy = true;
        try {
            const block = await rpc("/ai_agent/action", {
                message_id: this.props.message.id,
                block_id: this.props.block.id,
                confirm,
            });
            // The bus also updates the message; apply the result at once.
            const payload = this.props.message.ai_agent_payload;
            const index = payload.blocks.findIndex((b) => b.id === block.id);
            payload.blocks.splice(index, 1, block);
            this.props.message.ai_agent_payload = { ...payload };
            if (block.client_action && block.status === "done") {
                await this.action.doAction(block.client_action);
            }
        } catch (error) {
            this.notification.add(error?.data?.message || _t("The change could not be applied."), {
                type: "danger",
            });
        } finally {
            this.state.busy = false;
        }
    }

    openResult() {
        const block = this.props.block;
        const ids = block.result_ids || [];
        this.action.doAction(
            ids.length === 1
                ? {
                      type: "ir.actions.act_window",
                      res_model: block.model,
                      res_id: ids[0],
                      views: [[false, "form"]],
                  }
                : {
                      type: "ir.actions.act_window",
                      name: block.model_label,
                      res_model: block.model,
                      domain: [["id", "in", ids]],
                      views: [
                          [false, "list"],
                          [false, "form"],
                      ],
                  }
        );
    }
}

/** App cards: the app's own icon, opening the app or one of its menus. */
export class AiAgentApps extends Component {
    static template = "odoo_ai_agent.Apps";
    static props = ["message", "block"];

    setup() {
        this.menu = useService("menu");
        this.action = useService("action");
    }

    open(app) {
        const menu = this.menu.getMenu(app.menu_id);
        if (menu?.actionID) {
            this.menu.selectMenu(menu);
        } else if (app.action_id) {
            this.action.doAction(app.action_id, { clearBreadcrumbs: true });
        }
    }
}

export class AiAgentBlocks extends Component {
    static template = "odoo_ai_agent.Blocks";
    static components = { AiAgentTable, AiAgentRecord, AiAgentChart, AiAgentAction, AiAgentApps };
    static props = ["message", "thread?"];

    setup() {
        this.state = useState({ showSteps: false, showData: false });
    }

    get payload() {
        return this.props.message.ai_agent_payload || {};
    }

    get blocks() {
        return this.payload.blocks || [];
    }

    /** What the AI chose to show (everything, for answers made before "present" existed). */
    get visibleBlocks() {
        return this.blocks.filter((block) => !block.collapsed);
    }

    /** Intermediate results, folded under "Data used". */
    get dataUsed() {
        return this.blocks.filter((block) => block.collapsed);
    }

    get steps() {
        return this.payload.steps || [];
    }

    get failedSteps() {
        return this.steps.filter((step) => step.status === "error").length;
    }

    get myReaction() {
        const self = this.props.message.store.self;
        for (const reaction of this.props.message.reactions || []) {
            if (["👍", "👎"].includes(reaction.content) && reaction.personas?.some((p) => p.eq?.(self))) {
                return reaction.content;
            }
        }
        return null;
    }

    async feedback(content) {
        const message = this.props.message;
        const current = this.myReaction;
        if (current && current !== content) {
            const reaction = message.reactions.find((r) => r.content === current);
            await reaction?.remove();
        }
        if (current === content) {
            await message.reactions.find((r) => r.content === content)?.remove();
        } else {
            await message.react(content);
        }
    }
}
