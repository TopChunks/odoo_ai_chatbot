import { Component, useEffect, useRef, useState } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";
import { cookie } from "@web/core/browser/cookie";
import { getColor, getCustomColor } from "@web/core/colors/colors";
import { _t } from "@web/core/l10n/translation";

const CHART_TYPES = new Set(["bar", "line", "pie", "doughnut", "scatter"]);
// Odoo's graph-view palette and text/grid colours, so a chat chart looks like Odoo's own charts in light and dark mode.
const colorScheme = cookie.get("color_scheme");
const LABEL_COLOR = getCustomColor(colorScheme, "#111827", "#E4E4E4");
const GRID_COLOR = getCustomColor(colorScheme, "rgba(0,0,0,.1)", "rgba(255,255,255,.15)");
const isObject = (value) =>
    value !== null && typeof value === "object" && !Array.isArray(value) &&
    (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
const sameJson = (left, right) => {
    if (left === right) {
        return true;
    }
    if (!left || !right || typeof left !== "object" || typeof right !== "object" ||
        Array.isArray(left) !== Array.isArray(right)) {
        return false;
    }
    const keys = Object.keys(left);
    return keys.length === Object.keys(right).length &&
        keys.every((key) => Object.hasOwn(right, key) && sameJson(left[key], right[key]));
};

/** Draw charts at least at 2x so axis text stays sharp on scaled or zoomed displays. */
export function chartPixelRatio() {
    return Math.min(3, Math.max(2, globalThis.devicePixelRatio || 1));
}

export function visualizationPayload(payload) {
    if (!isObject(payload)) {
        return null;
    }
    if (payload.kind === "table" && typeof payload.title === "string" &&
        Array.isArray(payload.columns) && payload.columns.length &&
        payload.columns.every((column) =>
            isObject(column) && typeof column.key === "string" && typeof column.label === "string") &&
        Array.isArray(payload.rows) && payload.rows.every(isObject)) {
        return payload;
    }
    if (payload.kind === "chart" && typeof payload.title === "string" &&
        CHART_TYPES.has(payload.chart_type) && typeof payload.value_label === "string" &&
        Array.isArray(payload.labels) && payload.labels.every((label) => typeof label === "string") &&
        Array.isArray(payload.values) && payload.values.every(Number.isFinite) &&
        payload.labels.length === payload.values.length) {
        return payload;
    }
    if (payload.kind === "mcp_request" && Number.isSafeInteger(payload.request_id) && payload.request_id > 0 &&
        ["pending", "done", "cancelled", "shared"].includes(payload.state) &&
        typeof payload.title === "string" && typeof payload.details === "string" &&
        typeof payload.mutation === "boolean" && typeof payload.sharing_allowed === "boolean" &&
        (!payload.summary || (isObject(payload.summary) &&
            typeof payload.summary.headline === "string" && typeof payload.summary.subject === "string" &&
            Array.isArray(payload.summary.targets) &&
            payload.summary.targets.every((target) => typeof target === "string") &&
            Array.isArray(payload.summary.groups) && payload.summary.groups.every((group) => isObject(group) &&
                typeof group.title === "string" && Array.isArray(group.items) && group.items.every((item) =>
                    isObject(item) && typeof item.label === "string" && typeof item.value === "string")))) &&
        Array.isArray(payload.images) && payload.images.every((image) => isObject(image) &&
            ["image/png", "image/jpeg", "image/gif", "image/webp"].includes(image.mimeType) &&
            typeof image.data === "string" &&
            /^[A-Za-z0-9+/]*={0,2}$/.test(image.data)) &&
        (!payload.table || visualizationPayload(payload.table)) &&
        Array.isArray(payload.links) && payload.links.every((link) => isObject(link) &&
            typeof link.label === "string" && typeof link.url === "string" &&
            // A downloadable attachment, or a changed record's page.
            /^\/web\/content\/[a-zA-Z0-9_./]+(\?download=true)?$|^\/odoo\/[a-z0-9_.]+\/\d+$/.test(link.url))) {
        return payload;
    }
    return null;
}

export function visualizationContent(message) {
    const content = message.content || "";
    if (!visualizationPayload(message.payload) || typeof content !== "string") {
        return content;
    }
    try {
        const parsed = JSON.parse(content);
        if (sameJson(parsed, message.payload)) {
            return "";
        }
    } catch {
        // Ordinary prose remains visible below the structured result.
    }
    return content;
}

export class LocalVisualization extends Component {
    static template = "odoo_ai_assistant.LocalVisualization";
    static props = {
        payload: Object,
        busy: Boolean,
        onConfirm: Function,
        onCancel: Function,
        onPage: { type: Function, optional: true },
        onShare: { type: Function, optional: true },
        onExport: { type: Function, optional: true },
    };

    get hasRecordCount() {
        return this.props.payload.kind === "table" && Number.isSafeInteger(this.props.payload.total) &&
            this.props.payload.total >= 0;
    }

    get planState() {
        return {
            pending: _t("Pending confirmation"),
            done: _t("Completed"),
            cancelled: _t("Cancelled"),
            shared: _t("Shared with the configured AI provider"),
        }[this.props.payload.state];
    }

    setup() {
        this.canvasRef = useRef("chart");
        this.status = useState({ chartError: "" });
        this.chart = null;
        useEffect(() => {
            let cancelled = false;
            this.status.chartError = "";
            if (this.props.payload.kind === "chart" && this.canvasRef.el && !this.chartNotice) {
                const canvas = this.canvasRef.el;
                const config = this.chartConfig;
                const render = async () => {
                    let phase = _t("Unable to load chart library");
                    try {
                        await loadBundle("web.chartjs_lib");
                        if (cancelled) {
                            return;
                        }
                        phase = _t("Unable to render chart");
                        this.chart = new Chart(canvas, config);
                    } catch (error) {
                        if (!cancelled) {
                            this.destroyChart();
                            // Chart.js may register an instance before its constructor fails.
                            if (typeof Chart !== "undefined") {
                                Chart.getChart?.(canvas)?.destroy();
                            }
                            this.status.chartError = `${phase}: ${error.message || String(error)}`;
                        }
                    }
                };
                render();
            }
            return () => {
                cancelled = true;
                this.destroyChart();
            };
        }, () => [this.props.payload]);
    }

    /** Download this chart or table; a PDF also carries the chart exactly as drawn here. */
    exportAs(format) {
        const image = format === "pdf" && this.chart ? this.chart.toBase64Image().split(",")[1] : null;
        return this.props.onExport(format, image);
    }

    get chartNotice() {
        const payload = this.props.payload;
        if (payload.kind !== "chart") {
            return "";
        }
        if (!payload.values.length) {
            return _t("No data available to plot.");
        }
        if (["pie", "doughnut"].includes(payload.chart_type)) {
            if (payload.values.some((value) => value < 0)) {
                return _t("Pie and doughnut charts cannot represent negative values. Use the data table below.");
            }
            if (payload.values.every((value) => value === 0)) {
                return _t("All values are zero; there are no slices to plot. Use the data table below.");
            }
        }
        return "";
    }

    destroyChart() {
        if (this.chart) {
            this.chart.destroy();
            this.chart = null;
        }
    }

    cell(row, key) {
        const value = Object.hasOwn(row, key) ? row[key] : null;
        if (value === null || value === undefined) {
            return "";
        }
        // A [value, label] pair: a linked record's [id, name] or a grouped date's [date, "June 2026"].
        if (Array.isArray(value) && value.length === 2 && typeof value[1] === "string") {
            return value[1];
        }
        if (typeof value === "object") {
            try {
                return JSON.stringify(value);
            } catch {
                return _t("Unsupported value");
            }
        }
        return String(value);
    }

    get chartConfig() {
        const payload = this.props.payload;
        const circular = ["pie", "doughnut"].includes(payload.chart_type);
        const scatter = payload.chart_type === "scatter";
        const horizontal = this.horizontalBars;
        const count = payload.operation === "count";
        const categoryAxis = {
            type: "category",
            grid: { display: false },
            ticks: { color: LABEL_COLOR, font: { size: 13 }, autoSkip: !horizontal, maxRotation: 30 },
        };
        const valueAxis = {
            beginAtZero: payload.chart_type === "bar",
            grid: { color: GRID_COLOR },
            ticks: { color: LABEL_COLOR, font: { size: 13 }, ...(count ? { precision: 0 } : {}) },
            title: { display: true, text: payload.value_label, color: LABEL_COLOR, font: { size: 13 } },
        };
        return {
            type: payload.chart_type,
            data: {
                labels: [...payload.labels],
                datasets: [{
                    label: payload.value_label,
                    // No numeric X series is provided: scatter uses label positions, not invented data.
                    data: scatter
                        ? payload.values.map((value, index) => ({ x: index, y: value }))
                        : [...payload.values],
                    backgroundColor: circular
                        ? payload.values.map((_, index) => getColor(index, colorScheme, payload.values.length))
                        : getColor(0, colorScheme, 1),
                    borderColor: circular ? undefined : getColor(0, colorScheme, 1),
                    borderWidth: 2,
                    ...(payload.chart_type === "bar" ? { borderRadius: 4, maxBarThickness: 36 } : {}),
                    ...(payload.chart_type === "line" ? { pointRadius: 4, pointHoverRadius: 6 } : {}),
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                devicePixelRatio: chartPixelRatio(),
                font: { family: "Arial, sans-serif", size: 13 },
                indexAxis: horizontal ? "y" : "x",
                layout: { padding: { top: 8, right: 16, bottom: 8, left: 8 } },
                plugins: {
                    legend: {
                        display: circular, position: "bottom",
                        labels: { color: LABEL_COLOR, font: { size: 13 }, padding: 16, boxWidth: 14, boxHeight: 14 },
                    },
                    tooltip: {
                        callbacks: scatter ? {
                            title: (items) => payload.labels[items[0]?.parsed.x] || "",
                            label: (item) => `${payload.value_label}: ${item.parsed.y}`,
                        } : {},
                    },
                },
                scales: circular ? {} : {
                    x: horizontal ? valueAxis : categoryAxis,
                    y: horizontal ? categoryAxis : valueAxis,
                },
            },
        };
    }

    get horizontalBars() {
        const payload = this.props.payload;
        return payload.chart_type === "bar" &&
            (payload.labels.length > 6 || payload.labels.some((label) => label.length > 18));
    }

    get chartHeight() {
        return this.horizontalBars ? `${Math.max(320, this.props.payload.labels.length * 38 + 64)}px` : "340px";
    }
}
