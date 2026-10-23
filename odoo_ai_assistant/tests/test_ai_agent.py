import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.odoo_ai_assistant.models import native_mcp
from odoo.addons.odoo_ai_assistant.models.providers import SYSTEM_NOTE, ProviderClient, ProviderError
from odoo.addons.odoo_ai_assistant.models.tools import cell_value, clean_domain

NOTE = SYSTEM_NOTE + " an earlier request was completed.]"


def gemini(parts):
    """A provider answer in Gemini's format."""
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}


@tagged("post_install", "-at_install")
class TestAIAgent(TransactionCase):
    """The AI Agent with a scripted stand-in for the AI provider: no provider is contacted."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        provider_model = cls.env["odoo.ai.assistant.provider.model"].create(
            {"provider": "gemini", "identifier": "gemini-test", "name": "gemini-test"})
        cls.env["odoo.ai.assistant.config"]._configuration().write({
            "enabled": True, "provider": "gemini", "api_key": "FAKE-KEY", "model_id": provider_model.id,
            "local_tools": True, "native_actions": True, "business_data_sharing": False,
        })
        admin = cls.env.ref("base.user_admin")
        admin.ai_assistant_enabled = True
        cls.admin_env = cls.env(user=admin, context={"tz": "UTC", "lang": "en_US"})
        gates = cls.env["mcp.enabled.model"].with_context(active_test=False)
        gate = gates.search([("model_name", "=", "res.partner")], limit=1) or gates.create(
            {"model_id": cls.env["ir.model"]._get_id("res.partner")})
        gate.write({"active": True, "allow_read": True, "allow_create": True, "allow_write": True})
        gates.search([("model_name", "=", "res.partner.category")]).active = False
        cls.env["res.partner"].create([{"name": "AI-TEST-A"}, {"name": "AI-TEST-B"}])

    def ask(self, prompt, label, replies):
        """Sends ``prompt`` in a new conversation; the AI answers with ``replies`` in turn (a text, or tool calls
        as ``(name, arguments)`` pairs). Returns the conversation and its tool requests."""
        replies = list(replies)

        def fake_request(client, method, path, payload=None):
            system = ((payload or {}).get("systemInstruction") or {}).get("parts", [{}])[0].get("text", "")
            if "You are a request classifier" in system:
                return gemini([{"text": json.dumps({"request": label})}])
            if "Name an Odoo conversation" in system:
                return gemini([{"text": json.dumps({"title": "Test"})}])
            reply = replies.pop(0) if replies else "Done."
            if isinstance(reply, str):
                return gemini([{"text": reply}])
            return gemini([{"functionCall": {"name": name, "args": args}} for name, args in reply])

        Conversation = self.admin_env["odoo.ai.assistant.conversation"]
        with patch.object(ProviderClient, "_request", fake_request):
            conversation = Conversation.browse(Conversation.start_conversation()["id"])
            conversation.send_message(prompt)
        return conversation, self.env["odoo.ai.assistant.tool.request"].search(
            [("conversation_id", "=", conversation.id)], order="id")

    def found(self, requests):
        records = ((requests[:1].result or {}).get("structuredContent") or {}).get("records") or []
        return sorted(record["name"] for record in records)

    # ------------------------------------------------------------ MCP Server compatibility
    def test_mcp_server_compatible(self):
        self.assertEqual(native_mcp.mcp_server_missing(self.env), [])

    def test_incompatible_mcp_server_stops_chat(self):
        renamed = dict(native_mcp._MCP_SERVER_METHODS, **{"mcp.mixin": ("_renamed_in_a_later_version",)})
        with patch.object(native_mcp, "_MCP_SERVER_METHODS", renamed):
            self.assertEqual(native_mcp.mcp_server_missing(self.env), ["mcp.mixin._renamed_in_a_later_version"])
            with self.assertRaises(UserError):
                self.admin_env["odoo.ai.assistant.conversation"].start_conversation()

    # ------------------------------------------------------------ filters as models write them
    def test_clean_domain(self):
        cases = [
            ([["state", "in", "sale", "done"]], [["state", "in", ["sale", "done"]]]),
            ([["state", "in", "sale"]], [["state", "in", ["sale"]]]),
            ([["state", "not in", ("draft", "cancel")]], [["state", "not in", ["draft", "cancel"]]]),
            (["['name', 'ilike', 'A']"], [["name", "ilike", "A"]]),
            ('[["name", "=", "A"]]', [["name", "=", "A"]]),
            (["|", ["name", "=", "A"], ["name", "=", "B"]], ["|", ["name", "=", "A"], ["name", "=", "B"]]),
            # A broken list is left for MCP Server to refuse, never turned into a partial search.
            ([["state", "in", "[", "sale"]], [["state", "in", "[", "sale"]]),
            ("not a domain", "not a domain"),
        ]
        for domain, expected in cases:
            with self.subTest(domain=domain):
                self.assertEqual(clean_domain(domain), expected)

    def test_search_with_loose_filters(self):
        for domain in ([["name", "in", "AI-TEST-A", "AI-TEST-B"]], '[["name", "in", ["AI-TEST-A", "AI-TEST-B"]]]'):
            with self.subTest(domain=domain):
                _conversation, requests = self.ask("Show AI-TEST-A and AI-TEST-B", "READ", [[(
                    "mcp_search_records", {"model": "res.partner", "domain": domain, "fields": ["name"]})]])
                self.assertEqual(self.found(requests), ["AI-TEST-A", "AI-TEST-B"])

    def test_every_field_reads_default_fields(self):
        _conversation, requests = self.ask("Show AI-TEST-A", "READ", [[(
            "mcp_search_records", {"model": "res.partner", "domain": [["name", "=", "AI-TEST-A"]], "fields": ["*"]})]])
        self.assertEqual(self.found(requests), ["AI-TEST-A"])

    # ------------------------------------------------------------ permissions and confirmation
    def test_model_not_enabled_is_refused(self):
        _conversation, requests = self.ask("List partner tags", "READ", [[(
            "mcp_search_records", {"model": "res.partner.category", "domain": [], "fields": ["name"]})],
            "I cannot read partner tags."])
        self.assertFalse(requests.filtered(lambda request: request.state == "done"))

    def test_change_waits_for_confirmation(self):
        conversation, requests = self.ask("Create contact AI-TEST-NEW", "CREATE", [[(
            "mcp_create_record", {"model": "res.partner", "values": {"name": "AI-TEST-NEW"}})]])
        Partner = self.env["res.partner"]
        self.assertEqual(requests.mapped("state"), ["pending"])
        self.assertFalse(Partner.search([("name", "=", "AI-TEST-NEW")]))
        conversation.confirm_tool(requests.id)
        self.assertEqual(requests.state, "done")
        self.assertTrue(Partner.search([("name", "=", "AI-TEST-NEW")]))

    # ------------------------------------------------------------ table display
    def test_cell_value(self):
        Partner = self.admin_env["res.partner"]
        self.assertEqual(cell_value(Partner, "type", "contact"), "Contact")
        self.assertIsNone(cell_value(Partner, "name", False))
        shown = cell_value(Partner, "create_date", "2026-10-19 08:30:15")
        self.assertIn("2026", shown)
        self.assertEqual(shown.count(":"), 1)

    # ------------------------------------------------------------ provider answers
    def test_echoed_note_is_retried_without_notes(self):
        question = {"role": "user", "content": "Which contacts are tagged VIP?"}
        history = [question, {"role": "assistant", "content": NOTE}, question]
        sent, replies = [], [[{"text": NOTE}], [{"functionCall": {"name": "mcp_search_records", "args": {}}}]]

        def fake_request(client, method, path, payload=None):
            sent.append(json.dumps(payload))
            return gemini(replies[len(sent) - 1])

        with patch.object(ProviderClient, "_request", fake_request):
            _text, calls = ProviderClient("gemini", "FAKE-KEY", timeout=5).reply("gemini-x", history, [])
        self.assertEqual(calls[0]["name"], "mcp_search_records")
        self.assertEqual([NOTE[:20] in payload for payload in sent], [True, False])

    def test_echo_twice_is_an_empty_answer(self):
        history = [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": NOTE},
                   {"role": "user", "content": "Hi"}]
        with patch.object(ProviderClient, "_request", lambda *args, **kwargs: gemini([{"text": NOTE}])), \
                self.assertRaises(ProviderError) as caught:
            ProviderClient("gemini", "FAKE-KEY", timeout=5).reply("gemini-x", history, [])
        self.assertEqual(caught.exception.kind, "empty")
