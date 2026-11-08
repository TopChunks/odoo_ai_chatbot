"""Read tools: Odoo's results turned into tables, record cards and totals for the user. Part of the ai_agent.engine
model (see ai_agent_engine.py).
"""

import json
import logging
import re
import uuid

from odoo import _, api, models

from odoo.addons.mcp_server.controllers import utils as mcp_utils

from .ai_agent_engine_constants import (
    ARCHIVED_NOTE_MODELS, CELL_TEXT_CAP, NAME_HINT_MODELS, SCHEMA_TEXT_CAP, SHARED_TEXT_CAP, TABLE_ROW_CAP,
)

_logger = logging.getLogger(__name__)


class AiAgentEngineRead(models.AbstractModel):
    _inherit = "ai_agent.engine"

    # ------------------------------------------------------------------
    # Read tools
    # ------------------------------------------------------------------
    @api.model
    def _tool_read(self, tool, arguments, config, state):
        name = tool["name"]
        if name == "search_records" and arguments.get("limit"):
            arguments["limit"] = min(int(arguments["limit"]), TABLE_ROW_CAP)
        elif name == "search_records":
            arguments["limit"] = min(TABLE_ROW_CAP, 40)
        if isinstance(arguments.get("order"), str):
            arguments["order"] = arguments["order"].strip().strip("\"'").strip() or None
        signature = f"{name}:{json.dumps(arguments, sort_keys=True, default=str)}"
        if tool["kind"] != "schema":
            existing = state.setdefault("signatures", {}).get(signature)
            if existing:
                return f"[{existing}] Same query as {existing}; use that result."
        if any(key in arguments for key in ("screen", "filters", "period")):
            model = arguments.get("model")
            if model in self.env:
                extra, labels = self._scope_terms(model, arguments)
                domain = self._domain(arguments)
                arguments["domain"] = [list(t) if isinstance(t, tuple) else t for t in domain] + extra
                state.setdefault("filter_labels", {})[signature] = labels
            for key in ("screen", "filters", "period"):
                arguments.pop(key, None)
        sort_after = []
        dropped = self._drop_unsortable(arguments, sort_after)
        left_out = ""
        if name == "aggregate_records" and not arguments.get("groupby"):
            arguments.pop("groupby", None)
            structured = self._aggregate_totals(arguments)
            text = json.dumps(structured["groups"], ensure_ascii=False, default=str)
        else:
            result, left_out = self._call_mcp_readable(tool, arguments)
            if name == "search_records" and not (result.get("structuredContent") or {}).get("records"):
                singular = self._singular_retry(arguments)
                if singular:
                    # "bikes" finds nothing, the names say "Bike": search the singular word instead.
                    retried, left_out = self._call_mcp_readable(tool, singular[0])
                    if (retried.get("structuredContent") or {}).get("records"):
                        arguments, result = singular[0], retried
                        left_out += f" Nothing matched '{singular[1]}', so '{singular[2]}' was searched instead."
            text = self._result_text(result)
            if tool["kind"] == "schema":
                outline = ""
                if name == "get_fields" and isinstance(arguments.get("model"), str) and arguments["model"] in self.env:
                    # How the screens show these fields: their labels there can differ (price_subtotal is
                    # "Amount" in the order lines), and the user talks about what they see.
                    outline = self._screen_outline(arguments["model"])
                return text[:SCHEMA_TEXT_CAP - len(outline)] + outline
            structured = result.get("structuredContent") or {}
        if name == "search_records":
            summary = self._block_table(arguments, structured, state, signature)
            if sort_after and state["blocks"] and state["blocks"][-1].get("type") == "table":
                dropped = self._sort_rows(state["blocks"][-1], sort_after, dropped)
        elif name == "get_record":
            summary = self._block_record(arguments, structured, state, signature)
        elif name == "aggregate_records":
            summary = self._block_groups(arguments, structured, state, signature)
        elif name == "list_record_attachments":
            summary = self._block_attachments(structured, state, signature)
        else:
            summary = self._block_text(name, text, state, signature)
        summary += dropped + left_out + self._date_meanings(arguments)
        if config["share_data"]:
            summary += "\n\nRESULT (also shown to the user):\n" + text[:SHARED_TEXT_CAP]
        return summary

    @api.model
    def _aggregate_totals(self, arguments):
        """Overall totals of the matching records (aggregate_records without groupby), with the same MCP gates."""
        mixin = self.env["mcp.mixin"]
        model = arguments.get("model")
        mixin._resolve_model(model)
        mixin._check_op(model, "read")
        aggregates = arguments.get("aggregates") or ["__count"]
        if isinstance(aggregates, str):
            aggregates = [aggregates]
        domain = self._domain(arguments)
        with self.env.cr.savepoint():
            groups = self.env[model].formatted_read_group(domain, [], list(dict.fromkeys([*aggregates, "__count"])))
        # The number of matching records is not a record value: the AI gets it even with sharing off.
        count = groups[0].get("__count", 0) if groups else 0
        groups = [{key: value for key, value in group.items() if not key.startswith("__") or key in aggregates}
                  for group in groups]
        return {"model": model, "groupby": [], "aggregates": list(aggregates), "groups": groups, "count": count}

    @api.model
    def _drop_unsortable(self, arguments, sort_after=None):
        """Remove sort keys on computed fields Odoo cannot sort by, and say so (instead of a failed query).

        Computed fields of a plain list are collected in sort_after, to sort the rows once they are read."""
        model, order = arguments.get("model"), arguments.get("order")
        if not isinstance(order, str) or model not in self.env:
            return ""
        fields_map = self.env[model]._fields
        grouped = "groupby" in arguments
        groupby = arguments.get("groupby") or []
        groupby = [groupby] if isinstance(groupby, str) else groupby
        aggregates = arguments.get("aggregates") or ["__count"]
        aggregates = [aggregates] if isinstance(aggregates, str) else aggregates
        # a grouped query can only be sorted by its groups and its totals (what Odoo's pivot allows)
        sortable = {s.split(":")[0] for s in groupby} | set(groupby) | set(aggregates) | {"__count"}
        kept, dropped = [], []
        for term in (t.strip() for t in order.split(",") if t.strip()):
            name = term.split()[0]
            field = fields_map.get(name)
            if grouped and name not in sortable:
                dropped.append(field.string if field else name)
            elif ":" in name or name.startswith("__") or field is None or field.store:
                kept.append(term)  # aggregates and unknown names are left to MCP's own validation
            else:
                dropped.append(field.string or name)
                if sort_after is not None:
                    sort_after.append((name, field.string or name, term.lower().endswith("desc")))
        if not dropped:
            return ""
        arguments["order"] = ", ".join(kept) or None
        reason = ("a grouped result can only be sorted by its groups or totals" if grouped else
                  "Odoo computes it on the fly and cannot sort by it (filtering on it in the domain may still work)")
        return f" Not sorted by {', '.join(dropped)}: {reason}."

    @api.model
    def _date_meanings(self, arguments):
        """The date fields a search filters or groups on, as Odoo itself describes them (a sales order's Order Date
        is its confirmation date once confirmed), so the AI explains the period right instead of guessing."""
        model, groupby = arguments.get("model"), arguments.get("groupby") or []
        if not isinstance(model, str) or model not in self.env:
            return ""
        names = [t[0] for t in self._safe_domain(self._domain(arguments)) if isinstance(t, (list, tuple)) and len(t) == 3]
        names += [g.split(":")[0] for g in ([groupby] if isinstance(groupby, str) else groupby) if isinstance(g, str)]
        names = [n for n in dict.fromkeys(names) if isinstance(n, str) and n in self.env[model]._fields]
        meta = self.env[model].fields_get(names, attributes=["string", "type", "help"]) if names else {}
        notes = [f"{info['string']} ({name}): {' '.join(info['help'].split()).rstrip('.')}" for name, info in meta.items()
                 if info.get("type") in ("date", "datetime") and info.get("help")]
        return f" Date fields used, as Odoo describes them: {'; '.join(notes)}." if notes else ""

    @api.model
    def _ai_add_result(self, state, block, signature=None):
        """Name a data result (r1, r2, ...) for the AI and keep it for the answer."""
        results = state.setdefault("results", {})
        ref = f"r{len(results) + 1}"
        block["ref"] = ref
        results[ref] = block
        state["blocks"].append(block)
        if signature:
            state.setdefault("signatures", {})[signature] = ref
        return ref

    @staticmethod
    def _column_text(columns):
        return ", ".join(f"{c['name']} ({c['label']})" for c in columns)

    @api.model
    def _model_label(self, model):
        if model and model in self.env:
            return self.env["ir.model"]._get(model).name or model
        return model or _("records")

    @api.model
    def _columns(self, model, names):
        Model = self.env[model]
        meta = Model.fields_get(list(names), attributes=["string", "type", "selection", "currency_field", "digits",
                                                         "relation"])
        columns = []
        for name in names:
            info = meta.get(name)
            if not info or info["type"] in ("binary", "image") or name == "id":
                continue
            column = {"name": name, "label": info.get("string") or name, "type": info["type"]}
            if info.get("selection"):
                column["selection"] = info["selection"]
            if info.get("currency_field"):
                column["currency_field"] = info["currency_field"]
            if info["type"] in ("many2many", "one2many") and info.get("relation"):
                column["relation"] = info["relation"]
            columns.append(column)
        return columns

    @api.model
    def _with_x2many_names(self, columns, rows, shown=8):
        """Replace lists of record IDs (assignees, tags, groups, lines...) by the names the user may read."""
        for column in columns:
            relation = column.pop("relation", None)
            if not relation or relation not in self.env:
                continue
            ids = {i for row in rows for i in (row.get(column["name"]) or [])[:shown] if isinstance(i, int)}
            try:
                records = self.env[relation].browse(sorted(ids)).exists()._filtered_access("read")
                names = {record.id: record.display_name for record in records}
            except Exception:  # noqa: BLE001 - names are a convenience; the counts stay correct without them
                _logger.debug("AI Agent: skipped (%s)", 'names are a convenience; the counts stay correct without them', exc_info=True)
                continue
            for row in rows:
                value = row.get(column["name"])
                if not isinstance(value, list):
                    continue
                labels = [names.get(i, f"#{i}") for i in value[:shown]]
                more = _(" +%s more", len(value) - shown) if len(value) > shown else ""
                row[column["name"]] = ", ".join(labels) + more if labels else False
            column["type"] = "char"
        return rows

    @api.model
    def _with_document_names(self, columns, rows):
        """Activities, attachments, messages...: show "Opportunity" and the record's name, not crm.lead and 74."""
        names = {c["name"] for c in columns}
        if "res_model" not in names:
            return rows
        models = {row.get("res_model") for row in rows if isinstance(row.get("res_model"), str)}
        labels = {model: self._model_label(model) for model in models}
        documents = {}
        if "res_id" in names:
            for model in models:
                if model not in self.env:
                    continue
                ids = {row.get("res_id") for row in rows if row.get("res_model") == model and isinstance(row.get("res_id"), int)}
                try:
                    records = self.env[model].browse(sorted(ids)).exists()._filtered_access("read")
                    documents.update({(model, r.id): r.display_name for r in records})
                except Exception:  # noqa: BLE001 - a convenience; the IDs stay correct without it
                    _logger.debug("AI Agent: skipped (%s)", 'a convenience; the IDs stay correct without it', exc_info=True)
                    continue
        for row in rows:
            model = row.get("res_model")
            if "res_id" in names and isinstance(row.get("res_id"), int):
                row["res_id"] = documents.get((model, row["res_id"]), f"#{row['res_id']}")
            if isinstance(model, str):
                row["res_model"] = labels.get(model, model)
        for column in columns:
            if column["name"] == "res_id":
                column.update(type="char", label=_("Document"))
            elif column["name"] == "res_model":
                column.update(type="char", label=_("Document type"))
        return rows

    @api.model
    def _with_titles(self, model, rows):
        """Links to records whose name is a technical code (a tracked link's short URL): show their title instead."""
        if not model or model not in self.env:
            return rows
        fields_ = self.env[model]._fields
        for row in rows:
            for key, value in list(row.items()):
                field = fields_.get(str(key).split(":")[0])
                if field is None or field.type != "many2one" or not isinstance(value, (list, tuple)) or len(value) != 2 \
                        or "title" not in self.env[field.comodel_name]._fields:
                    continue
                try:
                    title = self.env[field.comodel_name].browse(value[0]).title
                except Exception:  # noqa: BLE001 - the name is kept
                    _logger.debug("AI Agent: skipped (%s)", 'the name is kept', exc_info=True)
                    continue
                if title:
                    row[key] = [value[0], title]
        return rows

    @api.model
    def _path_label(self, model, path):
        """'partner_id.city' on sale.order -> 'Customer > City'."""
        labels, current = [], self.env[model]
        for part in path.split("."):
            field = current._fields.get(part)
            if field is None:
                return path
            labels.append(field._description_string(self.env))
            if not field.relational:
                break
            current = self.env[field.comodel_name]
        return " > ".join(labels)

    @staticmethod
    def _cell(value):
        if isinstance(value, str) and len(value) > CELL_TEXT_CAP:
            return value[:CELL_TEXT_CAP] + "…"
        if isinstance(value, tuple):
            return list(value)
        return value

    @api.model
    def _block_table(self, arguments, structured, state, signature=None):
        model = structured.get("model")
        rows = (structured.get("records") or [])[:TABLE_ROW_CAP]
        total = structured.get("total", len(rows))
        rows, total = self._word_start_matches(arguments, rows, total)
        label = self._model_label(model)
        names = []
        for row in rows:
            names.extend(key for key in row if key not in names)
        columns = self._columns(model, names) if rows else []
        rows = self._with_x2many_names(columns, [dict(row) for row in rows])
        rows = self._with_document_names(columns, rows)
        rows = self._with_titles(model, rows)
        currency_field = next((c["currency_field"] for c in columns if c.get("currency_field")), None)
        ref = self._ai_add_result(state, {
            "type": "table",
            "id": uuid.uuid4().hex,
            "title": label,
            "model": model,
            "model_label": label,
            "columns": columns,
            "rows": [
                {
                    "id": row.get("id"),
                    "values": {c["name"]: self._cell(row.get(c["name"])) for c in columns},
                    # The currency of monetary amounts, when the search returned it.
                    "currency": self._cell(row.get(currency_field)) if currency_field else None,
                }
                for row in rows
            ],
            "total": total,
            "domain": self._safe_domain(self._domain(arguments)),
            "order": arguments.get("order") or False,
            "subtitle": self._scope_text(model, self._domain(arguments),
                                         state.get("filter_labels", {}).get(signature)),
        }, signature)
        ids = [row.get("id") for row in rows if row.get("id")]
        archived = self._archived_links(model, columns, rows, state) if rows else ""
        if not rows:
            return (f"[{ref}] No {label} records that this user can access match."
                    f"{self._name_hint(model, arguments)}{self._selection_hint(model, arguments)}")
        more = f" There are {total - len(rows)} more; the user can open the full list in Odoo." if total > len(rows) else ""
        facts = (self._row_facts(columns, rows, currency_field) if state.get("share_data")
                 else self._empty_columns(columns, rows))
        return (
            f"[{ref}] {total} {label} record(s) that this user can access match; {len(rows)} rows with the columns "
            f"{self._column_text(columns)}. Record IDs: {ids[:60]}.{more}{facts}{self._exact_name(model, arguments, ids)}"
            f"{archived}"
        )

    @staticmethod
    def _empty_columns(columns, rows):
        """Sharing off: which asked-for fields are not set at all (a GSTIN, an origin), so the AI says so plainly."""
        if not rows or len(rows) > 5:  # lookups of one or a few records, not lists
            return ""
        empty = [c["label"] for c in columns if c.get("type") not in ("boolean", "integer", "float", "monetary")
                 and all(row.get(c["name"]) in (None, False, "", []) for row in rows)]
        if not empty:
            return ""
        return (f" Not set (empty) in every row: {', '.join(empty)}. Only if the user asked for one of these by name, "
                "say plainly that it is not set (do not point to that column); otherwise do not mention it.")

    @api.model
    def _archived_links(self, model, columns, rows, state):
        """Rows linked to an archived product, contact... (e.g. stock of an archived product): Odoo says so itself."""
        notes = []
        for column in [c for c in columns if c["type"] == "many2one"][:4]:
            ids = {v[0] for row in rows for v in [row.get(column["name"])]
                   if isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[0], int)}
            field = self.env[model]._fields.get(column["name"])
            comodel = field.comodel_name if field is not None else None
            if not ids or comodel not in ARCHIVED_NOTE_MODELS or "active" not in self.env[comodel]._fields:
                continue
            try:
                records = self.env[comodel].with_context(active_test=False).browse(sorted(ids)[:80]).exists()
                gone = records._filtered_access("read").filtered(lambda r: not r.active)
            except Exception:  # noqa: BLE001 - a note is optional
                _logger.debug("AI Agent: skipped (%s)", 'a note is optional', exc_info=True)
                continue
            root = self.env.ref("base.partner_root", raise_if_not_found=False)
            for record in gone.filtered(lambda r: r != root)[:5]:  # OdooBot is archived by design
                text = _("%(label)s %(name)s is archived.", label=self._model_label(comodel), name=record.display_name)
                self._add_notice(state, text, "info")
                notes.append(f"{self._model_label(comodel)} id {record.id}")
        if not notes:
            return ""
        return f" Archived linked records: {', '.join(notes)}; the user sees a note, mention it in one short phrase."

    @staticmethod
    def _sort_rows(block, sort_after, dropped):
        """Sort a list by a computed field after reading it (e.g. users by last login); empty values go last."""
        key, label, descending = sort_after[0]
        if key not in {c["name"] for c in block.get("columns") or []}:
            return dropped
        rows = block.get("rows") or []
        filled = [r for r in rows if r["values"].get(key) not in (None, False, "")]
        empty = [r for r in rows if r["values"].get(key) in (None, False, "")]
        filled.sort(key=lambda r: r["values"][key], reverse=descending)
        block["rows"] = filled + empty
        more = (f" among the {len(rows)} rows shown (of {block.get('total')})"
                if (block.get("total") or 0) > len(rows) else "")
        return (f" Sorted by {label}{more}, {'descending' if descending else 'ascending'}, after reading (Odoo cannot "
                f"sort by this computed field itself); rows without a value are last.")

    @staticmethod
    def _searched_names(domain):
        return {term[2].strip() for term in domain
                if isinstance(term, (list, tuple)) and len(term) == 3 and isinstance(term[2], str)
                and term[1] in ("ilike", "=", "=ilike") and term[0] in ("name", "display_name", "complete_name", "title")
                and len(term[2].strip()) >= 3}

    @api.model
    def _singular_retry(self, arguments):
        """A one-word name search in the plural that found nothing ("bikes"): (arguments with the singular word,
        plural, singular), or None. Only plain words, only name-like fields, only when nothing was found."""
        domain = self._domain(arguments)
        names = self._searched_names(domain)
        if len(names) != 1:
            return None
        word = next(iter(names))
        if not re.fullmatch(r"[^\W\d_]{4,}", word) or not word.lower().endswith("s"):
            return None
        lower = word.lower()
        singular = word[:-3] + "y" if lower.endswith("ies") else word[:-2] if lower.endswith(("ses", "xes", "ches", "shes")) \
            else word[:-1]
        new_domain = [[t[0], t[1], singular] if isinstance(t, (list, tuple)) and len(t) == 3 and t[2] == word else t
                      for t in domain]
        return dict(arguments, domain=new_domain), word, singular

    @api.model
    def _word_start_matches(self, arguments, rows, total):
        """"Ravi" also finds "Travis" (Odoo searches inside words): when some names start a word with what the
        user wrote, keep only those. Nothing is dropped when no name does (a real partial search)."""
        names = self._searched_names(self._domain(arguments))
        if len(names) != 1 or len(rows) < 2:
            return rows, total
        word = next(iter(names))
        if not re.fullmatch(r"[^\W\d_]{3,}", word):
            return rows, total  # one plain word only, not a reference like "MB-200" or a phrase
        starts = re.compile(r"(?<![^\W\d_])" + re.escape(word), re.IGNORECASE)

        def name_of(row):
            return str(row.get("name") or row.get("display_name") or row.get("complete_name") or row.get("title") or "")

        if not all(name_of(row) for row in rows):
            return rows, total
        kept = [row for row in rows if starts.search(name_of(row))]
        if not kept or len(kept) == len(rows):
            return rows, total
        return kept, max(len(kept), total - (len(rows) - len(kept)))

    @api.model
    def _exact_name(self, model, arguments, ids):
        """Several matches, one of them with exactly the name the user wrote: say which (it is the user's own text)."""
        if len(ids) < 2:
            return ""
        names = self._searched_names(self._domain(arguments))
        if len(names) != 1:
            return ""
        name = next(iter(names)).lower()
        exact = [r.id for r in self.env[model].browse(ids[:80]) if (r.display_name or "").strip().lower() == name]
        if len(exact) != 1:
            return ""
        return (f" Record ID {exact[0]} has exactly the name the user wrote: use that one (the others only contain "
                "it); do not mention IDs to the user.")

    @api.model
    def _name_hint(self, model, arguments):
        """A name searched on the wrong kind of record ("TEST-Alpha Cycles" as a product): say where it exists.

        Only counts are given, so this is safe with sharing off; it only runs when the search found nothing.
        """
        domain = self._domain(arguments)
        names = self._searched_names(domain)
        searched_active = any(isinstance(t, (list, tuple)) and t and t[0] == "active" for t in domain)
        hints = []
        for name in sorted(names)[:3]:
            Model = self.env[model]
            if "active" in Model._fields and not searched_active:
                try:
                    archived = Model.with_context(active_test=False).search_count(
                        [(Model._rec_name or "name", "ilike", name), ("active", "=", False)], limit=5)
                except Exception:  # noqa: BLE001 - a hint is optional
                    _logger.debug("AI Agent: skipped (%s)", 'a hint is optional', exc_info=True)
                    archived = 0
                if archived:
                    return (f" But '{name}' matches {archived} archived {self._model_label(model)} record(s): search "
                            "again with [\"active\", \"in\", [true, false]] and tell the user it is archived.")
            for other, field in NAME_HINT_MODELS.items():
                if other == model or other not in self.env \
                        or not mcp_utils.check_model_operation_allowed(self.env, other, "read"):
                    continue
                try:
                    count = self.env[other].search_count([(field, "ilike", name)], limit=5)
                except Exception:  # noqa: BLE001 - a hint is optional
                    _logger.debug("AI Agent: skipped (%s)", 'a hint is optional', exc_info=True)
                    continue
                if count:
                    hints.append(f"'{name}' matches {count} {self._model_label(other)} ({other}) record(s)")
                    break
        if not hints:
            return ""
        return (" But " + "; ".join(hints) + ". The user means that kind of record: search that model instead "
                "(for sales or invoices of a contact, filter on partner_id).")

    @api.model
    def _row_facts(self, columns, rows, currency_field=None):
        """Exact counts per status/category and column totals of the returned rows, so the AI never counts itself."""
        parts = []
        for column in columns:
            values = [row.get(column["name"]) for row in rows]
            if column["type"] in ("selection", "many2one", "boolean"):
                labels = dict(column.get("selection") or [])
                counts = {}
                for value in values:
                    if isinstance(value, (list, tuple)) and len(value) == 2:
                        value = value[1]
                    key = labels.get(value, value) if value not in (None, False) else _("not set")
                    counts[str(key)] = counts.get(str(key), 0) + 1
                if 1 < len(counts) <= 12:
                    parts.append(f"{column['label']}: " + ", ".join(f"{k} {v}" for k, v in
                                                                    sorted(counts.items(), key=lambda kv: -kv[1])))
            elif column["type"] in ("integer", "float", "monetary") and column["name"] != "id":
                numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
                currencies = {str(row.get(currency_field)) for row in rows} if currency_field else set()
                if numbers and not (column["type"] == "monetary" and len(currencies) > 1):
                    parts.append(f"{column['label']} total {round(sum(numbers), 2):,}")
        if not parts:
            return ""
        return (f" Exact figures for these {len(rows)} rows (quote them; never count rows or add numbers yourself): "
                + "; ".join(parts) + ".")

    @api.model
    def _block_record(self, arguments, structured, state, signature=None):
        model = arguments.get("model")
        data = structured.get("record") or {}
        label = self._model_label(model)
        columns = self._columns(model, [k for k in data if k not in ("id", "display_name")])
        data = self._with_x2many_names(columns, [dict(data)])[0]
        ref = self._ai_add_result(state, {
            "type": "record",
            "id": uuid.uuid4().hex,
            "model": model,
            "model_label": label,
            "record_id": data.get("id"),
            "display_name": data.get("display_name") or "",
            "fields": [dict(column, value=self._cell(data.get(column["name"]))) for column in columns],
        }, signature)
        empty = "" if state.get("share_data") else self._empty_columns(columns, [data])
        return (f"[{ref}] The {label} record (ID {data.get('id')}) as a card with the fields "
                f"{', '.join(c['label'] for c in columns)}.{empty}")

    @api.model
    def _block_groups(self, arguments, structured, state, signature=None):
        model = structured.get("model")
        groupby = structured.get("groupby") or []
        aggregates = structured.get("aggregates") or []
        groups = self._with_titles(model, [dict(g) for g in structured.get("groups") or []])
        self._distinct_group_names(model, groupby, groups)
        label = self._model_label(model)
        meta = self.env[model].fields_get([g.split(":")[0] for g in groupby] + [
            a.split(":")[0] for a in aggregates if not a.startswith("__")
        ], attributes=["string", "type", "selection"])
        columns = []
        for spec in groupby:
            info = meta.get(spec.split(":")[0], {})
            granularity = f" ({spec.split(':')[1]})" if ":" in spec else ""
            path = spec.split(":")[0]
            name = info.get("string") or (self._path_label(model, path) if "." in path else spec)
            columns.append({"name": spec, "label": name + granularity,
                            "type": "group", "selection": info.get("selection")})
        for spec in aggregates:
            if spec == "__count":
                columns.append({"name": spec, "label": _("Count"), "type": "integer"})
            else:
                field, _sep, operator = spec.partition(":")
                info = meta.get(field, {})
                columns.append({"name": spec, "label": f"{info.get('string') or field} ({operator or 'sum'})",
                                "type": "float" if info.get("type") in ("monetary", "float") else info.get("type", "float")})
        domain = self._safe_domain(self._domain(arguments))
        row_domains = self._group_domains(model, groupby, domain)
        ref = self._ai_add_result(state, {
            "type": "table",
            "id": uuid.uuid4().hex,
            "title": _("%(model)s by %(groups)s", model=label,
                       groups=", ".join(c["label"] for c in columns[:len(groupby)])) if groupby
            else _("%s totals", label),
            "model": model,
            "model_label": label,
            "columns": columns,
            "rows": [{"values": {c["name"]: self._cell(group.get(c["name"])) for c in columns},
                      # The exact records behind this row, so the user can open them (like Odoo's pivot).
                      "domain": row_domains.get(self._group_key(group, groupby))} for group in groups],
            "total": len(groups),
            "grouped": True,
            "has_more": bool(structured.get("has_more")),  # more groups than shown (the list really is cut)
            "subtitle": self._scope_text(model, domain, state.get("filter_labels", {}).get(signature)),
            # How it was computed, so present can turn it into a native graph or pivot.
            "spec": {"model": model, "groupby": groupby, "aggregates": aggregates,
                     "domain": [list(t) if isinstance(t, tuple) else t for t in domain]},
        }, signature)
        keys = []
        for group in groups[:40]:
            for spec in groupby:
                value = group.get(spec)
                if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], int):
                    keys.append(f"{spec.split(':')[0]} id {value[0]}")
        more = " More groups exist (has_more)." if structured.get("has_more") else ""
        key_text = f" Group record IDs: {keys}." if keys else ""
        for spec in groupby:
            info = meta.get(spec.split(":")[0], {})
            if info.get("type") == "selection" and info.get("selection") and not structured.get("has_more"):
                # Status groups (paid / not paid...): which options have records, so a missing one is said as 0.
                present = {group.get(spec) for group in groups}
                found = [str(label) for value, label in info["selection"] if value in present]
                none = [str(label) for value, label in info["selection"] if value not in present]
                key_text += (f" {info.get('string') or spec} groups: {', '.join(found) or 'none'}"
                             + (f"; no records (0) with: {', '.join(none)}" if none else "") + ".")
        if not groups:
            return f"[{ref}] No {label} records that this user can access match, so there are no groups."
        if not groupby:
            count = structured.get("count")
            if count == 0:
                return (f"[{ref}] No {label} records that this user can access match (0 records), so the totals "
                        "are empty.")
            matching = f"{count} matching" if count is not None else "the matching"
            return (f"[{ref}] Overall totals of {matching} {label} records (records this user can access) with "
                    f"the columns {self._column_text(columns)}.")
        complete = "" if structured.get("has_more") else (
            f" This is the complete result: all {len(groups)} groups, nothing was cut off; do not say the list is "
            "capped or limited.")
        return (f"[{ref}] {len(groups)} group(s) of {label} (records this user can access) with the columns "
                f"{self._column_text(columns)}.{key_text}{more}{complete}")

    @api.model
    def _block_attachments(self, structured, state, signature=None):
        attachments = structured.get("attachments") or []
        ref = self._ai_add_result(state, {
            "type": "table",
            "id": uuid.uuid4().hex,
            "title": _("Attachments"),
            "model": "ir.attachment",
            "model_label": _("Attachments"),
            "columns": [
                {"name": "name", "label": _("Name"), "type": "char"},
                {"name": "mimetype", "label": _("Type"), "type": "char"},
                {"name": "file_size", "label": _("Size (bytes)"), "type": "integer"},
            ],
            "rows": [{"id": a.get("id"), "values": {k: a.get(k) for k in ("name", "mimetype", "file_size")}}
                     for a in attachments],
            "total": structured.get("total", len(attachments)),
        }, signature)
        return (f"[{ref}] {len(attachments)} attachment(s). Attachment IDs: "
                f"{[a.get('id') for a in attachments]}.")

    @api.model
    def _block_text(self, name, text, state, signature=None):
        ref = self._ai_add_result(state, {"type": "text", "id": uuid.uuid4().hex, "title": name,
                                     "text": text[:SHARED_TEXT_CAP]}, signature)
        return f"[{ref}] Result of {name} (text)."

    @staticmethod
    def _safe_domain(domain):
        try:
            json.dumps(domain)
            return domain or []
        except (TypeError, ValueError):
            return []
