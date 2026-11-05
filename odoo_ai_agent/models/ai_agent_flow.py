"""Admin-built, read-only index of installed custom-module implementation details."""

import os
import re
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
SKIP_DIRS = {"__pycache__", ".git", ".hg", ".svn", "node_modules", "tests", "test", "migrations"}
MAX_INDEX_CHUNKS = 8000
MAX_FILE_BYTES = 256_000
CHUNK_LINES = 32
CHUNK_CHARS = 2200
QUERY_STOPWORDS = {
    "how", "does", "our", "custom", "process", "work", "works", "what", "which", "is", "are", "the",
    "this", "that", "in", "of", "for", "as", "me", "tell", "please", "explain", "show", "your",
}


def redact_secrets(text):
    text = SECRET_ASSIGNMENT.sub(r"\1\2[redacted]\2", text or "")
    text = SECRET_TOKEN.sub("[redacted token]", text)
    text = BEARER_TOKEN.sub(r"\1[redacted token]", text)
    return PRIVATE_KEY_BLOCK.sub("[private key redacted]", text)


class AiAgentFlowIndex(models.Model):
    _name = "ai_agent.flow.index"
    _description = "AI Agent Business Flow Source Index"
    _order = "module_name, source_path, line_start"

    module_name = fields.Char(required=True, index=True)
    source_path = fields.Char(required=True, index=True)
    line_start = fields.Integer(required=True)
    content = fields.Text(required=True)

    @api.model
    def action_rebuild(self):
        if not self.env.user.has_group("odoo_ai_agent.group_ai_agent_manager"):
            raise AccessError(_("Only an AI Agent administrator can rebuild the business-flow index."))
        params = self.env["ir.config_parameter"].sudo()
        if params.get_param("odoo_ai_agent.business_flow_enabled") not in ("True", "1", "true"):
            raise UserError(_("Enable business-flow answers before rebuilding the index."))

        roots = [Path(path).resolve() for path in config.get("addons_path", []) if Path(path).is_dir()]
        core_addons = (Path(odoo.__file__).resolve().parent / "addons").resolve()
        modules = self.env["ir.module.module"].sudo().search([("state", "=", "installed")])
        indexed = self._database_flow_chunks()[:min(1000, MAX_INDEX_CHUNKS)]
        for module in modules:
            module_path = get_module_path(module.name, display_warning=False)
            if not module_path:
                continue
            root = Path(module_path).resolve()
            if root == core_addons or core_addons in root.parents:
                continue  # Core Odoo is not instance-specific workflow documentation.
            if not any(root == addon_root or addon_root in root.parents for addon_root in roots):
                continue
            for folder, directories, filenames in os.walk(root, followlinks=False):
                folder_path = Path(folder)
                directories[:] = [name for name in directories
                                  if name not in SKIP_DIRS and not (folder_path / name).is_symlink()]
                for filename in filenames:
                    if len(indexed) >= MAX_INDEX_CHUNKS or Path(filename).suffix.lower() not in SOURCE_SUFFIXES:
                        if len(indexed) >= MAX_INDEX_CHUNKS:
                            break
                        continue
                    source = folder_path / filename
                    try:
                        if source.is_symlink() or source.stat().st_size > MAX_FILE_BYTES:
                            continue
                    except OSError:
                        continue
                    try:
                        relative = source.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    if any(part.startswith(".") for part in Path(relative).parts):
                        continue
                    try:
                        text = source.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    text = redact_secrets(text)
                    lines = text.splitlines()
                    for offset in range(0, len(lines), CHUNK_LINES):
                        content = "\n".join(lines[offset:offset + CHUNK_LINES])[:CHUNK_CHARS].strip()
                        if content:
                            indexed.append({
                                "module_name": module.name,
                                "source_path": relative,
                                "line_start": offset + 1,
                                "content": content,
                            })
                        if len(indexed) >= MAX_INDEX_CHUNKS:
                            break
                if len(indexed) >= MAX_INDEX_CHUNKS:
                    break
            if len(indexed) >= MAX_INDEX_CHUNKS:
                break
        self.sudo().search([]).unlink()
        for offset in range(0, len(indexed), 500):
            self.sudo().create(indexed[offset:offset + 500])
        params.set_param("odoo_ai_agent.business_flow_indexed_at", fields.Datetime.now())
        return len(indexed)

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
                "line_start": 1,
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
                "line_start": 1,
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
                    "line_start": 1,
                    "content": content,
                })
        return chunks

    @api.model
    def search_for_question(self, query, limit=4):
        if (not self.env.user.has_group("odoo_ai_agent.group_ai_agent_user")
                or self.env["ir.config_parameter"].sudo().get_param(
                    "odoo_ai_agent.business_flow_enabled") not in ("True", "1", "true")):
            raise AccessError(_("Custom business-flow search is disabled or you do not have AI Agent access."))
        raw_terms = list(dict.fromkeys(re.findall(r"[\w.-]{3,}", (query or "").lower())))[:8]
        terms = []
        for term in raw_terms:
            if term in QUERY_STOPWORDS:
                continue
            terms.append(term)
            if term.endswith("ies") and len(term) > 5:
                terms.append(term[:-3] + "y")
            elif term.endswith("s") and len(term) > 4:
                terms.append(term[:-1])
        terms = list(dict.fromkeys(terms))
        if not terms:
            return []
        domain = Domain.OR([["content", "ilike", term] for term in terms])
        records = self.sudo().search(domain, limit=300)
        query_terms = set(terms)

        def score(record):
            haystack = f"{record.module_name} {record.source_path} {record.content}".lower()
            return sum(3 if term in f"{record.module_name} {record.source_path}".lower() else 1
                       for term in query_terms if term in haystack)

        records = sorted(records, key=score, reverse=True)[:max(1, min(int(limit or 4), 6))]
        return [{
            "module": record.module_name,
            "path": record.source_path,
            "line": record.line_start,
            "excerpt": record.content,
        } for record in records if score(record)]
