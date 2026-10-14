"""PDF and Excel files for a chart or grouped table shown in the chat."""
import base64
import io
import re

import xlsxwriter

from odoo import _
from odoo.tools.mimetypes import guess_mimetype

# Export format -> file mimetype.
EXPORT_FORMATS = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
# Chat chart type -> Excel chart type (an Excel scatter needs numeric categories, so it becomes a line).
_EXCEL_CHARTS = {"bar": "column", "line": "line", "pie": "pie", "doughnut": "doughnut", "scatter": "line"}


def _cell(value):
    """A table cell as shown in the chat: a linked record [id, name] shows its name, an empty value nothing."""
    if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[1], str):
        return value[1]
    return "" if value is None or value is False else value


def export_table(payload):
    """(title, headers, rows) of a chart or table payload."""
    if payload["kind"] == "chart":
        return payload["title"], [_("Label"), payload["value_label"]], list(zip(payload["labels"], payload["values"]))
    columns = payload["columns"]
    rows = [[_cell(row.get(column["key"])) for column in columns] for row in payload["rows"]]
    return payload.get("title") or "", [column["label"] for column in columns], rows


def file_name(title, file_format):
    return "%s.%s" % (re.sub(r"[^\w\- ]+", "", title).strip() or "chart", file_format)


def to_xlsx(title, headers, rows, chart_type=None):
    """The data table, and for a chart an editable Excel chart of it."""
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})
    sheet = workbook.add_worksheet()
    bold = workbook.add_format({"bold": True})
    sheet.write(0, 0, title, workbook.add_format({"bold": True, "font_size": 14}))
    sheet.write_row(2, 0, headers, bold)
    for index, row in enumerate(rows, start=3):
        sheet.write_row(index, 0, row)
    sheet.set_column(0, len(headers) - 1, 24)
    if chart_type in _EXCEL_CHARTS and rows:
        chart = workbook.add_chart({"type": _EXCEL_CHARTS[chart_type]})
        assert chart is not None  # add_chart returns None only for chart types outside _EXCEL_CHARTS
        last = 2 + len(rows)
        chart.add_series({"name": headers[1], "categories": [sheet.name, 3, 0, last, 0],
                          "values": [sheet.name, 3, 1, last, 1]})
        chart.set_title({"name": title})
        sheet.insert_chart(2, len(headers) + 1, chart)
    workbook.close()
    return buffer.getvalue()


def to_pdf(env, title, headers, rows, image=None):
    """The chart picture as drawn in the browser (``image``, raster bytes) above its data table, rendered
    by Odoo's report engine so every language and currency symbol prints."""
    html = env["ir.qweb"]._render("odoo_ai_assistant.chart_export_pdf", {
        "title": title, "headers": headers, "rows": rows,
        "image": image and "data:%s;base64,%s" % (guess_mimetype(image), base64.b64encode(image).decode("ascii")),
    })
    return env["ir.actions.report"]._run_wkhtmltopdf([html])
