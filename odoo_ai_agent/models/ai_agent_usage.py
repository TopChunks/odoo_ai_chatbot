from collections import defaultdict
from datetime import timedelta
from statistics import median

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

FEEDBACK_UP = "👍"
FEEDBACK_DOWN = "👎"


class AiAgentUsage(models.Model):
    """One row per question answered by the AI (all provider calls of that answer summed)."""

    _name = "ai_agent.usage"
    _description = "AI Usage"
    _order = "date desc, id desc"
    _rec_name = "date"

    date = fields.Datetime(default=fields.Datetime.now, required=True, index=True, readonly=True)
    user_id = fields.Many2one("res.users", required=True, index=True, readonly=True, ondelete="cascade")
    channel_id = fields.Many2one("discuss.channel", string="Conversation", readonly=True, ondelete="set null")
    message_id = fields.Many2one("mail.message", string="Answer", readonly=True, ondelete="set null")
    provider_id = fields.Many2one("ai_agent.provider", readonly=True, ondelete="set null", index=True)
    model_id = fields.Many2one("ai_agent.model", string="AI Model", readonly=True, ondelete="set null", index=True)
    model_code = fields.Char("Model ID", readonly=True)
    request_count = fields.Integer("Provider calls", readonly=True, aggregator="sum")
    input_tokens = fields.Integer(readonly=True, aggregator="sum")
    output_tokens = fields.Integer(readonly=True, aggregator="sum")
    cached_tokens = fields.Integer("Cached input tokens", readonly=True, aggregator="sum")
    total_tokens = fields.Integer(compute="_compute_total_tokens", store=True, aggregator="sum")
    cost = fields.Float("Cost ($)", digits=(16, 6), readonly=True, aggregator="sum")
    duration = fields.Float("Duration (s)", digits=(16, 2), readonly=True, aggregator="avg")
    tool_call_count = fields.Integer("Tool calls", readonly=True, aggregator="sum")
    tool_error_count = fields.Integer("Failed tool calls", readonly=True, aggregator="sum")
    tools_used = fields.Char(readonly=True)
    state = fields.Selection(
        [("success", "Answered"), ("error", "Failed")], default="success", required=True, readonly=True, index=True
    )
    error_message = fields.Text(readonly=True)

    @api.depends("input_tokens", "output_tokens")
    def _compute_total_tokens(self):
        for usage in self:
            usage.total_tokens = usage.input_tokens + usage.output_tokens

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    @api.model
    def get_dashboard_data(self, days=30, provider_id=False, model_id=False, user_id=False):
        if not self.env.user.has_group("odoo_ai_agent.group_ai_agent_manager"):
            raise AccessError(_("Only AI Agent administrators can see the usage reports."))
        days = max(1, min(int(days or 30), 730))
        now = fields.Datetime.now()
        start = now - timedelta(days=days)
        previous_start = start - timedelta(days=days)
        filters = []
        if provider_id:
            filters.append(("provider_id", "=", int(provider_id)))
        if model_id:
            filters.append(("model_id", "=", int(model_id)))
        if user_id:
            filters.append(("user_id", "=", int(user_id)))
        current = self.search([("date", ">=", start)] + filters, order="date")
        previous = self.search([("date", ">=", previous_start), ("date", "<", start)] + filters)
        feedback = self._feedback(current)
        return {
            "currency": "$",
            "period": {"days": days, "start": fields.Datetime.to_string(start), "end": fields.Datetime.to_string(now)},
            "kpis": self._kpis(current, previous, feedback),
            "daily": self._daily(current, start, days),
            "by_model": self._breakdown(current, lambda u: u.model_id.display_name or u.model_code or _("Unknown")),
            "by_provider": self._breakdown(current, lambda u: u.provider_id.name or _("Unknown")),
            "users": self._users(current, feedback),
            "hourly": self._hourly(current),
            "tools": self._tools(current),
            "errors": [
                {"date": fields.Datetime.to_string(u.date), "user": u.user_id.name, "model": u.model_code or "",
                 "message": (u.error_message or "")[:240]}
                for u in current.filtered(lambda u: u.state == "error").sorted("date", reverse=True)[:6]
            ],
            "filters": {
                "providers": [{"id": p.id, "name": p.name} for p in self.env["ai_agent.provider"].search([])],
                "models": [
                    {"id": m.id, "name": m.display_name}
                    for m in self.with_context(active_test=False).search([]).model_id
                ],
                "users": [{"id": u.id, "name": u.name} for u in self.search([]).user_id],
            },
        }

    def _feedback(self, usages):
        """👍 / 👎 reactions the asking user left on each AI answer, keyed by usage id."""
        message_ids = usages.message_id.ids
        if not message_ids:
            return {}
        reactions = self.env["mail.message.reaction"].sudo().search([
            ("message_id", "in", message_ids), ("content", "in", [FEEDBACK_UP, FEEDBACK_DOWN]),
        ])
        by_message = defaultdict(dict)
        for reaction in reactions:
            by_message[reaction.message_id.id][reaction.partner_id.id] = reaction.content
        result = {}
        for usage in usages:
            content = by_message.get(usage.message_id.id, {}).get(usage.user_id.partner_id.id)
            if content:
                result[usage.id] = "up" if content == FEEDBACK_UP else "down"
        return result

    @staticmethod
    def _totals(usages):
        count = len(usages)
        success = len(usages.filtered(lambda u: u.state == "success"))
        tokens = sum(usages.mapped("total_tokens"))
        cost = sum(usages.mapped("cost"))
        return {
            "questions": count,
            "cost": cost,
            "tokens": tokens,
            "input_tokens": sum(usages.mapped("input_tokens")),
            "output_tokens": sum(usages.mapped("output_tokens")),
            "cached_tokens": sum(usages.mapped("cached_tokens")),
            "users": len(usages.user_id),
            "success_rate": (success / count * 100) if count else 0.0,
            "avg_cost": (cost / count) if count else 0.0,
            "avg_tokens": (tokens / count) if count else 0.0,
            "avg_duration": (sum(usages.mapped("duration")) / count) if count else 0.0,
            "tool_calls": sum(usages.mapped("tool_call_count")),
        }

    def _kpis(self, current, previous, feedback):
        now_totals, before = self._totals(current), self._totals(previous)
        votes = list(feedback.values())
        now_totals["helpful_rate"] = (votes.count("up") / len(votes) * 100) if votes else None
        now_totals["feedback_count"] = len(votes)
        now_totals["previous"] = before
        return now_totals

    @staticmethod
    def _daily(usages, start, days):
        buckets = {}
        first_day = fields.Date.to_date(start)
        for offset in range(days + 1):
            day = first_day + timedelta(days=offset)
            buckets[day] = {"date": fields.Date.to_string(day), "cost": 0.0, "input": 0, "output": 0, "questions": 0}
        for usage in usages:
            bucket = buckets.get(usage.date.date())
            if bucket:
                bucket["cost"] += usage.cost
                bucket["input"] += usage.input_tokens
                bucket["output"] += usage.output_tokens
                bucket["questions"] += 1
        return list(buckets.values())

    @staticmethod
    def _breakdown(usages, key):
        groups = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "questions": 0})
        for usage in usages:
            group = groups[key(usage)]
            group["cost"] += usage.cost
            group["tokens"] += usage.total_tokens
            group["questions"] += 1
        return sorted(
            ({"name": name, **values} for name, values in groups.items()),
            key=lambda g: (g["cost"], g["tokens"]), reverse=True,
        )

    def _users(self, usages, feedback):
        rows = []
        per_user = defaultdict(lambda: self.browse())
        for usage in usages:
            per_user[usage.user_id] |= usage
        averages = [
            sum(u.mapped("total_tokens")) / max(len(u.filtered(lambda x: x.state == "success")), 1)
            for u in per_user.values()
        ]
        team_median = median(averages) if averages else 0
        for user, records in per_user.items():
            totals = self._totals(records)
            votes = [feedback[r.id] for r in records if r.id in feedback]
            helpful = (votes.count("up") / len(votes)) if votes else None
            answered = len(records.filtered(lambda r: r.state == "success"))
            tokens_per_answer = totals["tokens"] / max(answered, 1)
            token_efficiency = min(team_median / tokens_per_answer, 1.5) / 1.5 if tokens_per_answer else 0
            success = totals["success_rate"] / 100
            score = 100 * (0.5 * success + 0.35 * token_efficiency + 0.15 * (helpful if helpful is not None else success))
            rows.append({
                "id": user.id,
                "name": user.name,
                "avatar": f"/web/image/res.users/{user.id}/avatar_128",
                **totals,
                "tokens_per_answer": tokens_per_answer,
                "helpful_rate": helpful * 100 if helpful is not None else None,
                "feedback_up": votes.count("up"),
                "feedback_down": votes.count("down"),
                "efficiency": round(score),
                "last_used": fields.Datetime.to_string(max(records.mapped("date"))),
            })
        return sorted(rows, key=lambda r: r["tokens"], reverse=True)

    def _hourly(self, usages):
        hours = [0] * 24
        in_user_tz = self.with_context(tz=self.env.user.tz or "UTC")
        for usage in usages:
            hours[fields.Datetime.context_timestamp(in_user_tz, usage.date).hour] += 1
        return hours

    @staticmethod
    def _tools(usages):
        counts = defaultdict(int)
        for usage in usages:
            for name in (usage.tools_used or "").split(","):
                if name.strip():
                    counts[name.strip()] += 1
        return sorted(({"name": k, "count": v} for k, v in counts.items()), key=lambda t: t["count"], reverse=True)[:10]
