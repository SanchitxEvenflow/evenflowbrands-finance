import io
import os
import re

import pdfplumber

_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _find_cell(cells: list[str], marker: str) -> str | None:
    return next((c for c in cells if marker in c), None)


def parse_discrepancy_note(pdf_bytes: bytes) -> dict:
    """Parses a Blinkit/PartnersBiz Discrepancy Note PDF (see creditnotepdf.md) into structured fields.

    Uses extract_tables() rather than extract_text() — the DN's two-column header is drawn
    as a bordered table, and reading raw text interleaves the buyer/supplier columns.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        tables = [t for page in pdf.pages for t in page.extract_tables()]
    return _parse_tables(tables)


def _parse_tables(tables: list[list[list[str | None]]]) -> dict:
    """Header/buyer/supplier cells are only on page 1; line items may continue across
    later pages (with or without a repeated column header) — both are handled here since
    `tables` is already the flattened list of every page's tables, in page order."""
    all_cells = [c.strip() for table in tables for row in table for c in row if c]

    warehouse_cell = next(
        (c for c in all_cells
         if "Address:" in c and "Buyers's Detail" not in c and "Supplier's Detail" not in c),
        None,
    )
    buyer_cell = _find_cell(all_cells, "Buyers's Detail")
    supplier_cell = _find_cell(all_cells, "Supplier's Detail")

    warehouse_name = warehouse_address = None
    if warehouse_cell:
        lines = warehouse_cell.split("\n")
        warehouse_name = lines[0].strip()
        addr_lines = []
        capturing = False
        for line in lines[1:]:
            if line.startswith("Email"):
                break
            if line.startswith("Address:"):
                line = line[len("Address:"):]
                capturing = True
            if capturing:
                addr_lines.append(line.strip())
        warehouse_address = _clean(" ".join(addr_lines))

    dn_id = None
    if buyer_cell:
        m = re.search(r"\bId\s*:\s*(\S+)", buyer_cell)
        dn_id = m.group(1) if m else None

    original_invoice_id = None
    if supplier_cell:
        m = re.search(r"Original\s*:\s*(\S+)", supplier_cell)
        original_invoice_id = m.group(1) if m else None

    line_items = []
    header_found = False
    for table in tables:
        for row in table:
            if not header_found:
                if row and any(c and c.strip() == "UPC" for c in row):
                    header_found = True
                continue
            if not row or not row[0] or not row[0].strip().isdigit():
                continue
            _sr_no, upc, name, qty, _unit_price, subtotal, gst_pct, _net_amount, reason, remark = (
                (c or "").strip() for c in row[:10]
            )
            line_items.append({
                "upc": upc,
                "name": _clean(name),
                "qty": int(qty),
                "subtotal_excl_tax": float(subtotal),
                "gst_percent": float(gst_pct),
                "reason": _clean(reason),
                "remark": remark,
            })

    return {
        "dn_id": dn_id,
        "warehouse_name": warehouse_name,
        "warehouse_address": warehouse_address,
        "original_invoice_id": original_invoice_id,
        "line_items": line_items,
    }


def demo() -> None:
    """ponytail self-check: parses the real sample DN shipped in this folder."""
    fixture = os.path.join(os.path.dirname(__file__), "D25497DN26001725.pdf")
    with open(fixture, "rb") as f:
        data = parse_discrepancy_note(f.read())

    assert data["dn_id"] == "D25497DN26001725"
    assert data["original_invoice_id"] == "EL-KA-IN27-00609"
    assert data["warehouse_name"] == "BCPL - Nagpur N1 Feeder Warehouse"
    assert "Kalmeshwar" in data["warehouse_address"]
    assert len(data["line_items"]) == 2

    first = data["line_items"][0]
    assert first["upc"] == "8906157933838"
    assert first["qty"] == 50
    assert first["subtotal_excl_tax"] == 6159.50
    assert first["gst_percent"] == 5.00
    assert first["remark"] == "SHORT"
    assert first["reason"] == "Short quantity"
    assert "Ice Gel" in first["name"]

    # Multi-page check: split the item table so items land on separate "pages", with the
    # column header repeated on the second page (common in generated reports) — must still
    # aggregate both items and keep header/buyer/supplier fields from page 1 only.
    with open(fixture, "rb") as f:
        with pdfplumber.open(io.BytesIO(f.read())) as pdf:
            real_tables = [t for page in pdf.pages for t in page.extract_tables()]
    combined_table = real_tables[0]  # header/buyer/supplier + items all render as one bordered table
    header_idx = next(i for i, row in enumerate(combined_table) if any(c and c.strip() == "UPC" for c in row))
    header_row = combined_table[header_idx]
    item1, item2 = combined_table[header_idx + 1], combined_table[header_idx + 2]
    page1_tables = [combined_table[:header_idx] + [header_row, item1]]
    page2_tables = [[header_row, item2]]

    multi = _parse_tables(page1_tables + page2_tables)
    assert multi["dn_id"] == "D25497DN26001725"
    assert len(multi["line_items"]) == 2
    assert {i["upc"] for i in multi["line_items"]} == {"8906157933838", "8906158357510"}

    print("pdf_parser demo OK")


if __name__ == "__main__":
    demo()
