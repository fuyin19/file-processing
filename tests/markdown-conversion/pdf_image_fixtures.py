"""Invented, self-drawn PDF figure fixtures; no captured document content."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


DOCUMENT_ID = "sha256:" + "5" * 64
PAGE_SIZE = (400, 500)


def body_result(markdown: str, pages: int = 1) -> dict:
    from adapters import markdown_to_canonical
    from canonical import normalize_canonical_text

    result = markdown_to_canonical(markdown, DOCUMENT_ID, "preserve", "pdf")
    document = result["source_units"][0]
    document["locator"] = {"kind": "pdf", "page_range": [1, pages]}
    for record in result["content"] + result["tables"]:
        record["source_locator"] = {
            "source_unit_id": document["id"],
            "extraction_method": "pdf-inspector",
            "page_range": [1, pages],
        }
    for page in range(1, pages + 1):
        result["source_units"].append({
            "id": f"unit-{page:016x}", "type": "page", "index": page,
            "locator": {"kind": "pdf", "page": page, "bbox": [0, 0, *PAGE_SIZE]},
            "status": "complete", "warnings": [],
        })
    normalize_canonical_text(result["content"], result["tables"], "preserve")
    result.update(title="Synthetic figures", adapter={
        "name": "pdf-inspector", "version": "synthetic",
        "limitations": [],
    })
    return result


def body_projection(result: dict) -> dict:
    """Preserve text/table values and order, excluding final image-sensitive IDs."""
    nodes = []
    table_occurrences = {table["table_id"]: index for index, table in enumerate(result["tables"])}
    for node in result["content"]:
        if node["type"] != "image":
            clone = deepcopy(node)
            clone.pop("id", None)
            if clone["type"] == "table":
                clone["table_id"] = table_occurrences[clone["table_id"]]
            nodes.append(clone)
    tables = deepcopy(result["tables"])
    for table in tables:
        table.pop("table_id")
    return {"content": nodes, "tables": tables}


def draw_text(pdf, items, text, x, y, page=1, size=11):
    pdf.setFillColorRGB(0, 0, 0)
    pdf.setFont("Helvetica", size)
    pdf.drawString(x, y, text)
    # Inspector's PDF-space boxes are supplied by the caller's position seam.
    items.append({"text": text, "x": x, "y": y,
                  "width": stringWidth(text, "Helvetica", size),
                  "height": size + 2, "page": page})


def figure_pdf(path: Path, *, detached=False, vector_only=False, table=False,
               ambiguous=False, inline=False, pages=1, rotate_crop=False,
               caption_style=None):
    """Return source annotations independent of the enhancement output.

    The expected figure includes its offset caption and optional disconnected
    panel; neither the output crop nor the matcher defines this denominator.
    Coordinates are PDF bottom-left user space.
    """
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader

    pdf = canvas.Canvas(str(path), pagesize=PAGE_SIZE, pageCompression=0)
    items, annotations, markdown = [], {}, []
    for page in range(1, pages + 1):
        before_lines = (["Repeated boundary", "Repeated explanatory continuation"] if ambiguous else
                        [f"Opening observation for panel {page}.", "This is the complete introductory paragraph."])
        after_lines = (["Repeated boundary", "Repeated explanatory continuation"] if ambiguous else
                       [f"Closing observation for panel {page}.", "This is the complete concluding paragraph."])
        before, after = "\n".join(before_lines), "\n".join(after_lines)
        for index, line in enumerate(before_lines):
            draw_text(pdf, items, line, 35, 455 - 15 * index, page)
        if inline:
            tile = Image.new("RGB", (12, 12), "black")
            draw_text(pdf, items, "Person A", 35, 290, page)
            pdf.drawImage(ImageReader(tile), 80, 288, width=9, height=11)
            draw_text(pdf, items, "completed the review.", 92, 290, page)
            annotations[page] = {"bbox": [80, 288, 89, 299], "inline": True}
            markdown.extend([before, "Person A completed the review.", after])
        else:
            if not vector_only:
                tile = Image.new("RGBA", (100, 65), (255, 255, 255, 0))
                painting = ImageDraw.Draw(tile)
                painting.rectangle((4, 4, 46, 60), fill=(220, 35, 25, 200))
                painting.ellipse((53, 10, 95, 55), fill=(25, 150, 60, 255))
                pdf.drawImage(ImageReader(tile), 65, 255, width=120, height=85, mask="auto")
            pdf.setFillColorRGB(0.1, 0.25, 0.9)
            pdf.rect(190, 267, 45, 60, stroke=0, fill=1)
            pdf.setStrokeColorRGB(0.05, 0.05, 0.05)
            pdf.line(55, 250, 300, 250)
            pdf.line(55, 250, 55, 350)
            draw_text(pdf, items, "Internal label", 72, 315, page)
            if detached:
                pdf.setFillColorRGB(0.65, 0.1, 0.8)
                pdf.circle(278, 377, 20, stroke=0, fill=1)
                draw_text(pdf, items, "Detached label", 215, 408, page)
                top = 421
            else:
                top = 351
            caption = None
            if caption_style:
                caption_lines = ["Figure note for both panels.", "Read the panels together."]
                caption = "\n".join(caption_lines)
                caption_x, caption_size = (35, 8) if caption_style == "small" else (100, 11)
                for index, line in enumerate(caption_lines):
                    draw_text(pdf, items, line, caption_x, 212 - 15 * index, page, caption_size)
                # A second panel is separated from both the first drawing and
                # the caption by more than the text-based grouping tolerance.
                pdf.setFillColorRGB(0.65, 0.1, 0.8)
                pdf.rect(75, 155, 120, 25, stroke=0, fill=1)
            else:
                draw_text(pdf, items, "Figure note outside the drawing", 65, 212, page)
            if table:
                draw_text(pdf, items, "Item", 75, 295, page)
                draw_text(pdf, items, "Measure", 135, 295, page)
                draw_text(pdf, items, "Alpha", 75, 275, page)
                draw_text(pdf, items, "27", 135, 275, page)
                table_md = "| Item | Measure |\n| --- | --- |\n| Alpha | 27 |"
                markdown.extend([before, table_md, after])
            elif caption:
                markdown.extend([before, caption, after])
            else:
                markdown.extend([before, after])
            annotations[page] = {"bbox": [min(54, caption_x) if caption else 54,
                                           154 if caption else 210, 302, top], "inline": False}
        for index, line in enumerate(after_lines):
            draw_text(pdf, items, line, 35, 120 - 15 * index, page)
        pdf.showPage()
    pdf.save()
    if rotate_crop:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import RectangleObject
        reader = PdfReader(path)
        writer = PdfWriter()
        for page in reader.pages:
            page.cropbox = RectangleObject([20, 80, 380, 480])
            page.rotate(90)
            writer.add_page(page)
        rotated = path.with_name(path.stem + "-rotated.pdf")
        with rotated.open("wb") as stream:
            writer.write(stream)
        path = rotated
    return path, body_result("\n\n".join(markdown), pages), items, annotations


def decorated_prose_pdf(path: Path, case: str, variant: int):
    """Separate genuine diagrams/glyphs from ordinary document decoration.

    Labels, position and physical scale vary together without source-specific
    keywords. The returned figure bounds come from the drawing coordinates.
    """
    from PIL import Image, ImageDraw
    from reportlab.lib.utils import ImageReader

    scale, offset_x, offset_y, label = ((1.0, 0, 0, "Alpha") if variant == 0 else
                                      (0.82, 25, 30, "Willow"))
    pdf = canvas.Canvas(str(path), pagesize=PAGE_SIZE, pageCompression=0)
    items, paragraphs = [], []

    def point(x, y):
        return offset_x + x * scale, offset_y + y * scale

    def text(line, x, y, size=11):
        px, py = point(x, y)
        draw_text(pdf, items, line, px, py, size=size * scale)

    def paragraph(lines, x, y, size=11):
        paragraphs.append("\n".join(lines))
        for index, line in enumerate(lines):
            text(line, x, y - 15 * index, size)

    def line(x0, y0, x1, y1, width=0.7):
        pdf.setLineWidth(width * scale)
        pdf.line(*point(x0, y0), *point(x1, y1))

    def rectangle(x, y, width, height, fill):
        pdf.setFillColorRGB(*fill)
        pdf.setStrokeColorRGB(0.15, 0.15, 0.15)
        pdf.setLineWidth(0.8 * scale)
        pdf.rect(*point(x, y), width * scale, height * scale, stroke=1, fill=1)

    required_box = None
    if case in {"page_rules", "short_text_marks"}:
        for number in range(4):
            lines = [f"{label} observation {number} starts with a complete sentence.",
                     "Its supporting explanation continues on the following line."]
            baseline = 450 - number * 90
            paragraph(lines, 35, baseline, size=9)
            if case == "short_text_marks":
                for index, value in enumerate(lines):
                    width = stringWidth(value, "Helvetica", 9)
                    y = baseline - 15 * index
                    # Only the final short stroke overruns the glyph box, by a
                    # fraction of line height. It remains attached to prose.
                    line(35 + width - 12, y - 1, 35 + width + 2.5, y - 1, 0.5)
                    line(35 + width - 14, y + 3.5, 35 + width + 3, y + 3.5, 0.5)
        if case == "page_rules":
            line(25, 480, 375, 480)
            line(25, 474, 375, 474)
            for low, high in ((410, 440), (300, 340), (215, 245), (120, 160)):
                line(17, low, 17, high)
    else:
        paragraph([f"Opening {label} observation explains the report.",
                   "This is the complete introductory paragraph."], 35, 455)
        if case == "filled_prose_box":
            fill = (0.9, 0.9, 0.9) if variant == 0 else (0.72, 0.84, 0.97)
            rectangle(28, 270, 344, 88, fill)
            paragraph([f"The {label} note records the agreed operating details.",
                       "Its explanation continues as an ordinary body paragraph.",
                       "The final sentence completes the same retained note."], 40, 339, size=9)
        elif case == "inline_glyph_with_rule":
            left = "Person Al" if variant == 0 else "Member Jo"
            right = "completed the recorded review." if variant == 0 else "confirmed the written finding."
            paragraph_text = left + " " + right
            paragraphs.append(paragraph_text)
            text(left, 40, 300)
            left_width = stringWidth(left, "Helvetica", 11)
            glyph_x = 40 + left_width + 2
            glyph = Image.new("RGB", (12, 14), "white")
            painting = ImageDraw.Draw(glyph)
            painting.line((2, 2, 9, 11), fill="black", width=2)
            painting.line((9, 2, 2, 11), fill="black", width=2)
            pdf.drawImage(ImageReader(glyph), *point(glyph_x, 299),
                          width=8 * scale, height=12 * scale)
            right_x = glyph_x + 10
            text(right, right_x, 300)
            right_edge = right_x + stringWidth(right, "Helvetica", 11)
            # A long line intersects the small image. Connectivity to this rule
            # must not make the missing character look like a complete chart.
            line(39, 299.5, right_edge + 3, 299.5, 0.7)
            required_box = [*point(glyph_x, 299), *point(glyph_x + 8, 311)]
        elif case == "connected_text_boxes":
            fill = (0.88, 0.88, 0.88) if variant == 0 else (0.76, 0.88, 0.82)
            rectangle(65, 275, 120, 80, fill)
            rectangle(230, 275, 120, 80, fill)
            paragraph([f"Prepare {label}", "Check the inputs."], 75, 330, size=8)
            paragraph([f"Review {label}", "Record the result."], 240, 330, size=8)
            line(185, 315, 230, 315)
            line(222, 320, 230, 315)
            line(222, 310, 230, 315)
            required_box = [*point(64, 274), *point(351, 356)]
        else:
            raise ValueError(case)
        paragraph([f"Closing {label} observation completes the report.",
                   "This is the complete concluding paragraph."], 35, 120)
    pdf.showPage()
    pdf.save()
    return path, body_result("\n\n".join(paragraphs)), items, required_box
