<div align="center">

<img src="static/description/icon.png" width="110" alt="AI Agent"/>

# AI Agent for Odoo 19

### Ask your Odoo anything, in plain words, without handing your data to the AI.

*"How many open opportunities do I have?"* · *"Pie chart of this month's sales by product"* · *"Why can't I confirm this order?"*

**Your data stays in Odoo.** The AI decides *what* to look up; Odoo looks it up **with each user's own access rights**
and shows the answer as tables, cards and charts. Nothing is changed until the user clicks **Confirm**.

</div>

---

## Contents

1. [Why AI Agent? Your data stays in Odoo](#1-why-ai-agent-your-data-stays-in-odoo)
2. [What you need](#2-what-you-need)
3. [Installation, step by step](#3-installation-step-by-step)
4. [First-time setup in Odoo](#4-first-time-setup-in-odoo)
5. [Where users find it](#5-where-users-find-it)
6. [Every feature, explained](#6-every-feature-explained)
7. [Questions to try](#7-questions-to-try)
8. [Privacy: exactly what is sent to the AI provider](#8-privacy-exactly-what-is-sent-to-the-ai-provider)
9. [MCP: what it is, and cautions](#9-mcp-what-it-is-and-cautions)
10. [Always review changes before you confirm](#10-always-review-changes-before-you-confirm)
11. [Choosing a model and keeping costs low](#11-choosing-a-model-and-keeping-costs-low)
12. [Updating to a new version](#12-updating-to-a-new-version)
13. [Debugging and troubleshooting](#13-debugging-and-troubleshooting)
14. [Useful links](#14-useful-links)

---

## 1. Why AI Agent? Your data stays in Odoo

Most "AI for Odoo" tools copy your records (customers, prices, invoices, salaries) to an outside AI company so the AI can read them. **AI Agent is built the other way round.**

```
   User asks                The AI plans               Odoo does the work              The user sees
 ──────────────           ─────────────────          ─────────────────────          ─────────────────
 "Which invoices    ──►   "search invoices,    ──►   runs the search with     ──►   a table of the real
  are overdue?"            unpaid, due < today"       the USER's own rights          invoices, in Odoo
                                   ▲                           │
                                   └──── only counts and IDs ──┘
                                        (never names, amounts, emails…)
```

- The AI provider receives the **question**, the **list of tools** and **field definitions** (for example "an invoice has a *due date*"), and only **how many** records were found and their **IDs**.
- The **values** (customer names, amounts, phone numbers, notes) are shown to the user **by Odoo** and are **not sent** to the AI provider.
- An administrator can switch on **data sharing** if they *want* the AI to summarise values in its own words. It is **off by default** (see [section 8](#8-privacy-exactly-what-is-sent-to-the-ai-provider)).

| ✅ AI Agent can | ❌ AI Agent cannot |
|---|---|
| Find, count, total, compare and group your records | See records the user is not allowed to see |
| Draw bar, line and pie charts and pivot tables | Change anything without the user clicking **Confirm** |
| Show a record's history, changes and conversations | Reveal passwords, API keys or other secrets |
| Explain Odoo screens and settings, and open them | Act outside Odoo (send WhatsApp messages, book trucks…) |
| Explain error messages in plain words | Answer questions unrelated to the business (news, sports…) |
| Prepare a new contact, task or activity for you to confirm | Run tools on data models an administrator has not enabled |

---

## 2. What you need

| Requirement | Details |
|---|---|
| **Odoo 19** | Community or Enterprise. |
| **MCP Server module** (`mcp_server`) | Required. AI Agent uses its tools to read Odoo safely. [Download it from the Odoo Apps store](https://apps.odoo.com/apps/modules/19.0/mcp_server). |
| **An AI provider account and API key** | One of: OpenAI, Google Gemini, Anthropic Claude or OpenRouter (links in [section 14](#14-useful-links)). |
| **Internet access from the Odoo server** | To reach the provider's API. |
| **Python packages** | `authlib`, `defusedxml` and `packaging` (for MCP Server). `markdown2` is optional (nicer text formatting). |

---

## 3. Installation, step by step (Windows)

This guide uses the usual Windows layout where Odoo runs from its source code:

```
C:\Odoo\
├── odoo\              ← Odoo 19 source code (contains odoo-bin)
├── custom-addons\     ← your extra modules go here
├── venv\              ← Odoo's Python environment
└── odoo.conf          ← Odoo configuration file
```

If your folders have other names, use your own paths in the commands below.

### Step 1: Install Git for Windows

Download it from **https://git-scm.com/download/win** and install it with the default options. To check, open a **new** Command Prompt and type:

```bat
git --version
```

### Step 2: Create the custom-addons folder

Keep extra modules **outside** Odoo's own folder, so Odoo updates never overwrite them.

```bat
mkdir C:\Odoo\custom-addons
cd /d C:\Odoo\custom-addons
```

### Step 3: Get AI Agent with git

```bat
git init
git remote add origin https://github.com/TopChunks/odoo_ai_chatbot.git
git pull origin main
```

You now have `C:\Odoo\custom-addons\odoo_ai_agent\`.

### Step 4: Add the MCP Server module

Download **MCP Server** for Odoo 19 from the [Odoo Apps store](https://apps.odoo.com/apps/modules/19.0/mcp_server), unzip it, and put the `mcp_server` folder **next to** `odoo_ai_agent`:

```
C:\Odoo\custom-addons\
├── odoo_ai_agent\      ← AI Agent
└── mcp_server\         ← MCP Server
```

> ⚠️ After unzipping, check that you don't have a folder inside a folder (`mcp_server\mcp_server\`). The folder that contains `__manifest__.py` must sit directly in `C:\Odoo\custom-addons\`.

### Step 5: Install the Python packages

Activate Odoo's Python environment, then install the packages into it:

```bat
C:\Odoo\venv\Scripts\activate
pip install authlib defusedxml packaging markdown2
```

> 💡 After `activate`, the prompt starts with `(venv)`. That means `python` and `pip` now use **Odoo's** Python, not another Python on your PC.

### Step 6: Tell Odoo where the folder is

Open `C:\Odoo\odoo.conf` in Notepad. Find the `addons_path` line and **add** `C:\Odoo\custom-addons` at the end, after a comma. Keep the paths that are already there:

```ini
[options]
addons_path = C:\Odoo\odoo\odoo\addons,C:\Odoo\odoo\addons,C:\Odoo\custom-addons
```

Save the file.

### Step 7: Start (or restart) Odoo

If Odoo is already running, stop it first with **Ctrl + C** in its window. Then start it:

```bat
cd /d C:\Odoo\odoo
C:\Odoo\venv\Scripts\activate
python odoo-bin -c C:\Odoo\odoo.conf
```

Leave this window open: Odoo runs as long as it is open, and shows its log here. Open Odoo in your browser at **http://localhost:8069**.

### Step 8: Install the module in Odoo

1. Log in as an administrator.
2. Turn on **developer mode**: *Settings* → scroll to the bottom → **Activate the developer mode**.
3. Go to **Apps** → **Update Apps List** → **Update**.
4. Remove the *Apps* filter in the search bar, search **AI Agent** and click **Activate**.
   MCP Server is installed automatically with it.

> 💡 When AI Agent is installed, it switches on MCP Server and enables a starter set of common data models (contacts, CRM, sales, invoices, products, stock, projects, employees…) **for reading**. You can change this list at any time (see [section 4](#4-first-time-setup-in-odoo)).

<details>
<summary><b>Installing on a Linux server instead?</b></summary>

The same steps with Linux commands (Odoo in `/opt/odoo`, configuration in `/etc/odoo.conf`):

```bash
sudo mkdir -p /opt/odoo/custom-addons && cd /opt/odoo/custom-addons
sudo git init
sudo git remote add origin https://github.com/TopChunks/odoo_ai_chatbot.git
sudo git pull origin main
# put the mcp_server folder next to odoo_ai_agent, then:
/opt/odoo/venv/bin/pip install authlib defusedxml packaging markdown2
sudo chown -R odoo:odoo /opt/odoo/custom-addons
# add /opt/odoo/custom-addons to addons_path in /etc/odoo.conf, then:
sudo systemctl restart odoo
```

</details>

---

## 4. First-time setup in Odoo

Open **AI Agent → Configuration → Settings** (or *Settings → AI Agent*).

### ① Connect an AI provider

| Setting | What to do |
|---|---|
| **AI Provider** | Choose OpenAI, Google Gemini, Anthropic Claude or OpenRouter. |
| **API Key** | Paste the key from the provider's website (links in [section 14](#14-useful-links)). Only AI Agent administrators can read it. |
| **Refresh available models** | Click it after adding the key: Odoo loads the provider's current text models. |
| **AI Model** | Pick the model everyone uses by default (see [section 11](#11-choosing-a-model-and-keeping-costs-low)). |

Click **Save**.

### ② Check MCP Server

| Setting | What it means |
|---|---|
| **MCP Server** | Must show as on. If it says *"Switched off: the AI cannot read data"*, click **Switch on**. |
| **Enabled MCP models** | The kinds of records the AI may work with, and what it may do on each (*Read, Create, Update, Delete, Buttons*). Click **Manage MCP models** to add or remove them. A model that is not enabled cannot be used, not even by administrators. |

### ③ Give users access

Go to **Settings → Users**, open a user, and under **Access Rights → AI Agent** choose:

| Access | Can do |
|---|---|
| **User** | Chat with the AI and see their own usage. |
| **Administrator** | Everything a User can, plus settings, providers, models and everyone's usage and cost. |

> AI Agent users do **not** need the *MCP User* group. That group is only for connecting **outside** AI apps to Odoo's `/mcp` endpoint (see [section 9](#9-mcp-what-it-is-and-cautions)).

### ④ Optional settings

| Setting | Default | What it does |
|---|---|---|
| **Share record values with the AI provider** | **Off** (recommended) | On: record values are also sent so the AI can summarise them in its own words. See [section 8](#8-privacy-exactly-what-is-sent-to-the-ai-provider). |
| **Resolve errors with AI** | Off | Adds a **Resolve with AI** button to Odoo error messages. |
| **Custom business-flow answers** | Off | Lets the AI answer from your own custom modules. Click **Index installed custom modules** after switching it on and after each custom-module update. |
| **Tool steps per answer** | 8 | How many lookups the AI may chain for one answer (1–25). |
| **Answer length** | 2,048 tokens | Maximum length of each AI reply (256–65,536). |
| **Provider timeout** | 60 s | How long to wait for the AI provider (10–600). |

---

## 5. Where users find it

| Place | How |
|---|---|
| **AI Agent app** | The **AI Agent** icon on the Odoo home screen: full-page chat with the list of past conversations. |
| **Chat window** | The **AI Agent** icon in the top bar opens a small chat window on any screen. |
| **Discuss** | The **AI Agent** tab in the messaging menu lists your AI conversations. |
| **Error dialogs** | The **Resolve with AI** button, when the administrator has turned it on. |
| **Usage & Cost** | **AI Agent → Reporting → Usage & Cost** (each user sees their own; administrators see everyone). |

---

## 6. Every feature, explained

### 💬 Ask in plain language
Type questions the way you'd ask a colleague, in any language, and spelling mistakes are fine. Follow-ups work too: *"only the sent ones"*, *"and last month?"*, *"show that as a pie chart"*.

### 📊 Tables, record cards and native Odoo charts
Results appear as tables and record cards rendered by Odoo. Charts are real Odoo **bar, line, pie and pivot** views. Click a row or a bar to open the records behind it.

### 📥 Export to Excel and PDF
Every table and chart has **Excel** and **PDF** buttons.

### 🔐 Your own access rights, always
The AI runs every lookup **as the user who asked**. A salesperson asking about *"all opportunities"* only gets the ones Odoo lets them see. Role-play such as *"pretend I'm an administrator"* changes nothing.

### 🧭 "Where is…?" and "Open the screen"
Ask *"Where do I find orders to invoice?"* and the AI names the menu path and gives a button that opens the screen, with the right filter applied.

### ⚙️ Settings questions
*"Is Lock Confirmed Orders switched on?"*, *"Where do I enable multi-currency?"* The AI reads the real settings screen and tells you if the option is on, and where it is.

### 👤 Access rights
*"What access rights do I have?"* shows the rights exactly as on the user form. Administrators can ask about any user, or *"Who has Sales Administrator access?"*. Non-administrators can only see their own.

### 🕓 History, changes and conversations
- *"Who changed the price of S00042?"*: the change history of a record (who, when, old and new value).
- *"Show the conversation on this opportunity"*: messages, notes and automatic updates from the chatter.
- *"How are my salespeople doing with customer conversations?"*: response times and follow-up statistics per salesperson, calculated by Odoo.

### 🧮 Calculations Odoo does for you
Win rate, % change, differences, averages: the AI writes the formula and **Odoo** computes it on the real values, so the AI never needs the numbers themselves.

### ✏️ Prepare changes, then confirm
*"Create a contact for Ravi Kumar, phone…"*, *"Schedule a call with Alpha Cycles tomorrow"*. The AI prepares a **preview card** showing exactly what will change. Nothing happens until you click **Confirm**. See [section 10](#10-always-review-changes-before-you-confirm).

### 🩺 Resolve with AI (error help)
When Odoo shows an error (*"The discount above 20% needs approval"*), the user can click **Resolve with AI**:
- **Normal users** get plain words: *what happened, why, and what to do next*.
- **Administrators** also get the technical details: the custom module, file and rule that raised it.
- **Continue in AI Agent** opens a chat about the error, to ask follow-up questions.

### 🏗️ Custom business-flow answers
With **Custom business-flow answers** on and your custom modules indexed, the AI can answer *"What happens when a purchase order is above 10,000?"* from **your own** custom code, Studio views and automation rules. Standard Odoo modules are not indexed.

### 📈 Usage and cost dashboard
**AI Agent → Reporting → Usage & Cost** shows requests, tokens and cost per day, model, provider and user, plus busiest hours and most-used tools.

### 🔎 "Data used": check how an answer was made
Under each answer, **Data used** lists the steps the AI took and the results Odoo produced, so anyone can check where an answer came from.

---

## 7. Questions to try

| Area | Try asking… |
|---|---|
| Sales | *"How many sales orders were confirmed this month and what is the total?"* · *"Compare confirmed sales this month with last month"* |
| CRM | *"Which opportunities are expected to close this month?"* · *"What is our win rate this quarter?"* · *"Which leads have overdue activities?"* |
| Invoicing | *"Which customer invoices are overdue?"* · *"How much payment did we receive this month?"* |
| Purchase | *"Total purchase amount by vendor this month"* · *"Which purchase orders are waiting for vendor bills?"* |
| Inventory | *"How much stock of Mountain Bike MB-200 do we have?"* · *"What was scrapped this month?"* |
| Employees | *"Show a bar chart of employees by department"* · *"Who joined this month?"* |
| Users | *"Which users have Settings / Administration access?"* · *"What access rights do I have?"* |
| Settings | *"Is Lock Confirmed Orders enabled?"* · *"Where do I turn on multi-currency?"* |
| History | *"Who changed the price on S00042?"* · *"Show the conversation on this opportunity"* |
| Charts | *"Pie chart of this month's sales by product"* · *"Line chart of invoices per month this year"* |
| Help | *"How do I convert a lead into an opportunity?"* · *"Where do I find orders to invoice?"* |

---

## 8. Privacy: exactly what is sent to the AI provider

| Sent to the AI provider | Data sharing **off** (default) | Data sharing **on** |
|---|---|---|
| The user's questions | ✅ | ✅ |
| Tool list and field definitions (*"an invoice has a due date"*) | ✅ | ✅ |
| Number of records found and their IDs | ✅ | ✅ |
| **Record values** (names, amounts, emails, notes…) | ❌ **Never** | ✅ |
| Passwords, API keys, tokens | ❌ Never | ❌ Never |
| Error help (only when a user clicks **Resolve with AI**) | Error message, model, action and the related custom code. Values only if sharing is on. | ✅ Also record values |
| Business-flow answers (when switched on) | Relevant excerpts of **your custom module code** | ✅ |

> ⚠️ **Questions are always sent.** If a user *types* personal data into a question (*"customer Jane Smith, phone 555…"*), that text goes to the AI provider. Tell users to ask by reference or name only.

> ⚠️ Your AI provider's own data policy applies to what is sent. Use business or API accounts that don't train on your data, and check the provider's terms.

---

## 9. MCP: what it is, and cautions

**MCP (Model Context Protocol)** is a standard way for AI to use tools. The **MCP Server** module gives Odoo a set of safe tools (*search records, read a record, total by group, create a record…*) and **checks every call against the user's access rights**. AI Agent runs these tools **inside Odoo**: no extra server or process.

### Cautions

- 🔑 **Enable only the models you need.** In *Manage MCP models*, every enabled model is a model the AI can work with. Leave sensitive models (payroll, salaries, private HR data) **off** unless you really need them.
- ✍️ **Give write rights sparingly.** *Create, Update, Delete* and *Buttons* on a model let the AI **prepare** those changes. Prefer **Read only**. Turn on *Delete* only if you really need it.
- 🌐 **The external `/mcp` endpoint is a separate door.** MCP Server can also let **outside** AI apps (Claude Desktop, ChatGPT…) connect to Odoo. That needs the **MCP User** group and an API key or login. Give the *MCP User* group only to people who need it, use **"MCP only"** API keys, and remove keys that are no longer used.
- 📜 **Check the MCP Logs.** MCP Server keeps an audit log of tool calls (its **MCP Logs** menu); review it now and then.
- 👥 **Access rights are the real protection.** The AI can never see more than the user, so keep Odoo user rights and record rules tidy.

---

## 10. Always review changes before you confirm

AI can misunderstand. That's why **every change is a preview first**:

```
 ┌─────────────────────────────────────────────┐
 │  Create Contact                             │
 │  Name:   Ravi Kumar                         │
 │  Phone:  +91 98765 43210                    │
 │  Company: Alpha Cycles                      │
 │                                             │
 │              [ Cancel ]   [ Confirm ]       │
 └─────────────────────────────────────────────┘
```

Before you click **Confirm**:

1. ✅ **Is it the right record?** Check the name, number and company. *"Ravi"* can match several people.
2. ✅ **Are the values right?** Dates, amounts, quantities and the person it's assigned to.
3. ✅ **Is it the right action?** *Update* versus *create new*, and which button will be pressed.
4. ✅ **When in doubt, Cancel** and ask again more precisely.

> The change runs with **your** access rights, and Odoo's own rules still apply. Still, **you are responsible for what you confirm**: the AI only prepares it.

---

## 11. Choosing a model and keeping costs low

| Provider | Good everyday choice | For harder questions |
|---|---|---|
| Anthropic Claude | Claude Haiku | Claude Sonnet |
| OpenAI | A "mini" model | The standard model |
| Google Gemini | A "Flash" or "Flash Lite" model | A "Pro" model |
| OpenRouter | Any of the above, through one key | |

Tips:
- Start with a **small, fast model**. Because AI Agent sends counts and IDs instead of records, answers stay short and cheap. A typical question costs a fraction of a cent.
- Users can pick another model of the **same provider** for a conversation.
- Watch **Usage & Cost** weekly. Lower **Tool steps per answer** or **Answer length** if costs grow.
- Set a **monthly budget limit** in your provider's console as a safety net.

---

## 12. Updating to a new version

**1. Get the new code:**

```bat
cd /d C:\Odoo\custom-addons
git pull origin main
```

**2. Stop Odoo** with **Ctrl + C** in its window.

**3. Upgrade the module.** Replace `YOUR_DATABASE` with your database name. The command stops by itself when it's done:

```bat
cd /d C:\Odoo\odoo
C:\Odoo\venv\Scripts\activate
python odoo-bin -c C:\Odoo\odoo.conf -d YOUR_DATABASE -u odoo_ai_agent --stop-after-init
```

**4. Start Odoo again**, then refresh the browser with **Ctrl + Shift + R**:

```bat
python odoo-bin -c C:\Odoo\odoo.conf
```

> 💡 Instead of step 3, you can start Odoo normally and click **Apps** → search **AI Agent** → **⋮** → **Upgrade**.

On Linux: `git pull origin main` in the module folder, restart Odoo, then **Upgrade** in Apps.

> 💡 If you use **Custom business-flow answers**, click **Index installed custom modules** again after you update your custom modules.

---

## 13. Debugging and troubleshooting

### Common problems

| Problem | Fix |
|---|---|
| **AI Agent isn't in Apps** | Check `addons_path` in the config file, restart Odoo, then **Apps → Update Apps List**, and remove the *Apps* filter when searching. |
| **"MCP Server" missing when installing** | Put the `mcp_server` folder next to `odoo_ai_agent` (same `C:\Odoo\custom-addons` folder, no folder-inside-a-folder) and update the apps list. |
| **Installation error about a Python module** | Run `C:\Odoo\venv\Scripts\activate` first, then `pip install` (Step 5). Packages installed into another Python on your PC don't help Odoo. |
| **`python` is not recognized, or the wrong Python runs** | Run `C:\Odoo\venv\Scripts\activate` first: the prompt then starts with `(venv)`. |
| **`git` is not recognized** | Install Git for Windows, then open a **new** Command Prompt. |
| **Odoo stops right after starting** | Usually a typo in `odoo.conf` (check the commas in `addons_path`). The exact error is shown in the Odoo window. |
| **"Address already in use" (port 8069)** | Odoo is already running in another window. Stop it there (Ctrl + C) and start again. |
| **"No AI model is selected"** | Settings → choose a provider, paste the key, **Refresh available models**, choose a model, **Save**. |
| **"Invalid API key" / "401"** | The key is wrong, expired, or belongs to another provider. Create a new one in the provider's console. |
| **"Quota exceeded" / "429" / "insufficient credit"** | Add credit or raise the limit in the provider's billing page, or wait a minute and retry. |
| **"Odoo could not reach the provider"** | No internet access, or a firewall, antivirus or proxy blocks Odoo. In Command Prompt, try `curl https://api.openai.com` (or your provider's address). |
| **Answer is slow or times out** | Raise **Provider timeout**, pick a faster model, or ask a narrower question. |
| **"Not enabled for the AI"** | The model isn't in **Manage MCP models**, or the operation (create/update…) isn't ticked for it. |
| **"You don't have access to…"** | Working as intended: the user's Odoo rights don't allow it. Change the user's access rights if they should. |
| **The user can't see AI Agent** | Give them **Access Rights → AI Agent → User**. |
| **No "Resolve with AI" button on errors** | Switch on **Resolve errors with AI** in the settings, and refresh the browser. |
| **Changes in the code don't show** | Stop Odoo (Ctrl + C), upgrade the module and start it again (section 12), then hard-refresh the browser (Ctrl + Shift + R). |
| **The answer looks wrong** | Check the table or chart under the answer (that's Odoo's real data) and **Data used**; ask again more precisely, or try a stronger model. |

### Where to look

- **The Odoo window:** Odoo started with `python odoo-bin -c C:\Odoo\odoo.conf` shows its log in that window. Problems appear as `ERROR` lines, with the details below them.
- **Log to a file instead:** set `logfile = C:\Odoo\odoo.log` in `odoo.conf` and restart Odoo. Then follow only the AI Agent lines in **PowerShell**:
  ```powershell
  Get-Content C:\Odoo\odoo.log -Wait -Tail 50 | Select-String "AI Agent"
  ```
- **"Data used" under an answer** shows each step the AI took and the results Odoo produced: the first place to look when an answer seems wrong.
- **More detail in the log:** start Odoo with this extra option to see every step, including ones that were skipped on purpose:
  ```bat
  python odoo-bin -c C:\Odoo\odoo.conf --log-handler=odoo.addons.odoo_ai_agent:DEBUG
  ```
- **Usage & Cost** shows failed requests and their error messages per user and model.
- **MCP Server logs** (in its menu) list every tool call with its result, user and time.

---

## 14. Useful links

| What | Link |
|---|---|
| MCP Server module (Odoo Apps store) | https://apps.odoo.com/apps/modules/19.0/mcp_server |
| OpenAI API keys | https://platform.openai.com/api-keys |
| Google Gemini API keys | https://aistudio.google.com/app/apikey |
| Anthropic Claude API keys | https://console.anthropic.com/settings/keys |
| OpenRouter API keys | https://openrouter.ai/settings/keys |
| OpenAI pricing | https://openai.com/api/pricing |
| Google Gemini pricing | https://ai.google.dev/gemini-api/docs/pricing |
| Anthropic Claude pricing | https://www.anthropic.com/pricing |
| OpenRouter models and prices | https://openrouter.ai/models |
| Model Context Protocol (MCP) | https://modelcontextprotocol.io |
| Odoo 19 documentation | https://www.odoo.com/documentation/19.0/ |

---

<div align="center">

**AI Agent for Odoo 19** · LGPL-3

*Ask freely, let Odoo answer, and always review before you confirm.*

</div>
