import logging

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from creditnote.instamart.gmail_client import swiggy_gmail
from creditnote.instamart.pdf_parser import parse_discrepancy_note
from creditnote.instamart.zoho_client import instamart_zoho

logger = logging.getLogger("instamart_router")

router = APIRouter(prefix="/instamart", tags=["instamart"])


class ProcessPoRequest(BaseModel):
    po_number: str


class ApplyCreditNoteRequest(BaseModel):
    creditnote_id: str
    invoice_id: str
    amount: float | None = None


@router.post("/process-po", summary="PO number in, draft Zoho credit note out")
def process_po(payload: ProcessPoRequest):
    try:
        invoices = instamart_zoho.find_invoices_by_po(payload.po_number)
        if not invoices:
            raise HTTPException(status_code=404, detail=f"No Zoho invoice found for PO {payload.po_number!r}")
        invoice = instamart_zoho.get_invoice(invoices[0]["invoice_id"])

        try:
            pdf_bytes, pdf_filename = swiggy_gmail.find_discrepancy_note_pdf(payload.po_number)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        dn = parse_discrepancy_note(pdf_bytes)
        if not dn["line_items"]:
            raise HTTPException(status_code=422, detail=f"Discrepancy note {dn['dn_id']} parsed with no line items")

        creditnotes = instamart_zoho.create_credit_note(dn, invoice, pdf_bytes=pdf_bytes, pdf_filename=pdf_filename)
    except HTTPException:
        raise
    except requests.RequestException as exc:
        logger.exception("Zoho/Gmail request failed for PO %s", payload.po_number)
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
        result = instamart_zoho.apply_credit_note_to_invoice(payload.creditnote_id, payload.invoice_id, payload.amount)
    except requests.RequestException as exc:
        logger.exception("Zoho request failed applying credit note %s", payload.creditnote_id)
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}")
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result
