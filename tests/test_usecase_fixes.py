"""Fixes from the 2026-11-18 use-case run: yearly dates in formulas, option labels in change previews, full names
for rows that share a name, and the default MCP models offered to existing databases.
Run: odoo-bin -c <config> -d <test database> -u odoo_ai_agent --test-tags /odoo_ai_agent --stop-after-init
"""
import datetime
import json

from odoo.tests import TransactionCase, tagged

from odoo.addons.odoo_ai_agent.hooks import DEFAULT_MODELS, OFFERED_KEY, offer_default_models
from odoo.addons.odoo_ai_agent.models import ai_agent_formula as formulas


def run(formula, today, **values):
    return formulas.evaluate(formulas.parse(formula, set(values)), values, today=today)


@tagged("post_install", "-at_install")
class TestUseCaseFixes(TransactionCase):

    def test_yearly_dates(self):
        today = datetime.date(2026, 11, 18)
        self.assertEqual(run("days_to_anniversary(start)", today, start="2023-11-25"), 7)
        self.assertEqual(run("days_to_anniversary(start)", today, start="2021-12-02"), 14)
        self.assertEqual(run("days_to_anniversary(start)", today, start="2020-11-18"), 0)
        self.assertEqual(run("days_to_anniversary(start)", today, start="2020-11-17"), 364)  # passed: next year
        self.assertEqual(run("days_to_anniversary(start)", datetime.date(2027, 2, 27), start="2024-02-29"), 1)
        self.assertEqual(run("years(today, start)", today, start="2023-11-25"), 2)
        self.assertEqual(run("years(today, start)", today, start="2023-11-18"), 3)
        self.assertTrue(run("days_to_anniversary(start) <= 30 and years(today, start) >= 1", today,
                            start="2021-12-02"))
        self.assertFalse(run("days_to_anniversary(start) <= 30 and years(today, start) >= 1", today,
                             start="2026-11-25"))  # starts next week: no anniversary yet
        self.assertIsNone(run("days_to_anniversary(start)", today, start=None))  # empty date: empty cell
        self.assertEqual(run("month(d) * 100 + day(d)", today, d="2021-12-02"), 1202)

    def test_preview_names_the_option_chosen(self):
        engine = self.env["ai_agent.engine"]
        text = engine._preview_option_check("res.partner", {"values": {"type": "invoice"}})
        self.assertIn("'Invoice'", text)
        self.assertIn("call the same tool again", text)
        self.assertEqual(engine._preview_option_check("res.partner", {"values": {"name": "X"}}), "")

    def test_preview_shows_list_commands_by_name(self):
        engine = self.env["ai_agent.engine"]
        a, b = self.env["res.partner.category"].create([{"name": "TEST-Tag A"}, {"name": "TEST-Tag B"}])
        self.assertEqual(engine._preview_commands("res.partner.category", [[6, 0, [a.id, b.id]]]),
                         "TEST-Tag A, TEST-Tag B")
        self.assertEqual(engine._preview_commands("res.partner.category", [[4, b.id]]), "add TEST-Tag B")
        lines = engine._preview_fields("res.partner", {"category_id": [[6, 0, [a.id]]]})
        self.assertEqual(lines[0]["value"], "TEST-Tag A")
        self.assertEqual(engine._preview_commands("res.partner.category", [[0, 0, {"name": "x"}]]),
                         [[0, 0, {"name": "x"}]])  # anything else stays as sent

    def test_rows_sharing_a_name_show_full_names(self):
        company = self.env["res.partner"].create({"name": "TEST-Twin Co", "is_company": True})
        a = self.env["res.partner"].create({"name": "TEST-Twin", "parent_id": company.id})
        b = self.env["res.partner"].create({"name": "TEST-Twin"})
        rows = [{"id": a.id, "name": "TEST-Twin"}, {"id": b.id, "name": "TEST-Twin"}]
        self.env["ai_agent.engine"]._distinct_names("res.partner", [{"name": "name"}], rows)
        self.assertEqual([r["name"] for r in rows], [a.display_name, b.display_name])
        self.assertNotEqual(rows[0]["name"], rows[1]["name"])

    def test_default_models_offered_once(self):
        Enabled = self.env["mcp.enabled.model"].sudo().with_context(active_test=False)
        params = self.env["ir.config_parameter"].sudo()
        params.set_param(OFFERED_KEY, "{}")
        Enabled.search([("model_name", "in", ["res.partner", "res.country"])]).unlink()
        offer_default_models(self.env)
        partner = Enabled.search([("model_name", "=", "res.partner")])
        self.assertTrue(partner.allow_create and partner.allow_write)
        self.assertTrue(Enabled.search_count([("model_name", "=", "res.country")]))
        offered = json.loads(params.get_param(OFFERED_KEY))
        self.assertIn("allow_create", offered["res.partner"])
        # The administrator's later choices are kept: a removed model and an unticked flag stay as they are.
        Enabled.search([("model_name", "=", "res.country")]).unlink()
        partner.allow_create = False
        offer_default_models(self.env)
        self.assertFalse(Enabled.search_count([("model_name", "=", "res.country")]))
        self.assertFalse(partner.allow_create)
        # A flag added to the defaults later is still offered, once.
        DEFAULT_MODELS["res.partner"]["allow_unlink"] = True
        try:
            offer_default_models(self.env)
            self.assertTrue(partner.allow_unlink)
            self.assertFalse(partner.allow_create)
        finally:
            DEFAULT_MODELS["res.partner"].pop("allow_unlink")
