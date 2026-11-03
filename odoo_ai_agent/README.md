<div align="center">

# 🤖 Odoo AI Agent

### Your Odoo data, ready for a plain-language conversation.

Ask about sales, customers, invoices, projects, stock and more. Get clear answers, tables and charts right inside Odoo.

[🚀 Get started](#-get-started) · [🔒 Privacy](#-your-data-and-privacy) · [🧩 Install the MCP dependency](#-install-the-required-mcp-server) · [🛠️ Fix a problem](#-troubleshooting)

**Made for Odoo 19** · OpenAI · Google Gemini · Anthropic Claude · OpenRouter

</div>

---

## ✨ Meet your Odoo assistant

Odoo AI Agent brings an AI chat into Odoo Discuss. Ask in everyday language and the assistant finds information using the access rights you already have in Odoo. Results can appear as text, records, tables, charts and pivots.

Try questions like:

- “How many open opportunities do I have?”
- “Show overdue customer invoices.”
- “Compare this month’s sales with last month.”
- “Make a chart of sales by product category.”
- “Where do I find orders waiting to be invoiced?”

You can ask a follow-up too: “Only show the top five” or “Now make that a pie chart.”

### What it can do

- Find, count, compare and summarize information in Odoo.
- Show results in tables, cards, charts and pivots, with PDF and Excel export.
- Help you find an Odoo screen and open it.
- Prepare supported record changes for your review.
- Track usage and estimated costs for administrators.

### Changes stay in your hands

When the assistant prepares a supported change, it shows you a preview. The change is made only after you choose **Confirm**. Choose **Cancel** to leave Odoo unchanged. Odoo still checks what your account is allowed to do.

---

## 🚀 Get started

> **For Odoo administrators:** this add-on is for Odoo 19 and requires the [MCP Server module](#-install-the-required-mcp-server). Custom add-ons are not available on Odoo Online (SaaS); use Odoo.sh or a self-hosted Odoo server.

### 1. Install the two Odoo modules

1. Install the **MCP Server** module first. Follow [the MCP Server installation guide](https://apps.odoo.com/apps/modules/19.0/mcp_server).
2. Add this repository’s `odoo_ai_agent` folder to an Odoo add-ons directory.
3. Restart Odoo and update the Apps list.
4. Search for **AI Agent** and install it.

If you manage the server yourself, the add-ons directory is the folder listed in your Odoo configuration as `addons_path`. The folder name must remain `odoo_ai_agent`.

### 2. Choose an AI provider and add its key

In Odoo, open **AI Agent → Configuration → Settings**.

1. Choose a provider: OpenAI, Google Gemini, Anthropic Claude or OpenRouter.
2. Use **Get an API key** beside the key field to open the provider’s key page.
3. Create or copy an API key from that provider and paste it into Odoo.
4. Choose **Refresh available models**, then select a model.
5. Click **Save**.

The key belongs to the provider you selected. The provider may charge for use; check its pricing and account limits. The key is stored in your Odoo database and the add-on limits visibility to AI Agent administrators. Never paste it into a chat or commit it to a code repository.

### 3. Choose your privacy setting

On the same Settings page, find **Data and privacy**. Keep **Share record values with the AI provider** switched **off** to start. See [Your data and privacy](#-your-data-and-privacy) for exactly what this means.

The add-on uses the MCP tools inside Odoo. Installing AI Agent turns on the MCP Server setting if it is off, and enables a starter set of record types. Review **Enabled MCP models** and allow only the kinds of records and actions your team needs.

### 4. Give people access

Open **Settings → Users**, choose a person, and set **Access Rights → AI Agent → User**. Their existing Odoo permissions still apply. Refresh Odoo in the browser if the new AI Agent icon does not appear.

### 5. Start your first chat

Open the **AI Agent** app, choose **+ Chat**, and ask a question about information you can access in Odoo. You can also open the robot icon in the top bar for a quick chat.

**That’s it.** Ask a question, inspect the table or chart, and continue with a follow-up.

---

## 🧩 Install the required MCP Server

AI Agent depends on an Odoo add-on named **MCP Server**. It provides the tools that let AI Agent search Odoo records from inside Odoo. It is a separate module from this repository and must be installed in your Odoo 19 database.

**Install or read about it here:** [MCP Server for Odoo 19 — Odoo Apps Store](https://apps.odoo.com/apps/modules/19.0/mcp_server)

The MCP Server’s `/mcp` endpoint can also be connected to compatible external MCP clients. **You do not need to set up an external MCP client to use the AI Agent chat inside Odoo.** For a separate client connection, use the MCP Server’s own setup guide and access controls.

---

## 🔒 Your data and privacy

Privacy is designed to be the default: **record values fetched from Odoo are not sent to the AI provider unless an administrator turns on “Share record values with the AI provider.”** The switch is in **AI Agent → Configuration → Settings → Data and privacy**, and starts off.

| Setting | What the provider receives | What you get |
|---|---|---|
| **Sharing off** (default) | Your question and the conversation needed to answer it, the assistant’s instructions and tool definitions, plus limited tool output such as counts, field names and record IDs. The actual values read from records are kept out of the provider request. | Results are fetched and displayed by Odoo in the chat as tables, charts and records. The assistant may refer you to the results instead of describing their values in a sentence. |
| **Sharing on** | The above, plus record values returned by tools. This can include names, amounts, dates, notes and other values available to the user and enabled for the assistant. | The assistant can read those values to write summaries, comparisons and explanations in its response. |

### A few important details

- The model provider is an external service. **Even with sharing off, the question you type and conversation context are sent to the selected provider** so it can decide how to answer. Avoid putting sensitive information directly in a prompt if it should not leave your organization.
- Sharing off is about record values returned from Odoo. It does not mean that no information at all is sent externally.
- Sharing on is a deliberate administrator setting. Turn it on only if your organization has reviewed the provider and is comfortable sending the relevant record values to it.
- Odoo performs record lookups using the asking user’s permissions. The assistant is also limited to the record types and operations enabled by an administrator in **Enabled MCP models**.
- API keys are not sent as conversation content. The add-on uses the key to authenticate its server-to-provider request.
- The chosen provider’s own data handling, retention and billing terms apply to requests it receives. Review those terms with the provider.

With sharing off, the assistant is useful for locating information and presenting Odoo results. With sharing on, it can also read those results and describe what they mean in its own words.

---

## 🛡️ Access and safe changes

The assistant does not get a special all-powerful Odoo account. It works with the asking person’s Odoo access rights, and an administrator chooses which record types are available to it.

In **Enabled MCP models**, administrators can choose whether the assistant may read records, prepare create/update/delete actions, or prepare supported button actions. Only turn on the capabilities your users need. A prepared change still waits for the user to confirm it.

AI conversations are private to their owner. Users can rename or delete their own conversations.

---

## 💬 Where to find AI Agent

- **AI Agent app:** start and manage conversations, search chats, and view unread replies.
- **Robot icon in the top bar:** open a quick chat from anywhere in Odoo.
- **Discuss and the messaging menu:** find your AI Agent conversations alongside your regular chats.

The exact placement can vary slightly with your Odoo layout and installed apps.

---

## 📊 Models, usage and cost

Administrators select the provider and model in **AI Agent → Configuration → Settings**. Use **Refresh available models** after adding or changing an API key. The list contains the provider’s current supported text models. Each conversation can have its own model selection where available.

AI providers may charge for their API. Costs depend on the model and how much conversation is sent. Review the provider’s pricing before use. Administrators can review recorded usage and estimated cost in **AI Agent → Reporting → Usage & Cost**.

---

## 🛠️ Troubleshooting

| What happened? | Try this |
|---|---|
| **I can’t see the AI Agent app or robot icon.** | Ask an administrator to give you **AI Agent → User** access. Then refresh the page. |
| **“No AI model is selected.”** | An administrator should select a provider, enter its API key, refresh available models, choose a model and save. |
| **“API key is missing” or authentication failed.** | Check that the key belongs to the selected provider, is copied correctly, and is still active. Check the provider account for usage limits or billing setup too. |
| **The assistant says a record type is not enabled.** | An administrator can open **AI Agent → Configuration → Settings → Enabled MCP models → Manage MCP models** and enable the required record type and access. |
| **The assistant cannot find something I know is there.** | Check your Odoo access rights, company selector, filters, spelling and date range. The assistant cannot see records your account cannot access. |
| **It shows a table but does not explain the figures.** | This is expected when sharing is off. Read the Odoo result in the table, or ask an administrator whether enabling record sharing is acceptable. |
| **A question times out or the provider returns an error.** | Try again, check the provider’s status and account limits, or ask an administrator to choose a faster model or review the timeout setting. |
| **The answer stops too early.** | Ask a shorter question or split it into two questions. Administrators can review the answer-length limit in Settings. |
| **The screen looks outdated after an upgrade.** | Restart Odoo if required by your deployment, then force-refresh the browser (usually **Ctrl + Shift + R** on Windows/Linux or **Cmd + Shift + R** on macOS). |
| **The module will not install.** | Confirm the server runs Odoo 19, the required MCP Server module is installed, and the Python dependencies listed by that module are installed in Odoo’s Python environment. Check the Odoo server log for the first installation error. |

### Where to look for more help

1. Read the full [MCP Server setup and troubleshooting guide](https://apps.odoo.com/apps/modules/19.0/mcp_server) if the MCP dependency or its connection is the problem.
2. Ask your Odoo administrator to check the Odoo server log around the time of the error.
3. Open an issue in this repository and include your Odoo version, what you clicked, the exact error text and the relevant log excerpt. **Remove API keys, passwords, customer names and other private information first.**

---

## ❓ Frequently asked questions

<details>
<summary><b>Does sharing off mean no information is sent to an AI company?</b></summary>

No. Your prompt and conversation context are sent to the selected provider so it can generate a response. Sharing off keeps the record values fetched from Odoo out of provider requests. Do not include sensitive details in your prompt if they must stay inside your organization.
</details>

<details>
<summary><b>Can the assistant see every record in our database?</b></summary>

No. It uses the asking user’s access rights, and the administrator controls which Odoo record types are available. The sharing switch controls whether values returned from Odoo tools are also sent to the provider.
</details>

<details>
<summary><b>Can I use it without an API key?</b></summary>

The built-in provider choices require an API key from the provider you select. The module does not currently include a local AI model. The key is configured by an AI Agent administrator in Odoo Settings.
</details>

<details>
<summary><b>Do I need an MCP desktop app to start chatting?</b></summary>

No. Install the MCP Server Odoo module as a dependency, then use AI Agent inside Odoo. A separate MCP client is only needed if you want another application to connect to your Odoo MCP endpoint.
</details>

<details>
<summary><b>Will the assistant change something without asking?</b></summary>

Supported changes are shown for confirmation first. Nothing is changed until the user chooses **Confirm**, and Odoo enforces that user’s permissions.
</details>

---

<div align="center">

**Ask naturally. Keep control of your Odoo data.**

[⭐ Explore the repository](https://github.com/TopChunks/odoo_ai_chatbot/tree/main/odoo_ai_agent) · [🧩 MCP Server dependency](https://apps.odoo.com/apps/modules/19.0/mcp_server)

</div>
