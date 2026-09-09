#!/usr/bin/env python3
"""Extract line-item data from ContraCoGS invoice PDFs in a zip into a CSV."""
import argparse
import csv
import io
import re
import zipfile

import pdfplumber

# Generic table/page helpers are identical to the VRET extractor.
from extract_vret_pdf.extract_pdf import (
    GSTIN_RE,
    PAGE_MARKER_RE,
    STATE_RE,
    SUMMARY_ROW_RE,
    join_nosep,
    join_space,
    merge_continuation,
    norm_header,
    table_column_edges,
)

HEADERS = [
    "type", "bill_number", "source_of_supply", "gstin",
    "rate", "tax_percentage", "source_file",
]

# "Invoice Reference Number : <hash>" must not match, hence the explicit
# Invoice-then-Number adjacency. The colon is optional because the flattened
# text layer occasionally drops it.
BILL_NO_RE = re.compile(r"Invoice\s+Number\s*:?\s*(\S+)")
NUMERIC_RE = re.compile(r"^-?[\d,]+(\.\d+)?$")

# Checked in order per header cell; first match wins, so "gstrate" must
# precede "rate" or it would steal the GST Rate (%) column.
HEADER_FIELD_MAP = [
    ("itemdescription", "item_description"),
    ("gstrate", "tax_percentage"),
    ("taxrate", "tax_percentage"),
    ("assessablevalue", "assessable_value"),
    ("totalamount", "total_amount"),
    ("gstvalue", "gst_value"),
    ("rate", "rate"),
]


def build_col_map(header_row):
    col_map = {}
    for idx, cell in enumerate(header_row):
        n = norm_header(cell)
        for key, field in HEADER_FIELD_MAP:
            if key in n:
                col_map[field] = idx
                break
    return col_map


def extract_header_block(page_text_layout):
    """Pull type/state/GSTIN off page 1. The seller's Billing Address and the
    Customer Billing Address sit side by side, so the left block is sliced by
    the column where "Customer" starts — otherwise the customer's State Code
    and GSTID would win the regex search."""
    lines = page_text_layout.split("\n")
    heading = next(
        (l.strip() for l in lines if l.strip() and not PAGE_MARKER_RE.match(l.strip())),
        "",
    )
    result = {"type": heading, "source_of_supply": "", "gstin": ""}

    bill_idx = next(
        (i for i, l in enumerate(lines)
         if "Billing Address" in l and "Customer Billing Address" in l),
        None,
    )
    if bill_idx is not None:
        threshold = lines[bill_idx].index("Customer")
        end_idx = next(
            (i for i in range(bill_idx + 1, len(lines))
             if "Invoice Reference Number" in lines[i] or "Agreement No" in lines[i]),
            len(lines),
        )
        block = " ".join(
            l[:threshold].strip() for l in lines[bill_idx + 1:end_idx]
        )
        gstin_m = GSTIN_RE.search(block)
        if gstin_m:
            result["gstin"] = gstin_m.group(0)
        state_m = STATE_RE.search(block)
        if state_m:
            result["source_of_supply"] = state_m.group(1)
    return result


def build_row(cells, col_map, header_info, source_file):
    def get(field):
        idx = col_map.get(field)
        return cells[idx] if idx is not None and idx < len(cells) else ""

    # A genuine item row carries a description and a numeric rate; summary
    # rows (Sub Total, Total Invoice Value, Currency) do not.
    desc = join_space(get("item_description"))
    rate = join_nosep(get("rate"))
    if not desc or SUMMARY_ROW_RE.match(desc):
        return None
    if not NUMERIC_RE.match(rate):
        return None

    return {
        **header_info,
        "rate": rate,
        "tax_percentage": join_nosep(get("tax_percentage")),
        "source_file": source_file,
    }


def extract_pdf_rows(pdf_bytes, source_file):
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page0_layout = pdf.pages[0].extract_text(layout=True) or ""
        header_info = extract_header_block(page0_layout)
        bill_no_m = BILL_NO_RE.search(page0_layout)
        header_info["bill_number"] = bill_no_m.group(1) if bill_no_m else ""

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
                        # Tail of a row cut off by the page break above.
                        pending = merge_continuation(pending, cells, col_map)
                    else:
                        flush()
                        pending = list(cells)
        flush()
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("zip_path")
    ap.add_argument("-o", "--output", default="extract_contracogs.csv")
    args = ap.parse_args()

    with zipfile.ZipFile(args.zip_path) as zf, open(args.output, "w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=HEADERS)
        writer.writeheader()
        names = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
        errors = []
        for name in names:
            source_file = name.rsplit("/", 1)[-1]
            try:
                for row in extract_pdf_rows(zf.read(name), source_file):
                    writer.writerow(row)
            except Exception as e:
                errors.append((name, str(e)))
        print(f"Processed {len(names)} PDFs, {len(errors)} errors.")
        for name, err in errors:
            print(f"  FAILED: {name}: {err}")


if __name__ == "__main__":
    main()
