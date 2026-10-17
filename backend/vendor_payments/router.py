"""Pay vendors in bulk through Zoho Books' HSBC connected banking, the same calls the Zoho UI makes
on "Initiate Payment": POST /onlinepayments/duplicates, then POST /vendorpayments/online with the Secret PIN.
Each payment really moves money to the vendor's saved bank account ("Transfer To") and is booked as a
vendor advance. Both endpoints are undocumented (read from a browser HAR) and may change without notice."""
import json as jsonlib
import logging
import sys
import threading
from collections import Counter
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from email_sender import send_html  # importable via main.py adding backend/email to sys.path
from zoho_client import ZOHO_API_BASE, zoho

logger = logging.getLogger("vendor_payments")
router = APIRouter(prefix="/vendor-payments", tags=["vendor-payments"])

# Copied from a payment initiated in the Zoho UI (EL-KA-VP-05489); every vendor payment goes out the same way, dated today (IST).
PAID_THROUGH_ACCOUNT_ID = "727927000000014443"  # HSBC Bank Account
OFFSET_ACCOUNT_ID = "727927000000014012"        # "Deposit To": Vendor advances
LOCATION_ID = "727927000000014347"              # Karnataka
DESTINATION_OF_SUPPLY = "KA"
MAIL_FROM = "abdul@evenflowbrands.com"  # summary mail after each batch, via Resend (domain must be verified there)
MAIL_TO = "utsav@evenflowbrands.com"
MAIL_TO_CC = ["kiran.kulagod@evenflowbrands.com", "aveena.ml@evenflowbrands.com"]
RTGS_FROM = 200000                              # Zoho/HSBC: RTGS from ₹2,00,000, NEFT below
# No reference_number sent: Zoho fills in its own HSBC transaction reference when the payment is initiated.

# 5 wrong Secret PINs deactivate Zoho's whole HSBC integration, so we stop well before that.
# ponytail: in-memory, resets on restart; persist it if the server restarts often
MAX_WRONG_PINS = 2
_wrong_pins = 0
_send_lock = threading.Lock()  # one batch at a time, so two tabs can't double-send


class PaymentRow(BaseModel):
    vendor_name: str = Field(min_length=1)
    amount: float = Field(gt=0)
    bank_account_id: str = Field(min_length=1)  # the vendor bank account the user verified in the UI


class BulkPaymentRequest(BaseModel):
    pin: str = Field(pattern=r"^\d{6}$")  # Zoho HSBC Secret PIN: used for this batch only, never stored or logged
    rows: list[PaymentRow] = Field(min_length=1)

    @field_validator("rows")
    @classmethod
    def _no_duplicate_vendors(cls, rows: list[PaymentRow]) -> list[PaymentRow]:
        seen = Counter(r.vendor_name.strip().lower() for r in rows)
        if dups := [n for n, c in seen.items() if c > 1]:
            raise ValueError(f"Same vendor more than once in one batch: {', '.join(dups)}")
        return rows


class VerifyRequest(BaseModel):
    names: list[str] = Field(min_length=1)


class ZohoUnsure(RuntimeError):
    """Zoho gave no clear answer (timeout / 5xx): a POST may or may not have gone through."""


def _zoho_json(method: str, path: str, params: dict | None = None, json: dict | None = None, data: dict | None = None) -> dict:
    try:
        resp = zoho._request(method, f"{ZOHO_API_BASE}{path}", {**(params or {}), "organization_id": settings.org_id}, json=json, data=data)
    except requests.RequestException as exc:
        raise ZohoUnsure(f"No reply from Zoho ({exc.__class__.__name__})") from exc
    if resp.status_code >= 500:
        raise ZohoUnsure(f"Zoho returned HTTP {resp.status_code}")
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"message": resp.text}
    if not resp.ok or body.get("code", 0) != 0:
        raise RuntimeError(body.get("message") or f"HTTP {resp.status_code}")
    return body


def _norm(name: str) -> str:
    return " ".join(name.split()).casefold()


def _vendor(name: str) -> dict:
    # Zoho names often carry stray spaces ("RASHMI FABRICS (P)  LTD."), so an exact contact_name lookup misses them:
    # search by the longest word, then compare names with whitespace collapsed.
    found = _zoho_json("GET", "/contacts", {"contact_name_contains": max(name.split(), key=len),
                                            "contact_type": "vendor", "per_page": 200})["contacts"]
    found = [c for c in found if _norm(c["contact_name"]) == _norm(name)]
    contacts = [c for c in found if c.get("status") == "active"]
    if found and not contacts:
        raise RuntimeError(f"Vendor {name!r} is inactive in Zoho")
    if len(contacts) != 1:
        raise RuntimeError(f"{len(contacts)} vendors named {name!r} in Zoho (need exactly 1)")
    return contacts[0]


def _vendor_banks(vendor_id: str) -> list[dict]:
    # Undocumented in Zoho's API reference; found by reading the vendor's "Transfer To" account in the UI payment.
    return [
        {"account_id": b["account_id"], "bank_name": b["bank_name"], "account_number": b["account_number"],
         "ifsc": b["routing_number"], "beneficiary_name": b["beneficiary_name"], "is_primary": b["is_primary"]}
        for b in _zoho_json("GET", f"/contacts/{vendor_id}/bankaccounts")["bank_accounts"]
        if b["status"] == "active"
    ]


@router.post("/verify", summary="Look up each vendor and its active bank accounts, for the user to check before paying")
def verify(req: VerifyRequest):
    out = []
    for name in dict.fromkeys(n.strip() for n in req.names):
        try:
            out.append({"vendor_name": name, "banks": _vendor_banks(_vendor(name)["contact_id"])})
        except Exception as exc:
            out.append({"vendor_name": name, "banks": [], "error": str(exc)})
    return out


def _recently_paid(bank_account_id: str, amount: float) -> bool:
    """Zoho's own duplicate check: same amount to the same account within its threshold (30 min)."""
    data = _zoho_json("POST", "/onlinepayments/duplicates", data={"JSONString": jsonlib.dumps({
        "debit_account_id": PAID_THROUGH_ACCOUNT_ID, "partner_bank": "hsbc_bank", "entity_type": "vendor_payment",
        "payment_details": [{"credit_account_id": bank_account_id, "amount": str(amount)}],
    })})["data"]
    return any(d["recent_transactions"] for d in data["payment_details"])


def _online_payload(vendor: dict, row: PaymentRow, today: str, pin: str) -> dict:
    amount = str(round(row.amount, 2))
    return {
        "location_id": LOCATION_ID,
        "vendor_id": vendor["contact_id"],
        "gateway_details": {"gateway": "hsbc_bank", "save_account": False,
                            "transfer_type": "rtgs" if row.amount >= RTGS_FROM else "neft"},
        "payment_mode": "Bank Transfer",
        "date": today,
        "amount": amount,
        "total_payment_amount": amount,
        "paid_through_account_id": PAID_THROUGH_ACCOUNT_ID,
        "card_id": row.bank_account_id,  # "Transfer To"
        "custom_fields": [],
        "gst_treatment": vendor.get("gst_treatment", ""),
        "gst_no": vendor.get("gst_no", ""),
        "source_of_supply": vendor.get("place_of_contact", ""),
        "destination_of_supply": DESTINATION_OF_SUPPLY,
        "is_advance_payment": True,
        "tds_tax_id": "",
        "is_tds_amount_in_percent": True,
        "tds_override_preference": "no_override",
        "offset_account_id": OFFSET_ACCOUNT_ID,
        "secret_pin": pin,
        "tags": [],
    }


def _mail_summary(results: list[dict]) -> None:
    """Mail the initiated payments, last CSV row first. A mail failure must never fail the batch: money already moved."""
    ok = [r for r in reversed(results) if r["status"] == "ok"]
    if not ok:
        return
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y")
    cells = "".join(
        f"<tr><td>{escape(r['vendor_name'])}</td><td>{escape(r.get('payment_number') or '')}</td>"
        f"<td>{escape(r.get('reference_number') or '')}</td><td style='text-align:right'>₹{r['amount']:,.2f}</td></tr>"
        for r in ok)
    html = ("<table border='1' cellpadding='6' style='border-collapse:collapse'>"
            "<tr><th>Vendor Name</th><th>Payment Number</th><th>UTR Number</th><th>Amount</th></tr>"
            f"{cells}</table>")
    try:
        send_html(MAIL_FROM, MAIL_TO, f"Overview of Vendor Payment Initiated on ({today})", html, cc=MAIL_TO_CC)
    except Exception:
        logger.exception("Vendor payment summary mail failed")


@router.post("/bulk", summary="Pay one vendor per row via HSBC (real money, Secret PIN once per batch)")
def bulk(req: BulkPaymentRequest):
    global _wrong_pins
    if _wrong_pins >= MAX_WRONG_PINS:
        raise HTTPException(status_code=423, detail="Sending locked after wrong Secret PINs (Zoho deactivates HSBC at 5). Check the PIN, then restart the backend.")
    if not _send_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another batch is being sent right now")
    try:
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        results, stop = [], None
        # Sequential on purpose: each row's outcome is known before the next is sent, and the batch halts at the first rejection.
        for row in req.rows:
            if stop:
                results.append({**row.model_dump(), "status": "failed", "error": f"Not sent: batch stopped ({stop})"})
                continue
            try:
                vendor = _vendor(row.vendor_name.strip())
                # Re-check server side: never send money to an account that isn't this vendor's active account.
                if row.bank_account_id not in {b["account_id"] for b in _vendor_banks(vendor["contact_id"])}:
                    raise RuntimeError("Bank account is not an active account of this vendor — verify again")
                if _recently_paid(row.bank_account_id, row.amount):
                    raise RuntimeError("Same amount already sent to this account in the last 30 min — check Zoho")
                payload = _online_payload(vendor, row, today, req.pin)
            except Exception as exc:  # nothing posted yet: safe to fix and retry
                logger.error("Payment not sent for %s: %s", row.vendor_name, exc)
                results.append({**row.model_dump(), "status": "failed", "error": str(exc)})
                continue
            try:
                body = _zoho_json("POST", "/vendorpayments/online", data={"JSONString": jsonlib.dumps(payload)})
                payment = body["vendorpayment"]
            except ZohoUnsure as exc:
                logger.error("Payment UNKNOWN for %s (check Zoho before retrying): %s", row.vendor_name, exc)
                results.append({**row.model_dump(), "status": "unknown", "error": str(exc)})
                stop = "Zoho stopped answering"
                continue
            except Exception as exc:  # Zoho rejected it: nothing initiated
                msg = str(exc)
                if "pin" in msg.lower():
                    _wrong_pins += 1
                logger.error("Payment rejected for %s: %s", row.vendor_name, msg)
                results.append({**row.model_dump(), "status": "failed", "error": msg})
                stop = f"{row.vendor_name} was rejected"
                continue
            _wrong_pins = 0
            logger.info("Payment %s initiated to %s: %s (%s)", payment.get("payment_number"), row.vendor_name, row.amount, body.get("message"))
            results.append({**row.model_dump(), "status": "ok", "payment_number": payment.get("payment_number"),
                            "reference_number": payment.get("reference_number")})
        _mail_summary(results)
        return {"results": results}
    finally:
        _send_lock.release()
