"""The answer check (ANSWER_RULES, AiAgentEngine._clean_answer) on real answers.

Every rule has sentences it must fix (taken from real answers that were wrong) and normal sentences it must keep.
When a rule changes, add the new wording here: a pattern that starts removing good sentences fails these tests.
Run: odoo-bin -c <config> -d <test database> -u odoo_ai_agent --test-tags /odoo_ai_agent --stop-after-init
"""
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

TABLE = {"type": "table", "rows": [{"values": {}}], "total": 1}
MORE = {"type": "table", "rows": [{"values": {}}], "total": 80, "has_more": True}
ORDER = {"type": "table", "model": "sale.order", "rows": [{"id": 76, "values": {"name": "TEST-S00110"}}], "total": 1}

# (what it shows, the AI's text, the question, the blocks shown, data shared with the AI, the expected answer)
CASES = [
    # Tables and charts are always shown under the text.
    ("table is below", "The table above lists them.", "", [TABLE], False, "The table below lists them."),
    # Internal record numbers.
    ("product ID removed", "Serial 0029 is in WH/Stock (product ID 39).", "Where is serial 0029?", [TABLE], False,
     "Serial 0029 is in WH/Stock."),
    ("reference number removed", "The transfer (reference 96) is Ready.", "Was it shipped?", [TABLE], False,
     "The transfer is Ready."),
    ("record type and number removed", "TEST-Lead Injection (lead 77) is assigned to you.", "Summarise the lead.",
     [TABLE], False, "TEST-Lead Injection is assigned to you."),
    ("month and year kept", "Sales grew (November 2026) and orders doubled (2 orders).", "", [TABLE], False,
     "Sales grew (November 2026) and orders doubled (2 orders)."),
    ("IDs kept when asked", "The order (ID 12) is confirmed.", "What is the ID of S00012?", [TABLE], False,
     "The order (ID 12) is confirmed."),
    ("identification number kept", "Ravi's ID number is ID-RAVI.", "Show his ID number.", [TABLE], False,
     "Ravi's ID number is ID-RAVI."),
    # The AI talking about its own steps.
    ("presented twice removed",
     "Your win rate is shown below. Two tables appeared because I presented the calculation twice; both agree.",
     "What is our win rate?", [TABLE], False, "Your win rate is shown below."),
    ("by mistake removed", "I ran the chart twice by mistake. Here it is.", "", [TABLE], False, "Here it is."),
    ("data about 'twice' kept", "The customer was contacted twice this week.", "", [TABLE], False,
     "The customer was contacted twice this week."),
    ("no earlier note", "The chart is below. I should correct my earlier note: it counts confirmed orders.", "",
     [TABLE], False, "The chart is below. It counts confirmed orders."),
    # "The list is capped": only when the table really is complete.
    ("capped removed when complete", "Here are 3 orders. The list is capped at 80 rows.", "", [TABLE], False,
     "Here are 3 orders."),
    ("capped kept when cut", "Here are 80 of 283 moves. The list is capped at 80 rows.", "", [MORE], False,
     "Here are 80 of 283 moves. The list is capped at 80 rows."),
    # Hand-backs, with data sharing off.
    ("see whether removed",
     "No receipts are expected today. The breakdown is below, so you can see whether any fall on today's date.",
     "Which receipts are expected today?", [TABLE], False, "No receipts are expected today."),
    ("if it doesn't appear removed",
     "No receipts are expected today. If today's date doesn't appear there, nothing is due today.",
     "Which receipts are expected today?", [TABLE], False, "No receipts are expected today."),
    ("cannot tell whether removed",
     "No quotations were created this week. I can't tell from the results whether that week is this one.",
     "Show quotations created this week.", [TABLE], False, "No quotations were created this week."),
    ("telling which one kept", "I can't tell which customer you mean: two contacts match.", "", [TABLE], False,
     "I can't tell which customer you mean: two contacts match."),
    ("check the column removed",
     "No, it has not been shipped. I can't see the exact status text myself, so check the Status column for whether "
     "it's waiting or ready.", "Has the delivery been shipped?", [TABLE], False, "No, it has not been shipped."),
    ("column word in data kept", "The Status column of the order is Sales Order.", "", [TABLE], False,
     "The Status column of the order is Sales Order."),
    ("cannot see the values removed", "Here are the invoices. I can't see the amounts, but they are in the table.",
     "", [TABLE], False, "Here are the invoices."),
    ("check yourself removed", "Here are the orders. Please compare the totals yourself.", "", [TABLE], False,
     "Here are the orders."),
    ("refusal with a reason kept", "I can't see his opportunities because your access rights do not allow it.", "",
     [TABLE], False, "I can't see his opportunities because your access rights do not allow it."),
    ("hand-back kept when values are shared", "Here are the orders. Please compare the totals yourself.", "",
     [TABLE], True, "Here are the orders. Please compare the totals yourself."),
    ("normal sentences kept", "Here are the 5 overdue receipts, so you can plan the week. If the vendor confirms, "
     "the receipt moves to Ready.", "", [TABLE], False,
     "Here are the 5 overdue receipts, so you can plan the week. If the vendor confirms, the receipt moves to Ready."),
    ("no data shown: nothing removed", "I can't see the amounts.", "", [], False, "I can't see the amounts."),
    # A record's database ID in the text: the name the user sees in the table instead.
    ("record ID swapped for its name", "It belongs to sales order 76.", "Which order is it from?", [ORDER], False,
     "It belongs to sales order TEST-S00110."),
    ("ID of another record type kept", "Lead 76 is open.", "", [ORDER], False, "Lead 76 is open."),
    ("count and year kept", "You have 2 orders from 2026.", "", [ORDER], False, "You have 2 orders from 2026."),
]


@tagged("post_install", "-at_install", "ai_agent")
class TestAnswerCheck(TransactionCase):

    def test_answer_rules(self):
        engine = self.env["ai_agent.engine"]
        for name, text, question, blocks, shared, expected in CASES:
            with self.subTest(name):
                state = {"question": question, "blocks": [dict(b) for b in blocks]}
                self.assertEqual(engine._clean_answer(text, state, {"share_data": shared}), expected)

    def test_fiscal_year_note(self):
        """A whole April-March year shown: Odoo says it is the fiscal year, unless the user named the year type."""
        if "fiscalyear_last_month" not in self.env.company._fields:
            self.skipTest("Accounting is not installed")
        self.env.company.fiscalyear_last_month = "3"
        engine = self.env["ai_agent.engine"]

        def notes(question, start, end):
            chart = {"type": "chart", "model": "account.move",
                     "domain": [["invoice_date", ">=", start], ["invoice_date", "<", end]]}
            state = {"question": question, "blocks": [chart]}
            engine._fiscal_year_note(state)
            return [b["text"] for b in state["blocks"] if b.get("type") == "notice"]

        self.assertIn("is your company's fiscal year (April to March)",
                      notes("Sales by customer this year", "2026-04-01", "2027-04-01")[0])
        self.assertFalse(notes("Sales this fiscal year", "2026-04-01", "2027-04-01"))
        self.assertFalse(notes("Sales in calendar year 2026", "2026-01-01", "2027-01-01"))
        self.assertFalse(notes("Sales since April", "2026-04-01", "2026-11-15"))
        self.env.company.fiscalyear_last_month = "12"
        self.assertFalse(notes("Sales this year", "2026-01-01", "2027-01-01"))

    def test_form_tab(self):
        """A tab the user names: the fields shown there, in screen order; an unknown tab names the real ones."""
        engine = self.env["ai_agent.engine"]
        user = self.env.ref("base.user_admin")
        tab = engine._form_tab("res.users", user.id, "preferences tab")
        names = [name for name, _label in tab["fields"]]
        self.assertEqual(tab["title"], "Preferences")
        self.assertIn("lang", names)
        self.assertNotIn("tz", names)  # on the Calendar tab, not Preferences
        self.assertNotIn("odoobot_state", names)  # shown in developer mode only
        with self.assertRaisesRegex(UserError, "Its tabs: .*Preferences"):
            engine._form_tab("res.users", user.id, "Payroll")

    def test_screen_outline(self):
        """The form the user opens (not Odoo's short default user form), with conditional fields in words."""
        outline = self.env["ai_agent.engine"]._screen_outline("res.users")
        self.assertIn("Tab 'Preferences'", outline)
        self.assertIn("(shown only when not Share User)", outline)

    def test_find_setting_whole_words(self):
        """Settings match on whole words ("web" is not "Website"); a setting that is only a link is found too."""
        engine = self.env["ai_agent.engine"]
        for query, expected in (("progressive web app", "Web App Name"), ("default access rights", "a link")):
            with self.subTest(query):
                text = engine._tool_find_setting({"query": query}, {"blocks": [], "notices": [], "question": ""})
                first = next(line for line in text.splitlines() if line.startswith("- "))
                self.assertIn(expected, first)

    def test_count_in_list(self):
        """A count of records a complete list above already shows is redundant; any other count is kept."""
        engine = self.env["ai_agent.engine"]
        first, second = self.env["res.partner"].create([{"name": "AI count test A"}, {"name": "AI count test B"}])
        listed = {"type": "table", "model": "res.partner", "rows": [{"id": first.id, "values": {}}], "total": 1}

        def count(domain, aggregates=("__count",)):
            return {"type": "table", "grouped": True, "model": "res.partner",
                    "spec": {"groupby": [], "aggregates": list(aggregates), "domain": domain}}

        same = count([["id", "=", first.id]])
        self.assertTrue(engine._count_in_list(same, [listed, same]))
        nothing = count([["id", "=", 0]])
        self.assertTrue(engine._count_in_list(nothing, [listed, nothing]))
        both = count([["id", "in", [first.id, second.id]]])  # B is not in the list: the count says more
        self.assertFalse(engine._count_in_list(both, [listed, both]))
        cut = dict(listed, has_more=True)
        self.assertFalse(engine._count_in_list(same, [cut, same]))
        total = count([["id", "=", first.id]], ("id:sum",))
        self.assertFalse(engine._count_in_list(total, [listed, total]))

    def test_show_app_same_name_elsewhere(self):
        """An app shown only in developer mode, with a screen of the same name in an app the user opens: that screen,
        and the full app's developer mode note."""
        Menu = self.env["ir.ui.menu"]
        action = self.env["ir.actions.act_window"].create({"name": "AI test", "res_model": "res.partner"})
        app = Menu.create({"name": "AI Test Tool", "group_ids": [(4, self.env.ref("base.group_no_one").id)]})
        Menu.create({"name": "List", "parent_id": app.id, "action": f"ir.actions.act_window,{action.id}"})
        Menu.create({"name": "AI Test Tool", "parent_id": self.env.ref("base.menu_administration").id,
                     "action": f"ir.actions.act_window,{action.id}"})
        state = {"blocks": [], "notices": [], "question": ""}
        text = self.env["ai_agent.engine"]._tool_show_app({"app": "AI Test Tool"}, state)
        cards = [a["path"] for b in state["blocks"] if b.get("type") == "app" for a in b["apps"]]
        self.assertEqual(cards, ["Settings › AI Test Tool"])
        self.assertIn("developer mode", text)

    def test_find_setting_names_an_app(self):
        """Words that are an app's name are about the app: never a setting sharing one word, never its card."""
        engine = self.env["ai_agent.engine"]
        module = self.env["ir.module.module"].search(
            [("state", "=", "uninstalled"), ("shortdesc", "=ilike", "% %")], limit=1)  # e.g. "Email Marketing"
        if not module:
            self.skipTest("No uninstalled app with a two-word name")
        state = {"blocks": [], "notices": [], "question": ""}
        text = engine._tool_find_setting({"query": module.shortdesc}, state)
        self.assertIn("is an app", text)
        self.assertFalse([b for b in state["blocks"] if b.get("type") == "app"])
