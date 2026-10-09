import logging
from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from netting.zoho_netting_client import zoho_netting

logger = logging.getLogger("netting")
router = APIRouter(prefix="/netting", tags=["netting"])


class InvoiceApply(BaseModel):
    invoice_id: str
    amount_applied: float = Field(gt=0)


class BillApply(BaseModel):
    bill_id: str
    amount_applied: float = Field(gt=0)


class CreateNettingRequest(BaseModel):
    customer_id: str
    currency_id: str
    location_id: str
    net_adjustment_date: date
    invoices: list[InvoiceApply] = Field(min_length=1)
    bills: list[BillApply] = Field(min_length=1)
    autonumbergenerationgroup_id: str | None = None
    net_adjustment_number: str | None = None
    description: str = ""


def _zoho(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("", summary="List netting entries")
def list_nettings(page: int = 1, per_page: int = 100):
    return _zoho(zoho_netting.list_nettings, page, per_page)


@router.get("/setup/{customer_id}", summary="Linked vendor, AR/AP outstanding, numbering groups")
def setup(customer_id: str):
    return _zoho(zoho_netting.get_editpage, customer_id)


@router.get("/open-items/{customer_id}", summary="Unpaid invoices + unpaid bills of the linked vendor")
def open_items(customer_id: str, date_start: str | None = None, date_end: str | None = None,
               date_less_equals: str | None = None):
    setup = _zoho(zoho_netting.get_editpage, customer_id)
    vendor = setup.get("vendor") or {}
    if not vendor.get("vendor_id"):
        raise HTTPException(status_code=400, detail="Customer has no linked vendor in Zoho")
    currency_id = setup["customer"]["currency_id"]
    dates = {"date_start": date_start, "date_end": date_end,
             "date_less_equals": date_less_equals or (None if date_start or date_end else date.today().isoformat())}
    return {
        "customer": setup["customer"],
        "vendor": vendor,
        "nettable_amount": setup.get("nettable_amount"),
        "invoices": _zoho(zoho_netting.unpaid_invoices, customer_id, currency_id, **dates),
        "bills": _zoho(zoho_netting.unpaid_bills, vendor["vendor_id"], currency_id, **dates),
    }


@router.get("/{net_adjustment_id}", summary="Netting detail with applied invoices/bills")
def get_netting(net_adjustment_id: str):
    return _zoho(zoho_netting.get_netting, net_adjustment_id)


@router.get("/{net_adjustment_id}/journals", summary="Accounting journal for a netting entry")
def get_journals(net_adjustment_id: str):
    return _zoho(zoho_netting.get_journals, net_adjustment_id)


@router.post("", summary="Create netting: offset invoices against bills")
def create_netting(req: CreateNettingRequest):
    inv_total = round(sum(i.amount_applied for i in req.invoices), 2)
    bill_total = round(sum(b.amount_applied for b in req.bills), 2)
    if inv_total != bill_total:
        raise HTTPException(status_code=400, detail=f"Invoice total {inv_total} != bill total {bill_total}")
    if not (req.autonumbergenerationgroup_id or req.net_adjustment_number):
        raise HTTPException(status_code=400, detail="Send autonumbergenerationgroup_id or net_adjustment_number")

    apply_date = req.net_adjustment_date.isoformat()
    payload = {
        "description": req.description,
        "customer_id": req.customer_id,
        "currency_id": req.currency_id,
        "location_id": req.location_id,
        "net_adjustment_date": apply_date,
        "net_amount": inv_total,
        "invoices": [{**i.model_dump(), "apply_date": apply_date} for i in req.invoices],
        "bills": [{**b.model_dump(), "apply_date": apply_date} for b in req.bills],
    }
    for key in ("autonumbergenerationgroup_id", "net_adjustment_number"):
        if getattr(req, key):
            payload[key] = getattr(req, key)
    # Balance limits (amount_applied <= balance) are enforced by Zoho; its error is passed through as 502.
    return _zoho(zoho_netting.create_netting, payload)


@router.delete("/{net_adjustment_id}", summary="Delete netting entry (restores invoice/bill balances)")
def delete_netting(net_adjustment_id: str):
    return {"message": _zoho(zoho_netting.delete_netting, net_adjustment_id)}
