# Instamart Credit Note API

Base: `/instamart`. No login/OTP step — Gmail access is a server-side refresh token
(`SWIGGY_REFRESH_TOKEN` in `.env`), never exposed to the frontend. Single call: `process-po`.

## POST /instamart/process-po

Full flow: PO number → Zoho invoice lookup → Gmail search (subject `GRN & Purchase Return` +
PO number) → discrepancy note PDF → parse → push draft credit note(s) to Zoho. One PO can
yield two credit notes (shortage bucket + other-reasons bucket).

```
→ { "po_number": "KOCPO115501" }
← 200 {
    "po_number": "KOCPO115501",
    "invoice_number": "EL-KA-IN27-05151",
    "dn_id": "FC5-DN772809",
    "credit_notes": [
      { "creditnote_id": "727927000223999057", "creditnote_number": "EL-KA-CN2700478" }
    ]
  }
← 404 no Zoho invoice found for that PO, or no matching mail / no Discrepancy_Note*
    attachment on the matching mail
← 422 discrepancy note parsed with no line items, or a SKU on the note didn't match
    the invoice
← 502 Gmail or Zoho request failed (network/upstream error)
```

## Frontend flow

```
POST /instamart/process-po { po_number }
  → show credit_notes[] (id + number) to user
  → 404/422 detail is a plain string, show as an error toast
```
