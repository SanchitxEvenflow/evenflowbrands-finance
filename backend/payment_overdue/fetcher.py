"""Fetch every unpaid invoice, bucketed by due date into past / this_week / future."""
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zoho_client import MAX_WORKERS, ZOHO_API_BASE, zoho

INVOICE_FIELDS = (
    "invoice_id", "invoice_number", "customer_id", "customer_name",
    "date", "due_date", "total", "balance", "status", "currency_code",
)

BUCKET_NAMES = ("past", "this_week", "future")

# Not a high-traffic dashboard (checked once or twice a morning), so a long TTL
# trades freshness for near-zero Zoho calls on repeat views/refreshes.
_CACHE_TTL_SECS = 30 * 60
_cache: dict[str, tuple[float, dict]] = {}  # org_id -> (fetched_at, summary)


def _fetch_page(org_id: str, page: int) -> dict:
    resp = zoho._get(f"{ZOHO_API_BASE}/invoices", params={
        "page": page, "per_page": 200, "sort_column": "created_time", "sort_order": "D",
        "usestate": "false", "status": "unpaid", "organization_id": org_id,
    })
    resp.raise_for_status()
    return resp.json()


def fetch_unpaid_invoices(org_id: str) -> list[dict]:
    # Total page count isn't known upfront (Zoho only tells us has_more_page),
    # so fetch in batches of MAX_WORKERS pages at a time instead of one page
    # per round-trip — cuts wall time ~5x while staying within Zoho's
    # concurrent-call limit (same worker count zoho_client.py already tunes for).
    first = _fetch_page(org_id, 1)
    invoices = list(first.get("invoices", []))

    page = 2
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        more = first.get("page_context", {}).get("has_more_page")
        while more:
            batch = list(pool.map(lambda p: _fetch_page(org_id, p), range(page, page + MAX_WORKERS)))
            more = False
            for data in batch:
                invoices.extend(data.get("invoices", []))
                if data.get("page_context", {}).get("has_more_page"):
                    more = True
            page += MAX_WORKERS

    return [{k: inv.get(k) for k in INVOICE_FIELDS} for inv in invoices]


def bucket_invoices(invoices: list[dict], today: date | None = None) -> dict[str, list[dict]]:
    """past: due_date <= today. this_week: today+1 <= due_date <= today+7. future: due_date > today+7.

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

        if due <= today:
            buckets["past"].append(row)
        elif due <= week_end:
            buckets["this_week"].append(row)
        else:
            buckets["future"].append(row)

    return buckets


def get_overdue_summary(org_id: str, force_refresh: bool = False) -> dict:
    cached = _cache.get(org_id)
    if not force_refresh and cached and time.time() - cached[0] < _CACHE_TTL_SECS:
        return cached[1]

    invoices = fetch_unpaid_invoices(org_id)
    buckets = bucket_invoices(invoices)
    kpis = {
        name: {"count": len(invs), "amount": sum(inv["balance"] or 0 for inv in invs)}
        for name, invs in buckets.items()
    }

    summary = {
        "invoices_checked": len(invoices),
        "kpis": kpis,
        "buckets": buckets,
    }
    _cache[org_id] = (time.time(), summary)
    return summary


def demo() -> None:
    today = date(2026, 8, 26)
    sample = [
        {"invoice_id": "1", "due_date": "2026-08-20", "balance": 100},  # 6 days past
        {"invoice_id": "2", "due_date": "2026-08-26", "balance": 200},  # due today -> past
        {"invoice_id": "3", "due_date": "2026-08-27", "balance": 250},  # due tomorrow -> this_week
        {"invoice_id": "4", "due_date": "2026-09-02", "balance": 300},  # due in 7 days (edge, this_week)
        {"invoice_id": "5", "due_date": "2026-09-03", "balance": 400},  # due in 8 days (future)
        {"invoice_id": "6", "due_date": None, "balance": 50},           # no due date -> past
    ]
    buckets = bucket_invoices(sample, today=today)
    assert [i["invoice_id"] for i in buckets["past"]] == ["1", "2", "6"]
    assert [i["invoice_id"] for i in buckets["this_week"]] == ["3", "4"]
    assert [i["invoice_id"] for i in buckets["future"]] == ["5"]
    assert next(i for i in buckets["past"] if i["invoice_id"] == "1")["days_overdue"] == 6
    assert next(i for i in buckets["this_week"] if i["invoice_id"] == "4")["days_overdue"] == -7
    print("ok")


if __name__ == "__main__":
    demo()
