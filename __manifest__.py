{
    "name": "AI Agent",
    "summary": "Chat with your Odoo data and custom workflows, get AI help for validation errors, "
    "and review confirmed changes",
    "description": """
AI Agent
========

An AI assistant built into Odoo Discuss. Users ask questions in plain language;
the AI plans which MCP tools to run, and Odoo runs them with the user's own
access rights and shows the results (tables, records, native Odoo graphs) to
the user only. Record values are not sent to the AI provider unless an
administrator turns on data sharing.

* Providers: OpenAI, Google Gemini, Anthropic Claude, OpenRouter
* Model catalogue synced from each provider (current text models only)
* AI conversations in Discuss, a chat window from the top bar, and an
  "AI Agent" tab in the messaging menu
* Native Odoo graphs and pivots, with PDF and Excel export
* User-requested explanations for supported validation errors
* Optional business flow context: a Business Flow menu (stages, actions, roles) and custom-code answers,
  updated on demand or every N days
* Every change (create, update, delete, button) is a preview the user confirms
* Usage and cost reporting per user, model and provider
""",
    "version": "19.0.1.0.0",
    "category": "Productivity/Discuss",
    "author": "AI Agent",
    "license": "LGPL-3",
    "depends": ["base_setup", "web", "mail", "mcp_server"],
    "data": [
        "security/ai_agent_security.xml",
        "security/ir.model.access.csv",
        "data/ai_agent_data.xml",
        "views/ai_agent_provider_views.xml",
        "views/ai_agent_model_views.xml",
        "views/ai_agent_usage_views.xml",
        "views/ai_agent_business_flow_views.xml",
        "views/res_config_settings_views.xml",
        "views/ai_agent_export_templates.xml",
        "views/ai_agent_menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "odoo_ai_agent/static/src/**/*",
        ],
    },
    "post_init_hook": "post_init_hook",
    "application": True,
    "installable": True,
}
