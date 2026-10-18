from urllib.parse import urlsplit

from lxml import html
from markdown2 import markdown

from odoo.tools import html_sanitize


def format_reply(content):
    rendered = markdown(
        content, safe_mode="escape",
        extras=["fenced-code-blocks", "tables", "strike"],
    )
    clean = html_sanitize(
        rendered, silent=False, sanitize_attributes=True,
        strip_style=True, strip_classes=True,
    )
    root = html.fragment_fromstring(clean, create_parent="div")
    # Markdown images must not load provider-supplied remote resources.
    for image in root.iter("img"):
        image.tail = (image.get("alt") or "") + (image.tail or "")
        image.drop_tag()
    for link in root.iter("a"):
        href = link.get("href", "")
        try:
            parsed = urlsplit(href)
        except ValueError:
            link.drop_tag()
            continue
        if parsed.scheme.lower() not in {"", "http", "https", "mailto"} or href.startswith("//"):
            link.drop_tag()
            continue
        link.set("rel", "noopener noreferrer")
        if parsed.scheme.lower() in {"http", "https"}:
            link.set("target", "_blank")
    return html.tostring(root, encoding="unicode")
