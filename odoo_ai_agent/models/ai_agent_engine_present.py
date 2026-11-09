"""Presenting the answer: present, calculations and charts. Part of the ai_agent.engine model (see ai_agent_engine.py).
"""

import copy
import json
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.fields import Domain

from odoo.addons.mcp_server.controllers import utils as mcp_utils

from . import ai_agent_formula as formulas

from .ai_agent_engine_constants import (
    CHART_INTENT, COMPARE_INTENT,
)


class AiAgentEnginePresent(models.AbstractModel):
    _inherit = "ai_agent.engine"

    # ------------------------------------------------------------------
    # Presenting the answer
    # ------------------------------------------------------------------
    @api.model
    def _tool_present(self, arguments, state):
        """Build what the user sees from named results, in Odoo; the AI only gives names and labels."""
        results = state.get("results", {})
        chart_requested = bool(CHART_INTENT.search(state.get("question") or ""))
        items = [item for item in arguments.get("items") or [] if isinstance(item, dict)]
        if not items:
            raise UserError(_("items needs at least one result to show."))
        shown, notes = [], []
        # A chart in this call or already shown: the user has their chart, so other tables stay tables
        # (a count table next to the chart, or an overall total).
        has_chart = any((i.get("view") or "table") != "table" for i in items) or any(
            b.get("type") == "chart" and not b.get("collapsed") for b in state.get("blocks", []))
        for item in items:
            view = item.get("view") or "table"
            if chart_requested and view == "table" and not has_chart:
                ref = item.get("result")
                source = results.get(ref) if ref else None
                combined = [results.get(r) for r in item.get("combine") or []]
                if ((source and source.get("grouped") and (source.get("spec") or {}).get("groupby"))
                        or (combined and all(r and r.get("grouped") and (r.get("spec") or {}).get("groupby")
                                             for r in combined))):
                    view = "bar"
                    has_chart = True  # one chart answers the request; further tables stay tables
            labels = {str(k).lower(): str(v) for k, v in (item.get("labels") or {}).items() if v}
            again = json.dumps([item.get("result"), item.get("combine"), item.get("view") or "table"], default=str)
            if not item.get("combine") and item.get("result") not in results:
                # Checked before marking it shown: a failed call must not block the corrected one.
                raise UserError(_("Unknown result %(ref)s. Available results: %(refs)s. Results of earlier "
                                  "answers are not kept: run the query again in this answer.",
                                  ref=item.get("result"), refs=", ".join(results) or "none"))
            if again in state.setdefault("presented_items", []):
                notes.append(_("%s is already shown; it is not shown twice, so do not mention it.",
                               item.get("result") or ", ".join(item.get("combine") or [])))
                continue
            state["presented_items"].append(again)
            if item.get("combine"):
                block = None
                if view in ("bar", "line"):
                    block = self._present_period_chart(item["combine"], results, view, labels, item.get("title"),
                                                       item.get("measure"))
                if block is None:
                    block = self._present_combine(item["combine"], results, labels, item.get("title"))
                    specs = [results.get(ref, {}).get("spec") or {} for ref in item["combine"]]
                    if chart_requested and any(
                            "." in group.split(":")[0] for spec in specs for group in spec.get("groupby") or []):
                        notes.append(_("This grouping uses a related field that Odoo's native charts cannot draw, "
                                       "so the comparison is shown as a table."))
            else:
                ref = item.get("result")
                if ref not in results:
                    raise UserError(_("Unknown result %(ref)s. Available results: %(refs)s. Results of earlier "
                                      "answers are not kept: run the query again in this answer.",
                                      ref=ref, refs=", ".join(results) or "none"))
                source = results[ref]
                spec_groupby = (source.get("spec") or {}).get("groupby") or []
                if view != "table" and any("." in g.split(":")[0] for g in spec_groupby):
                    # Odoo's graph and pivot cannot group through a relation: show the table, and say how to chart it.
                    view = "table"
                    notes.append(_("%(ref)s is grouped through a related record (%(groupby)s), which Odoo charts cannot "
                                   "draw, so it is shown as a table. To chart it, group by a field of the model itself "
                                   "(a reporting model such as sale.report has many of them).",
                                   ref=ref, groupby=", ".join(spec_groupby)))
                if view in ("bar", "line", "pie", "pivot"):
                    block = self._present_chart(source, view, labels, item.get("title"), item.get("measure"))
                else:
                    block = copy.deepcopy(source)
                    block.pop("ref", None)
                    block["id"] = uuid.uuid4().hex
                    if item.get("title"):
                        block["title"] = item["title"]
                    if block["type"] == "table":
                        self._present_columns(block, labels, item.get("columns"))
                        if item.get("share") or item.get("change"):
                            self._add_share_change(block, item.get("share"), item.get("change"))
                    # Shown as is: no need to repeat it under "Data used".
                    state.setdefault("presented_refs", []).append(ref)
            block["presented"] = True
            state["blocks"].append(block)
            kind = view if not item.get("combine") else ("combined table" if block["type"] == "table" else f"{view} chart")
            shown.append(f"{block.get('title') or block['type']} ({kind})")
        state["presented"] = True
        return ("Shown to the user, below your text: " + "; ".join(shown) + ". Do not repeat these tables in your text."
                + "".join(" " + note for note in notes))

    @api.model
    def _auto_present_chart(self, state):
        """Weak models sometimes finish after computing a grouped result without calling present."""
        if not CHART_INTENT.search(state.get("question") or "") or state.get("presented"):
            return False
        if any(block.get("type") == "chart" for block in state.get("blocks", [])):
            return False
        results = state.get("results") or {}
        candidates = [(ref, result) for ref, result in results.items()
                      if result.get("type") == "table" and result.get("grouped") and result.get("spec")]
        if not candidates:
            return False
        item = {"result": candidates[-1][0], "view": "bar"}
        if COMPARE_INTENT.search(state.get("question") or "") and len(candidates) >= 2:
            previous, latest = candidates[-2:]
            left, right = previous[1]["spec"], latest[1]["spec"]
            if left.get("model") == right.get("model") and left.get("groupby") == right.get("groupby"):
                item = {"combine": [previous[0], latest[0]], "view": "bar"}
        self._tool_present({"items": [item]}, state)
        return True

    # ------------------------------------------------------------------
    # Calculations: the AI writes the formula, Odoo computes it on values the AI never sees
    # ------------------------------------------------------------------
    @api.model
    def _tool_calculate(self, arguments, state):
        mode = arguments.get("mode")
        inputs = arguments.get("inputs") or {}
        label = (arguments.get("label") or "").strip() or _("Result")
        formula = (arguments.get("formula") or "").strip()
        if mode not in ("column", "filter", "total"):
            raise UserError(_("mode must be column, filter or total."))
        if not isinstance(inputs, dict) or not inputs:
            raise UserError(_("inputs needs at least one variable, e.g. {\"won\": \"r1.__count\"}."))
        for name in inputs:
            if not isinstance(name, str) or not name.isidentifier() or name in formulas.FUNCTIONS or name == "today":
                raise UserError(_("%s cannot be used as a variable name.", name))
        try:
            tree = formulas.parse(formula, set(inputs))
        except formulas.FormulaError as err:
            raise UserError(str(err)) from err
        today = fields.Date.context_today(self.with_context(tz=self.env.user.tz or "UTC"))
        results = state.get("results", {})
        if mode == "total":
            return self._calculate_total(inputs, tree, formula, label, results, state, today)
        ref = arguments.get("result")
        source = results.get(ref)
        if not source or source.get("type") != "table":
            raise UserError(_("%(mode)s needs a table result of this answer as 'result' (available: %(refs)s).",
                              mode=mode, refs=", ".join(r for r, b in results.items() if b.get("type") == "table")
                              or "none"))
        columns = {name: self._calc_column(source, column) for name, column in inputs.items()}
        rows = source.get("rows") or []
        values = [formulas.evaluate(tree, {name: self._calc_value(row, column) for name, column in columns.items()},
                                    today=today) for row in rows]
        block = copy.deepcopy(source)
        block.pop("ref", None)
        block["id"] = uuid.uuid4().hex
        block["calculated"] = True
        caption = f"{label} = {formula}"
        block["subtitle"] = " · ".join(part for part in (block.get("subtitle"), caption) if part)
        partial = (f" It is computed on the {len(rows)} rows shown, of {source.get('total')} in total."
                   if (source.get("total") or 0) > len(rows) else "")
        if mode == "filter":
            kept = [row for row, value in zip(block["rows"], values) if value is True]
            block["rows"] = kept
            block["total"] = len(kept)
            block["title"] = label
            ids = [row.get("id") for row in kept if row.get("id")]
            if not block.get("grouped"):
                block["domain"] = [["id", "in", ids]]  # "Open in Odoo" opens exactly these records
            new_ref = self._ai_add_result(state, block)
            return (f"[{new_ref}] {len(kept)} of {len(rows)} rows of {ref} meet the condition ({label}).{partial} "
                    f"It is shown to the user (list {new_ref} in present if you call it).")
        key = f"__calc_{len(block['columns'])}"
        filled = [v for v in values if v is not None]
        kind = "boolean" if filled and all(isinstance(v, bool) for v in filled) else \
            "float" if filled and all(isinstance(v, (int, float)) for v in filled) else "char"
        block["columns"] = block["columns"] + [{"name": key, "label": label, "type": kind}]
        for row, value in zip(block["rows"], values):
            row["values"][key] = value if value is not None else False
        if arguments.get("order") in ("asc", "desc") and kind == "float":
            # Rows without a value go last either way.
            block["rows"].sort(key=lambda r: (r["values"].get("enough_data") is False,  # too little data: not ranked
                                              r["values"][key] is False,
                                              -r["values"][key] if arguments["order"] == "desc" and r["values"][key] is not False
                                              else (r["values"][key] or 0)))
        new_ref = self._ai_add_result(state, block)
        if kind == "boolean":
            detail = f" The condition is true for {sum(1 for v in filled if v)} of {len(rows)} rows."
        elif state.get("share_data"):
            detail = f" Values: {[round(v, 2) if isinstance(v, float) else v for v in values[:30]]}."
        else:
            detail = ""
        empty = f" {len(rows) - len(filled)} row(s) could not be computed (empty value or division by zero)." \
            if len(filled) < len(rows) else ""
        return (f"[{new_ref}] = {ref} with the column '{label}' added ({len(filled)} of {len(rows)} rows "
                f"computed).{detail}{empty}{partial} It is shown to the user (list {new_ref} in present if you call it); "
                "do not state the values yourself.")

    @api.model
    def _calculate_total(self, inputs, tree, formula, label, results, state, today):
        values = {}
        for name, spec in inputs.items():
            spec, _at, row_name = str(spec).partition("@")
            ref, _sep, column_name = spec.partition(".")
            source = results.get(ref)
            if not source or source.get("type") != "table" or not column_name:
                raise UserError(_("For total, each input is '<result>.<column>' of this answer, e.g. 'r1.__count' "
                                  "(got %s).", spec))
            column = self._calc_column(source, column_name)
            rows = source.get("rows") or []
            if row_name.strip():
                rows = [row for row in rows if self._row_name(source, row) == row_name.strip().lower()]
                if not rows:
                    raise UserError(_("%(ref)s has no row named %(name)s. Its rows: %(rows)s.", ref=ref,
                                      name=row_name.strip(), rows=", ".join(
                                          self._row_name(source, row, lower=False) for row in source.get("rows") or [])))
            if column.get("currency_field") or column["type"] == "monetary":
                currencies = {str(row.get("currency")) for row in rows if row.get("currency")}
                if len(currencies) > 1:
                    raise UserError(_("%(column)s of %(ref)s mixes currencies; group or filter by currency first.",
                                      column=column["label"], ref=ref))
            cells = [self._calc_value(row, column) for row in rows]
            values[name] = cells[0] if len(cells) == 1 else cells
        value = formulas.evaluate(tree, values, today=today)
        kind = "boolean" if isinstance(value, bool) else "float" if isinstance(value, (int, float)) else "char"
        block = {
            "type": "table",
            "id": uuid.uuid4().hex,
            "title": label,
            "model": False,
            "model_label": _("Calculation"),
            "columns": [{"name": "value", "label": label, "type": kind}],
            "rows": [{"values": {"value": value if value is not None else False}}],
            "total": 1,
            "subtitle": f"{label} = {formula}",
            "calculated": True,
        }
        new_ref = self._ai_add_result(state, block)
        if value is None:
            return (f"[{new_ref}] {label} could not be computed (an input is empty or a division by zero). Say so; "
                    "do not guess a value.")
        if kind == "boolean":
            return f"[{new_ref}] {label}: {'yes' if value else 'no'}. It is shown to the user; give that answer."
        shown = f" Value: {round(value, 4) if isinstance(value, float) else value}." if state.get("share_data") else ""
        return (f"[{new_ref}] {label} computed.{shown} It is shown to the user (list {new_ref} in present if you "
                "call it); do not state the value yourself.")

    @staticmethod
    def _row_name(block, row, lower=True):
        """The name of a row: its group (customer, month...) or, for a list, its first text column."""
        columns = block.get("columns") or []
        first = next((c for c in columns if c["type"] == "group"), None) or \
            next((c for c in columns if c["type"] in ("char", "many2one", "selection")), None)
        value = (row.get("values") or {}).get(first["name"]) if first else None
        if isinstance(value, (list, tuple)) and len(value) == 2:
            value = value[1]
        text = str(value if value not in (None, False) else "").strip()
        return text.lower() if lower else text

    @staticmethod
    def _calc_column(block, wanted):
        for column in block.get("columns") or []:
            if wanted in (column["name"], column["label"]) or str(wanted).lower() == str(column["label"]).lower():
                return column
        raise UserError(_("%(ref)s has no column %(column)s. Its columns: %(columns)s.", ref=block.get("ref"),
                          column=wanted, columns=", ".join(f"{c['name']} ({c['label']})"
                                                          for c in block.get("columns") or [])))

    @staticmethod
    def _calc_value(row, column):
        value = (row.get("values") or {}).get(column["name"])
        if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[0], int):
            return value[1]  # a linked record: its name
        if value is False and column["type"] != "boolean":
            return None  # empty in Odoo
        return value

    @staticmethod
    def _add_share_change(block, share, change):
        """Computed by Odoo, not the AI: % of total per number column, and change versus the previous row."""
        numeric = [c for c in block["columns"] if c["type"] in ("integer", "float", "monetary")][:2]
        rows = block["rows"]
        added = []
        for column in numeric:
            name = column["name"]
            values = [r["values"].get(name) if isinstance(r["values"].get(name), (int, float))
                      and not isinstance(r["values"].get(name), bool) else 0 for r in rows]
            if share:
                total = sum(values)
                key = f"{name}__share"
                added.append({"name": key, "label": _("%s (%% of total)", column["label"]), "type": "float"})
                for row, value in zip(rows, values):
                    row["values"][key] = round(value * 100.0 / total, 1) if total else 0.0
            if change and len(rows) > 1:
                diff_key, pct_key = f"{name}__diff", f"{name}__pct"
                added += [{"name": diff_key, "label": _("%s (change)", column["label"]), "type": "float"},
                          {"name": pct_key, "label": _("%s (%% change)", column["label"]), "type": "float"}]
                for index, row in enumerate(rows):
                    previous = values[index - 1] if index else None
                    row["values"][diff_key] = round(values[index] - previous, 2) if previous is not None else False
                    row["values"][pct_key] = (round((values[index] - previous) * 100.0 / previous, 1)
                                              if previous else False)
        block["columns"] = block["columns"] + added

    @staticmethod
    def _present_columns(block, labels, columns):
        for column in block["columns"]:
            new = labels.get(column["name"].lower()) or labels.get(str(column["label"]).lower())
            if new:
                column["label"] = new
        if columns:
            wanted = [str(c).lower() for c in columns]
            kept = []
            for wanted_name in wanted:
                for column in block["columns"]:
                    if wanted_name in (column["name"].lower(), str(column["label"]).lower()) and column not in kept:
                        kept.append(column)
            if kept:
                block["columns"] = kept

    @api.model
    def _present_combine(self, refs, results, labels, title):
        refs = [r for r in refs if isinstance(r, str)]
        missing = [r for r in refs if r not in results]
        if len(refs) < 2 or missing:
            raise UserError(_("combine needs two or more existing results (unknown: %s).", ", ".join(missing) or "-"))
        sources = [results[r] for r in refs]
        groupings = []
        for ref, source in zip(refs, sources):
            if source["type"] != "table" or not source.get("grouped"):
                raise UserError(_("%s is not a grouped result; combine works on aggregate_records results.", ref))
            groupings.append([c["name"] for c in source["columns"] if c["type"] == "group"])
        if any(g != groupings[0] for g in groupings):
            raise UserError(_("These results are not grouped the same way: %s.",
                              "; ".join(f"{r} by {', '.join(g)}" for r, g in zip(refs, groupings))))
        group_names = groupings[0]
        names = [c["name"] for s in sources for c in s["columns"] if c["type"] != "group"]

        def key(row):
            return tuple(json.dumps(v[0] if isinstance(v, list) and len(v) == 2 else v, default=str)
                         for v in (row["values"].get(g) for g in group_names))

        columns = [dict(c) for c in sources[0]["columns"] if c["type"] == "group"]
        for column in columns:
            column["label"] = labels.get(column["name"].lower()) or column["label"]
        rows = {}
        for ref, source in zip(refs, sources):
            for column in source["columns"]:
                if column["type"] == "group":
                    continue
                name = f"{ref}.{column['name']}"
                label = (labels.get(name.lower())
                         or (labels.get(column["name"].lower()) if names.count(column["name"]) == 1 else None)
                         or f"{column['label']} – {source.get('title') or ref}")
                columns.append(dict(column, name=name, label=label))
            for row in source["rows"]:
                entry = rows.setdefault(key(row), {g: row["values"].get(g) for g in group_names})
                for column in source["columns"]:
                    if column["type"] != "group":
                        entry[f"{ref}.{column['name']}"] = row["values"].get(column["name"])
        for entry in rows.values():
            for column in columns:
                if column["type"] in ("integer", "float", "monetary") and entry.get(column["name"]) is None:
                    entry[column["name"]] = 0
        return {
            "type": "table",
            "id": uuid.uuid4().hex,
            "title": title or sources[0].get("title"),
            "model": sources[0].get("model"),
            "model_label": sources[0].get("model_label"),
            "columns": columns,
            "rows": [{"values": values} for values in rows.values()],
            "total": len(rows),
            "grouped": True,
            "has_more": any(source.get("has_more") for source in sources),
        }

    @api.model
    def _present_period_chart(self, refs, results, view, labels, title, measure):
        """Grouped results that differ only by a date period (this month vs last month...) as one native Odoo chart
        with one series per period. None when they do not fit that shape (the caller then shows a combined table)."""
        sources = [results.get(r) for r in refs if isinstance(r, str)]
        specs = [source.get("spec") if source else None for source in sources]
        if len(specs) < 2 or not all(specs):
            return None
        model = specs[0]["model"]
        if any(s["model"] != model or s["groupby"] != specs[0]["groupby"] for s in specs) or len(specs[0]["groupby"]) != 1:
            return None
        if "." in specs[0]["groupby"][0].split(":")[0]:
            return None  # Odoo's graph cannot group through a relation
        def field_at(path):
            """The field a (dotted) path ends on, e.g. order_id.date_order on sale.order.line."""
            current, field = self.env[model], None
            for name in str(path).split("."):
                field = current._fields.get(name)
                if field is None:
                    return None
                if field.relational:
                    current = self.env[field.comodel_name]
            return field

        parts = []
        for spec in specs:
            dated, rest = {}, []
            for term in spec["domain"]:
                field = field_at(term[0]) if isinstance(term, (list, tuple)) and len(term) == 3 else None
                if field is not None and field.type in ("date", "datetime") and term[1] in (">=", ">", "<", "<="):
                    dated.setdefault(term[0], []).append(term)
                else:
                    rest.append(term)
            parts.append((dated, json.dumps(rest, default=str)))
        date_fields = {name for dated, _rest in parts for name in dated}
        if len(date_fields) != 1 or len({rest for _dated, rest in parts}) != 1:
            return None
        date_field = date_fields.pop()
        if "." in date_field:
            return None  # Odoo's chart cannot group by a date through a relation: keep the combined table
        spans = []
        for dated, _rest in parts:
            try:
                bounds = [fields.Datetime.to_datetime(str(term[2])[:19]) for term in dated.get(date_field, [])]
            except (TypeError, ValueError):
                return None
            if len(bounds) != 2:
                return None
            spans.append((max(bounds) - min(bounds)).days)
        days = max(spans)
        granularity = ("day" if days <= 1 else "week" if days <= 7 else "month" if days <= 31
                       else "quarter" if days <= 92 else "year")
        block = self._present_chart(sources[0], view, labels, title, measure)
        block["groupby"] = [specs[0]["groupby"][0], f"{date_field}:{granularity}"]
        block["group_labels"] = [block["group_labels"][0],
                                 field_at(date_field)._description_string(self.env)]
        block["domain"] = [list(t) if isinstance(t, tuple) else t
                           for t in Domain.OR([spec["domain"] for spec in specs])]
        block["subtitle"] = " vs ".join(s.get("subtitle") or "" for s in sources if s.get("subtitle")) or None
        return block

    @api.model
    def _present_chart(self, source, view, labels, title, measure):
        spec = source.get("spec")
        if source["type"] != "table" or not spec:
            raise UserError(_("Charts need a grouped result from aggregate_records (or use show_chart)."))
        self._check_model_read(spec["model"])
        groupby = spec["groupby"][:2]
        aggregates = spec["aggregates"] or ["__count"]
        chosen = measure if measure in aggregates else next((a for a in aggregates if a != "__count"), aggregates[0])
        by_name = {c["name"]: c for c in source["columns"]}

        def field_of(aggregate):
            return "__count" if aggregate == "__count" else aggregate.split(":")[0]

        def label_of(aggregate):
            return labels.get(aggregate.lower()) or by_name.get(aggregate, {}).get("label") or aggregate

        return {
            "type": "chart",
            "id": uuid.uuid4().hex,
            "title": title or source.get("title"),
            "model": spec["model"],
            "model_label": source.get("model_label"),
            "chart_type": view,
            "groupby": groupby,
            "group_labels": [labels.get(g.lower()) or by_name.get(g, {}).get("label") or g for g in groupby],
            "measure": field_of(chosen),
            "measure_label": label_of(chosen),
            # A pivot shows every measure of the result side by side.
            "measures": [field_of(a) for a in aggregates] if view == "pivot" else [field_of(chosen)],
            "measure_labels": [label_of(a) for a in aggregates] if view == "pivot" else [label_of(chosen)],
            "domain": spec["domain"],
            "order": None,
            "stacked": False,
            "cumulative": False,
            "subtitle": source.get("subtitle"),
        }

    # ------------------------------------------------------------------
    # Charts
    # ------------------------------------------------------------------
    @api.model
    def _check_model_read(self, model):
        if not model or model not in self.env:
            raise UserError(_("Unknown model: %s", model))
        if not mcp_utils.check_model_operation_allowed(self.env, model, "read"):
            raise AccessError(self._not_enabled_message(model, "read"))
        self.env[model].check_access("read")
        return self.env[model]

    @api.model
    def _tool_chart(self, arguments, state):
        Model = self._check_model_read(arguments.get("model"))
        model = Model._name
        groupby = arguments.get("groupby") or []
        if isinstance(groupby, str):
            groupby = [groupby]
        groupby = [g.strip() for g in groupby if isinstance(g, str) and g.strip()][:2]
        if not groupby:
            raise UserError(_("groupby needs at least one field."))
        measure = (arguments.get("measure") or "__count").strip()
        refused = self._cost_refused(model, [*groupby, measure, *(t[0] for t in self._domain(arguments)
                                                                 if isinstance(t, (list, tuple)) and t)])
        if refused:
            self._add_notice(state, refused)
            raise UserError(refused)
        chart_type = arguments.get("chart_type") or "bar"
        if chart_type not in ("bar", "line", "pie", "pivot"):
            chart_type = "bar"
        field_names = [g.split(":")[0] for g in groupby] + ([measure] if measure != "__count" else [])
        meta = Model.fields_get(field_names, attributes=["string", "type", "groupable", "aggregator", "store"])
        for spec in groupby:
            field, _sep, granularity = spec.partition(":")
            info = meta.get(field)
            if not info:
                raise UserError(_("%(model)s has no field %(field)s. Check the names with get_fields.",
                                  model=model, field=field))
            if info.get("groupable") is False:
                raise UserError(_("Records cannot be grouped by %s.", field))
            if granularity and info["type"] not in ("date", "datetime"):
                raise UserError(_("Only date fields take a granularity like ':month' (%s is not a date).", field))
        aggregate = "__count"
        if measure != "__count":
            info = meta.get(measure)
            if not info or info["type"] not in ("integer", "float", "monetary"):
                raise UserError(_("The measure must be a number field of %(model)s or '__count' (got %(measure)s).",
                                  model=model, measure=measure))
            aggregate = f"{measure}:{info.get('aggregator') or 'sum'}"
        domain = [list(t) if isinstance(t, tuple) else t
                  for t in self._domain(arguments)]
        extra, filter_labels = self._scope_terms(model, dict(arguments))
        domain += extra
        scope_domain = list(domain)
        order = (arguments.get("order") or "").lower()
        limit = arguments.get("limit")
        first, first_type = groupby[0].split(":")[0], meta[groupby[0].split(":")[0]]["type"]
        if limit and ":" not in groupby[0] and first_type in ("many2one", "selection", "char", "integer", "boolean"):
            top = Model.formatted_read_group(
                domain, [first], [aggregate], order=f"{aggregate} {order or 'desc'}", limit=int(limit),
            )
            keys = [g[first][0] if isinstance(g[first], (list, tuple)) else g[first] for g in top]
            domain = list(domain) + [(first, "in", keys)]
        groups = Model.formatted_read_group(domain, groupby[:1], [aggregate], limit=500)
        label = self._model_label(model)
        if not groups:
            return f"No {label} records that this user can access match these filters, so no chart was drawn."
        # A pie of the top N only: its slices are not the whole; say so (Odoo's pie has no "Others" slice).
        all_groups = 0
        if limit and chart_type == "pie" and ":" not in groupby[0]:
            all_groups = len(Model.formatted_read_group(scope_domain, [first], [aggregate], limit=501))
        measure_label = _("Count") if measure == "__count" else meta[measure]["string"]
        group_labels = [meta[g.split(":")[0]]["string"] for g in groupby]
        title = arguments.get("title") or _("%(measure)s by %(groups)s", measure=measure_label,
                                            groups=" / ".join(group_labels))
        chart_domain = self._safe_domain([list(term) if isinstance(term, tuple) else term for term in domain])
        if any(b.get("type") == "chart" and b.get("model") == model and b.get("chart_type") == chart_type
               and b.get("groupby") == groupby and b.get("measure") == measure and b.get("domain") == chart_domain
               for b in state["blocks"]):
            return "This chart is already shown to the user; it is not drawn twice. Do not mention this."
        state["blocks"].append({
            "type": "chart",
            "id": uuid.uuid4().hex,
            "title": title,
            "model": model,
            "model_label": label,
            "chart_type": chart_type,
            "groupby": groupby,
            "group_labels": group_labels,
            "measure": measure,
            "measure_label": measure_label,
            "domain": self._safe_domain([list(term) if isinstance(term, tuple) else term for term in domain]),
            "order": order.upper() if order in ("asc", "desc") else None,
            "stacked": bool(arguments.get("stacked")),
            "cumulative": bool(arguments.get("cumulative")),
            "subtitle": self._scope_text(model, scope_domain, filter_labels,
                                         int(limit) if limit and ":" not in groupby[0] else None)
            + (_(" · Top %(shown)s of %(all)s; the others are not in this pie.", shown=len(groups), all=all_groups)
               if all_groups > len(groups) else ""),
        })
        shared = ""
        if state.get("share_data"):
            # The groups the chart shows, so the answer names the right months, customers... (no guessing).
            values = []
            for group in groups[:24]:
                key = group.get(groupby[0])
                key = key[1] if isinstance(key, (list, tuple)) and len(key) == 2 else key
                values.append(f"{key if key not in (None, False) else _('None')}: {group.get(aggregate)}")
            more = f" (+{len(groups) - 24} more)" if len(groups) > 24 else ""
            shared = f" Groups ({measure_label}): " + "; ".join(values) + more + "."
        partial = (f" It shows only the top {len(groups)} of {all_groups} groups, so its slices do not add up to "
                   "the full total: tell the user, and suggest a bar chart if they want every group."
                   if all_groups > len(groups) else "")
        same = json.dumps(scope_domain, default=str, ensure_ascii=False)
        return (f"A {chart_type} chart of {measure_label} by {', '.join(group_labels)} on {label} "
                f"({len(groups)} groups) is shown to the user.{partial}{shared} If the user asks for a total of "
                f"this chart, use aggregate_records on {model} with exactly this domain: {same}.")
