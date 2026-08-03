# Blinkit Credit Note API

Base: `/blinkit`. Call in order: `status` → (`send-otp` + `verify-otp` if not logged in) → `process-po`.

## GET /blinkit/status

Check login state before deciding whether to run the OTP flow.

```
→ 200 { "logged_in": boolean }
```

## POST /blinkit/send-otp

Emails an OTP to the configured Blinkit account. No body.

```
→ 200 { "success": true, "message": "...", ... }  // raw Blinkit response
```

## POST /blinkit/verify-otp

```
→ { "otp": "123456" }
← 200 { "logged_in": true }
← 401 { "detail": "..." }   // wrong/expired OTP
```

On success, access + refresh token cached to `tokens.json` on disk — survives server restart, so `send-otp`/`verify-otp` only needed again after the token actually expires (no refresh endpoint; a 401 on `process-po` means re-login).

## POST /blinkit/process-po

Full flow: Blinkit PO → discrepancy note PDF → parse → push draft credit note(s) to Zoho. One PO can yield two credit notes (shortage bucket + other-reasons bucket).

```
→ { "po_number": "1723710043291" }
← 200 {
    "po_number": "1723710043291",
    "invoice_number": "EL-KA-IN27-04320",
    "dn_id": "D17237DN26013372",
    "credit_notes": [
      { "creditnote_id": "727927000223960002", "creditnote_number": "EL-KA-CN2700476" }
    ]
  }
← 401 not logged in, or Blinkit rejected the cached token — cleared server-side on
    detection, so a subsequent GET /blinkit/status immediately reflects logged_in=false.
    Send user back through send-otp/verify-otp.
← 404 no Zoho invoice or no Blinkit PO found for that number
← 422 discrepancy note parsed with no line items, or a UPC on the note didn't match the invoice
← 502 Blinkit discrepancy note PDF download failed
```

## Frontend flow

```
GET  /blinkit/status
  logged_in=false →
    POST /blinkit/send-otp
    (user enters OTP from email)
    POST /blinkit/verify-otp { otp }
POST /blinkit/process-po { po_number }
  → show credit_notes[] (id + number) to user
```
