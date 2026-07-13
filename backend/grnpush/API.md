# GRN Push — Backend API Reference

Base URL: `http://localhost:8000`

---

## Overview — Queue-Review Flow

GRNs are pulled from Unicommerce, grouped by vendor invoice number, and merged into a
persistent **pending-bills queue** (`grnpush/data/pending_bills.json`, keyed by `bill_number`).
A human reviews/edits each queue entry, then pushes it to Zoho Books individually — no
one-click bulk push. On a clean push, the entry is deleted from the queue.

Only one facility is pulled: **`EL_VIRTUAL_BLR`** (virtual warehouse).

```
1. POST /grn-push/receipts        → list GRN codes in a date range
2. POST /grn-push/fetch-details   → fetch + group by invoice number, merge into queue
3. GET  /grn-push/queue           → review pending entries (re-callable any time)
4. PATCH /grn-push/queue/{bill_number}   → edit an entry before pushing
5. POST /grn-push/queue/{bill_number}/push  → push one entry to Zoho (+ optional PDF)
```

Partial GRNs for the same vendor invoice merge automatically: pulling GRNs across
multiple dates for one invoice concatenates line items into the same queue entry rather
than creating duplicate bills.

---

## Step 1 — List GRN Codes

### `POST /grn-push/receipts`

Fetches all GRN (inflow receipt) codes from Unicommerce for a date range, facility
`EL_VIRTUAL_BLR` only.

**Request**
```json
{ "start": "2026-06-01", "end": "2026-06-16" }
```

**Response**
```json
{ "facility": "EL_VIRTUAL_BLR", "codes": ["G3612", "G3613", "G2005"] }
```

Returns `503` if `UNICOMMERCE_USERNAME`/`UNICOMMERCE_PASSWORD` aren't configured.

---

## Step 2 — Fetch Details & Merge Into Queue

### `POST /grn-push/fetch-details`

Fetches full GRN detail per code (parallel, up to 5 workers), groups by vendor invoice
number, and merges each group into the pending-bills queue. Bills whose invoice number
contains `kitting`, `dekitting`, or `return` are internal stock moves — they're filtered
out here and never enter the queue.

**Request**
```json
{ "codes": ["G3612", "G3613"] }
```

**Response**
```json
{
  "facility": "EL_VIRTUAL_BLR",
  "receipts": [{ "code": "G3612", "detail": { "...": "..." } }],
  "errors": [{ "code": "G3614", "error": "timeout" }],
  "queued": ["26-27/142"],
  "skipped": ["dekitting-batch-04"]
}
```

| Field | Description |
|-------|-------------|
| `receipts` | Raw GRN detail fetched successfully |
| `errors` | GRN codes that failed to fetch, with error message |
| `queued` | `bill_number`s upserted into the queue this call |
| `skipped` | `bill_number`s filtered out (kitting/dekitting/return) |

**Partial-fetch safety**: if any GRN in the batch failed (`errors` non-empty) while
other GRNs in the same batch did queue successfully, every touched queue entry is
flagged `status: "fetch_incomplete"` with an explanatory note in `issues`. A
`fetch_incomplete` entry **cannot be pushed** until `fetch-details` is re-run for the
failed codes and the flag clears on the next successful merge.

---

## Queue

### `GET /grn-push/queue`

Returns the full pending-bills queue, keyed by `bill_number`.

```json
{
  "26-27/142": {
    "bill_number": "26-27/142",
    "po_code": "EL/KN/PO/2526/3205",
    "grn_codes": ["G3612"],
    "facilities": ["EL_VIRTUAL_BLR"],
    "vendor_code": "EL_METROBAG",
    "vendor_name": "METRO BAG",
    "vendor_gst": "29ABCDE1234F1Z5",
    "date": "2026-06-16",
    "invoice_date": "2026-06-13",
    "line_items": [
      { "name": "Insulated Lunch Bag", "description": "SKU FRW-LNCH-REC-BAG-BLK", "quantity": 600, "rate": 122.72, "sku": "FRW-LNCH-REC-BAG-BLK" }
    ],
    "notes": "Uniware GRN(s) G3612 | PO EL/KN/PO/2526/3205 | Vendor EL_METROBAG | Gate Entry IGP2003",
    "status": "pending",
    "issues": [],
    "updated_at": "2026-06-16T10:22:00Z"
  }
}
```

### Status values

| Status | Meaning | Can push? |
|--------|---------|-----------|
| `pending` | Normal, ready for review/push | Yes |
| `fetch_incomplete` | Merged from a batch with failed GRN fetches — may be missing items | No — re-run fetch-details first |
| `sku_flagged` | Last push attempt hit a SKU not found in Zoho | No — fix line item SKU(s), then retry push |
| `vendor_flagged` | Last push attempt found no Zoho vendor match at all | No — fix vendor fields, then retry push |

### `PATCH /grn-push/queue/{bill_number}`

Edit `line_items`, `vendor_name`, `vendor_gst`, and/or `po_code` before pushing. Only
fields provided are updated (each field is a full replace, not a deep merge). 404 if no
pending entry exists for that `bill_number`.

**Request**
```json
{ "vendor_gst": "29ABCDE1234F1Z5", "line_items": [ /* corrected array */ ] }
```

### `DELETE /grn-push/queue/{bill_number}`

Discards a queue entry without pushing.

---

## Push

### `POST /grn-push/queue/{bill_number}/push`

`multipart/form-data`, optional field `invoice_pdf` (file).

Resolves vendor + SKUs against Zoho, creates the draft Bill, attaches the PDF if given,
and removes the entry from the queue on success. Steps, in order:

1. **`fetch_incomplete` guard** — blocks with `422` if the entry was flagged in Step 2.
2. **Duplicate check** — queries Zoho for an existing bill with this `bill_number`. If
   found, backfills the GRN audit log against the existing bill and clears the queue
   entry **without creating a new bill** (handles queue rebuilds after a crash).
3. **SKU resolution** — every line item's SKU must resolve to a Zoho item. Any miss
   blocks the push entirely (`422`), flags the entry `sku_flagged`, and sends a Slack
   alert. **This is a hard block — no bill is created.**
4. **Vendor resolution** — GST exact → name exact → substring → word-overlap → fuzzy
   match (`rapidfuzz`, threshold 80). No match at all blocks the push (`422`), flags
   `vendor_flagged`, and Slack-alerts. A match found via substring/word-overlap/fuzzy
   (i.e. not GST or exact name) still pushes, but sends a Slack alert asking for manual
   verification.
5. **Bill creation** — draft bill POSTed to Zoho Books with resolved vendor, items,
   intra/interstate tax.
6. **PDF attachment** — if `invoice_pdf` was uploaded, attaches it to the created bill.
   Attachment failure does **not** roll back the bill — the bill was already created —
   but `attachment_error` is returned non-null so the UI can surface it and offer retry.
7. **Audit log** — GRN codes are appended to the local GRN→bill audit log
   (`GET /grn-push/log`).
8. Queue entry deleted.

**Response**
```json
{
  "bill_id": "727927000219115022",
  "bill_number": "26-27/142",
  "vendor_match_method": "gst",
  "attachment_error": null
}
```

| Field | Description |
|-------|-------------|
| `vendor_match_method` | `"gst"` \| `"exact_name"` \| `"substring"` \| `"word_overlap"` \| `"fuzzy"` \| `"existing"` (duplicate short-circuit) |
| `attachment_error` | Non-null string if the PDF was uploaded but attach failed (bill still created) |

### `POST /grn-push/bills/{bill_id}/attachment`

`multipart/form-data`, required field `invoice_pdf`. Retries attaching a PDF to a bill
that already exists in Zoho — for when the push succeeded but the attachment upload
failed, so there's no queue entry left to push through again.

```json
{ "bill_id": "727927000219115022", "attached": true }
```

Returns `502` with the error message if the retry also fails.

---

## Slack Alerts

Sent (best-effort — logged always, POSTed only if `SLACK_WEBHOOK_URL` is configured)
on:
- SKU not found in Zoho (push blocked)
- No vendor match at all (push blocked)
- Vendor matched via substring/word-overlap/fuzzy (push proceeds, flagged for review)

---

## Sync Log & History

### `POST /grn-push/sync-log`

Imports existing Zoho bills (by their `cf_grn` custom field) into the local GRN audit
log — for backfilling history from bills created outside this tool.

```json
{ "date_from": "2026-01-01", "date_to": "2026-06-16" }
```
```json
{ "fetched": 42, "added": 7 }
```

### `GET /grn-push/log`

Returns the full GRN → bill audit trail (`grn_code`, `bill_number`, `bill_id`,
`created_at`).

---

## Error Handling Summary

| Scenario | HTTP status |
|----------|-------------|
| Unicommerce not configured | 503 |
| Push attempted on `fetch_incomplete` entry | 422 |
| SKU not found in Zoho | 422, entry flagged `sku_flagged` |
| No vendor match | 422, entry flagged `vendor_flagged` |
| No pending entry for `bill_number` | 404 |
| Zoho bill creation fails | 502 |
| Attachment upload/retry fails | 502 (bill itself still exists) |
