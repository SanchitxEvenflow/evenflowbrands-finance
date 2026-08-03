import logging

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from creditnote.blinkit.blinkit_client import BlinkitAuthExpired, blinkit_client
from creditnote.blinkit.pdf_parser import parse_discrepancy_note
from creditnote.blinkit.zoho_client import blinkit_zoho

logger = logging.getLogger("blinkit_router")

router = APIRouter(prefix="/blinkit", tags=["blinkit"])


class VerifyOtpRequest(BaseModel):
    otp: str


class ProcessPoRequest(BaseModel):
    po_number: str


class ApplyCreditNoteRequest(BaseModel):
    creditnote_id: str
    invoice_id: str
    amount: float | None = None


@router.post("/send-otp", summary="Step 1 — email an OTP for the Blinkit login")
def send_otp():
    return blinkit_client.send_otp()


@router.post("/verify-otp", summary="Step 1 — exchange the emailed OTP for a session")
def verify_otp(payload: VerifyOtpRequest):
    try:
        blinkit_client.verify_otp(payload.otp)
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    return {"logged_in": True}


@router.get("/status")
def status():
    return {"logged_in": blinkit_client.is_authenticated}


@router.post("/process-po", summary="Step 2 — PO number in, draft Zoho credit note out")
def process_po(payload: ProcessPoRequest):
    if not blinkit_client.is_authenticated:
        raise HTTPException(status_code=401, detail="Not logged in to Blinkit — call /blinkit/send-otp then /blinkit/verify-otp first")

    try:
        invoices = blinkit_zoho.find_invoices_by_po(payload.po_number)
        if not invoices:
            raise HTTPException(status_code=404, detail=f"No Zoho invoice found for PO {payload.po_number!r}")
        invoice = blinkit_zoho.get_invoice(invoices[0]["invoice_id"])

        try:
            po_id = blinkit_client.find_po_id(payload.po_number)
        except BlinkitAuthExpired as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        if not po_id:
            raise HTTPException(status_code=404, detail=f"No Blinkit PO found for {payload.po_number!r}")

        try:
            pdf_bytes = blinkit_client.download_discrepancy_note_pdf(po_id, invoice["invoice_number"])
        except BlinkitAuthExpired as exc:
            raise HTTPException(status_code=401, detail=str(exc))

        dn = parse_discrepancy_note(pdf_bytes)
        if not dn["line_items"]:
            raise HTTPException(status_code=422, detail=f"Discrepancy note {dn['dn_id']} parsed with no line items")

        creditnotes = blinkit_zoho.create_credit_note(dn, invoice, pdf_bytes=pdf_bytes, pdf_filename=f"{dn['dn_id']}.pdf")
    except HTTPException:
        raise
    except requests.RequestException as exc:
        logger.exception("Zoho/Blinkit request failed for PO %s", payload.po_number)
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}")
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "po_number": payload.po_number,
        "invoice_id": invoice["invoice_id"],
        "invoice_number": invoice["invoice_number"],
        "dn_id": dn["dn_id"],
        "credit_notes": [
            {"creditnote_id": cn["creditnote_id"], "creditnote_number": cn["creditnote_number"]}
            for cn in creditnotes
        ],
    }


@router.post("/apply-credit-note", summary="Apply a pushed credit note's balance to its invoice")
def apply_credit_note(payload: ApplyCreditNoteRequest):
    try:
        result = blinkit_zoho.apply_credit_note_to_invoice(payload.creditnote_id, payload.invoice_id, payload.amount)
    except requests.RequestException as exc:
        logger.exception("Zoho request failed applying credit note %s", payload.creditnote_id)
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}")
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result
