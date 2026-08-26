"""Fetch every unpaid invoice, bucketed by due date into past / this_week / future."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zoho_client import ZOHO_API_BASE, zoho

INVOICE_FIELDS = (
    "invoice_id", "invoice_number", "customer_id", "customer_name",
    "date", "due_date", "total", "balance", "status", "currency_code",
)

BUCKET_NAMES = ("past", "this_week", "future")


def fetch_unpaid_invoices(org_id: str) -> list[dict]:
    invoices = []
    page = 1
    while True:
        resp = zoho._get(f"{ZOHO_API_BASE}/invoices", params={
            "page": page, "per_page": 200, "sort_column": "created_time", "sort_order": "D",
            "usestate": "false", "status": "unpaid", "organization_id": org_id,
        })
        resp.raise_for_status()
        data = resp.json()
        invoices.extend(data.get("invoices", []))
        if not data.get("page_context", {}).get("has_more_page"):
            break
        page += 1
    return [{k: inv.get(k) for k in INVOICE_FIELDS} for inv in invoices]


def bucket_invoices(invoices: list[dict], today: date | None = None) -> dict[str, list[dict]]:
    """past: due_date < today. this_week: today <= due_date <= today+7. future: due_date > today+7.

    Each returned invoice gets `days_overdue`: positive = days past due,
    0 = due today, negative = days remaining until due.
    """
    today = today or date.today()
    week_end = today + timedelta(days=7)
    buckets: dict[str, list[dict]] = {name: [] for name in BUCKET_NAMES}

    for inv in invoices:
        due_raw = inv.get("due_date")
        row = {**inv}
        if not due_raw:
            # ponytail: no due_date treated as past-due (safest default), days_overdue left null
            row["days_overdue"] = None
            buckets["past"].append(row)
            continue

        due = date.fromisoformat(due_raw)
        row["days_overdue"] = (today - due).days

        if due < today:
            buckets["past"].append(row)
        elif due <= week_end:
            buckets["this_week"].append(row)
        else:
            buckets["future"].append(row)

    return buckets


def get_overdue_summary(org_id: str) -> dict:
    invoices = fetch_unpaid_invoices(org_id)
    buckets = bucket_invoices(invoices)
    kpis = {
        name: {"count": len(invs), "amount": sum(inv["balance"] or 0 for inv in invs)}
        for name, invs in buckets.items()
    }

    return {
        "invoices_checked": len(invoices),
        "kpis": kpis,
        "buckets": buckets,
    }


def demo() -> None:
    today = date(2026, 8, 26)
    sample = [
        {"invoice_id": "1", "due_date": "2026-08-20", "balance": 100},  # 6 days past
        {"invoice_id": "2", "due_date": "2026-08-26", "balance": 200},  # due today
        {"invoice_id": "3", "due_date": "2026-09-02", "balance": 300},  # due in 7 days (edge, this_week)
        {"invoice_id": "4", "due_date": "2026-09-03", "balance": 400},  # due in 8 days (future)
        {"invoice_id": "5", "due_date": None, "balance": 50},           # no due date -> past
    ]
    buckets = bucket_invoices(sample, today=today)
    assert [i["invoice_id"] for i in buckets["past"]] == ["1", "5"]
    assert [i["invoice_id"] for i in buckets["this_week"]] == ["2", "3"]
    assert [i["invoice_id"] for i in buckets["future"]] == ["4"]
    assert next(i for i in buckets["past"] if i["invoice_id"] == "1")["days_overdue"] == 6
    assert next(i for i in buckets["this_week"] if i["invoice_id"] == "3")["days_overdue"] == -7
    print("ok")


if __name__ == "__main__":
    demo()
