"""PDF and Excel export of the tables and charts shown under AI answers.

Tables are exported from the rows the user was shown. Charts are recomputed now, with the
user's own access rights, from the chart's model, filters and groupings.
"""

import io
import re

import xlsxwriter

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import html2plaintext

from .discuss_channel import AI_CHANNEL_TYPE

PNG_DATA_URL = re.compile(r"^data:image/png;base64,[A-Za-z0-9+/=]+$")
XLSX_CHART_TYPES = {"bar": "column", "line": "line", "pie": "pie"}


class AiAgentExport(models.AbstractModel):
    _name = "ai_agent.export"
    _description = "AI Agent Export"

    @api.model
    def _block(self, message_id, block_id):
        message = self.env["mail.message"].browse(int(message_id)).exists()
        channel = self.env["discuss.channel"].browse(message.res_id) if message.model == "discuss.channel" else None
        if not message or not channel or channel.channel_type != AI_CHANNEL_TYPE:
            raise AccessError(_("This AI answer is not available."))
        channel._ai_agent_check_conversation()
        for block in (message.sudo().ai_agent_payload or {}).get("blocks", []):
            if block.get("id") == block_id and block.get("type") in ("table", "chart", "record"):
                return message, block
        raise UserError(_("This result cannot be exported."))

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------
    @staticmethod
    def _label(value, column=None):
        if isinstance(value, (list, tuple)):
            if len(value) == 2 and isinstance(value[0], (int, str, bool)) and not isinstance(value[1], (list, dict)):
                return str(value[1]) if value[1] not in (None, False) else ""
            return _("%s records", len(value))
        if isinstance(value, bool):
            # Odoo reads empty fields as False; only a real checkbox shows "No".
            if column and column.get("type") == "boolean":
                return _("Yes") if value else _("No")
            return ""
        if value is None:
            return ""
        if column and column.get("selection"):
            return dict(map(tuple, column["selection"])).get(value, value)
        if column and column.get("type") == "html":
            return html2plaintext(value or "")
        return value

    @api.model
    def _table_data(self, block):
        if block["type"] == "record":
            headers = [_("Field"), _("Value")]
            rows = [[field["label"], self._label(field.get("value"), field)] for field in block.get("fields", [])]
            return headers, rows
        columns = block.get("columns", [])
        headers = [c["label"] for c in columns]
        rows = [[self._label(row["values"].get(c["name"]), c) for c in columns] for row in block.get("rows", [])]
        return headers, rows

    @api.model
    def _chart_data(self, block):
        engine = self.env["ai_agent.engine"]
        Model = engine._check_model_read(block["model"])
        measure = block["measure"]
        if measure == "__count":
            aggregate = "__count"
        else:
            info = Model.fields_get([measure], attributes=["aggregator"])[measure]
            aggregate = f"{measure}:{info.get('aggregator') or 'sum'}"
        groupby = block["groupby"][:2]
        meta = Model.fields_get([g.split(":")[0] for g in groupby], attributes=["selection", "type"])

        def key_label(spec, value):
            info = meta.get(spec.split(":")[0], {})
            if value in (False, None):
                return _("None")
            return str(self._label(value, {"selection": info.get("selection")} if info.get("selection") else None))

        order = f"{aggregate} {block['order']}" if block.get("order") and len(groupby) == 1 else None
        measures = block.get("measures") or [measure]
        if block.get("chart_type") == "pivot" and len(measures) > 1 and len(groupby) == 1:
            # A pivot of several measures: one column per measure.
            meta_measures = Model.fields_get([m for m in measures if m != "__count"], attributes=["aggregator", "string"])
            specs = ["__count" if m == "__count" else f"{m}:{meta_measures[m].get('aggregator') or 'sum'}" for m in measures]
            labels = block.get("measure_labels") or [
                _("Count") if m == "__count" else meta_measures[m]["string"] for m in measures]
            groups = Model.formatted_read_group(block.get("domain") or [], groupby, specs, limit=1000)
            headers = [block["group_labels"][0], *labels]
            rows = [[key_label(groupby[0], g[groupby[0]]), *[g.get(s) or 0 for s in specs]] for g in groups]
            return headers, rows
        groups = Model.formatted_read_group(block.get("domain") or [], groupby, [aggregate], order=order, limit=1000)
        if len(groupby) == 1:
            headers = [block["group_labels"][0], block["measure_label"]]
            rows = [[key_label(groupby[0], g[groupby[0]]), g.get(aggregate) or 0] for g in groups]
            return headers, rows
        row_keys, column_keys, cells = [], [], {}
        for group in groups:
            row = key_label(groupby[0], group[groupby[0]])
            column = key_label(groupby[1], group[groupby[1]])
            if row not in row_keys:
                row_keys.append(row)
            if column not in column_keys:
                column_keys.append(column)
            cells[(row, column)] = group.get(aggregate) or 0
        headers = [block["group_labels"][0], *column_keys, _("Total")]
        rows = []
        for row in row_keys:
            values = [cells.get((row, column), 0) for column in column_keys]
            rows.append([row, *values, sum(values)])
        return headers, rows

    @api.model
    def _data(self, block):
        if block["type"] == "chart":
            return self._chart_data(block)
        return self._table_data(block)

    @api.model
    def _title(self, block):
        if block["type"] == "record":
            return f"{block.get('model_label')}: {block.get('display_name')}"
        return block.get("title") or block.get("model_label") or _("AI Agent result")

    @staticmethod
    def _filename(title, extension):
        safe = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "_")[:60] or "ai_agent"
        return f"{safe}.{extension}"

    # ------------------------------------------------------------------
    # Excel
    # ------------------------------------------------------------------
    @api.model
    def export_xlsx(self, message_id, block_id):
        _message, block = self._block(message_id, block_id)
        headers, rows = self._data(block)
        title = self._title(block)
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {"in_memory": True})
        sheet_name = _("Data")[:31]
        sheet = workbook.add_worksheet(sheet_name)
        title_format = workbook.add_format({"bold": True, "font_size": 14})
        header_format = workbook.add_format({"bold": True, "bg_color": "#EDE9FE", "border": 1})
        number_format = workbook.add_format({"num_format": "#,##0.00", "border": 1})
        text_format = workbook.add_format({"border": 1})
        sheet.write(0, 0, title, title_format)
        sheet.write(1, 0, _("Exported from Odoo AI Agent on %s", fields.Datetime.to_string(fields.Datetime.now())))
        for col, header in enumerate(headers):
            sheet.write(3, col, str(header), header_format)
            sheet.set_column(col, col, max(12, min(40, len(str(header)) + 4)))
        for row_index, row in enumerate(rows, start=4):
            for col, value in enumerate(row):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    sheet.write_number(row_index, col, value, number_format)
                else:
                    sheet.write(row_index, col, "" if value is None else str(value), text_format)
        chart_type = XLSX_CHART_TYPES.get(block.get("chart_type")) if block["type"] == "chart" else None
        if chart_type and rows:
            last_row = 3 + len(rows)
            value_columns = range(1, len(headers) - (1 if len(block["groupby"]) > 1 else 0))
            options = {"type": chart_type}
            if block.get("stacked") and chart_type in ("column", "line") and len(block["groupby"]) > 1:
                options["subtype"] = "stacked"
            chart = workbook.add_chart(options)
            for col in value_columns:
                chart.add_series({  # the sheet's real (translated) name, or the chart points at nothing
                    "name": [sheet_name, 3, col],
                    "categories": [sheet_name, 4, 0, last_row, 0],
                    "values": [sheet_name, 4, col, last_row, col],
                })
            chart.set_title({"name": title})
            chart.set_size({"width": 720, "height": 400})
            sheet.insert_chart(3, len(headers) + 1, chart)
        workbook.close()
        return self._filename(title, "xlsx"), output.getvalue()

    # ------------------------------------------------------------------
    # PDF
    # ------------------------------------------------------------------
    @api.model
    def export_pdf(self, message_id, block_id, image=None):
        _message, block = self._block(message_id, block_id)
        headers, rows = self._data(block)
        title = self._title(block)
        if image and not PNG_DATA_URL.match(image):
            image = None
        lang = self.env["res.lang"]._get_data(code=self.env.user.lang or "en_US")

        def display(value):
            if isinstance(value, float):
                return f"{value:,.2f}".replace(",", "\x00").replace(".", lang.decimal_point or ".").replace(
                    "\x00", lang.thousands_sep or ",")
            if isinstance(value, int) and not isinstance(value, bool):
                return f"{value:,}".replace(",", lang.thousands_sep or ",")
            return "" if value is None else str(value)

        html = self.env["ir.qweb"]._render("odoo_ai_agent.export_document", {
            "title": title,
            "subtitle": block.get("model_label") or "",
            "company": self.env.company,
            "user": self.env.user,
            "date": fields.Datetime.context_timestamp(self, fields.Datetime.now()).strftime("%Y-%m-%d %H:%M"),
            "image": image,
            "headers": headers,
            "rows": [[display(v) for v in row] for row in rows],
            "numeric": [all(isinstance(r[i], (int, float)) and not isinstance(r[i], bool) for r in rows if r[i] != "")
                        for i in range(len(headers))] if rows else [],
        })
        pdf = self.env["ir.actions.report"]._run_wkhtmltopdf(
            [str(html)], landscape=len(headers) > 6,
            specific_paperformat_args={"data-report-margin-top": 12, "data-report-header-spacing": 0},
        )
        return self._filename(title, "pdf"), pdf
