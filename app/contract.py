"""Visualização do contrato de garantia (.docx) como HTML imprimível. O arquivo original nunca é alterado."""

from __future__ import annotations

import html
from pathlib import Path

from docx import Document


def _runs(paragraph) -> str:
    out = []
    for run in paragraph.runs:
        text = html.escape(run.text)
        if not text:
            continue
        if run.bold:
            text = f"<strong>{text}</strong>"
        if run.italic:
            text = f"<em>{text}</em>"
        if run.underline:
            text = f"<u>{text}</u>"
        out.append(text)
    return "".join(out)


def contract_html(path: Path) -> str:
    document = Document(str(path))
    body = []
    for block in document.element.body.iterchildren():
        tag = block.tag.rsplit("}", 1)[-1]
        if tag == "p":
            from docx.text.paragraph import Paragraph
            paragraph = Paragraph(block, document)
            text = _runs(paragraph)
            if not text.strip():
                body.append("<p class='gap'>&nbsp;</p>")
                continue
            style = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
            align = {1: "center", 2: "right", 3: "justify"}.get(paragraph.alignment, "left") if paragraph.alignment is not None else "left"
            name = "h2" if style.startswith("heading") or style == "title" else "p"
            body.append(f"<{name} style='text-align:{align}'>{text}</{name}>")
        elif tag == "tbl":
            from docx.table import Table
            table = Table(block, document)
            rows = ["<tr>" + "".join(f"<td>{html.escape(cell.text)}</td>" for cell in row.cells) + "</tr>" for row in table.rows]
            body.append("<table>" + "".join(rows) + "</table>")
    return (
        "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Contrato de garantia</title><style>"
        "body{font-family:Arial,Helvetica,sans-serif;max-width:820px;margin:24px auto;padding:0 16px;line-height:1.45;color:#111;background:#fff}"
        "table{border-collapse:collapse;width:100%}td{border:1px solid #999;padding:4px 6px}p.gap{margin:4px 0}"
        ".bar{position:sticky;top:0;background:#fff;padding:8px 0;border-bottom:1px solid #ddd;margin-bottom:12px}"
        "button{padding:8px 14px;margin-right:8px;cursor:pointer}"
        "@media print{.bar{display:none}body{margin:0;max-width:none}}"
        "</style></head><body onload=\"if(location.search.indexOf('print=1')>-1)window.print()\"><div class='bar'><button onclick='window.print()'>Imprimir</button>"
        "<a href='/api/guarantees/contract/file'><button type='button'>Baixar .docx</button></a></div>"
        + "".join(body) + "</body></html>"
    )
