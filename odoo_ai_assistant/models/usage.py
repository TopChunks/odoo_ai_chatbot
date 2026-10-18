import time
from datetime import datetime, time as day_time, timedelta

import pytz

from odoo import api, fields, models, SUPERUSER_ID, _
from odoo.exceptions import ValidationError

from .providers import PROVIDERS

# Dashboard periods in days; None means all recorded usage.
_DASHBOARD_PERIODS = {"7d": 7, "30d": 30, "90d": 90, "all": None}


class AIUsage(models.Model):
    _name = "odoo.ai.assistant.usage"
    _description = "AI API Usage"
    _order = "id desc"

    user_id = fields.Many2one("res.users", ondelete="set null", readonly=True)
    provider = fields.Selection(PROVIDERS, readonly=True)
    model_name = fields.Char(readonly=True)
    purpose = fields.Selection([
        ("scope", "Request check"), ("intent", "Business action check"), ("reply", "Chat response"),
        ("catalogue", "Model catalogue"),
        ("title", "Conversation title"),
    ], readonly=True)
    status = fields.Selection([
        ("pending", "Attempt started"), ("received", "Response received"),
        ("failed", "Request failed"),
    ], readonly=True)
    api_calls = fields.Integer(default=1, readonly=True)
    input_tokens = fields.Integer(readonly=True)
    output_tokens = fields.Integer(readonly=True)
    cached_tokens = fields.Integer(readonly=True)
    cache_write_tokens = fields.Integer(readonly=True)
    total_tokens = fields.Integer(readonly=True)
    tokens_known = fields.Boolean(readonly=True)
    # USD, as providers bill: the provider's own figure when it reports one (OpenRouter), else the tokens at the
    # model's prices; empty when neither is known.
    cost = fields.Float(string="Cost (USD)", digits=(16, 6), readonly=True)
    cost_source = fields.Selection([("provider", "Reported by provider"), ("estimate", "Estimated from prices")],
                                   readonly=True)
    duration_ms = fields.Integer(readonly=True)
    http_status = fields.Integer(readonly=True)
    note = fields.Char(readonly=True)

    @api.model
    def dashboard_data(self, period="30d"):
        """Aggregates for the Usage dashboard (administrators only)."""
        self.env["odoo.ai.assistant.config"]._require_admin()
        if period not in _DASHBOARD_PERIODS:
            raise ValidationError(_("Choose a supported dashboard period."))
        days = _DASHBOARD_PERIODS[period]
        today = fields.Date.context_today(self)
        domain, previous_domain, start = [], None, None
        if days:
            start = today - timedelta(days=days - 1)
            domain = [("create_date", ">=", self._local_midnight(start))]
            previous_domain = [
                ("create_date", ">=", self._local_midnight(start - timedelta(days=days))),
                ("create_date", "<", self._local_midnight(start)),
            ]
        amounts = ["api_calls:sum", "total_tokens:sum", "cost:sum"]
        answered = [("purpose", "=", "reply"), ("status", "=", "received")]

        def totals(where):
            calls, tokens, cost, incoming, outgoing, cached = self._read_group(
                where, [], amounts + ["input_tokens:sum", "output_tokens:sum", "cached_tokens:sum"])[0]
            failed = self.search_count(where + [("status", "=", "failed")])
            users = len(self._read_group(where + [("user_id", "!=", False)], ["user_id"], ["__count"]))
            return {
                "calls": calls or 0, "tokens": tokens or 0, "cost": cost or 0, "input_tokens": incoming or 0,
                "output_tokens": outgoing or 0, "cached_tokens": cached or 0,
                "failed_calls": failed, "active_users": users, "answers": self.search_count(where + answered),
                # Calls whose tokens are known but whose model has no price: the cost total leaves them out.
                "unpriced_calls": self.search_count(where + [("tokens_known", "=", True), ("cost_source", "=", False)]),
            }

        def breakdown(groupby, label, extra=lambda key: {}):
            answers = dict(self._read_group(domain + answered, [groupby], ["__count"]))
            failed = dict(self._read_group(domain + [("status", "=", "failed")], [groupby], ["__count"]))
            rows = [{"label": label(key), "calls": calls or 0, "tokens": tokens or 0, "cost": cost or 0,
                     "answers": answers.get(key, 0), "failed": failed.get(key, 0), **extra(key)}
                    for key, calls, tokens, cost in self._read_group(domain, [groupby], amounts)]
            return sorted(rows, key=lambda row: (row["cost"], row["tokens"], row["calls"]), reverse=True)

        purposes = dict(self._fields["purpose"]._description_selection(self.env))
        providers = dict(self._fields["provider"]._description_selection(self.env))
        daily = {}
        for day, calls, tokens, cost in self._read_group(domain, ["create_date:day"], amounts):
            day = day.date() if hasattr(day, "date") else day
            daily[day] = {"calls": calls or 0, "tokens": tokens or 0, "cost": cost or 0}
        if daily or start:
            first = start or min(daily)
            span = (today - first).days + 1
            series = [{"date": fields.Date.to_string(first + timedelta(days=offset)),
                       **daily.get(first + timedelta(days=offset), {"calls": 0, "tokens": 0, "cost": 0})}
                      for offset in range(max(span, 0))]
        else:
            series = []
        usd = self.env.ref("base.USD")
        return {
            "period": period,
            # Providers bill in US dollars; the dashboard shows costs with Odoo's own USD symbol and position.
            "currency": {"symbol": usd.symbol, "position": usd.position},
            "totals": totals(domain),
            "previous": totals(previous_domain) if previous_domain is not None else None,
            "daily": series,
            "by_model": breakdown("model_name", lambda key: key or _("Unknown model")),
            "by_provider": breakdown("provider", lambda key: providers.get(key, _("Unknown provider"))),
            # Odoo's system user (OdooBot) runs technical calls, not a person's: counted, never ranked.
            "by_user": breakdown("user_id", lambda key: key.display_name if key else _("Deleted user"),
                                 lambda key: {"person": bool(key) and not key._is_superuser()}),
            "by_purpose": breakdown("purpose", lambda key: purposes.get(key, _("Other"))),
        }

    @api.model
    def _local_midnight(self, day):
        """UTC datetime of midnight on `day` in the administrator's timezone."""
        local = pytz.timezone(self.env.user.tz or "UTC")
        return local.localize(datetime.combine(day, day_time.min)).astimezone(pytz.UTC).replace(tzinfo=None)

    @api.model
    def _persist(self, values, usage_id=None):
        # API spend must survive rollback of a failed chat or local tool query.
        with self.env.registry.cursor() as cr:
            ledger = api.Environment(cr, SUPERUSER_ID, {})[self._name]
            if usage_id:
                ledger.browse(usage_id).write(values)
                return usage_id
            return ledger.create(values).id

    @api.model
    def _observer(self, provider, model_name, purpose):
        user_id = self.env.uid
        usage_id = None
        started = None

        def observe(event, data):
            nonlocal usage_id, started
            if event == "start":
                started = time.monotonic()
                usage_id = self._persist({
                    "user_id": user_id, "provider": provider, "model_name": model_name,
                    "purpose": purpose, "status": "pending",
                })
                return
            values = {
                "status": "received" if event == "received" else "failed",
                "duration_ms": round((time.monotonic() - started) * 1000),
                "http_status": data.get("http_status", 0),
                "note": data.get("note", ""),
            }
            counts = data.get("tokens")
            if data.get("cost") is not None:
                values.update(cost=data["cost"], cost_source="provider")
            elif counts is not None:
                model = self.env["odoo.ai.assistant.provider.model"].sudo().with_context(active_test=False).search(
                    [("provider", "=", provider), ("identifier", "=", model_name)], limit=1)
                if model and (cost := model._cost(counts)) is not None:
                    values.update(cost=cost, cost_source="estimate")
            if counts is not None:
                values.update(counts, tokens_known=True)
            elif purpose != "catalogue" and event == "received":
                values["note"] = "Provider did not return complete, valid token usage."
            self._persist(values, usage_id)

        return observe
