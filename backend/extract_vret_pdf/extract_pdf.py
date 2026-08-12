#!/usr/bin/env python3
"""Extract line-item data from Clicktech_VRET PDFs in a zip into a CSV
matching the column layout of extract_VRET.csv."""
import argparse
import csv
import io
import re
import zipfile

import pdfplumber

HEADERS = [
    "heading", "billing_name", "billing_address", "state_code", "gstin",
    "invoice_number", "invoice_date", "item_description", "hsn_sac",
    "asin_code", "po_no", "vendor_invoice_no", "vendor_invoice_date",
    "qty", "price_per_unit", "tax_rate", "tax_type", "source_file",
]

GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z][A-Z0-9]\b")
STATE_RE = re.compile(r"State Code:\s*([A-Z]{2})\b")
INVOICE_NO_RE = re.compile(
    r"(?:Invoice|Debit Note|Credit Note)\s+(?:Number|No)\.?\s*:\s*(\S+)"
)
INVOICE_DATE_RE = re.compile(
    r"(?:Invoice Date|Debit Note Date|Credit Note Date)\s*:\s*(\d{1,2}-[A-Za-z]{3}-\d{4})"
)
PO_PATTERN = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]+-\d+$")
SUMMARY_ROW_RE = re.compile(
    r"^(sub\s*total|total\b|currency|grand\s*total"
    r"|tax amount in|invoice amount in|amount in wor)", re.I)
PAGE_MARKER_RE = re.compile(r"^Page\s+\d+\s+of\s+\d+$", re.I)

# Checked in order per header cell; first match wins, so specific keys
# (e.g. "gstrate") must precede generic ones (e.g. "rate") that would
# otherwise steal the same cell.
HEADER_FIELD_MAP = [
    ("itemdescription", "item_description"),
    ("hsn", "hsn_sac"),
    ("asincode", "asin_code"),
    ("purchaseorder", "po_no"),
    ("pono", "po_no"),
    ("vendorinvoicedate", "vendor_invoice_date"),
    ("vendorinvoiceno", "vendor_invoice_no"),
    ("quantity", "qty"),
    ("qty", "qty"),
    ("priceperunit", "price_per_unit"),
    ("taxrate", "tax_rate"),
    ("gstrate", "tax_rate"),
    ("taxtype", "tax_type"),
    ("rate", "price_per_unit"),
]


def norm_header(cell):
    return re.sub(r"\s+", "", cell or "").lower()


def build_col_map(header_row):
    col_map = {}
    for idx, cell in enumerate(header_row):
        n = norm_header(cell)
        for key, field in HEADER_FIELD_MAP:
            if key in n:
                col_map[field] = idx
                break
    return col_map


def join_nosep(cell):
    return (cell or "").replace("\n", "").strip()


def join_space(cell):
    return re.sub(r"\s+", " ", (cell or "").replace("\n", " ")).strip()


def tax_fields(rate_cell, type_cell):
    type_lines = [l.strip() for l in (type_cell or "").split("\n") if l.strip()]
    rate_lines = [l.strip() for l in (rate_cell or "").split("\n") if l.strip()]
    n = len(type_lines) or 1
    if len(rate_lines) == n:
        rate_val = "+".join(rate_lines)
    else:
        combined = "".join(rate_lines)
        rate_val = "+".join([combined] * n)
    return rate_val, "+".join(type_lines)


def extract_header_block(page_text_layout):
    lines = page_text_layout.split("\n")
    heading = next(
        (l.strip() for l in lines if l.strip() and not PAGE_MARKER_RE.match(l.strip())),
        "",
    )

    bill_idx = next(
        (i for i, l in enumerate(lines) if "Receiver Billing Address" in l), None
    )
    result = {
        "heading": heading, "billing_name": "", "billing_address": "",
        "state_code": "", "gstin": "",
    }
    if bill_idx is not None:
        threshold = lines[bill_idx].index("Receiver")
        end_idx = next(
            (i for i in range(bill_idx + 1, len(lines)) if "Shipping Address" in lines[i]),
            len(lines),
        )
        left_lines = [l[:threshold].strip() for l in lines[bill_idx + 1:end_idx]]
        left_lines = [l for l in left_lines if l]
        if left_lines:
            result["billing_name"] = re.sub(r"\s*\([^)]*\)\s*$", "", left_lines[0])
        block = " ".join(left_lines)
        gstin_m = GSTIN_RE.search(block)
        if gstin_m:
            result["gstin"] = gstin_m.group(0)
        state_m = STATE_RE.search(block)
        if state_m:
            result["state_code"] = state_m.group(1)
        addr_lines = [
            l for l in left_lines[1:]
            if "State Code:" not in l and "GSTIN" not in l and "PAN No" not in l
            and not re.fullmatch(r"[A-Z0-9]{15}", l)
        ]
        result["billing_address"] = ", ".join(addr_lines)
    return result


def table_column_edges(page):
    """X-coordinates of the line-item table's column dividers on this page,
    used to force the same column layout on every page of the PDF. Without
    this, a table split across a page break can pick up a phantom divider on
    the continuation page (pdfplumber re-detects lines per page), shifting
    every column after it by one and corrupting the merge in build_row."""
    tables = page.find_tables()
    if not tables:
        return None
    edges = set()
    for row in tables[0].rows:
        for cell in row.cells:
            if cell:
                edges.add(cell[0])
                edges.add(cell[2])
    return sorted(edges) if edges else None


def merge_continuation(pending, cells, col_map):
    desc_idx = col_map.get("item_description")
    merged = []
    for idx, (a, b) in enumerate(zip(pending, cells)):
        a, b = a or "", b or ""
        if idx == desc_idx and a and b:
            merged.append(a + "\n" + b)
        else:
            merged.append(a + b)
    return merged


def build_row(cells, col_map, header_info, source_file):
    def get(field):
        idx = col_map.get(field)
        return cells[idx] if idx is not None and idx < len(cells) else ""

    # A genuine item row has a description plus at least one other
    # identifying field; summary/footer rows (subtotal, totals, signature
    # block) carry only stray text and are skipped.
    desc = join_space(get("item_description"))
    if not desc or SUMMARY_ROW_RE.match(desc):
        return None
    if not any(join_nosep(get(f)) for f in
               ("hsn_sac", "asin_code", "po_no", "vendor_invoice_no")):
        return None

    po_no = join_nosep(get("po_no"))
    vendor_invoice_no = join_nosep(get("vendor_invoice_no"))
    if not PO_PATTERN.match(po_no) and PO_PATTERN.match(vendor_invoice_no):
        po_no, vendor_invoice_no = vendor_invoice_no, po_no
    tax_rate, tax_type = tax_fields(get("tax_rate"), get("tax_type"))

    return {
        **header_info,
        "item_description": desc,
        "hsn_sac": join_nosep(get("hsn_sac")),
        "asin_code": join_nosep(get("asin_code")),
        "po_no": po_no,
        "vendor_invoice_no": vendor_invoice_no,
        "vendor_invoice_date": join_nosep(get("vendor_invoice_date")),
        "qty": join_nosep(get("qty")),
        "price_per_unit": join_nosep(get("price_per_unit")),
        "tax_rate": tax_rate,
        "tax_type": tax_type,
        "source_file": source_file,
    }


def extract_pdf_rows(pdf_bytes, source_file):
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page0_text = pdf.pages[0].extract_text() or ""
        page0_layout = pdf.pages[0].extract_text(layout=True) or ""
        header_info = extract_header_block(page0_layout)
        invoice_number_m = INVOICE_NO_RE.search(page0_text)
        invoice_date_m = INVOICE_DATE_RE.search(page0_text)
        header_info["invoice_number"] = invoice_number_m.group(1) if invoice_number_m else ""
        header_info["invoice_date"] = invoice_date_m.group(1) if invoice_date_m else ""

        edges = table_column_edges(pdf.pages[0])
        table_settings = {
            "vertical_strategy": "explicit",
            "explicit_vertical_lines": edges,
            "horizontal_strategy": "lines",
        } if edges else None

        col_map = {}
        pending = None  # raw cells of a row that may continue on the next page

        def flush():
            nonlocal pending
            if pending is not None:
                row = build_row(pending, col_map, header_info, source_file)
                if row:
                    rows.append(row)
                pending = None

        for page in pdf.pages:
            tables = (page.extract_tables(table_settings=table_settings)
                      if table_settings else page.extract_tables())
            for table in tables:
                if not table:
                    continue
                start = 0
                maybe_map = build_col_map(table[0])
                if "item_description" in maybe_map:
                    col_map = maybe_map
                    start = 1
                if not col_map:
                    continue

                for i, cells in enumerate(table[start:], start=start):
                    if not cells:
                        continue
                    if join_nosep(cells[0]):
                        flush()
                        pending = list(cells)
                    elif i == start and pending is not None:
                        # First row of this page's table with a blank leading
                        # cell: the tail end of a row cut off by the page
                        # break above. Glue it onto the row still pending.
                        pending = merge_continuation(pending, cells, col_map)
                    else:
                        flush()
                        pending = list(cells)
        flush()
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("zip_path")
    ap.add_argument("-o", "--output", default="extract_output.csv")
    args = ap.parse_args()

    with zipfile.ZipFile(args.zip_path) as zf, open(args.output, "w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=HEADERS)
        writer.writeheader()
        names = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
        errors = []
        for name in names:
            source_file = name.rsplit("/", 1)[-1]
            try:
                rows = extract_pdf_rows(zf.read(name), source_file)
                for row in rows:
                    writer.writerow(row)
            except Exception as e:
                errors.append((name, str(e)))
        print(f"Processed {len(names)} PDFs, {len(errors)} errors.")
        for name, err in errors:
            print(f"  FAILED: {name}: {err}")


if __name__ == "__main__":
    main()
