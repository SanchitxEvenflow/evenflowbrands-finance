import io
import os
import re

import pdfplumber

_WS_RE = re.compile(r"\s+")
_SKU_RE = re.compile(r"^(?:\d{3}-)?(\d+)$")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def parse_discrepancy_note(pdf_bytes: bytes) -> dict:
    """Parses an Instamart (PJTJ Technologies) Discrepancy Note PDF into structured fields.

    The DN renders as one bordered table per page with a repeated Sr/SKU/Reason/... header,
    so column position within a row is stable relative to that header.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        tables = [t for page in pdf.pages for t in page.extract_tables()]
    return _parse_tables(tables)


def _parse_tables(tables: list[list[list[str | None]]]) -> dict:
    all_cells = [c.strip() for table in tables for row in table for c in row if c]

    dn_id = None
    detail_cell = next((c for c in all_cells if "DN No" in c), None)
    if detail_cell:
        m = re.search(r"DN No\s*:-\s*(\S+)", detail_cell)
        dn_id = m.group(1) if m else None

    line_items = []
    header_found = False
    for table in tables:
        for row in table:
            if not header_found:
                if row and any(c and _clean(c) == "SKU Code" for c in row):
                    header_found = True
                continue
            if not row or not row[0] or not row[0].strip().isdigit():
                continue

            sku_cell = (row[1] or "").strip()
            sku_line = sku_cell.split("\n")[0].strip()
            sku_match = _SKU_RE.match(sku_line)
            sku = sku_match.group(1) if sku_match else None

            reason = _clean(row[4] or "")
            remark = _clean(row[5] or "")
            dn_qty = int((row[9] or "0").strip())

            line_items.append({
                "sku": sku,
                "reason": reason,
                "remark": remark,
                "dn_qty": dn_qty,
            })

    return {
        "dn_id": dn_id,
        "line_items": line_items,
    }


def demo() -> None:
    """ponytail self-check: parses the real sample DN shipped in this folder."""
    fixture = os.path.join(os.path.dirname(__file__), "Discrepancy_Note_FC5-DN772809.pdf")
    with open(fixture, "rb") as f:
        data = parse_discrepancy_note(f.read())

    assert data["dn_id"] == "FC5-DN772809"
    assert len(data["line_items"]) == 1

    item = data["line_items"][0]
    assert item["sku"] == "117407"
    assert item["reason"] == "Wrong SKU"
    assert item["remark"] == "sku not add in po"
    assert item["dn_qty"] == 50

    # SKU code without the 3-digit prefix must parse the same way; suffix length varies
    # (seen both 5 and 6 digits in real DNs), so it isn't hardcoded to 6.
    assert _SKU_RE.match("117407").group(1) == "117407"
    assert _SKU_RE.match("102-117407").group(1) == "117407"
    assert _SKU_RE.match("101-64758").group(1) == "64758"

    print("instamart pdf_parser demo OK")


if __name__ == "__main__":
    demo()
