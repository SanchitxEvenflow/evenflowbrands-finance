from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger("mapper")


class BillGroup(BaseModel):
    bill_number: str        # vendorInvoiceNumber → Zoho Bill#
    po_code: str            # purchaseOrder.code → reference_number / cf_order_number
    grn_codes: list[str]
    facilities: list[str]
    vendor_code: str        # purchaseOrder.vendorCode
    vendor_name: str        # purchaseOrder.vendorName
    vendor_gst: str = ""   # purchaseOrder GST (if Unicommerce provides it)
    date: Optional[str] = None          # GRN created date (epoch ms → YYYY-MM-DD)
    invoice_date: Optional[str] = None  # vendorInvoiceDate
    line_items: list[dict]
    notes: str


def _epoch_ms_to_date(ms) -> str | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _min_date(dates: list[str | None]) -> str | None:
    valid = [d for d in dates if d]
    return min(valid) if valid else None


def _gate_entry(grn: dict) -> str | None:
    for cf in grn.get("customFieldValues") or []:
        if cf.get("fieldName") == "GE" and cf.get("fieldValue"):
            return cf["fieldValue"]
    return None


def group_grns(grns: list[tuple[dict, str]]) -> list[BillGroup]:
    """
    Group GRN detail dicts by vendorInvoiceNumber (= Zoho bill_number).
    Input: list of (inflowReceipt dict, facility_str)
    """
    # key = vendorInvoiceNumber; value = list of (grn, facility)
    groups: dict[str, list[tuple[dict, str]]] = {}
    for grn, facility in grns:
        bill_num = grn.get("vendorInvoiceNumber") or grn.get("code", "UNKNOWN")
        groups.setdefault(bill_num, []).append((grn, facility))

    result: list[BillGroup] = []
    for bill_number, entries in groups.items():
        grn_codes: list[str] = []
        facilities_seen: list[str] = []
        po_code = ""
        vendor_code = ""
        vendor_name = ""
        vendor_gst = ""
        dates: list[str | None] = []
        invoice_dates: list[str | None] = []
        gate_entries: list[str] = []
        merged_items: list[dict] = []

        for grn, facility in entries:
            grn_code: str = grn.get("code", "")
            grn_codes.append(grn_code)

            if facility not in facilities_seen:
                facilities_seen.append(facility)

            po = grn.get("purchaseOrder") or {}
            if not po_code:
                po_code = po.get("code", "")
            if not vendor_code:
                vendor_code = po.get("vendorCode", "")
            if not vendor_name:
                vendor_name = po.get("vendorName", "")
            if not vendor_gst:
                vendor_gst = (
                    po.get("vendorGstin")
                    or po.get("vendorGstIn")
                    or po.get("gstNo")
                    or po.get("gstin")
                    or po.get("gstNumber")
                    or ""
                )

            dates.append(_epoch_ms_to_date(grn.get("created")))
            invoice_dates.append(grn.get("vendorInvoiceDate"))

            ge = _gate_entry(grn)
            if ge and ge not in gate_entries:
                gate_entries.append(ge)

            for item in grn.get("inflowReceiptItems") or []:
                sku: str = item.get("itemSKU") or ""
                name: str = item.get("itemTypeName") or sku
                ean: str = item.get("ean") or ""
                qty = item.get("quantity") or 0
                rate = item.get("unitPrice") or 0

                desc_parts = [
                    f"SKU {sku}" if sku else None,
                    f"EAN {ean}" if ean else None,
                    f"Uniware GRN {grn_code}",
                ]
                description = " | ".join(p for p in desc_parts if p)

                merged_items.append({
                    "name": name,
                    "description": description,
                    "quantity": qty,
                    "rate": rate,
                    "sku": sku,
                })

        date = _min_date(dates)
        invoice_date = next((d for d in invoice_dates if d), None)

        notes_parts = [
            f"Uniware GRN(s) {', '.join(grn_codes)}",
            f"PO {po_code}" if po_code else None,
            f"Vendor {vendor_code}" if vendor_code else None,
        ]
        if gate_entries:
            notes_parts.append(f"Gate Entry {', '.join(gate_entries)}")
        notes = " | ".join(p for p in notes_parts if p)

        result.append(BillGroup(
            bill_number=bill_number,
            po_code=po_code,
            grn_codes=grn_codes,
            facilities=facilities_seen,
            vendor_code=vendor_code,
            vendor_name=vendor_name,
            vendor_gst=vendor_gst,
            date=date,
            invoice_date=invoice_date,
            line_items=merged_items,
            notes=notes,
        ))

    return result


def merge_bill_groups(existing: BillGroup, new: BillGroup) -> BillGroup:
    """
    Merge a newly-pulled GRN group into an existing not-yet-pushed one sharing
    the same bill_number (vendorInvoiceNumber) — handles partial GRNs where a
    vendor invoice arrives across multiple Unicommerce pulls.

    Line items are concatenated, not summed by SKU — each keeps its own
    "Uniware GRN {code}" description so a partial-then-partial invoice stays
    traceable back to the exact GRN it came from.
    """
    grn_codes = existing.grn_codes + [c for c in new.grn_codes if c not in existing.grn_codes]
    facilities = existing.facilities + [f for f in new.facilities if f not in existing.facilities]
    line_items = existing.line_items + new.line_items

    dates = [d for d in (existing.date, new.date) if d]
    date = min(dates) if dates else None
    invoice_date = existing.invoice_date or new.invoice_date

    notes_parts = [
        f"Uniware GRN(s) {', '.join(grn_codes)}",
        f"PO {existing.po_code or new.po_code}" if (existing.po_code or new.po_code) else None,
        f"Vendor {existing.vendor_code or new.vendor_code}" if (existing.vendor_code or new.vendor_code) else None,
    ]
    notes = " | ".join(p for p in notes_parts if p)

    return BillGroup(
        bill_number=existing.bill_number,
        po_code=existing.po_code or new.po_code,
        grn_codes=grn_codes,
        facilities=facilities,
        vendor_code=existing.vendor_code or new.vendor_code,
        vendor_name=existing.vendor_name or new.vendor_name,
        vendor_gst=existing.vendor_gst or new.vendor_gst,
        date=date,
        invoice_date=invoice_date,
        line_items=line_items,
        notes=notes,
    )


def build_bill_payload(
    group: BillGroup,
    vendor_id: str,
    item_meta_map: dict[str, dict | None],
    is_interstate: bool = False,
) -> dict:
    _FALLBACK_ACCOUNT = "727927000216583195"   # Purchase (COGS)
    _FALLBACK_INTRA_TAX = "727927000000014271" # GST18
    _FALLBACK_INTER_TAX = "727927000000014229" # IGST18

    line_items = []
    for item in group.line_items:
        meta = item_meta_map.get(item.get("sku", "")) or {}
        if is_interstate:
            tax_id = meta.get("inter_tax_id") or _FALLBACK_INTER_TAX
        else:
            tax_id = meta.get("intra_tax_id") or _FALLBACK_INTRA_TAX
        item_id = meta.get("item_id")
        if item_id:
            line_items.append({
                "item_id": item_id,
                "tax_id": tax_id,
                "quantity": item["quantity"],
                "rate": item["rate"],
            })
        else:
            line_items.append({
                "account_id": meta.get("account_id") or _FALLBACK_ACCOUNT,
                "tax_id": tax_id,
                "name": item["name"],
                "quantity": item["quantity"],
                "rate": item["rate"],
            })

    custom_fields: list[dict] = [
        {"api_name": "cf_grn", "value": ", ".join(group.grn_codes)},
    ]
    if group.date:
        custom_fields.append({"api_name": "cf_grn_date", "value": group.date})
    if group.invoice_date:
        custom_fields.append({"api_name": "cf_original_invoice_date", "value": group.invoice_date})

    payload: dict = {
        "vendor_id": vendor_id,
        "bill_number": group.bill_number,
        "reference_number": group.po_code,
        "gst_treatment": "business_gst",
        "notes": group.notes,
        "status": "draft",
        "line_items": line_items,
        "custom_fields": custom_fields,
    }
    if group.date:
        payload["date"] = group.date

    return payload
