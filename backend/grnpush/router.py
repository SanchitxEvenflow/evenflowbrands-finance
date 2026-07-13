import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from config import settings
from grn_log import LogEntry, append_entries, read_log
from grnpush import store
from grnpush.mapper import BillGroup, build_bill_payload, group_grns
from grnpush.slack_notify import notify as slack_notify
from grnpush.unicommerce_client import unicommerce
from grnpush.zoho_bill_client import zoho_bill

logger = logging.getLogger("grn_push")

router = APIRouter(prefix="/grn-push", tags=["grn-push"])

# GRNs are only pulled from the virtual warehouse facility.
FACILITY = "EL_VIRTUAL_BLR"

# Bills matching these keywords in the vendor invoice number are internal
# stock movements, not real vendor purchases — never queued, never billed.
_SKIP_KEYWORDS = {"kitting", "dekitting", "return"}

MAX_DETAIL_WORKERS = 5


def _should_skip(bill_number: str) -> bool:
    bill_lower = bill_number.lower()
    return any(kw in bill_lower for kw in _SKIP_KEYWORDS)


def _check_unicommerce_config() -> None:
    if not settings.unicommerce_username or not settings.unicommerce_password:
        raise HTTPException(
            status_code=503,
            detail="UNICOMMERCE_USERNAME / UNICOMMERCE_PASSWORD not configured in .env",
        )


class ReceiptsRequest(BaseModel):
    start: str = Field(..., description="YYYY-MM-DD or DD/MM/YYYY")
    end: str = Field(..., description="YYYY-MM-DD or DD/MM/YYYY")


class FetchDetailsRequest(BaseModel):
    codes: list[str] = Field(..., min_length=1)


class QueuePatchRequest(BaseModel):
    line_items: list[dict] | None = None
    vendor_name: str | None = None
    vendor_gst: str | None = None
    po_code: str | None = None


@router.post("/receipts", summary="Step 1 — list GRN codes created in a date range")
def list_receipts(payload: ReceiptsRequest):
    _check_unicommerce_config()
    codes = unicommerce.get_inflow_receipts_range(payload.start, payload.end, FACILITY)
    return {"facility": FACILITY, "codes": codes}


def _fetch_one(code: str) -> tuple[str, dict | None, str | None]:
    try:
        return code, unicommerce.get_inflow_receipt(code, FACILITY), None
    except Exception as exc:
        logger.exception("Failed fetching GRN detail %s", code)
        return code, None, str(exc)


@router.post("/fetch-details", summary="Step 2 — fetch GRN detail, group by vendor invoice number, merge into queue")
def fetch_details(payload: FetchDetailsRequest):
    _check_unicommerce_config()
    with ThreadPoolExecutor(max_workers=MAX_DETAIL_WORKERS) as executor:
        futures = [executor.submit(_fetch_one, code) for code in payload.codes]
        results = [f.result() for f in as_completed(futures)]

    order = {code: i for i, code in enumerate(payload.codes)}
    results.sort(key=lambda r: order[r[0]])

    receipts = [{"code": code, "detail": detail} for code, detail, err in results if err is None]
    errors = [{"code": code, "error": err} for code, _, err in results if err]

    grns = [(r["detail"], FACILITY) for r in receipts]
    groups = group_grns(grns)

    # Kitting/dekitting/return invoice numbers are internal stock moves, not
    # real vendor bills — never enter the queue at all.
    skipped = [g.bill_number for g in groups if _should_skip(g.bill_number)]
    groups = [g for g in groups if not _should_skip(g.bill_number)]

    queued = [store.upsert_merged(g).bill_number for g in groups]

    # A failed GRN detail fetch means we don't know its vendor invoice number,
    # so we can't tell which queue entry (if any) it belonged to. Flag every
    # entry touched in this same batch rather than silently leaving them
    # looking complete — a bill built from a partial fetch is a financial-data bug.
    if errors and queued:
        failed_codes = ", ".join(e["code"] for e in errors)
        note = f"Fetch batch had {len(errors)} failed GRN(s) — {failed_codes}. Re-run fetch-details for these codes before pushing."
        for bill_number in queued:
            store.patch(bill_number, status="fetch_incomplete", issues=[note])

    return {"facility": FACILITY, "receipts": receipts, "errors": errors, "queued": queued, "skipped": skipped}


@router.get("/queue")
def list_queue():
    """All pending (not yet pushed) GRN groups, keyed by vendor invoice number."""
    return store.load_all()


@router.patch("/queue/{bill_number:path}")
def edit_queue_entry(bill_number: str, payload: QueuePatchRequest):
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=422, detail="No fields to update")
    try:
        return store.patch(bill_number, **fields)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No pending entry for {bill_number}")


@router.delete("/queue/{bill_number:path}")
def discard_queue_entry(bill_number: str):
    store.delete(bill_number)
    return {"deleted": bill_number}


def _log_grn_codes(grn_codes: list[str], bill_number: str, bill_id: str, created_at: str | None = None) -> None:
    created_at = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing_codes = {e.grn_code for e in read_log()}
    new_entries = [
        LogEntry(grn_code=code, bill_number=bill_number, bill_id=bill_id, created_at=created_at)
        for code in grn_codes
        if code not in existing_codes
    ]
    if new_entries:
        append_entries(new_entries)


@router.post("/queue/{bill_number:path}/push")
def push_bill(bill_number: str, invoice_pdf: UploadFile | None = File(None)):
    """Resolve vendor + SKUs against Zoho, create the draft Bill, attach the PDF if given,
    and drop the entry from the queue on success."""
    entry = store.get(bill_number)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No pending entry for {bill_number}")

    if entry.get("status") == "fetch_incomplete":
        raise HTTPException(
            status_code=422,
            detail=f"GRN push blocked for {bill_number}: last fetch had failures, entry may be missing line items — re-run fetch-details first.",
        )

    # Vendor resolution — needed before the duplicate check below so a
    # same-numbered bill belonging to a different vendor isn't mistaken for
    # this one. A low-confidence match still pushes, just gets flagged for review.
    vendor_id, method, score = zoho_bill.find_vendor_id(entry["vendor_code"], entry["vendor_name"], entry["vendor_gst"])
    if vendor_id is None:
        message = f"GRN push blocked for {bill_number}: no Zoho vendor match for '{entry['vendor_name']}'"
        store.patch(bill_number, status="vendor_flagged", issues=[message])
        slack_notify(message)
        raise HTTPException(status_code=422, detail=message)
    if method not in ("gst", "exact_name"):
        slack_notify(
            f"GRN {bill_number}: vendor matched via '{method}' (score {score:.0f}) — "
            f"'{entry['vendor_name']}' → Zoho contact {vendor_id}. Please verify."
        )

    # Zoho-side duplicate check — this bill may already exist from a prior push
    # (e.g. queue was rebuilt after a crash). Scoped to vendor_id so a same-numbered
    # bill from a different vendor doesn't get mistaken for this one. If it does
    # match, just backfill the audit log and clear the queue entry rather than
    # creating a second bill.
    existing = zoho_bill.find_bill(bill_number, vendor_id)
    if existing:
        _log_grn_codes(entry["grn_codes"], bill_number, existing["bill_id"], existing.get("date"))
        store.delete(bill_number)
        return {
            "bill_id": existing["bill_id"],
            "bill_number": bill_number,
            "vendor_match_method": "existing",
            "attachment_error": None,
        }

    # SKU resolution — any miss blocks the push entirely.
    item_meta_map: dict[str, dict | None] = {}
    missing_skus: list[str] = []
    for item in entry["line_items"]:
        sku = item.get("sku", "")
        meta = zoho_bill.find_item_metadata(sku) if sku else None
        if meta is None:
            missing_skus.append(sku or "(blank)")
        else:
            item_meta_map[sku] = meta

    if missing_skus:
        message = f"GRN push blocked for {bill_number}: SKU(s) not found in Zoho — {', '.join(missing_skus)}"
        store.patch(bill_number, status="sku_flagged", issues=[f"SKU not found in Zoho: {s}" for s in missing_skus])
        slack_notify(message)
        raise HTTPException(status_code=422, detail=message)

    group = BillGroup(**{k: v for k, v in entry.items() if k in BillGroup.model_fields})
    is_interstate = zoho_bill.is_interstate_vendor(vendor_id)
    payload = build_bill_payload(group, vendor_id, item_meta_map, is_interstate)

    try:
        bill = zoho_bill.create_draft_bill(payload)
    except Exception as exc:
        logger.exception("Bill creation failed for %s", bill_number)
        raise HTTPException(status_code=502, detail=f"Zoho bill creation failed: {exc}")

    attachment_error = None
    if invoice_pdf is not None:
        content = invoice_pdf.file.read()
        try:
            zoho_bill.upload_bill_attachment(bill["bill_id"], invoice_pdf.filename, content, invoice_pdf.content_type)
        except Exception as exc:
            logger.exception("Attachment upload failed for bill %s (bill still created)", bill.get("bill_id"))
            attachment_error = str(exc)

    _log_grn_codes(group.grn_codes, bill_number, bill["bill_id"])
    store.delete(bill_number)
    return {
        "bill_id": bill.get("bill_id"),
        "bill_number": bill_number,
        "vendor_match_method": method,
        "attachment_error": attachment_error,
    }


@router.post("/bills/{bill_id}/attachment")
def retry_attachment(bill_id: str, invoice_pdf: UploadFile = File(...)):
    """Retry attaching a PDF to a bill that already exists in Zoho — for when
    the push itself succeeded (bill created) but the attachment upload failed,
    so there's no queue entry left to push through again."""
    content = invoice_pdf.file.read()
    try:
        zoho_bill.upload_bill_attachment(bill_id, invoice_pdf.filename, content, invoice_pdf.content_type)
    except Exception as exc:
        logger.exception("Attachment retry failed for bill %s", bill_id)
        raise HTTPException(status_code=502, detail=f"Attachment upload failed: {exc}")
    return {"bill_id": bill_id, "attached": True}


# ── Sync Log ─────────────────────────────────────────────────────────────────

class SyncLogRequest(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None


class SyncLogResponse(BaseModel):
    fetched: int
    added: int


@router.post("/sync-log", response_model=SyncLogResponse, summary="Import existing Zoho bills into the local GRN log")
def sync_log(body: SyncLogRequest) -> SyncLogResponse:
    bills = zoho_bill.list_bills(date_from=body.date_from, date_to=body.date_to)

    existing_codes = {e.grn_code for e in read_log()}
    new_entries: list[LogEntry] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for bill in bills:
        bill_id = bill["bill_id"]
        bill_number = bill["bill_number"]
        cf_grn = bill["cf_grn"]
        if not cf_grn or not bill_id:
            continue
        for grn_code in [g.strip() for g in cf_grn.split(",") if g.strip()]:
            if grn_code not in existing_codes:
                new_entries.append(LogEntry(
                    grn_code=grn_code,
                    bill_number=bill_number,
                    bill_id=bill_id,
                    created_at=bill["date"] or today,
                ))
                existing_codes.add(grn_code)

    if new_entries:
        append_entries(new_entries)
    logger.info("sync-log: fetched=%d added=%d", len(bills), len(new_entries))
    return SyncLogResponse(fetched=len(bills), added=len(new_entries))


# ── Log ──────────────────────────────────────────────────────────────────────

class LogResponse(BaseModel):
    entries: list[LogEntry]


@router.get("/log", response_model=LogResponse, summary="View GRN push history log")
def get_log() -> LogResponse:
    return LogResponse(entries=read_log())
