"""Admin-built, read-only index of installed custom-module implementation details.

Only modules that are specific to this instance are indexed: standard Odoo (community and enterprise, by
location or by author) and the AI Agent / MCP Server modules themselves are skipped. Python code is split per
method (with its class and model), XML per record or template, and each module gets an overview chunk
(models, added fields, overridden methods, error messages it raises), so questions about a business flow and
"why did this error appear" can be answered from the right piece of code.
"""

import ast
import json
import os
import re
from datetime import timedelta
from pathlib import Path

import odoo
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.fields import Domain
from odoo.modules.module import get_module_path
from odoo.tools import config


SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|token|secret|password|credential|client[_-]?secret|private[_-]?key)\b"
    r"\s*[:=]\s*)(['\"])[^'\"]{4,}\2"
)
SECRET_TOKEN = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,})\b"
)
PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL
)
BEARER_TOKEN = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{12,}")
SOURCE_SUFFIXES = {".py", ".xml", ".csv", ".js"}
SKIP_DIRS = {"__pycache__", ".git", ".hg", ".svn", "node_modules", "tests", "test", "migrations", "i18n",
             "static/lib", "lib"}
# The AI Agent and the MCP server are tools, not this instance's business flow.
SELF_MODULES = {"odoo_ai_agent", "mcp_server"}
MAX_INDEX_CHUNKS = 8000
SMALL_MODULE_METHODS = 6  # a model with this many custom methods or fewer is shown whole
MAX_EXCERPTS = 10  # excerpts sent to the AI for one question
MAX_FILE_BYTES = 256_000
CHUNK_LINES = 40
CHUNK_CHARS = 3000
QUERY_STOPWORDS = {
    "how", "does", "our", "custom", "process", "work", "works", "what", "which", "is", "are", "the",
    "this", "that", "in", "of", "for", "as", "me", "tell", "please", "explain", "show", "your", "when", "why",
    "do", "we", "can", "and", "with", "flow", "instance", "odoo", "about", "there", "any", "get", "has", "have",
}
ERROR_CLASSES = {"UserError", "ValidationError", "AccessError", "RedirectWarning", "MissingError", "Warning"}
XML_BLOCK = re.compile(r"<(record|template|menuitem)\b.*?(?:/>|</\1>)", re.DOTALL)
PLACEHOLDER = re.compile(r"%\(\w+\)[sdfr]|%[sdfr]|\{\w*\}")


def redact_secrets(text):
    text = SECRET_ASSIGNMENT.sub(r"\1\2[redacted]\2", text or "")
    text = SECRET_TOKEN.sub("[redacted token]", text)
    text = BEARER_TOKEN.sub(r"\1[redacted token]", text)
    return PRIVATE_KEY_BLOCK.sub("[private key redacted]", text)


def _literal(node):
    """The text of a string literal, also inside _("...") or "...".format()/% calls."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Call) and node.args:
        return _literal(node.args[0])
    if isinstance(node, ast.BinOp):
        return _literal(node.left)
    return None


def _raised_messages(node):
    """Messages of the Odoo errors raised in this code (raise UserError(_("..."))...)."""
    messages = []
    for item in ast.walk(node):
        if isinstance(item, ast.Raise) and isinstance(item.exc, ast.Call):
            func = item.exc.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name in ERROR_CLASSES and item.exc.args:
                text = _literal(item.exc.args[0])
                if text and text.strip():
                    messages.append(" ".join(text.split())[:400])
    return list(dict.fromkeys(messages))


def _class_model(node):
    """_name or _inherit of an Odoo model class."""
    found = {}
    for item in node.body:
        if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
            key = item.targets[0].id
            if key in ("_name", "_inherit"):
                value = item.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    found[key] = value.value
                elif isinstance(value, (ast.List, ast.Tuple)) and value.elts:
                    names = [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                    if names:
                        found[key] = names[0]
    return found.get("_name") or found.get("_inherit") or "", bool(found.get("_name")) and not found.get("_inherit")


def _class_fields(node):
    """Fields declared in a model class: (name, type, label)."""
    out = []
    for item in node.body:
        if not (isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name)):
            continue
        call = item.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name) and call.func.value.id == "fields"):
            continue
        label = None
        for keyword in call.keywords:
            if keyword.arg == "string":
                label = _literal(keyword.value)
        if label is None and call.args and call.func.attr not in ("Many2one", "One2many", "Many2many", "Selection"):
            label = _literal(call.args[0])
        out.append((item.targets[0].id, call.func.attr, label or ""))
    return out


def python_chunks(text, relative):
    """One chunk per method (with its class and model) and one summary per model class."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.splitlines()
    chunks, summaries = [], []

    def source(node):
        start = min([d.lineno for d in getattr(node, "decorator_list", [])] + [node.lineno])
        return start, node.end_lineno or node.lineno, "\n".join(lines[start - 1:node.end_lineno])

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            model, new = _class_model(node)
            declared = _class_fields(node)
            methods = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            header = f"# {relative} · class {node.name}" + (f" · model {model}" + (" (new)" if new else " (extends)")
                                                             if model else "")
            for method in methods:
                start, end, code = source(method)
                chunks.append({
                    "kind": "method", "model_name": model, "symbol": f"{node.name}.{method.name}",
                    "line_start": start, "line_end": end, "messages": _raised_messages(method),
                    "content": (header + f" · method {method.name}\n" + code)[:CHUNK_CHARS],
                })
            field_text = "; ".join(f"{n} ({t}{', ' + repr(lbl) if lbl else ''})" for n, t, lbl in declared)
            summary = [header]
            if field_text:
                summary.append(f"Fields: {field_text}")
            if methods:
                summary.append("Methods: " + ", ".join(m.name for m in methods))
            constraints = [m.name for m in methods for d in m.decorator_list
                           if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "constrains"]
            if constraints:
                summary.append("Constraints: " + ", ".join(constraints))
            messages = _raised_messages(node)
            if messages:
                summary.append("Errors raised: " + " | ".join(messages))
            if model and (declared or methods):
                summaries.append({"model": model, "new": new, "class": node.name, "path": relative,
                                  "fields": declared, "methods": [m.name for m in methods], "messages": messages})
            chunks.append({
                "kind": "class", "model_name": model, "symbol": node.name, "line_start": node.lineno,
                "line_end": node.end_lineno or node.lineno, "messages": messages,
                "content": "\n".join(summary)[:CHUNK_CHARS],
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start, end, code = source(node)
            chunks.append({
                "kind": "method", "model_name": "", "symbol": node.name, "line_start": start, "line_end": end,
                "messages": _raised_messages(node), "content": (f"# {relative} · function {node.name}\n" + code)[:CHUNK_CHARS],
            })
    return chunks, summaries


def xml_chunks(text, relative):
    """One chunk per record, template or menu item, with the model it is about."""
    chunks = []
    for match in XML_BLOCK.finditer(text):
        block = match.group(0)
        line = text.count("\n", 0, match.start()) + 1
        model = re.search(r'<field name="model">([\w.]+)</field>', block) or re.search(r'model="([\w.]+)"', block)
        ident = re.search(r'id="([\w.]+)"', block)
        chunks.append({
            "kind": "view" if "ir.ui.view" in block[:200] or match.group(1) == "template" else "data",
            "model_name": model.group(1) if model else "", "symbol": ident.group(1) if ident else match.group(1),
            "line_start": line, "line_end": line + block.count("\n"), "messages": [],
            "content": (f"<!-- {relative} -->\n" + block)[:CHUNK_CHARS],
        })
    return chunks


def window_chunks(text, relative):
    lines = text.splitlines()
    out = []
    for offset in range(0, len(lines), CHUNK_LINES):
        content = "\n".join(lines[offset:offset + CHUNK_LINES])[:CHUNK_CHARS].strip()
        if content:
            out.append({"kind": "other", "model_name": "", "symbol": "", "line_start": offset + 1,
                        "line_end": min(len(lines), offset + CHUNK_LINES), "messages": [],
                        "content": f"// {relative}\n{content}"})
    return out


def message_match(literal, message):
    """How much of an error template ("Order %s needs approval") the shown message contains, from 0 to 1."""
    words = [w for part in PLACEHOLDER.split(literal or "") for w in re.findall(r"\w{3,}", part.lower())]
    if not words:
        return 0.0
    shown = message.lower()
    found = sum(1 for w in words if w in shown)
    return found / len(words)


class AiAgentFlowIndex(models.Model):
    _name = "ai_agent.flow.index"
    _description = "AI Agent Business Flow Source Index"
    _order = "module_name, source_path, line_start"

    module_name = fields.Char(required=True, index=True)
    source_path = fields.Char(required=True, index=True)
    line_start = fields.Integer(required=True)
    line_end = fields.Integer()
    kind = fields.Char(index=True)  # overview, class, method, view, data, other, database
    model_name = fields.Char(index=True)
    symbol = fields.Char()
    messages = fields.Text()  # JSON list of the error messages this code raises
    content = fields.Text(required=True)

    # ------------------------------------------------------------------
    # Which modules are this instance's own
    # ------------------------------------------------------------------
    @api.model
    def _standard_roots(self):
        """Folders of the Odoo distribution itself (base/web and the community addons next to it)."""
        roots = set()
        for base in list(getattr(odoo, "__path__", []) or []):
            base = Path(base).resolve()
            roots.add(base / "addons")
            roots.add(base.parent / "addons")
        return {root for root in roots if root.is_dir()}

    @api.model
    def _custom_modules(self):
        """(module, path) of installed modules specific to this instance."""
        addon_roots = [Path(path).resolve() for path in config.get("addons_path", []) if Path(path).is_dir()]
        standard = self._standard_roots()
        out = []
        for module in self.env["ir.module.module"].sudo().search([("state", "=", "installed")], order="name"):
            if module.name in SELF_MODULES or "odoo s.a" in (module.author or "").lower():
                continue  # the AI tools themselves, and standard / enterprise modules by Odoo S.A.
            module_path = get_module_path(module.name, display_warning=False)
            if not module_path:
                continue
            root = Path(module_path).resolve()
            if any(root.parent == folder or folder in root.parents for folder in standard):
                continue
            if not any(root == folder or folder in root.parents for folder in addon_roots):
                continue
            out.append((module, root))
        return out

    # ------------------------------------------------------------------
    # Building the index
    # ------------------------------------------------------------------
    @api.model
    def action_rebuild(self):
        if not self.env.user.has_group("odoo_ai_agent.group_ai_agent_manager"):
            raise AccessError(_("Only an AI Agent administrator can rebuild the business-flow index."))
        params = self.env["ir.config_parameter"].sudo()
        if params.get_param("odoo_ai_agent.business_flow_enabled") not in ("True", "1", "true"):
            raise UserError(_("Enable business-flow answers before rebuilding the index."))
        return self._rebuild()

    @api.model
    def _rebuild(self):
        """Index the custom code, then build the Business Flow menu from it. No AI provider is called."""
        indexed = [dict(chunk, kind="database") for chunk in self._database_flow_chunks()[:1000]]
        modules = self._custom_modules()
        for module, root in modules:
            if len(indexed) >= MAX_INDEX_CHUNKS:
                break
            indexed += self._module_chunks(module, root, MAX_INDEX_CHUNKS - len(indexed))
        self.sudo().search([]).unlink()
        for chunk in indexed:
            chunk["messages"] = json.dumps(chunk.get("messages") or [], ensure_ascii=False) if chunk.get("messages") else False
        for offset in range(0, len(indexed), 500):
            self.sudo().create(indexed[offset:offset + 500])
        # The flows read the custom rules from the index just written, and are then indexed for chat questions.
        self.sudo().create(self.env["ai_agent.business.flow"]._rebuild({module.name for module, _root in modules}))
        self.env["ir.config_parameter"].sudo().set_param("odoo_ai_agent.business_flow_indexed_at",
                                                         fields.Datetime.now())
        return len(indexed)

    @api.model
    def _cron_refresh(self):
        """Scheduled update: rebuild when the context is older than the days set in Settings (0 = never)."""
        params = self.env["ir.config_parameter"].sudo()
        days = int(params.get_param("odoo_ai_agent.business_flow_refresh_days") or 0)
        if days <= 0 or params.get_param("odoo_ai_agent.business_flow_enabled") not in ("True", "1", "true"):
            return
        last = fields.Datetime.to_datetime(params.get_param("odoo_ai_agent.business_flow_indexed_at") or False)
        if not last or fields.Datetime.now() - last >= timedelta(days=days):
            self._rebuild()

    @api.model
    def _module_chunks(self, module, root, room):
        chunks, summaries = [], []
        for folder, directories, filenames in os.walk(root, followlinks=False):
            folder_path = Path(folder)
            directories[:] = sorted(name for name in directories
                                    if name not in SKIP_DIRS and not name.startswith(".")
                                    and not (folder_path / name).is_symlink())
            for filename in sorted(filenames):
                source = folder_path / filename
                if source.suffix.lower() not in SOURCE_SUFFIXES or filename.startswith("."):
                    continue
                try:
                    if source.is_symlink() or source.stat().st_size > MAX_FILE_BYTES:
                        continue
                    relative = source.relative_to(root).as_posix()
                    text = redact_secrets(source.read_text(encoding="utf-8", errors="ignore"))
                except (OSError, ValueError):
                    continue
                if relative.startswith("static/lib/"):
                    continue
                parts = None
                if source.suffix == ".py":
                    parsed = python_chunks(text, relative)
                    if parsed:
                        parts, found = parsed
                        summaries += found
                elif source.suffix == ".xml":
                    parts = xml_chunks(text, relative)
                if not parts:
                    parts = window_chunks(text, relative)
                for part in parts:
                    chunks.append(dict(part, module_name=module.name, source_path=relative))
                if len(chunks) >= room:
                    break
            if len(chunks) >= room:
                break
        overview = self._overview(module, root, summaries)
        return [overview] + chunks[:max(0, room - 1)]

    @api.model
    def _overview(self, module, root, summaries):
        """What the module adds to this instance, in one chunk the AI can read first."""
        manifest = {}
        try:
            manifest = ast.literal_eval((root / "__manifest__.py").read_text(encoding="utf-8", errors="ignore"))
        except (OSError, ValueError, SyntaxError):
            manifest = {}
        lines = [f"Custom module {module.name}: {module.shortdesc or manifest.get('name') or module.name}"]
        if manifest.get("summary"):
            lines.append(f"Summary: {manifest['summary']}")
        description = " ".join(str(manifest.get("description") or "").split())[:500]
        if description:
            lines.append(f"Description: {description}")
        if manifest.get("depends"):
            lines.append("Depends on: " + ", ".join(manifest["depends"]))
        messages = []
        for item in summaries:
            what = "new model" if item["new"] else "extends"
            line = f"- {what} {item['model']} ({item['path']}, class {item['class']})"
            if item["fields"]:
                line += ": adds fields " + ", ".join(f"{n} ({lbl or t})" for n, t, lbl in item["fields"][:15])
            overridden = [m for m in item["methods"] if not m.startswith("_compute") and not m.startswith("_onchange")]
            if overridden:
                line += "; methods " + ", ".join(overridden[:15])
            lines.append(line)
            messages += item["messages"]
        if messages:
            lines.append("Error messages it can show: " + " | ".join(dict.fromkeys(messages)))
        return {"kind": "overview", "module_name": module.name, "source_path": "__manifest__.py", "line_start": 1,
                "line_end": 1, "model_name": "", "symbol": module.name, "messages": list(dict.fromkeys(messages)),
                "content": redact_secrets("\n".join(lines))[:CHUNK_CHARS]}

    @api.model
    def _database_flow_chunks(self):
        """Capture database-defined Studio views and automation rules without reading business records."""
        chunks = []
        ModelData = self.env["ir.model.data"].sudo()
        studio_views = ModelData.search([
            ("model", "=", "ir.ui.view"), ("module", "ilike", "studio_customization"),
        ], limit=300)
        views = self.env["ir.ui.view"].sudo().browse(studio_views.mapped("res_id")).exists()
        for view in views:
            content = f"Studio view: {view.name}\nModel: {view.model}\nType: {view.type}\n{view.arch_db or ''}"
            content = redact_secrets(content)[:CHUNK_CHARS]
            chunks.append({
                "module_name": "database customization",
                "source_path": f"Studio view: {view.name} ({view.model or 'unknown model'})",
                "line_start": 1, "model_name": view.model or "", "symbol": view.name,
                "content": content,
            })

        studio_actions = ModelData.search([
            ("model", "=", "ir.actions.server"), ("module", "ilike", "studio_customization"),
        ], limit=300)
        actions = self.env["ir.actions.server"].sudo().browse(studio_actions.mapped("res_id")).exists()
        for action in actions:
            model_name = action.model_id.model if action.model_id else "unknown model"
            lines = [f"Studio server action: {action.name}", f"Model: {model_name}", f"Action type: {action.state}"]
            if action.state == "code" and "code" in action._fields:
                lines.append(redact_secrets(action.code or "")[:1800])
            content = "\n".join(lines)[:CHUNK_CHARS]
            chunks.append({
                "module_name": "database customization",
                "source_path": f"Studio server action: {action.name} ({model_name})",
                "line_start": 1, "model_name": action.model_id.model if action.model_id else "", "symbol": action.name,
                "content": content,
            })

        if "base.automation" in self.env:
            Automation = self.env["base.automation"].sudo()
            available = set(Automation._fields)
            fields_to_read = [name for name in ("name", "model_id", "trigger", "filter_domain", "active",
                                                "action_server_ids") if name in available]
            for rule in Automation.search([], limit=200):
                model_name = rule.model_id.model if "model_id" in available and rule.model_id else "unknown model"
                lines = [f"Automation: {rule.name}", f"Model: {model_name}"]
                for field_name in ("active", "trigger", "filter_domain"):
                    if field_name in fields_to_read:
                        lines.append(f"{field_name}: {getattr(rule, field_name) or ''}")
                if "action_server_ids" in fields_to_read:
                    for action in rule.action_server_ids[:5]:
                        lines.extend([f"Action: {action.name}", f"Action type: {action.state}"])
                        if action.state == "code" and "code" in action._fields:
                            lines.append(redact_secrets(action.code or "")[:1200])
                content = "\n".join(lines)[:CHUNK_CHARS]
                chunks.append({
                    "module_name": "database customization",
                    "source_path": f"Automation: {rule.name} ({model_name})",
                    "line_start": 1, "model_name": model_name if model_name != "unknown model" else "",
                    "symbol": rule.name, "content": content,
                })
        return chunks

    # ------------------------------------------------------------------
    # Searching
    # ------------------------------------------------------------------
    @api.model
    def _check_search_access(self):
        if (not self.env.user.has_group("odoo_ai_agent.group_ai_agent_user")
                or self.env["ir.config_parameter"].sudo().get_param(
                    "odoo_ai_agent.business_flow_enabled") not in ("True", "1", "true")):
            raise AccessError(_("Custom business-flow search is disabled or you do not have AI Agent access."))

    @api.model
    def _terms(self, query):
        raw_terms = list(dict.fromkeys(re.findall(r"[\w.-]{3,}", (query or "").lower())))[:10]
        terms = []
        for term in raw_terms:
            if term in QUERY_STOPWORDS:
                continue
            terms.append(term)
            if term.endswith("ies") and len(term) > 5:
                terms.append(term[:-3] + "y")
            elif term.endswith("s") and len(term) > 4:
                terms.append(term[:-1])
            if "_" in term:
                terms += [t for t in term.split("_") if len(t) > 3]
        return list(dict.fromkeys(terms))

    @api.model
    def _model_terms(self, terms):
        """Business words to technical models: 'purchase order' -> purchase.order, via the model names."""
        models_found = set()
        Model = self.env["ir.model"].sudo()
        for term in terms:
            if "." in term and term in self.env:
                models_found.add(term)
        if terms:
            indexed = set(self.sudo().search([("model_name", "!=", False)]).mapped("model_name"))
            for record in Model.search([("model", "in", list(indexed))]):
                label = (record.name or "").lower()
                if any(term in label.split() or term in record.model.split(".") for term in terms):
                    models_found.add(record.model)
        return models_found

    @api.model
    def search_for_question(self, query, limit=5, model=None):
        self._check_search_access()
        terms = self._terms(query)
        if not terms and not model:
            return []
        wanted_models = self._model_terms(terms)
        if model:
            wanted_models.add(model)
        parts = [[("content", "ilike", term)] for term in terms]
        if wanted_models:
            parts.append([("model_name", "in", list(wanted_models))])
        records = self.sudo().search(Domain.OR(parts), limit=600)

        def score(record):
            head = f"{record.module_name} {record.source_path} {record.symbol or ''}".lower()
            body = (record.content or "").lower()
            value = sum(3 if term in head else 1 for term in terms if term in head or term in body)
            if record.model_name and record.model_name in wanted_models:
                value += 3
            if record.messages and any(term in record.messages.lower() for term in terms):
                value += 2
            if record.kind in ("overview", "class"):
                value += 1  # what the module adds: a good first read
            return value

        ranked = sorted(records, key=score, reverse=True)
        top = [r for r in ranked[:max(1, min(int(limit or 5), 8))] if score(r)]
        # The overview of the modules found, so the answer sees the whole flow, not only one method.
        modules = {r.module_name for r in top}
        overviews = self.sudo().search([("kind", "=", "overview"), ("module_name", "in", list(modules))])
        top = list(overviews.filtered(lambda o: o not in top)[:2]) + top
        # "What custom rules do we have on sales orders?": for a small module, every method of the model asked
        # about, so the answer lists all its rules and not only the best-matching ones.
        models_asked = wanted_models or {r.model_name for r in top if r.model_name}
        for module in modules:
            methods = self.sudo().search([("module_name", "=", module), ("kind", "=", "method"),
                                          ("model_name", "in", list(models_asked))])
            if len(methods) <= SMALL_MODULE_METHODS:
                top += [m for m in methods if m not in top]
        return [self._excerpt(record) for record in top[:MAX_EXCERPTS]]

    @api.model
    def find_error_source(self, message, frames=None, model=None):
        """The code that raised this error: the error template matching the shown message, and for a crash
        the custom-module frames of its traceback."""
        self._check_search_access()
        found = []
        if message:
            candidates = self.sudo().search([("messages", "!=", False), ("kind", "in", ("method", "class"))],
                                            limit=3000)
            best = []
            for record in candidates:
                try:
                    templates = json.loads(record.messages)
                except ValueError:
                    continue
                ratio = max((message_match(t, message) for t in templates), default=0.0)
                if ratio >= 0.6:
                    bonus = 0.05 if model and record.model_name == model else 0.0
                    best.append((ratio + bonus + (0.02 if record.kind == "method" else 0.0), record))
            best.sort(key=lambda pair: pair[0], reverse=True)
            found += [record for _ratio, record in best[:2]]
        for frame in (frames or [])[-4:]:
            record = self.sudo().search([
                ("module_name", "=", frame["module"]), ("source_path", "=", frame["path"]),
                ("kind", "=", "method"), ("line_start", "<=", frame["line"]), ("line_end", ">=", frame["line"]),
            ], limit=1)
            if record and record not in found:
                found.append(record)
        return [self._excerpt(record) for record in found[:4]]

    @api.model
    def _excerpt(self, record):
        return {
            "module": record.module_name,
            "path": record.source_path,
            "line": record.line_start,
            "kind": record.kind or "",
            "model": record.model_name or "",
            "symbol": record.symbol or "",
            "excerpt": record.content,
        }

    @api.model
    def custom_frames(self, traceback_text):
        """Frames of a server traceback that are in this instance's custom modules, as (module, path, line)."""
        roots = {str(root): module.name for module, root in self._custom_modules()}
        frames = []
        for path, line, function in re.findall(r'File "([^"]+)", line (\d+), in (\w+)', traceback_text or ""):
            try:
                resolved = Path(path).resolve()
            except (OSError, ValueError):
                continue
            for root, module in roots.items():
                root_path = Path(root)
                if root_path == resolved or root_path in resolved.parents:
                    frames.append({"module": module, "path": resolved.relative_to(root_path).as_posix(),
                                   "line": int(line), "function": function})
                    break
        return frames[-6:]
