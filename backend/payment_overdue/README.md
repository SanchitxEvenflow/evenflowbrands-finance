# Payment Overdue

Backs the "Payment Overdue" frontend tab: 3 KPI cards (Past / This Week /
Future), each drilling into a per-invoice list.

## Data source

Zoho Books `GET /invoices?status=unpaid&organization_id=...` — a single
paginated call, no per-customer looping. `status=unpaid` already covers every
invoice with `balance > 0` (Zoho's internal statuses `sent`,
`partially_paid`, `overdue` all fall under it) across all customers.
See `fetcher.fetch_unpaid_invoices`.

## Bucketing

Compared against `date.today()`, using each invoice's `due_date`:

| Bucket      | Condition                          |
|-------------|-------------------------------------|
| `past`      | `due_date < today`                  |
| `this_week` | `today <= due_date <= today + 7d`   |
| `future`    | `due_date > today + 7d`             |

An invoice with no `due_date` (shouldn't happen in practice) is put in
`past` with `days_overdue: null`.

Logic lives in `fetcher.bucket_invoices` — pure function, no network calls,
takes `today` as an optional override for testing.

## `days_overdue` sign convention

Every invoice in every bucket carries one signed field:

- **positive** → days past due (e.g. `6` = 6 days overdue)
- **`0`** → due today
- **negative** → days remaining until due (e.g. `-7` = due in 7 days)

One field, one meaning everywhere — the frontend picks the label
("Overdue by 6 days" / "Due today" / "Due in 7 days") from the sign.

## API

```
GET /payment-overdue/summary?organization_id=<org_id>
```

`organization_id` defaults to `settings.org_id` if omitted.

### Response shape

```json
{
  "invoices_checked": 6276,
  "kpis": {
    "past":      { "count": 4155, "amount": 64032763.95 },
    "this_week": { "count": 13,   "amount": 697879.11 },
    "future":    { "count": 2108, "amount": 147879604.00 }
  },
  "buckets": {
    "past":      [ { ...invoice, "days_overdue": 6 } ],
    "this_week": [ { ...invoice, "days_overdue": -3 } ],
    "future":    [ { ...invoice, "days_overdue": -12 } ]
  }
}
```

Each invoice object (see `fetcher.INVOICE_FIELDS`):

| Field           | Notes                                   |
|-----------------|-------------------------------------------|
| `invoice_id`    | Zoho internal ID                          |
| `invoice_number`| e.g. `EL-KA-IN27-04286`                   |
| `customer_id`   | Zoho contact ID                           |
| `customer_name` |                                            |
| `date`          | invoice date                              |
| `due_date`      | ISO `YYYY-MM-DD`, null-safe               |
| `total`         | invoice total                             |
| `balance`       | outstanding amount (what's actually owed) |
| `status`        | Zoho's own status string                  |
| `currency_code` | e.g. `INR`                                |
| `days_overdue`  | signed, see above                         |

## Frontend wiring

- 3 KPI cards read `kpis.past`, `kpis.this_week`, `kpis.future` for count +
  amount — one `GET` covers all three, no separate calls per card.
- Clicking a card renders `buckets.<name>` as a table: customer, invoice
  number, `days_overdue` (formatted per the sign convention), `balance`.
- Bucket boundaries are fixed at request time (server's `today`) — if the
  tab stays open across midnight, re-fetch to reclassify.
