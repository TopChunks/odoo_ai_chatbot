import { Component, onWillStart, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { chartPixelRatio } from "./visualization";

const PERIODS = [
    { key: "7d", label: _t("7 days") },
    { key: "30d", label: _t("30 days") },
    { key: "90d", label: _t("90 days") },
    { key: "all", label: _t("All time") },
];

// Chart metrics; their bars are drawn in the theme's colours (Bootstrap's CSS variables).
const METRICS = {
    cost: { label: _t("Cost"), color: "--primary" },
    tokens: { label: _t("Tokens"), color: "--info" },
    calls: { label: _t("API calls"), color: "--success" },
};

/** A light tint of any CSS colour: the browser normalises it to rgb(), then a quarter of its opacity is kept. */
function translucent(color) {
    const probe = new Option().style;
    probe.color = color;
    return probe.color.replace(/rgba?\(([^,]+),([^,]+),([^,)]+)[^)]*\)/, "rgba($1,$2,$3,0.25)");
}

export class UsageDashboard extends Component {
    static template = "odoo_ai_assistant.UsageDashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.periods = PERIODS;
        this.metrics = METRICS;
        this.state = useState({ period: "30d", metric: "cost", breakdown: "by_user", data: null, loading: true });
        this.canvasRef = useRef("chart");
        this.chart = null;
        onWillStart(async () => {
            await loadBundle("web.chartjs_lib");
            await this.load();
        });
        useEffect(() => this.renderChart(), () => [this.state.data, this.state.metric]);
        onWillUnmount(() => this.chart?.destroy());
    }

    async load(period = this.state.period) {
        this.state.loading = true;
        try {
            this.state.data = await this.orm.call("odoo.ai.assistant.usage", "dashboard_data", [period]);
            this.state.period = period;
        } finally {
            this.state.loading = false;
        }
    }

    selectMetric(metric) {
        this.state.metric = metric;
    }

    renderChart() {
        const canvas = this.canvasRef.el;
        this.chart?.destroy();
        this.chart = null;
        const daily = this.state.data?.daily || [];
        if (!canvas || !daily.length) {
            return;
        }
        const metric = METRICS[this.state.metric];
        const color = getComputedStyle(canvas).getPropertyValue(metric.color).trim();
        const format = (value) => this.formatMetric(this.state.metric, value);
        this.chart = new Chart(canvas, {
            type: "bar",
            data: {
                labels: daily.map((day) => this.formatDay(day.date)),
                datasets: [{
                    label: metric.label,
                    data: daily.map((day) => day[this.state.metric]),
                    // Fades from the theme colour at the top of each bar to a light tint at its base.
                    backgroundColor: ({ chart }) => {
                        const gradient = chart.ctx.createLinearGradient(0, chart.chartArea?.top || 0, 0, chart.chartArea?.bottom || 1);
                        gradient.addColorStop(0, color);
                        gradient.addColorStop(1, translucent(color));
                        return gradient;
                    },
                    borderRadius: 6,
                    maxBarThickness: 28,
                }],
            },
            options: {
                maintainAspectRatio: false,
                devicePixelRatio: chartPixelRatio(),
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { label: (item) => `${metric.label}: ${format(item.raw)}` } },
                },
                scales: {
                    x: { grid: { display: false }, ticks: { autoSkip: true, maxRotation: 0 } },
                    y: { beginAtZero: true, ticks: { callback: (value) => format(value) } },
                },
            },
        });
    }

    formatDay(value) {
        return new Date(`${value}T00:00:00`).toLocaleDateString([], { month: "short", day: "numeric" });
    }

    formatNumber(value) {
        return new Intl.NumberFormat([], { notation: value >= 100000 ? "compact" : "standard" }).format(value || 0);
    }

    /** US dollars with Odoo's USD symbol and position; small amounts keep enough decimals to be seen. */
    formatMoney(value) {
        const amount = new Intl.NumberFormat([], {
            minimumFractionDigits: 2, maximumFractionDigits: Math.abs(value || 0) < 1 ? 4 : 2,
        }).format(value || 0);
        const { symbol, position } = this.state.data.currency;
        return position === "after" ? `${amount} ${symbol}` : `${symbol}${amount}`;
    }

    formatMetric(metric, value) {
        return metric === "cost" ? this.formatMoney(value) : this.formatNumber(value);
    }

    change(key) {
        const previous = this.state.data?.previous?.[key];
        const current = this.state.data?.totals[key];
        if (!previous) {
            return null;
        }
        const percent = ((current - previous) / previous) * 100;
        return { up: percent >= 0, text: `${percent >= 0 ? "+" : ""}${percent.toFixed(0)}%` };
    }

    get cards() {
        const totals = this.state.data.totals;
        return [
            { key: "cost", label: _t("Total cost"), icon: "fa-dollar", value: this.formatMoney(totals.cost),
              detail: totals.answers ? _t("%s per answer", this.formatMoney(totals.cost / totals.answers)) : _t("No answers yet") },
            { key: "answers", label: _t("Answers"), icon: "fa-magic", value: this.formatNumber(totals.answers),
              detail: _t("%s failed calls", this.formatNumber(totals.failed_calls)) },
            { key: "tokens", label: _t("Tokens"), icon: "fa-bolt", value: this.formatNumber(totals.tokens),
              detail: _t("%(input)s in · %(output)s out", {
                  input: this.formatNumber(totals.input_tokens), output: this.formatNumber(totals.output_tokens),
              }) },
            { key: "active_users", label: _t("Active users"), icon: "fa-users", value: this.formatNumber(totals.active_users),
              detail: _t("Users who asked the AI") },
        ];
    }

    /** Who gets answers with the fewest tokens, and who costs the most, among this period's users. */
    get highlights() {
        const users = (this.state.data?.by_user || []).filter((row) => row.person && row.answers);
        if (!users.length) {
            return [];
        }
        const efficient = users.reduce((best, row) => (row.tokens / row.answers < best.tokens / best.answers ? row : best));
        const spender = users.reduce((top, row) => (row.cost > top.cost ? row : top));
        return [
            { key: "efficient", icon: "fa-trophy", title: _t("Most efficient user"), name: efficient.label,
              detail: _t("%s tokens per answer", this.formatNumber(Math.round(efficient.tokens / efficient.answers))) },
            { key: "spender", icon: "fa-line-chart", title: _t("Highest spend"), name: spender.label,
              detail: _t("%(cost)s for %(answers)s answers", {
                  cost: this.formatMoney(spender.cost), answers: this.formatNumber(spender.answers),
              }) },
        ];
    }

    get comparisonTitle() {
        const period = PERIODS.find((entry) => entry.key === this.state.period);
        return _t("Compared with the previous %s", period?.label || "");
    }

    get breakdowns() {
        return [
            ["by_user", _t("User")], ["by_model", _t("Model")],
            ["by_provider", _t("Provider")], ["by_purpose", _t("Request type")],
        ];
    }

    get breakdownRows() {
        return this.state.data?.[this.state.breakdown] || [];
    }

    /** A row's share of the period's cost, or of its tokens when no cost is known. */
    share(row) {
        const totals = this.state.data.totals;
        const [part, whole] = totals.cost > 0 ? [row.cost, totals.cost] : [row.tokens, totals.tokens];
        return whole > 0 ? Math.round((part / whole) * 100) : 0;
    }

    openLog() {
        return this.action.doAction("odoo_ai_assistant.usage_action");
    }
}

registry.category("actions").add("odoo_ai_assistant.usage_dashboard", UsageDashboard);
