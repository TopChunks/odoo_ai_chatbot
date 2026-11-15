"""Checks around each tool call: notes under tables, domain repair, apps and settings, error messages and MCP calls.
Part of the ai_agent.engine model (see ai_agent_engine.py).
"""

import ast
import difflib
import json
import logging
import re
import uuid

from odoo import _, api, models
from odoo.exceptions import AccessError, UserError
from odoo.fields import Domain
from odoo.http import request
from odoo.tools import ormcache
from odoo.tools.safe_eval import safe_eval

from odoo.addons.mcp_server.controllers import utils as mcp_utils
from odoo.addons.mcp_server.tools.smart_fields import is_sensitive_field_name

from .ai_agent_engine_constants import (
    APP_GATE_NOT_PARENTS, COMPARE_INTENT, CONVERSATION_SCORE_TEXT, COST_FIELDS, COST_GROUPS, COST_HIDDEN,
    FIELD_ACCESS_ERROR, FORM_TAB_VIEW_CAP, HIGHLIGHT_CLAIM, LIMIT_WORDS, LOWEST,
    OPERATION_CHECKBOXES, SCHEMA_TOOLS, SCREEN_OUTLINE_CAP, SETTING_SECRET, SETTING_STOPWORDS, SUPERLATIVE,
    TOOL_OPERATIONS, TRACE_NOTE_CAP,
)

_logger = logging.getLogger(__name__)


class AiAgentEngineChecks(models.AbstractModel):
    _inherit = "ai_agent.engine"

    @api.model
    def _add_top_note(self, state, text=""):
        """"Which product sold the most?": Odoo names the top row itself (the AI never sees the values)."""
        question = state.get("question") or ""
        tables = [b for b in state["blocks"] if b.get("type") == "table" and not b.get("collapsed") and b.get("rows")
                  and not b.get("pick_list")]  # a pick list asks a question: it has no winner
        charts = [b for b in state["blocks"] if b.get("type") == "chart" and not b.get("collapsed")]
        for block in tables:
            names = {c["name"] for c in block.get("columns") or []}
            if "score" not in names and CONVERSATION_SCORE_TEXT in (block.get("subtitle") or ""):
                # The score is not shown in this table, so its explanation does not belong under it either.
                block["subtitle"] = " · ".join(p for p in block["subtitle"].split(" · ") if p != CONVERSATION_SCORE_TEXT)
        if COMPARE_INTENT.search(question):
            self._add_difference_note(state, tables, charts)
        match = SUPERLATIVE.search(LIMIT_WORDS.sub("", question))  # "below their minimum" is a limit, not the lowest
        scored = next((b for b in tables if any(c["name"] == "score" for c in b["columns"])), None)
        if scored is not None and (not match or self._question_column(scored, question) in (None, "score")):
            # A salesperson comparison that shows the conversation score: name the best score.
            self._top_note(state, scored, next(c for c in scored["columns"] if c["name"] == "score"), False,
                           _("Highest conversation score"), suffix=_(" of 100"),
                           eligible=lambda r: r["values"].get("enough_data") is not False)
            return
        if not match and not HIGHLIGHT_CLAIM.search(text):
            return  # neither asked for nor promised in the text
        lowest = bool(LOWEST.search(match.group(0) if match else text))
        # First a table that has the figure the question names (conversion -> win rate), then any table.
        named = [(b, self._question_column(b, question)) for b in tables]
        ordered = [b for b, col in named if col] + [b for b, col in named if not col]
        for block in ordered:
            column = self._top_measure(block, question)
            if column and self._top_note(state, block, column, lowest):
                return
        for block in charts:
            groups = self._chart_groups(block, lowest)
            if groups:
                value = groups[0][1]
                tied = [name for name, v in groups if v == value]
                who = tied[0] if len(tied) == 1 else _("%(first)s and %(last)s, tied", first=", ".join(tied[:-1][:3]),
                                                        last=tied[-1])
                label = block.get("measure_label") or _("Count")
                heading = _("Lowest %s", label) if lowest else _("Highest %s", label)
                self._add_notice(state, f"{heading}: {who} ({self._number(value)}).", "info", top=True)
                return

    @staticmethod
    def _number(value):
        return f"{value:,.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else f"{value:,}"

    @api.model
    def _chart_groups(self, block, lowest=False, limit=5):
        """[(name, value)] of a chart's groups, best first, computed by Odoo with the chart's own filter."""
        model = block.get("model")
        groupby = (block.get("groupby") or [])[:1]
        if not model or model not in self.env or not groupby:
            return []
        measure = block.get("measure") or "__count"
        try:
            aggregator = (self.env[model]._fields[measure].aggregator or "sum") if measure != "__count" else None
            aggregate = f"{measure}:{aggregator}" if aggregator else "__count"
            with self.env.cr.savepoint():  # runs while the answer is posted: a failed query must not abort it
                groups = self.env[model].formatted_read_group(
                    block.get("domain") or [], groupby, [aggregate], order=f"{aggregate} {'asc' if lowest else 'desc'}",
                    limit=limit)
        except Exception:  # noqa: BLE001 - a note is optional
            _logger.debug("AI Agent: skipped (a note is optional)", exc_info=True)
            return []
        field = self.env[model]._fields.get(groupby[0].split(":")[0])
        titled = field is not None and field.type == "many2one" and "title" in self.env[field.comodel_name]._fields
        out = []
        for group in groups:
            key, value = group.get(groupby[0]), group.get(aggregate)
            name = key[1] if isinstance(key, (list, tuple)) and len(key) == 2 else key
            if titled and isinstance(key, (list, tuple)):
                name = self.env[field.comodel_name].browse(key[0]).title or name  # a link's title, not its URL
            if name not in (None, False) and isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append((str(name), value))
        return out

    @api.model
    def _add_difference_note(self, state, tables, charts):
        """"Compare A and B": with exactly two rows or bars shown, Odoo states the difference itself."""
        if any(b.get("calculated") for b in state["blocks"] if not b.get("collapsed")):
            return  # the AI already calculated it
        question = state.get("question") or ""
        pairs = []
        for block in tables:
            if len(block["rows"]) != 2:
                continue
            if any(c["name"].endswith("__diff") for c in block.get("columns") or []):
                return  # present change: true already shows the difference and % change, in date order
            column = self._top_measure(block, question)
            if not column:
                continue
            values = [r["values"].get(column["name"]) for r in block["rows"]]
            names = [self._row_name(block, r, lower=False) for r in block["rows"]]
            pairs.append((column["label"], list(zip(names, values))))
        if any(len(block.get("groupby") or []) > 1 for block in charts):
            return  # a two-series chart (this month vs last month) compares series, not two bars
        for block in charts:
            groups = self._chart_groups(block, limit=3)
            if len(groups) == 2:
                pairs.append((block.get("measure_label") or _("Count"), groups))
        for label, ((name_a, first), (name_b, second)) in pairs:
            if not (name_a and name_b) or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                  for v in (first, second)):
                continue
            diff = first - second
            pct = f" ({diff * 100.0 / second:+.1f}%)" if second else ""
            self._add_notice(state, _("%(measure)s, %(a)s minus %(b)s: %(diff)s%(pct)s.", measure=label, a=name_a,
                                      b=name_b, diff=self._number(diff), pct=pct), "info", top=True)
            return

    @api.model
    def _top_note(self, state, block, column, lowest, title=None, suffix="", eligible=None):
        """Sort the table by the column and name its top row (and any rows tied with it) in a note. Rows that are
        not eligible (a provisional score) are sorted after the others and never named."""
        key = column["name"]
        eligible = eligible or (lambda r: True)
        rows = [r for r in block["rows"] if isinstance((r.get("values") or {}).get(key), (int, float))
                and not isinstance(r["values"][key], bool) and eligible(r)]
        if not rows:
            return False
        block["rows"].sort(key=lambda r: (not eligible(r), not isinstance(r["values"].get(key), (int, float))
                                          or isinstance(r["values"].get(key), bool),
                                          (r["values"][key] if lowest else -r["values"][key])
                                          if isinstance(r["values"].get(key), (int, float)) else 0))
        value = (min if lowest else max)(r["values"][key] for r in rows)
        if len(rows) > 1 and all(r["values"][key] == value for r in rows):
            return False  # every row has the same value (all 0 clicks): nothing stands out
        tied = [self._row_name(block, r, lower=False) for r in rows if r["values"][key] == value]
        tied = [n for n in tied if n]
        if not tied:
            return False
        shown = self._number(value)
        who = tied[0] if len(tied) == 1 else _("%(first)s and %(last)s, tied", first=", ".join(tied[:-1][:3]),
                                                last=tied[-1])
        heading = title or (_("Lowest %s", column["label"]) if lowest else _("Highest %s", column["label"]))
        self._add_notice(state, f"{heading}: {who} ({shown}{suffix}).", "info", top=True)
        return True

    @staticmethod
    def _question_column(block, question):
        """The numeric column the user's words name (conversion -> win rate, revenue, price, clicks...)."""
        def stems(text):
            out = set()
            for word in re.findall(r"[a-z]{3,}", text.lower()):
                out.add(word[:-3] + "y" if word.endswith("ies") else word[:-1] if word.endswith("s") else word)
            return out

        # Words that say who or what is ranked, not by which figure.
        generic = {"opportunity", "customer", "salesperson", "salespeople", "email", "the", "sum", "avg", "total",
                   "which", "who", "has", "have", "with", "their", "best", "most", "top", "highest", "lowest"}
        words = stems(question) - generic
        if words & {"conversion", "convert", "converted", "converting"}:
            words |= {"win", "rate"}
        best, hits = None, 0
        for column in block.get("columns") or []:
            if column["type"] not in ("integer", "float", "monetary"):
                continue
            label = stems(str(column["label"])) - generic
            overlap = len(label & words)
            if overlap > hits:
                best, hits = column["name"], overlap
        return best

    @classmethod
    def _top_measure(cls, block, question=""):
        """The figure a ranking is about: the one the question names, the sort column, or the first measure."""
        columns = block.get("columns") or []
        numeric = [c for c in columns if c["type"] in ("integer", "float", "monetary") and not c["name"].startswith("id")]
        if not numeric:
            return None
        named = cls._question_column(block, question)
        if named:
            return next(c for c in numeric if c["name"] == named)
        order = str(block.get("order") or "").split(",")[0].split(" ")[0].strip()
        for column in numeric:
            if order and column["name"] == order:
                return column
        if block.get("calculated"):
            return numeric[-1]  # the computed column
        measures = [c for c in numeric if c["name"] != "__count"]
        return (measures or numeric)[0]

    @api.model
    def _count_in_list(self, block, blocks):
        """A totals table that only counts records a complete list of the same model already shows ("link_tracker
        installed: 1" under the list of modules with their status): the list answers it, the count adds nothing.
        A count over records the list does not all show ("80 of 300") is kept: it says something new."""
        spec = block.get("spec") or {}
        model = block.get("model")
        if (block.get("type") != "table" or not block.get("grouped") or spec.get("groupby")
                or set(spec.get("aggregates") or []) - {"__count"} or not isinstance(model, str)
                or model not in self.env):
            return False
        lists = [b for b in blocks if b is not block and not b.get("collapsed") and b.get("type") == "table"
                 and not b.get("grouped") and b.get("model") == model and not b.get("has_more") and b.get("rows")
                 and all(row.get("id") for row in b["rows"]) and b.get("total") == len(b["rows"])]
        if not lists:
            return False
        try:
            with self.env.cr.savepoint():  # the user's own rights; a domain the database rejects changes nothing
                limit = max(len(b["rows"]) for b in lists) + 1  # one more than any list: a bigger count never fits
                counted = set(self.env[model].search(spec.get("domain") or [], limit=limit).ids)
        except Exception:  # noqa: BLE001 - keep the count when it cannot be compared
            _logger.debug("AI Agent: count not compared with the list", exc_info=True)
            return False
        return any(counted <= {row["id"] for row in b["rows"]} for b in lists)

    @staticmethod
    def _trivial_result(block):
        """A helper result with nothing to check: empty, all zero, or the lookup of one record by its name."""
        if block.get("type") != "table":
            return False
        rows = block.get("rows") or []
        if not rows:
            return True
        numbers = [v for row in rows for v in (row.get("values") or {}).values()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if block.get("grouped") and len(rows) == 1 and all(not v for v in numbers) \
                and all(not v or isinstance(v, (int, float)) for v in rows[0].get("values", {}).values()):
            return True  # e.g. "Count 0" totals
        if len(rows) != 1:
            return False
        terms = [t for t in block.get("domain") or [] if isinstance(t, (list, tuple))]
        name_fields = ("name", "display_name", "complete_name", "title", "default_code", "login", "email")
        names = [t for t in terms if len(t) == 3 and t[0] in name_fields]
        others = [t for t in terms if len(t) == 3 and t[0] not in name_fields and t[0] not in ("active", "id")]
        return bool(names) and not others

    @api.model
    def _refers_to_missing(self, model, domain, state):
        """Does this domain point at a record that get_record reported as not found (e.g. order_id = 16)?"""
        missing = [tuple(pair) for pair in state.get("not_found") or []]
        if not missing or not isinstance(model, str) or model not in self.env or not isinstance(domain, list):
            return False
        fields_ = self.env[model]._fields
        for term in domain:
            if not (isinstance(term, (list, tuple)) and len(term) == 3 and isinstance(term[0], str)):
                continue
            field = fields_.get(term[0].split(".")[0])
            if field is None or field.type != "many2one" or term[1] not in ("=", "in", "child_of"):
                continue
            ids = term[2] if isinstance(term[2], (list, tuple)) else [term[2]]
            if any((field.comodel_name, i) in missing for i in ids if isinstance(i, int)):
                return True
        return False

    @api.model
    def _selection_hint(self, model, arguments):
        """Nothing matched a status filter: give the labels, in case the AI picked the wrong value for a word."""
        domain = self._domain(arguments)
        hints = []
        for term in domain:
            if not (isinstance(term, (list, tuple)) and len(term) == 3 and isinstance(term[0], str)
                    and "." not in term[0] and term[1] in ("=", "in")):
                continue
            field = self.env[model]._fields.get(term[0])
            if field is not None and field.type == "selection":
                options = field._description_selection(self.env)
                hints.append(f"{term[0]} values: " + "; ".join(f"{key} ({label})" for key, label in options))
        if not hints:
            return ""
        return " Check that the status value matches the user's words: " + " | ".join(hints) + "."

    @api.model
    def _wrong_selection(self, model, domain):
        """A status filter with a value the field does not have silently matches nothing: say which values exist."""
        if not isinstance(model, str) or model not in self.env:
            return None
        Model = self.env[model]
        for term in domain:
            if not (isinstance(term, (list, tuple)) and len(term) == 3 and isinstance(term[0], str)
                    and "." not in term[0] and term[1] in ("=", "!=", "in", "not in")):
                continue
            field = Model._fields.get(term[0])
            if field is None or field.type != "selection":
                continue
            values = term[2] if isinstance(term[2], (list, tuple)) else [term[2]]
            options = dict(field._description_selection(self.env))
            unknown = [v for v in values if isinstance(v, str) and v not in options]
            if unknown and len(unknown) < len(values) and isinstance(term[2], (list, tuple)):
                # e.g. state in ["sale", "done"] where "done" no longer exists: same result without it.
                term[2] = [v for v in values if v not in unknown] if isinstance(term, list) else term[2]
                continue
            if unknown:
                return _("%(field)s has no value %(values)s. Its values are: %(options)s. Use the value (left), not "
                         "the label.", field=term[0], values=", ".join(repr(v) for v in unknown),
                         options="; ".join(f"{key} ({label})" for key, label in options.items()))
        return None

    @api.model
    def _empty_related(self, model, domain):
        """["login_date", "=", False] never matches when the field is read through a list (log_ids.create_date):
        also match records whose list is empty (users who never logged in)."""
        if not isinstance(model, str) or model not in self.env:
            return domain
        fields_ = self.env[model]._fields
        result = []
        for term in domain:
            field = fields_.get(term[0]) if isinstance(term, (list, tuple)) and len(term) == 3 else None
            related = field.related if field is not None and not field.store else None
            hop = fields_.get(related.split(".")[0]) if isinstance(related, str) else None
            if hop is not None and hop.type in ("one2many", "many2many") and term[1] == "=" and term[2] in (False, None):
                result += ["|", [hop.name, "=", False], list(term)]
            else:
                result.append(term)
        return result

    @api.model
    def _effective_members(self, model, domain):
        """Filter on effective membership: Odoo keeps direct links (group_ids, user_ids, implied_ids) next to
        all_* fields that also follow implied groups. "Has this user got Variants?" is answered by all_group_ids
        (as the user form and Settings show it); the direct field misses access given through another group.

        Generic: any many2many field X with a searchable all_X to the same model is switched. Returns
        (domain, [(X, all_X)...]).
        """
        if not model or model not in self.env:
            return domain, []
        fields = self.env[model]._fields
        switched, out = [], []
        for term in domain:
            if isinstance(term, (list, tuple)) and len(term) == 3 and isinstance(term[0], str):
                path = term[0].split(".")
                field = fields.get(path[0])
                twin = fields.get(f"all_{path[0]}")
                if (field is not None and twin is not None and field.type == "many2many"
                        and twin.type == "many2many" and twin.comodel_name == field.comodel_name
                        and (twin.store or twin.search)):
                    switched.append((path[0], twin.name))
                    term = [".".join([twin.name] + path[1:]), term[1], term[2]]
            out.append(term)
        return out, list(dict.fromkeys(switched))

    @api.model
    def _other_options(self, model, domain):
        """"Not fully received" means the other statuses: (status, '!=', X) on a selection field also matches records
        with no status at all (nothing to receive), which the user never means. So "!=" / "not in" on a selection is
        rewritten to "in" the remaining options, like Odoo's own filters ("Not Received", "Partially Received").
        Explicit empty values (!= False) are kept as written."""
        if not model or model not in self.env:
            return domain
        out = []
        for term in domain:
            if isinstance(term, (list, tuple)) and len(term) == 3 and term[1] in ("!=", "not in") \
                    and isinstance(term[0], str) and "." not in term[0]:
                field = self.env[model]._fields.get(term[0])
                values = term[2] if isinstance(term[2], (list, tuple)) else [term[2]]
                if field is not None and field.type == "selection" and values and all(
                        isinstance(v, str) and v for v in values):
                    options = [key for key, _label in field._description_selection(self.env)]
                    term = [term[0], "in", [key for key in options if key not in values]]
            out.append(term)
        return out

    @api.model
    def _domain(self, arguments):
        """The tool's domain as a list (MCP accepts it as a list or a JSON string)."""
        return self.env["mcp.mixin"]._coerce_domain(arguments.get("domain")) or []

    @api.model
    def _is_admin(self):
        """The current user is an administrator (Settings access)."""
        return self.env.user.has_group("base.group_system")

    @staticmethod
    def _repair_domain(domain):
        """[A, "|", B] (an OR written between its terms) -> ["|", A, B]; valid domains are returned unchanged."""
        if not any(token in ("|", "&") for token in domain):
            return domain
        try:
            Domain(domain)
            return domain
        except Exception:  # noqa: BLE001 - malformed: try reading it as infix below
            pass
        chains, current, pending_or = [], [], False
        for token in domain:
            if token in ("|", "&"):
                pending_or = token == "|"
                continue
            if token == "!":
                return domain
            if pending_or and current:
                current.append(token)
            else:
                if current:
                    chains.append(current)
                current = [token]
            pending_or = False
        if current:
            chains.append(current)
        repaired = [part for chain in chains for part in ["|"] * (len(chain) - 1) + chain]
        try:
            Domain(repaired)
            return repaired
        except Exception:  # noqa: BLE001 - leave it to the tool's own error message
            return domain

    @staticmethod
    def _requested_fields(arguments):
        """Field names a read tool asks for or filters, groups or sorts on (first part of dotted names)."""
        names = list(arguments.get("fields") or []) + list(arguments.get("groupby") or [])
        names += [t[0] for t in arguments.get("domain") or [] if isinstance(t, (list, tuple)) and t and isinstance(t[0], str)]
        names += [t.split()[0] for t in str(arguments.get("order") or "").split(",") if t.strip()]
        return [str(n).split(".")[0].split(":")[0] for n in names]

    @api.model
    def _cost_refused(self, model, names):
        """COST_HIDDEN when one of the field paths (product_id.standard_price, value:sum...) is a cost this user may
        not see, else None."""
        if any(self.env.ref(group, raise_if_not_found=False) and self.env.user.has_group(group)
               for group in COST_GROUPS):
            return None
        for name in names:
            current = model
            for part in str(name).split(":")[0].split("."):
                field = self.env[current]._fields.get(part) if current in self.env else None
                if field is None:
                    break
                if part in COST_FIELDS.get(current, ()):
                    return COST_HIDDEN
                current = field.comodel_name
        return None

    @api.model
    def _developer_mode(self):
        """The user's browser is in developer mode: Odoo then also shows the menus of "Technical Features" (Link
        Tracker, Technical...). Every internal user has that group; only the mode decides. Off outside a browser."""
        try:
            return bool(request) and bool(request.session.debug)
        except RuntimeError:  # no HTTP request (shell, cron)
            return False

    @api.model
    def _visible_menus(self, developer_mode=None):
        """Ids of the menus this user sees in Odoo right now (or in developer mode / normal mode when given)."""
        debug = self._developer_mode() if developer_mode is None else developer_mode
        return set(self.env["ir.ui.menu"]._visible_menu_ids(debug))

    @api.model
    def _app_blocked(self, model, state, depth=0):
        """The user's apps decide what the chatbot shows: data whose apps the user cannot open in Odoo's menu (e.g.
        Purchase: No) is refused, even where Odoo grants a read for linked documents. Only restricts, never grants;
        administrators are not limited. Returns the message for the user, or None."""
        if not isinstance(model, str) or model not in self.env or self._is_admin():
            return None
        cache = state.setdefault("app_gate", {})
        if model in cache:
            return cache[model]
        cache[model] = None  # also stops loops between related models
        Menu = self.env["ir.ui.menu"]
        if "visible_roots" not in state:
            apps = Menu.sudo().search([("parent_id", "=", False)])
            visible = self._visible_menus()
            state["visible_roots"] = {m.id for m in apps if m.id in visible}
            # Apps Odoo would show this user in developer mode only (not in it now): the honest answer is "turn on
            # developer mode", not "ask an administrator for access" (the user already has it).
            in_developer_mode = visible if self._developer_mode() else self._visible_menus(True)
            state["developer_roots"] = {m.id for m in apps if m.id in in_developer_mode} - state["visible_roots"]
        actions = self.env["ir.actions.act_window"].sudo().search([("res_model", "=", model)])
        menus = Menu.sudo().search([("action", "in", [f"ir.actions.act_window,{a.id}" for a in actions])]) \
            if actions else Menu.sudo()
        # Technical menus (Settings > Technical) are tools for administrators, not access to an app.
        menus = menus.filtered(lambda m: "/technical/" not in ("/" + (m.complete_name or "").lower() + "/"))
        roots = {int(m.parent_path.split("/")[0]) for m in menus if m.parent_path}
        message = None
        if roots:
            if not roots & state["visible_roots"]:
                apps = " / ".join(Menu.sudo().browse(sorted(roots)).mapped("name"))
                label = self._model_label(model).lower()
                if roots & state["developer_roots"]:
                    message = self._developer_mode_text(apps, label)
                else:
                    message = _("You don't have access to %(apps)s, so I can't show these %(label)s records. An "
                                "administrator can give you access in Settings > Users.", apps=apps, label=label)
        elif depth < 2:
            # Lines follow their document: purchase order lines need access to Purchase. Only true parents count
            # (deleted with their lines), never people, companies or technical records.
            for field in self.env[model]._fields.values():
                if field.type == "many2one" and field.comodel_name != model and field.ondelete == "cascade" \
                        and field.comodel_name in self.env and field.comodel_name not in APP_GATE_NOT_PARENTS:
                    parent = self._app_blocked(field.comodel_name, state, depth + 1)
                    if parent:
                        message = parent
                        break
        cache[model] = message
        return message

    @staticmethod
    def _developer_mode_text(apps, label=None):
        what = _("these %s records", label) if label else _("it")
        return _("Odoo shows %(apps)s only in developer mode, so I can't show %(what)s right now. Turn on developer "
                 "mode (add ?debug=1 to the page address, right after /odoo, and reload the page), then ask again.",
                 apps=apps, what=what)

    @api.model
    def _similar_models(self, model):
        """Real models matching every word of a guessed name, in their technical name or label (whatsapp.template ->
        tcs.whatsapp.template "WhatsApp Template"). Names only, no data; at most 6."""
        words = [w for w in re.split(r"[._\s]+", (model or "").lower())
                 if len(w) > 2 and w not in ("res", "base", "model", "models")]
        if not words:
            return self.env["ir.model"]
        domain = [("transient", "=", False)]
        for word in words:
            domain += ["|", ("model", "ilike", word), ("name", "ilike", word)]
        found = self.env["ir.model"].sudo().search(domain, limit=6)
        return found.filtered(lambda m: m.model in self.env and m.model != model)

    @api.model
    def _tool_not_installed(self, state, step, call, model):
        """Unknown model: find the app that provides it (from the model name or the user's words), and tell the user
        plainly whether it is turned off in Settings, not installed, or not available in this Odoo."""
        similar = self._similar_models(model)
        if similar:
            # A guessed name for a model that exists under another name (a third-party app: whatsapp.template ->
            # tcs.whatsapp.template). Its real name lets the next call get the usual "not enabled, how to enable".
            return self._tool_failed(state, step, call, _(
                "Unknown model: %(model)s. Models of this database that match: %(found)s. Call the tool again with "
                "the right one (it may not be enabled for the AI yet; Odoo then says how to enable it).",
                model=model, found=", ".join(f"{m.model} ({m.name})" for m in similar)))
        feature = self._missing_feature(model, state.get("question") or "")
        if not feature:
            return self._tool_failed(state, step, call, _(
                "Unknown model: %s. It does not exist in this database; if you guessed the name, check list_models.",
                model))
        if feature.get("installed_models"):
            # The app is installed: the AI only guessed a wrong technical name.
            return self._tool_failed(state, step, call, _(
                "Unknown model: %(model)s. %(app)s is installed; its data is in: %(models)s.", model=model,
                app=feature["label"], models=", ".join(feature["installed_models"][:6])))
        text = self._unavailable_text(feature["label"], feature["state"], feature.get("where"))
        self._add_notice(state, text)
        step.update(status="error", detail=text)
        state["tool_errors"] += 1
        return (f"NOT INSTALLED (already shown to the user): {text} Say this plainly and confidently in one or two "
                "sentences; do not say the data or model 'does not exist', and do not suggest other apps unless asked.")

    @api.model
    def _missing_feature(self, model, question):
        """The Odoo app behind a model that is not in this database: {label, state, where} or None."""
        Module = self.env["ir.module.module"].sudo()
        parts = [p for p in model.split(".") if p]
        candidates = []
        for n in range(len(parts), 0, -1):
            base = "_".join(parts[:n])
            candidates += [base, base + "s"]
        modules = Module.search([("name", "in", candidates)])
        by_name = {m.name: m for m in modules}
        module = next((by_name[c] for c in candidates if c in by_name and by_name[c].state != "installed"), None)
        settings = self._module_settings()
        if module is None:
            # The user's words may name the feature ("purchase agreements"), even if the AI guessed the model name.
            words = (question or "").lower()
            named = [s for s in settings.values() if s["setting"] and s["setting"].lower() in words]
            named.sort(key=lambda s: -len(s["setting"]))
            if named:
                module = Module.search([("name", "=", named[0]["module"])], limit=1) or None
            if module is not None and module.state == "installed":
                ids = self.env["ir.model.data"].sudo().search(
                    [("module", "=", module.name), ("model", "=", "ir.model")]).mapped("res_id")
                models = self.env["ir.model"].sudo().browse(ids).mapped("model")
                # The app's own models first (purchase.requisition), then the ones it only extends.
                stem = module.name.rstrip("s")
                models = sorted(models, key=lambda m: not m.replace(".", "_").startswith(stem))
                return {"label": named[0]["setting"], "installed_models": models} if models else None
        if module is None or module.state == "installed":
            return None
        setting = settings.get(module.name)
        label = (setting or {}).get("setting") or module.shortdesc or module.name
        where = None
        if setting and setting.get("app") and module.state == "uninstalled":
            app = setting["app"]
            where = (_("Settings > %(app)s > %(setting)s", app=app, setting=label) if app == "General Settings"
                     else _("%(app)s > Configuration > Settings > %(setting)s", app=app, setting=label))
        return {"label": label, "state": module.state, "where": where}

    @api.model
    def _settings_catalog(self):
        """Every setting of the Settings screen of the installed apps: where it is, what it says, its fields.

        Read from the settings form itself (as the administrator, since the form hides admin-only settings from
        other users), so it covers every app, including custom ones, without knowing any setting in advance.
        """
        from lxml import etree
        from odoo import SUPERUSER_ID
        Settings = self.env["res.config.settings"].with_user(SUPERUSER_ID)
        try:
            arch = etree.fromstring(Settings.get_view(view_type="form")["arch"])
        except Exception:  # noqa: BLE001 - no settings form, nothing to describe
            _logger.debug("AI Agent: skipped (%s)", 'no settings form, nothing to describe', exc_info=True)
            return []
        catalog = []
        for app in arch.iter("app"):
            app_name = app.get("string") or app.get("data-string") or app.get("name") or ""
            for setting in app.iter("setting"):
                own = [f.get("name") for f in setting.xpath(".//field[@name]")
                       if f.xpath("ancestor::setting[1]")[0] is setting and f.get("name") in Settings._fields]
                # A setting that is only a link ("Default Access Rights", "Manage API Keys"): found by its name too.
                buttons = [" ".join(b.get("string").split()) for b in setting.xpath(".//button[@string]")
                           if b.xpath("ancestor::setting[1]")[0] is setting]
                if not own and not (setting.get("string") and buttons):
                    continue  # a technical block without a name or a link
                main = own[0] if own else None
                blocks = setting.xpath("ancestor::block[1]")
                parents = setting.xpath("ancestor::setting[1]")
                parent = ""
                if parents:
                    first = [f.get("name") for f in parents[0].xpath(".//field[@name]")
                             if f.get("name") in Settings._fields][:1]
                    parent = parents[0].get("string") or (Settings._fields[first[0]].string if first else "")
                catalog.append({
                    "app": app_name,
                    "general": app.get("name") == "general_settings",
                    "section": (blocks[0].get("title") or "") if blocks else "",
                    "label": setting.get("string") or Settings._fields[main].string or main,
                    "buttons": buttons,
                    "help": " ".join((setting.get("help") or "").split()),
                    "parent": parent,
                    "company": setting.get("company_dependent") == "1",
                    "fields": own,
                    "options": (" ".join(Settings._fields[f].string or "" for f in own[1:]) if own
                                else " ".join(buttons)),
                })
        return catalog

    @api.model
    def _setting_values(self, catalog_entries, technical):
        """{field: shown value} of the settings found, for the current company. Only safe kinds of values: on/off and
        choices for everyone; numbers and linked records for administrators; never text (keys, passwords...)."""
        Settings = self.env["res.config.settings"].sudo().with_company(self.env.company)
        safe = []
        for entry in catalog_entries:
            for name in entry["fields"]:
                field = Settings._fields.get(name)
                # an on/off or a choice cannot hold a secret ("Enforce two-factor authentication"): only other
                # kinds of values with a secret-like name are kept out
                if (field is None or name in safe
                        or (SETTING_SECRET.search(name) and field.type not in ("boolean", "selection"))):
                    continue
                if field.type in ("boolean", "selection") or (
                        technical and field.type in ("integer", "float", "monetary", "many2one")):
                    safe.append(name)
        if not safe:
            return {}
        # The values the Settings screen shows: a temporary settings record (defaults and company-related
        # fields), read inside a savepoint that is always rolled back, so nothing is saved or applied.
        values = {}
        try:
            with self.env.cr.savepoint() as savepoint:
                record = Settings.create({})
                for name in safe:
                    field = Settings._fields[name]
                    value = record[name]
                    values[name] = value.id if field.type == "many2one" else value
                savepoint.close(rollback=True)  # undo the temporary record and clear its cache
        except Exception:  # noqa: BLE001 - the place of the setting can still be told
            _logger.debug("AI Agent: skipped (%s)", 'the place of the setting can still be told', exc_info=True)
            return {}
        meta = Settings.fields_get(safe, attributes=["selection", "type"])
        shown = {}
        for name in safe:
            value = values.get(name)
            kind = meta.get(name, {}).get("type")
            if kind == "boolean":
                shown[name] = _("on") if value else _("off")
            elif kind == "selection":
                labels = dict(meta[name].get("selection") or [])
                shown[name] = labels.get(value, value) if value not in (None, False) else _("not set")
            elif kind == "many2one":
                record = self.env[Settings._fields[name].comodel_name].sudo().browse(value).exists() if value else None
                shown[name] = record.display_name if record else _("not set")
            elif value not in (None, False):
                shown[name] = str(value)
        return shown

    @api.model
    def _tool_find_setting(self, arguments, state):
        """Where a setting is in Configuration > Settings, what it does, and whether it is on (generic, all apps)."""
        query = " ".join(str(arguments.get("query") or "").split())[:200]
        if not query:
            raise UserError(_("Say which option to look for, e.g. 'lock confirmed orders'."))
        app_filter = " ".join(str(arguments.get("app") or "").casefold().split())
        terms = []
        for word in re.findall(r"[^\W\d_]{3,}", query.casefold()):
            if word in SETTING_STOPWORDS:
                continue
            terms.append(word)
            if word.endswith("ies") and len(word) > 5:
                terms.append(word[:-3] + "y")
                continue
            for suffix in ("ing", "ed", "es", "s"):
                if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                    terms.append(word[:-len(suffix)])
                    break
        terms = list(dict.fromkeys(terms))
        if not terms:
            raise UserError(_("Say which option to look for, e.g. 'lock confirmed orders'."))
        catalog = self._settings_catalog()

        def has(text, term):
            """The term starts a word of the text ("order" in "Orders", "invoic" in "Invoices"); a short term is a
            whole word ("web" is not in "Website", "app" not in "Apply")."""
            return re.search(rf"\b{re.escape(term)}" + (r"\b" if len(term) < 4 else ""), text) is not None

        words = [w for w in dict.fromkeys(re.findall(r"[^\W\d_]{3,}", query.casefold())) if w not in SETTING_STOPWORDS]

        def exact(entry):
            """The setting's name is exactly the user's words ("Warnings", not "Sale Warnings")."""
            own = [w for w in re.findall(r"[^\W\d_]{3,}", entry["label"].casefold()) if w not in SETTING_STOPWORDS]
            return bool(own) and own == words

        def score(entry):
            label, help_text = entry["label"].casefold(), entry["help"].casefold()
            # the section is a name the user sees and types ("Progressive Web App"): above the help text
            other = f"{entry['section']} {entry['parent']}".casefold()
            options = entry["options"].casefold()
            value = sum(4 if has(label, term) else 3 if has(options, term) else 2 if has(other, term)
                        else 1 if has(help_text, term) else 0 for term in terms)
            if app_filter and app_filter not in entry["app"].casefold():
                value = 0
            elif value and any(has(entry["app"].casefold(), term) for term in terms):
                value += 2
            if value and exact(entry):
                value += 6  # asked for by its exact name: first, before longer names sharing the words
            return value

        found = sorted((e for e in catalog if score(e)), key=score, reverse=True)
        if found:
            best = score(found[0])
            found = [e for e in found if score(e) >= max(2, best / 2)][:3]
        # A setting is the answer only when its own name has most of the user's words: one word in a detail
        # ("link" in "Display Link QR-code" of Pay Invoices Online) is not "Link Tracker".

        def hits(entry):
            links = "" if entry["fields"] else " ".join(entry["buttons"])  # a link setting is named by its link too
            name = f"{entry['label']} {entry['parent']} {entry['section']} {links}".casefold()
            # a word counts when it, or its stem ("orders" -> "order"), is in the setting's name or section
            return sum(1 for w in words if any(has(name, t) for t in terms if w.startswith(t)))

        def named(entry):
            return hits(entry) * 2 >= len(words)

        # The words are exactly an app's name ("CRM", "Time Off", "Accounting") and no setting's own name has those
        # very words: the user means the app, not a setting in its section or with a word like it.
        exact_app = self._module_named(query, exact=True)
        # the full name, "on"/"off" included ("Time Off"), as whole words ("time" is not "timeout")
        phrase = [w for w in re.findall(r"[^\W\d_]+", query.casefold())
                  if w not in SETTING_STOPWORDS or w in ("on", "off")]
        if exact_app and not any(all(re.search(rf"\b{re.escape(w)}\b", f"{e['label']} {e['parent']}".casefold())
                                     for w in phrase) for e in found):
            return exact_app
        if not any(hits(e) == len(words) for e in found):
            # No setting has all the words in its name: words that are an app's exact name ("Email Marketing",
            # "Link Tracker") are about that app, not about a setting that shares one word with it.
            app = self._module_named(query)
            if app:
                return app
        weak = not any(named(e) for e in found)
        found = [e for e in found if named(e)] or found
        if not found:
            return (f"No option matching '{query}' exists in Configuration > Settings of the installed apps. Say so "
                    "plainly; it may be done on the record itself, need an app that is not installed, or not exist.")
        technical = self._is_admin()
        values = self._setting_values(found, technical)
        modules = self.env["ir.module.module"].sudo()
        lines = []
        for entry in found:
            path = [p for p in (entry["section"], entry["label"]) if p]
            where = " > ".join((["Settings"] if entry["general"] else [entry["app"], "Configuration", "Settings"]) + path)
            if entry["parent"]:
                where += f" (under {entry['parent']})"
            main = entry["fields"][0] if entry["fields"] else ""
            if not main:
                status = _("a link on that Settings screen (%s), not an on/off option", ", ".join(entry["buttons"]))
            elif main.startswith("module_"):
                module = modules.search([("name", "=", main[7:])], limit=1)
                if not module or module.state == "uninstallable":
                    status = _("not available in this edition (Enterprise)")
                elif module.state == "installed":
                    status = _("on (its app is installed)")
                else:
                    status = _("off (turning it on installs the %s app)", module.shortdesc or main[7:])
            elif SETTING_SECRET.search(main) and main not in values:
                status = _("value not shown (secret)")
            else:
                status = values.get(main, _("value not shown"))
            extra = [f"{self.env['res.config.settings']._fields[f].string}: {values[f]}"
                     for f in entry["fields"][1:] if f in values]
            line = f"- {entry['label']}: {status}. Where: {where}."
            if entry["help"]:
                line += f" What it does: {entry['help'].rstrip('.')}."
            if entry["company"]:
                line += f" Per company (this is {self.env.company.name})."
            if extra:
                line += " Details: " + "; ".join(extra) + "."
            lines.append(line)
        who = ("The user is an administrator and can change these settings." if technical else
               "The user is not an administrator: tell them an administrator can change it there.")
        if weak:
            # No card: opening an unrelated Settings screen ("Pay Invoices Online" for "link tracker") misleads.
            return (f"No option on the Settings screens is named like '{query}'. These options only mention some of "
                    "those words in their details:\n" + "\n".join(lines) + "\nSay there is no setting with that name, "
                    "unless one of these is clearly what the user meant. If it is an app or a screen, call show_app "
                    "with its name.")
        if technical:
            target = found[0]
            app = "Settings" if target["general"] else target["app"]
            self._tool_show_app({"app": app, "menu": "Settings"}, state)
        return ("Settings found on the Configuration > Settings screens (current company), best match first:\n"
                + "\n".join(lines) + f"\n{who} Answer with the best match: say clearly whether it is on or off and "
                "exactly where it is. Mention another match only if it may be what the user meant.")

    @api.model
    def _module_named(self, query, exact=False):
        """When the words name an app rather than a setting ("link tracker"): what Odoo knows about it, for the AI.
        Any module (custom ones included) in any state, from Odoo's own module list; None when no app has that name.
        exact: the words are the app's name and nothing more ("Time Off"; "turn on" and the like aside)."""
        words = set(re.findall(r"[^\W\d_]+", query.casefold()))
        meaningful = words - SETTING_STOPWORDS
        best = None
        for module in self.env["ir.module.module"].sudo().search([]):
            name_words = re.findall(r"[^\W\d_]+", (module.shortdesc or "").casefold())
            if exact and not (meaningful and set(name_words) - SETTING_STOPWORDS == meaningful):
                continue
            # every word of the app's name is in the question, and it is not a one- or two-letter name
            if name_words and sum(len(w) for w in name_words) >= 3 and set(name_words) <= words:
                key = (len(name_words), module.application, module.state == "installed")
                if best is None or key > best[0]:
                    best = (key, module)
        if not best:
            return None
        module = best[1]
        label = module.shortdesc
        if module.state == "installed":
            return (f"'{label}' is an app (module {module.name}), and it is installed; it is not an option on a "
                    f"Settings screen. Call show_app with app '{label}' to open it, or to learn why the user does not "
                    "see it (for example a menu Odoo shows only in developer mode).")
        return (f"'{label}' is an app, not an option on a Settings screen. Tell the user: "
                f"{self._unavailable_text(label, module.state)}")

    @api.model
    def _module_settings(self):
        """The Settings checkboxes that install an app: {module: {module, app, setting}}, from the settings catalog."""
        out = {}
        Settings = self.env["res.config.settings"]
        for entry in self._settings_catalog():
            for field in entry["fields"]:
                if field.startswith("module_"):
                    label = entry["label"] if field == entry["fields"][0] else Settings._fields[field].string
                    out[field[7:]] = {"module": field[7:], "app": entry["app"], "setting": label}
        return out

    @api.model
    def _not_enabled(self, tool_name, arguments):
        """The message for a model or operation the MCP settings do not allow, or None when allowed."""
        operation = TOOL_OPERATIONS.get(tool_name)
        model = arguments.get("model")
        if not operation or not isinstance(model, str) or model not in self.env:
            return None
        if operation == "method":
            allowed = mcp_utils.check_model_method_allowed(self.env, model)
        else:
            allowed = mcp_utils.check_model_operation_allowed(self.env, model, operation)
        return None if allowed else self._not_enabled_message(model, operation)

    @api.model
    def _not_enabled_message(self, model, operation):
        """Administrators learn exactly what to enable and where; other users only that it is not enabled."""
        if not self.env.user.has_group("odoo_ai_agent.group_ai_agent_manager"):
            return _("Your administrator has not enabled this for you.")
        label = self._model_label(model)
        checkbox = OPERATION_CHECKBOXES[operation]
        where = _("AI Agent > Configuration > Settings > Enabled MCP models > Manage MCP models")
        if mcp_utils.is_model_mcp_enabled(self.env, model):
            return _("%(label)s (%(model)s) is enabled in MCP, but \"%(checkbox)s\" is not ticked. To allow it, go "
                     "to %(where)s, open %(label)s and tick \"%(checkbox)s\".",
                     label=label, model=model, checkbox=checkbox, where=where)
        return _("%(label)s (%(model)s) is not enabled in MCP. To allow it, go to %(where)s, click New, choose "
                 "the model %(label)s (%(model)s) and tick \"%(checkbox)s\".",
                 label=label, model=model, checkbox=checkbox, where=where)

    def _add_notice(self, state, message, level="warning", top=False):
        """A note under the answer, once per answer (info notes about a table: level info, top for its best row)."""
        if message not in state.setdefault("notices", []):
            state["notices"].append(message)
            state["blocks"].append({"type": "notice", "id": uuid.uuid4().hex, "level": level, "text": message,
                                    **({"top": True} if top else {})})

    def _tool_not_enabled(self, state, step, call, message):
        step.update(status="error", detail=message)
        self._add_notice(state, message)
        text = f"NOT ENABLED (already shown to the user): {message}"
        self._note(state, call, text)
        return text

    @api.model
    def _explain_error(self, message):
        """Add what the AI needs to correct itself: the real fields close to a wrong one, the order syntax."""
        quoted = re.search(r"Invalid field '(\w+)' on '([\w.]+)'", message)
        match = re.search(r"Invalid field ([\w.]+)", message)
        target = None
        if quoted and quoted.group(2) in self.env:
            target = (quoted.group(2), quoted.group(1))
        elif match:
            parts = match.group(1).split(".")
            for index in range(len(parts) - 1, 0, -1):
                if ".".join(parts[:index]) in self.env:
                    target = (".".join(parts[:index]), parts[index])
                    break
        if target:
            model, field = target
            fields_map = self.env[model]._fields
            similar = difflib.get_close_matches(field, list(fields_map), n=6, cutoff=0.5)
            message += f" {model} has no field '{field}'."
            if field == "active":
                message += " These records cannot be archived, so do not filter on 'active'."
            if similar:
                message += f" Similar fields: {', '.join(similar)}."
            if not difflib.get_close_matches(field, list(fields_map), n=1, cutoff=0.75):
                # Nothing close by name (fields renamed between Odoo versions): show the links to other records.
                links = [f"{name} ({f.string} → {f.comodel_name})" for name, f in fields_map.items()
                         if f.relational and not name.startswith(("message_", "activity_", "website_message"))
                         and name not in ("create_uid", "write_uid")][:20]
                if links:
                    message += f" Fields of {model} that link to other records: {', '.join(links)}."
            message += " For statuses, get_filters lists the model's own filters."
        elif match := re.search(r"Cannot convert ([\w.]+)\.(\w+)\b.* to SQL because", message):
            model, field_name = match.group(1), match.group(2)
            field = self.env[model]._fields.get(field_name) if model in self.env else None
            searchable = bool(field and field.search)
            message += (f" '{field_name}' is computed on the fly, so it cannot be used in groupby or order"
                        + (" (it can be used in the domain)." if searchable else " or in the domain.")
                        + " get_filters may list a ready-made filter for it.")
            try:
                depends = [d for d in field.get_depends(self.env[model])[0] if "." in d or d in self.env[model]._fields]
            except Exception:  # noqa: BLE001 - the hint is optional
                _logger.debug("AI Agent: skipped (%s)", 'the hint is optional', exc_info=True)
                depends = []
            if depends:
                message += (f" It is computed from: {', '.join(depends[:6])}. For totals or charts, use those stored "
                            f"fields (on the linked records) or a stored field with the same meaning.")
        elif "Invalid order" in message or "order" in message.lower() and "invalid" in message.lower():
            message += (" Write order without quotes, e.g. 'amount_total desc'; for aggregate_records use a groupby "
                        "or aggregate name such as 'amount_total:sum desc'.")
        return message

    def _tool_failed(self, state, step, call, message):
        state["tool_errors"] += 1
        step.update(status="error", detail=message)
        text = f"ERROR: {message}"
        self._note(state, call, text)
        return text

    @api.model
    def _note(self, state, call, result):
        """A one-line record of the call for follow-up questions (what the AI saw, nothing more)."""
        arguments = json.dumps(call.get("arguments") or {}, ensure_ascii=False, default=str)
        tool_result = result if call["name"] not in SCHEMA_TOOLS else "definitions read"
        note = f"{call['name']}({arguments}) -> {tool_result}"
        state["notes"].append(note[:TRACE_NOTE_CAP])

    @api.model
    def _check_arguments(self, schema, arguments):
        missing = [key for key in schema.get("required") or [] if key not in arguments]
        if missing:
            raise UserError(_("Missing required argument(s): %s", ", ".join(missing)))
        properties = schema.get("properties")
        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            unknown = sorted(key for key in arguments if key not in properties)
            if unknown:
                raise UserError(_("Unknown argument(s): %s", ", ".join(unknown)))

    @api.model
    def _call_mcp(self, tool, arguments):
        """Run an MCP tool as the current user, exactly like the /mcp endpoint does."""
        self._check_arguments(tool["raw_schema"], arguments)
        with self.env.cr.savepoint():
            if tool.get("custom_tool_id"):
                custom = self.env["mcp.custom.tool"].browse(tool["custom_tool_id"])
                if not custom._user_can_run():
                    raise AccessError(_("You are not allowed to run the '%s' tool.", custom.name))
                output = custom._run_tool(arguments)
                text = json.dumps(output if output is not None else _("Done."), ensure_ascii=False, default=str)
                return {"content": [{"type": "text", "text": text}], "structuredContent": {"result": output}}
            return dict(getattr(self.env["mcp.mixin"], tool["method"])(**arguments))

    @api.model
    def _model_forms(self, model):
        """The model's forms as this user gets them (groups and developer mode applied), the one they open first
        first: from the menus they see (Settings > Users opens the full user form; Odoo's default user form is a short
        one without tabs), then Odoo's default form, then the model's other main forms."""
        from lxml import etree

        view_ids = []
        for menu in self.env["ir.ui.menu"].sudo().browse(sorted(self._visible_menus())):
            action = menu.action
            if action and action._name == "ir.actions.act_window" and action.res_model == model:
                view_ids += [v.view_id.id for v in action.view_ids if v.view_mode == "form" and v.view_id]
                if action.view_id.type == "form":
                    view_ids.append(action.view_id.id)
        view_ids.append(None)
        view_ids += self.env["ir.ui.view"].sudo().search(
            [("model", "=", model), ("type", "=", "form"), ("mode", "=", "primary")], order="priority, id").ids
        for view_id in list(dict.fromkeys(view_ids))[:FORM_TAB_VIEW_CAP]:
            try:
                yield etree.fromstring(self.env[model].get_view(view_id, "form")["arch"])
            except Exception:  # noqa: BLE001 - a form this user cannot load: try the next one
                _logger.debug("AI Agent: skipped form view %s of %s", view_id, model, exc_info=True)

    @api.model
    def _form_tab(self, model, record_id, tab):
        """One tab of a record's form exactly as this user sees it: {"title", "fields": [(name, label)], "buttons"}.

        Odoo's own form for this user (fields of groups they lack and developer-mode-only fields are already left
        out by get_view), then each "invisible" rule evaluated on this record, in screen order with screen labels.
        Raises UserError naming the real tabs when none matches, so the AI can retry with one of them.
        """
        Model = self.env[model]
        label = self._model_label(model)
        try:
            record = Model.browse(int(record_id)).exists()
        except (TypeError, ValueError):
            record = Model.browse()
        if not record:
            raise UserError(_("No %(label)s with ID %(id)s exists.", label=label, id=record_id))
        record.check_access("read")
        # sudo: values used only to evaluate the screen's "invisible" rules (e.g. "not has_access_livechat");
        # they are never shown or sent. The record itself was access-checked above.
        values = record.sudo()
        base = {**self._domain_eval_context(), "id": record.id, "current_company_id": self.env.company.id}

        def screen_value(name):
            field = Model._fields[name]
            value = values[name]
            if field.type == "many2one":
                return value.id or False
            if field.type in ("one2many", "many2many"):
                return value.ids
            if field.type in ("date", "datetime"):
                return field.to_string(value) if value else False
            return value

        def shown(node):
            """Odoo's rule for this node on this record; an expression that cannot be evaluated here keeps the node
            (showing a field the screen hides is a smaller mistake than hiding one it shows)."""
            rule = str(node.get("invisible") or "").strip()
            if rule in ("", "0", "False", "false"):
                return True
            if rule in ("1", "True", "true"):
                return False
            try:
                names = {n.id for n in ast.walk(ast.parse(rule, mode="eval")) if isinstance(n, ast.Name)}
                context = dict(base, **{n: screen_value(n) for n in names if n in Model._fields})
                return not safe_eval(rule, context)
            except Exception:  # noqa: BLE001
                _logger.debug("AI Agent: kept a node whose rule %r could not be evaluated", rule, exc_info=True)
                return True

        def visible(node, page):
            """The node and every group or section around it inside the tab are shown (the tab is checked apart)."""
            for current in [node, *node.iterancestors()]:
                if current is page:
                    return True
                if not shown(current):
                    return False
            return True

        def title(page):
            return " ".join(str(page.get("string") or page.get("name") or "").split())

        wanted = " ".join(str(tab or "").casefold().split()).removesuffix(" tab")
        form = page = None
        all_tabs = []
        for candidate in self._model_forms(model):
            pages = [p for p in candidate.iter("page")
                     if not any(a.tag == "field" for a in p.iterancestors()) and title(p)]
            all_tabs += [title(p) for p in pages if shown(p)]
            match = ([p for p in pages if wanted in (title(p).casefold(), str(p.get("name") or "").casefold())]
                     or [p for p in pages if wanted and wanted in title(p).casefold()])
            if match:
                form, page = candidate, match[0]
                break
        if page is None:
            raise UserError(_("The %(label)s form has no tab '%(tab)s'. Its tabs: %(tabs)s. Call get_record again "
                              "with one of them as 'tab', or without 'tab'.", label=label, tab=tab,
                              tabs=", ".join(dict.fromkeys(all_tabs)) or "none"))
        if not shown(page):
            raise UserError(_("The '%(tab)s' tab is not shown on this %(label)s, so there is nothing to list. Say so.",
                              tab=title(page), label=label))
        # The screen's own label for a field: its string, else a <label for="..."> next to it, else the field's.
        labels = {n.get("for"): " ".join(n.get("string").split()) for n in form.iter("label")
                  if n.get("for") and n.get("string")}

        def section(node):
            """The titled section of the tab the node is in ("Sales", "Purchase"), as on the screen."""
            for current in node.iterancestors():
                if current is page:
                    return ""
                if current.tag == "group" and current.get("string"):
                    return " ".join(current.get("string").split())
            return ""

        found = []
        for node in page.iter("field"):
            name = node.get("name")
            field = Model._fields.get(name)
            if (field is None or any(a.tag == "field" for a in node.iterancestors()) or not visible(node, page)
                    or field.type in ("binary", "image") or is_sensitive_field_name(name)
                    or not Model._has_field_access(field, "read") or name in {n for n, _l, _s in found}):
                continue
            found.append((name, node.get("string") or labels.get(name) or field.string or name, section(node)))
        # A label used twice on the tab ("Payment Terms" under Sales and under Purchase) gets its section, as on screen.
        repeated = {lbl for _n, lbl, _s in found if sum(1 for _x, other, _y in found if other == lbl) > 1}
        fields_shown = [(name, f"{sec}: {lbl}" if lbl in repeated and sec else lbl) for name, lbl, sec in found]
        buttons = list(dict.fromkeys(
            " ".join(n.get("string").split()) for n in page.iter("button")
            if n.get("string") and not any(a.tag == "field" for a in n.iterancestors()) and visible(n, page)))
        return {"title": title(page), "fields": fields_shown, "buttons": buttons}

    @api.model
    # screens change only with modules (cached until restart); developer mode shows other fields
    @ormcache("model", "self.env.uid", "self.env.lang", "self._developer_mode()")
    def _screen_outline(self, model):
        """What the user sees for this model, read from its real screens (any model, custom ones included):
        the tabs of its form with the labels shown there, and, for line models, the columns of the list they
        appear in on their parent's form ("Order Lines" on a sales order: ... Disc.%, Amount). Columns and fields
        that are optional or shown only in some cases say so in words, from the screen's own rules."""
        Flow = self.env["ai_agent.business.flow"]

        def is_hidden(node):
            return any(str(node.get(attr) or "").strip() in ("1", "True", "true")
                       for attr in ("invisible", "column_invisible"))

        def label(model_name, node):
            field = self.env[model_name]._fields.get(node.get("name"))
            return node.get("string") or (field.string if field else node.get("name"))

        def when(model_name, node, attr, parent=None):
            """' (shown only when ...)' for a rule that depends on the record, in Odoo's own labels; '' otherwise."""
            rule = str(node.get(attr) or "").strip()
            if rule in ("", "0", "False", "false", "1", "True", "true"):
                return ""
            try:
                words = Flow._words(model_name, ast.parse(rule, mode="eval").body, True, False, parent)
            except SyntaxError:
                words = _("other conditions allow it")
            if not words:
                return ""  # e.g. "once the record is saved": always true on a record the user looks at
            if words == _("other conditions allow it"):
                return " (shown only in some cases)"
            return f" (shown only when {words})"

        def columns(model_name, list_node, parent):
            out = []
            for node in list_node.iterchildren("field"):
                if (not node.get("name") or is_hidden(node) or node.get("widget") == "handle"  # a drag handle
                        or not str(label(model_name, node) or "").strip()):
                    continue
                text = (f"{label(model_name, node)} [{node.get('name')}]"
                        + when(model_name, node, "column_invisible", parent))
                if node.get("optional") == "hide":
                    text += " (optional column, hidden by default)"
                out.append(text)
            return list(dict.fromkeys(out))

        def form_arch(model_name):
            return next(self._model_forms(model_name), None)

        Model = self.env[model]
        lines = []
        form = form_arch(model)
        if form is not None:
            def top_fields(scope):
                """Fields of this form (not of an embedded list) inside scope."""
                return [n for n in scope.xpath(".//field[@name]") if not n.xpath("ancestor::field")
                        and not is_hidden(n) and str(label(model, n) or "").strip()]  # unlabelled helpers left out

            def describe(node):
                text = f"{label(model, node)} [{node.get('name')}]"
                field = Model._fields.get(node.get("name"))
                inline = node.xpath("./list | ./tree")
                text += when(model, node, "invisible")
                if field is not None and field.type in ("one2many", "many2many") and inline:
                    text += f" (a list with columns: {', '.join(columns(field.comodel_name, inline[0], model))})"
                return text

            tree = form.getroottree()
            pages = [p for p in form.xpath("//page") if not is_hidden(p) and not p.xpath("ancestor::field")]
            # lxml elements are not stable objects: compare them by their path in the form
            in_pages = {tree.getpath(n) for p in pages for n in top_fields(p)}
            main = [describe(n) for n in top_fields(form) if tree.getpath(n) not in in_pages]
            if main:
                lines.append("Main part of the form: " + ", ".join(dict.fromkeys(main)))
            for page in pages:
                title = f"Tab '{page.get('string') or page.get('name') or ''}'" + when(model, page, "invisible")
                shown = [describe(n) for n in top_fields(page)]
                panels = [w.get("name") for w in page.xpath(".//widget[@name]")]
                # The tab's titled sections ("Sales", "Delivery", "Invoicing", "Tracking"), as on the screen.
                sections = [g for g in page.xpath(".//group[@string]") if not g.xpath("ancestor::field")
                            and not is_hidden(g) and not g.xpath("ancestor::group[@string]")]
                if shown and sections:
                    placed, parts = set(), []
                    for group in sections:
                        names = [n for n in top_fields(group)]
                        placed |= {tree.getpath(n) for n in names}
                        if names:
                            parts.append(f"{group.get('string')}: " + ", ".join(dict.fromkeys(describe(n) for n in names)))
                    rest = [describe(n) for n in top_fields(page) if tree.getpath(n) not in placed]
                    if rest:
                        parts.append("Other: " + ", ".join(dict.fromkeys(rest)))
                    lines.append(f"{title}, in sections: " + " | ".join(parts))
                elif shown:
                    lines.append(f"{title}: " + ", ".join(dict.fromkeys(shown)))
                elif panels:
                    lines.append(f"{title}: a special panel ({', '.join(dict.fromkeys(panels))}), no plain fields")
        # A line model (sale.order.line): the list it is shown in on its parent's form, with its column labels.
        for field in Model._fields.values():
            if field.type != "many2one" or field.comodel_name not in self.env or field.comodel_name == model:
                continue
            parent = self.env[field.comodel_name]
            o2m = [f for f in parent._fields.values() if f.type == "one2many" and f.comodel_name == model
                   and f.inverse_name == field.name]
            if not o2m:
                continue
            parent_form = form_arch(field.comodel_name)
            if parent_form is None:
                continue
            for line_field in o2m[:2]:
                for node in parent_form.xpath(f"//field[@name='{line_field.name}']")[:1]:
                    inline = node.xpath("./list | ./tree")
                    if inline:
                        lines.append(f"Shown as '{label(field.comodel_name, node)}' on the "
                                     f"{self._model_label(field.comodel_name)} form, with columns: "
                                     + ", ".join(columns(model, inline[0], field.comodel_name)))
        if not lines:
            return ""
        text = "\n".join(lines)[:SCREEN_OUTLINE_CAP]
        return ("\n\nHOW THE SCREENS SHOW THIS (labels as the user sees them; [technical name]; a column can have "
                "another label than the field, e.g. a Subtotal field shown as 'Amount'; some columns only show with "
                "certain settings; a column or field marked 'shown only when' or 'optional' is not always on the "
                "screen: say so when you list it):\n" + text)

    def _call_mcp_readable(self, tool, arguments):
        """Run a read tool; when Odoo refuses it only because some asked-for fields are private for this user
        (an employee's wage, or a field of the private profile), leave those fields out and run it again.

        Returns (result, note for the AI). If every field is blocked, the original error is raised unchanged, so
        a question about private data is still refused.
        """
        try:
            return self._call_mcp(tool, arguments), ""
        except AccessError as err:
            fields = arguments.get("fields")
            message = str(err.args[0] if err.args else err)
            if (tool["name"] not in ("search_records", "get_record") or not isinstance(fields, list)
                    or len(fields) < 2 or not FIELD_ACCESS_ERROR.search(message)):
                raise
            blocked = []
            for field in fields:
                probe = dict(arguments, fields=[field])
                if tool["name"] == "search_records":
                    probe["limit"] = 1
                try:
                    self._call_mcp(tool, probe)
                except AccessError:
                    blocked.append(field)
            readable = [f for f in fields if f not in blocked]
            if not blocked or not readable:
                raise err from None
            arguments["fields"] = readable
            result = self._call_mcp(tool, arguments)
        model = arguments.get("model")
        labels = blocked
        if model in self.env:
            # sudo: only the field labels ("Private Street"), which Odoo hides along with the private values.
            meta = self.env[model].sudo().fields_get(blocked, attributes=["string"])
            labels = [meta.get(f, {}).get("string") or f for f in blocked]
        return result, (f" Not readable for this user under Odoo's access rights, so left out: {', '.join(labels)}. "
                        "Every other asked-for field is shown. Mention the left-out ones only if the user asked for "
                        "them (say they are private to HR or restricted); do not say other fields failed.")

    @staticmethod
    def _result_text(result):
        return "\n".join(
            part.get("text", "") for part in result.get("content") or [] if isinstance(part, dict)
        ).strip()
