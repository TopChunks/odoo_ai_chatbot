import { Component, onMounted, onPatched, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";

import { loadBundle } from "@web/core/assets";
import { cookie } from "@web/core/browser/cookie";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

// Validated categorical palette (fixed order; light and dark steps chosen per surface).
const PALETTE = {
    light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
};

function scheme() {
    return cookie.get("color_scheme") === "dark" ? "dark" : "light";
}

export function formatCost(value) {
    const amount = Number(value || 0);
    if (amount === 0) {
        return "$0";
    }
    if (amount < 0.01) {
        return `$${amount.toFixed(4)}`;
    }
    return `$${amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatCompact(value) {
    return Number(value || 0).toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 });
}

/** One Chart.js canvas; rebuilt whenever its config object changes. */
class DashboardChart extends Component {
    static template = "odoo_ai_agent.DashboardChart";
    static props = ["config", "height?"];

    setup() {
        this.canvas = useRef("canvas");
        this.chart = null;
        this.lastConfig = null;
        onMounted(() => this.render());
        onPatched(() => this.render());
        onWillUnmount(() => this.chart?.destroy());
    }

    render() {
        if (this.lastConfig === this.props.config) {
            return;
        }
        this.lastConfig = this.props.config;
        this.chart?.destroy();
        this.chart = new window.Chart(this.canvas.el, this.props.config);
    }
}

export class AiAgentUsageDashboard extends Component {
    static template = "odoo_ai_agent.UsageDashboard";
    static components = { DashboardChart };
    static props = ["*"];

    setup() {
        this.action = useService("action");
        this.periods = [
            { days: 7, label: _t("7 days") },
            { days: 30, label: _t("30 days") },
            { days: 90, label: _t("90 days") },
            { days: 365, label: _t("12 months") },
        ];
        this.state = useState({
            days: 30,
            provider_id: false,
            model_id: false,
            user_id: false,
            data: null,
            loading: true,
            sortKey: "tokens",
            sortDesc: true,
        });
        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
            await this.load();
        });
    }

    async load() {
        this.state.loading = true;
        const { days, provider_id, model_id, user_id } = this.state;
        this.state.data = await rpc("/ai_agent/dashboard", { days, provider_id, model_id, user_id });
        this.charts = this.buildCharts(this.state.data);
        this.state.loading = false;
    }

    setPeriod(days) {
        this.state.days = days;
        this.load();
    }

    setFilter(name, value) {
        this.state[name] = value ? parseInt(value) : false;
        this.load();
    }

    // ------------------------------------------------------------------
    // KPIs
    // ------------------------------------------------------------------
    get kpis() {
        const k = this.state.data.kpis;
        const p = k.previous;
        return [
            { label: _t("Total cost"), value: formatCost(k.cost), delta: this.delta(k.cost, p.cost), icon: "fa-usd" },
            {
                label: _t("Tokens"),
                value: formatCompact(k.tokens),
                delta: this.delta(k.tokens, p.tokens),
                icon: "fa-cubes",
                hint: _t("%(input)s in · %(output)s out", {
                    input: formatCompact(k.input_tokens),
                    output: formatCompact(k.output_tokens),
                }),
            },
            { label: _t("Questions"), value: formatCompact(k.questions), delta: this.delta(k.questions, p.questions), icon: "fa-comments-o" },
            { label: _t("Active users"), value: k.users, delta: this.delta(k.users, p.users), icon: "fa-users" },
            { label: _t("Cost per question"), value: formatCost(k.avg_cost), delta: this.delta(k.avg_cost, p.avg_cost), icon: "fa-calculator" },
            {
                label: _t("Answered"),
                value: `${k.success_rate.toFixed(1)}%`,
                delta: this.delta(k.success_rate, p.success_rate, true),
                icon: "fa-check-circle-o",
                good: true,
            },
            { label: _t("Avg. answer time"), value: `${k.avg_duration.toFixed(1)} s`, delta: this.delta(k.avg_duration, p.avg_duration), icon: "fa-clock-o" },
            {
                label: _t("Helpful answers"),
                value: k.helpful_rate === null ? "—" : `${k.helpful_rate.toFixed(0)}%`,
                hint: _t("%s 👍/👎 votes", k.feedback_count),
                icon: "fa-thumbs-o-up",
            },
        ];
    }

    delta(current, previous, points = false) {
        if (!previous) {
            return null;
        }
        if (points) {
            const diff = current - previous;
            return { up: diff >= 0, text: `${diff >= 0 ? "+" : ""}${diff.toFixed(1)} pts` };
        }
        const change = ((current - previous) / previous) * 100;
        return { up: change >= 0, text: `${change >= 0 ? "+" : ""}${change.toFixed(0)}%` };
    }

    // ------------------------------------------------------------------
    // Charts
    // ------------------------------------------------------------------
    baseOptions({ horizontal = false, stacked = false, legend = false, money = false } = {}) {
        const dark = scheme() === "dark";
        const grid = dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)";
        const ink = dark ? "#c3c2b7" : "#52514e";
        const valueAxis = {
            beginAtZero: true,
            stacked,
            grid: { color: grid },
            border: { display: false },
            ticks: {
                color: ink,
                maxRotation: 0,
                maxTicksLimit: 6,
                callback: (v) => (money ? formatCost(v) : formatCompact(v)),
            },
        };
        const categoryAxis = {
            stacked,
            grid: { display: false },
            border: { display: false },
            ticks: { color: ink, autoSkip: true, maxRotation: 0 },
        };
        return {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: horizontal ? "y" : "x",
            interaction: { mode: "index", intersect: false },
            plugins: {
                legend: { display: legend, position: "top", align: "end", labels: { color: ink, boxWidth: 10, boxHeight: 10 } },
                tooltip: {
                    callbacks: {
                        label: (ctx) => {
                            const value = horizontal ? ctx.parsed.x : ctx.parsed.y;
                            const text = money ? formatCost(value) : value.toLocaleString();
                            return ctx.dataset.label ? `${ctx.dataset.label}: ${text}` : text;
                        },
                    },
                },
            },
            scales: horizontal ? { x: valueAxis, y: categoryAxis } : { x: categoryAxis, y: valueAxis },
        };
    }

    bar(labels, datasets, options = {}) {
        const colors = PALETTE[scheme()];
        return {
            type: "bar",
            data: {
                labels,
                datasets: datasets.map((dataset, index) => ({
                    borderRadius: 4,
                    borderSkipped: "start",
                    maxBarThickness: 26,
                    backgroundColor: colors[dataset.slot ?? index],
                    ...dataset,
                })),
            },
            options: this.baseOptions(options),
        };
    }

    /** Top N rows, the rest folded into "Other" (never a generated 9th color). */
    top(rows, key, n = 8) {
        const sorted = [...rows].sort((a, b) => b[key] - a[key]);
        if (sorted.length <= n) {
            return sorted;
        }
        const rest = sorted.slice(n - 1);
        return [
            ...sorted.slice(0, n - 1),
            { name: _t("Other"), [key]: rest.reduce((sum, row) => sum + row[key], 0) },
        ];
    }

    buildCharts(data) {
        const days = data.daily.map((d) => d.date.slice(5));
        const byModel = this.top(data.by_model, "cost");
        const byProvider = this.top(data.by_provider, "tokens");
        const users = this.top(data.users, "tokens");
        const tools = data.tools;
        return {
            cost: this.bar(days, [{ label: _t("Cost"), data: data.daily.map((d) => d.cost) }], { money: true }),
            tokens: this.bar(
                days,
                [
                    { label: _t("Input tokens"), data: data.daily.map((d) => d.input), slot: 0 },
                    { label: _t("Output tokens"), data: data.daily.map((d) => d.output), slot: 1 },
                ],
                { stacked: true, legend: true }
            ),
            models: this.bar(byModel.map((m) => m.name), [{ label: _t("Cost"), data: byModel.map((m) => m.cost) }], {
                horizontal: true,
                money: true,
            }),
            providers: this.bar(
                byProvider.map((p) => p.name),
                [{ label: _t("Tokens"), data: byProvider.map((p) => p.tokens) }],
                { horizontal: true }
            ),
            users: this.bar(users.map((u) => u.name), [{ label: _t("Tokens"), data: users.map((u) => u.tokens) }], {
                horizontal: true,
            }),
            hours: this.bar(
                data.hourly.map((_v, h) => `${String(h).padStart(2, "0")}h`),
                [{ label: _t("Questions"), data: data.hourly }]
            ),
            tools: this.bar(tools.map((t) => t.name), [{ label: _t("Calls"), data: tools.map((t) => t.count) }], {
                horizontal: true,
            }),
        };
    }

    // ------------------------------------------------------------------
    // Leaderboard
    // ------------------------------------------------------------------
    get leaderboard() {
        const { sortKey, sortDesc } = this.state;
        const rows = [...this.state.data.users];
        rows.sort((a, b) => {
            const left = a[sortKey] ?? -1;
            const right = b[sortKey] ?? -1;
            return (left > right ? 1 : left < right ? -1 : 0) * (sortDesc ? -1 : 1);
        });
        return rows;
    }

    sortBy(key) {
        if (this.state.sortKey === key) {
            this.state.sortDesc = !this.state.sortDesc;
        } else {
            this.state.sortKey = key;
            this.state.sortDesc = true;
        }
    }

    sortIcon(key) {
        if (this.state.sortKey !== key) {
            return "";
        }
        return this.state.sortDesc ? "fa fa-caret-down" : "fa fa-caret-up";
    }

    efficiencyClass(score) {
        return score >= 75 ? "bg-success" : score >= 50 ? "bg-warning" : "bg-danger";
    }

    formatCost(value) {
        return formatCost(value);
    }

    formatCompact(value) {
        return formatCompact(value);
    }

    openAnalysis() {
        this.action.doAction("odoo_ai_agent.ai_agent_usage_action");
    }

    openUser(user) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: _t("AI usage of %s", user.name),
            res_model: "ai_agent.usage",
            domain: [["user_id", "=", user.id]],
            views: [
                [false, "list"],
                [false, "pivot"],
                [false, "graph"],
                [false, "form"],
            ],
        });
    }
}

registry.category("actions").add("odoo_ai_agent.usage_dashboard", AiAgentUsageDashboard);
