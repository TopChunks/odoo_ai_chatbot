"""The answer check (ANSWER_RULES, AiAgentEngine._clean_answer) on real answers.

Every rule has sentences it must fix (taken from real answers that were wrong) and normal sentences it must keep.
When a rule changes, add the new wording here: a pattern that starts removing good sentences fails these tests.
Run: odoo-bin -c <config> -d <test database> -u odoo_ai_agent --test-tags /odoo_ai_agent --stop-after-init
"""
from odoo.tests import TransactionCase, tagged

TABLE = {"type": "table", "rows": [{"values": {}}], "total": 1}
MORE = {"type": "table", "rows": [{"values": {}}], "total": 80, "has_more": True}

# (what it shows, the AI's text, the question, the blocks shown, data shared with the AI, the expected answer)
CASES = [
    # Tables and charts are always shown under the text.
    ("table is below", "The table above lists them.", "", [TABLE], False, "The table below lists them."),
    # Internal record numbers.
    ("product ID removed", "Serial 0029 is in WH/Stock (product ID 39).", "Where is serial 0029?", [TABLE], False,
     "Serial 0029 is in WH/Stock."),
    ("reference number removed", "The transfer (reference 96) is Ready.", "Was it shipped?", [TABLE], False,
     "The transfer is Ready."),
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
]


@tagged("post_install", "-at_install", "ai_agent")
class TestAnswerCheck(TransactionCase):

    def test_answer_rules(self):
        engine = self.env["ai_agent.engine"]
        for name, text, question, blocks, shared, expected in CASES:
            with self.subTest(name):
                state = {"question": question, "blocks": [dict(b) for b in blocks]}
                self.assertEqual(engine._clean_answer(text, state, {"share_data": shared}), expected)
