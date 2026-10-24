"""Build the paper PDF from PAPER.md without a browser.

Requires: pip install reportlab
Usage: python paper/render_pdf.py
"""
from __future__ import annotations

import html
from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

HERE = Path(__file__).resolve().parent
PAPER_MD = HERE / "PAPER.md"
OUTPUT_PDF = HERE / "PAPER.pdf"

NAVY = colors.HexColor("#0F253B")
BLUE = colors.HexColor("#1165A6")
GRAY = colors.HexColor("#586676")
LIGHT = colors.HexColor("#ECF2F7")
WHITE = colors.white


def register_fonts() -> tuple[str, str]:
    font_dir = Path("C:/Windows/Fonts")
    regular = font_dir / "segoeui.ttf"
    bold = font_dir / "segoeuib.ttf"
    if regular.exists() and bold.exists():
        pdfmetrics.registerFont(TTFont("PaperRegular", str(regular)))
        pdfmetrics.registerFont(TTFont("PaperBold", str(bold)))
        pdfmetrics.registerFontFamily("PaperRegular", normal="PaperRegular", bold="PaperBold")
        return "PaperRegular", "PaperBold"
    return "Helvetica", "Helvetica-Bold"


REGULAR, BOLD = register_fonts()
BODY = ParagraphStyle(
    "PaperBody", fontName=REGULAR, fontSize=9.25,
    leading=14.6, textColor=NAVY, spaceAfter=7,
    allowWidows=0, allowOrphans=0,
)
TITLE = ParagraphStyle(
    "PaperTitle", parent=BODY, fontName=BOLD,
    fontSize=19.2, leading=25.5, spaceAfter=15,
)
H1 = ParagraphStyle(
    "PaperHeading", parent=BODY, fontName=BOLD,
    fontSize=12.5, leading=17.5, textColor=BLUE,
    spaceBefore=17, spaceAfter=7, keepWithNext=True,
)
H2 = ParagraphStyle(
    "PaperSubheading", parent=BODY, fontName=BOLD,
    fontSize=10.0, leading=14.5, spaceBefore=11,
    spaceAfter=5, keepWithNext=True,
)
CAPTION = ParagraphStyle(
    "PaperCaption", parent=BODY, fontSize=8.2,
    leading=12.0, textColor=GRAY,
)
LIST = ParagraphStyle(
    "PaperList", parent=BODY, leftIndent=13,
    firstLineIndent=-9, spaceAfter=4,
)
CODE = ParagraphStyle(
    "PaperCode", fontName="Courier", fontSize=7.4,
    leading=10.4, backColor=LIGHT,
    borderPadding=7, spaceAfter=9,
)


def inline_md(value: str) -> str:
    value = html.escape(value, quote=False)
    value = re.sub(r"\x60([^\x60]+)\x60", r'<font color="#005A98">\1</font>', value)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", value)
    return value


def footer(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4
    canvas.setStrokeColor(colors.HexColor("#D6DEE8"))
    canvas.line(2.1 * cm, 1.85 * cm, width - 2.1 * cm, 1.85 * cm)
    canvas.setFont(BOLD, 7.4)
    canvas.setFillColor(GRAY)
    canvas.drawString(2.1 * cm, 1.47 * cm, "DeepSeek Web HTTP Client | Relatorio tecnico 1.1")
    canvas.drawRightString(width - 2.1 * cm, 1.47 * cm, str(doc.page))
    canvas.restoreState()


def table_block(lines: list[str], available_width: float) -> Table:
    rows = []
    for line in lines:
        cells = [v.strip() for v in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
            continue
        rows.append(cells)
    columns = max(len(r) for r in rows)
    rows = [r + [""] * (columns - len(r)) for r in rows]
    widths = [available_width / columns] * columns
    data = []
    for idx, row in enumerate(rows):
        style = ParagraphStyle(
            "CellBold" if idx == 0 else "Cell",
            parent=CAPTION,
            fontName=BOLD if idx == 0 else REGULAR,
            textColor=WHITE if idx == 0 else NAVY,
            fontSize=7.7, leading=10.7, spaceAfter=0,
        )
        data.append([Paragraph(inline_md(cell), style) for cell in row])
    t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, BLUE),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return t


def build_story(lines: list[str], width: float):
    story = []
    para_lines: list[str] = []

    def flush_paragraph() -> None:
        if para_lines:
            story.append(Paragraph(inline_md(" ".join(para_lines)), BODY))
            para_lines.clear()

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            i += 1
            continue
        if stripped.startswith("```"):
            flush_paragraph()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            story.append(Preformatted("\n".join(code), CODE, maxLineLength=94, splitChars=" ,"))
            i += 1
            continue
        if stripped.startswith("|"):
            flush_paragraph()
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            story.append(table_block(block, width))
            story.append(Spacer(1, 10))
            continue
        if stripped.startswith("# "):
            flush_paragraph()
            story.append(Paragraph(inline_md(stripped[2:]), TITLE))
            story.append(HRFlowable(width="100%", thickness=1.1, color=BLUE, spaceAfter=13))
        elif stripped.startswith("## "):
            flush_paragraph()
            story.append(Paragraph(inline_md(stripped[3:]), H1))
        elif stripped.startswith("### "):
            flush_paragraph()
            story.append(Paragraph(inline_md(stripped[4:]), H2))
        elif re.match(r"^(?:\d+\. |- )", stripped):
            flush_paragraph()
            body = re.sub(r"^(?:\d+\. |- )", "", stripped)
            bullet = re.match(r"^(\d+)\.", stripped)
            mark = bullet.group(1) + "." if bullet else "-"
            story.append(Paragraph(inline_md(mark + " " + body), LIST))
        else:
            para_lines.append(stripped)
        i += 1
    flush_paragraph()
    return story


def main() -> None:
    lines = PAPER_MD.read_text(encoding="utf-8").splitlines()
    document = SimpleDocTemplate(
        str(OUTPUT_PDF), pagesize=A4,
        rightMargin=2.1 * cm, leftMargin=2.1 * cm,
        topMargin=2.2 * cm, bottomMargin=2.3 * cm,
        title="Engenharia de um cliente HTTP sem navegador para o DeepSeek Web",
        author="Projeto DeepSeek Web HTTP Client",
        subject="Paper tecnico de engenharia reversa, streaming e upload de midias",
    )
    width = A4[0] - document.leftMargin - document.rightMargin
    document.build(build_story(lines, width), onFirstPage=footer, onLaterPages=footer)
    print(str(OUTPUT_PDF))


if __name__ == "__main__":
    main()
