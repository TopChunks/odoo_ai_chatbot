import { Component, onWillStart, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";
import { loadBundle } from "@web/core/assets";
import { cookie } from "@web/core/browser/cookie";
import { getColor } from "@web/core/colors/colors";
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

const colorScheme = cookie.get("color_scheme");
const METRICS = {
    tokens: { label: _t("Tokens"), color: getColor(0, colorScheme, 2) },
    calls: { label: _t("API calls"), color: getColor(1, colorScheme, 2) },
};

export class UsageDashboard extends Component {
    static template = "odoo_ai_assistant.UsageDashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.periods = PERIODS;
        this.metrics = METRICS;
        this.state = useState({ period: "30d", metric: "tokens", breakdown: "by_model", data: null, loading: true });
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
        this.chart = new Chart(canvas, {
            type: "bar",
            data: {
                labels: daily.map((day) => this.formatDay(day.date)),
                datasets: [{
                    label: metric.label,
                    data: daily.map((day) => day[this.state.metric]),
                    backgroundColor: metric.color,
                    borderRadius: 4,
                    maxBarThickness: 28,
                }],
            },
            options: {
                maintainAspectRatio: false,
                devicePixelRatio: chartPixelRatio(),
                plugins: {
                    legend: { display: false },
                    tooltip: { callbacks: { label: (item) => `${metric.label}: ${this.formatNumber(item.raw)}` } },
                },
                scales: {
                    x: { grid: { display: false }, ticks: { autoSkip: true, maxRotation: 0 } },
                    y: { beginAtZero: true, ticks: { callback: (value) => this.formatNumber(value) } },
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
            { key: "tokens", label: _t("Tokens"), value: this.formatNumber(totals.tokens),
              detail: _t("%(input)s in · %(output)s out", {
                  input: this.formatNumber(totals.input_tokens), output: this.formatNumber(totals.output_tokens),
              }) },
            { key: "calls", label: _t("API calls"), value: this.formatNumber(totals.calls),
              detail: _t("%s failed", this.formatNumber(totals.failed_calls)) },
            { key: "active_users", label: _t("Active users"), value: this.formatNumber(totals.active_users),
              detail: _t("Users who made requests") },
        ];
    }

    get comparisonTitle() {
        const period = PERIODS.find((entry) => entry.key === this.state.period);
        return _t("Compared with the previous %s", period?.label || "");
    }

    get breakdowns() {
        return [
            ["by_model", _t("Model")], ["by_provider", _t("Provider")],
            ["by_user", _t("User")], ["by_purpose", _t("Request type")],
        ];
    }

    get breakdownRows() {
        return this.state.data?.[this.state.breakdown] || [];
    }

    share(row) {
        const totals = this.state.data.totals;
        const base = totals.tokens > 0 ? row.tokens / totals.tokens : totals.calls > 0 ? row.calls / totals.calls : 0;
        return Math.round(base * 100);
    }

    openLog() {
        return this.action.doAction("odoo_ai_assistant.usage_action");
    }
}

registry.category("actions").add("odoo_ai_assistant.usage_dashboard", UsageDashboard);
