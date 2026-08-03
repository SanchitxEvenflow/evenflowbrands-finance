import logging
import time
from abc import ABC, abstractmethod

import requests

from config import settings
from zoho_client import MAX_RETRIES, BACKOFF_BASE, MAX_WAIT_SECS, ZOHO_API_BASE, token_manager

KARNATAKA_LOCATION_ID = "727927000000014347"
UNITED_WH_LOCATION_ID = "727927000099392821"
RETURNS_WH_LOCATION_ID = "727927000024898475"


def _match_billing_address_id(addresses: list[dict], target: dict | None) -> str | None:
    """Matches the invoice's real billing_address to one of the customer's stored address-book
    entries by zip (unique enough on its own) — the resulting address_id is the only thing
    that reliably persists as a creditnote's billing_address_id."""
    if not target or not target.get("zip"):
        return None
    return next((a["address_id"] for a in addresses if a.get("zip") == target["zip"]), None)


def _split_dn_items(line_items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Shortage-reason items go on one credit note (United WH); everything else — damage,
    wrong SKU/EAN mismatch, etc. — goes on a separate credit note (Returns WH)."""
    short = [i for i in line_items if "SHORT" in i["reason"].strip().upper()]
    other = [i for i in line_items if "SHORT" not in i["reason"].strip().upper()]
    return short, other


class MarketplaceZohoClient(ABC):
    """Shared Zoho Books client for marketplace discrepancy-note credit-note flows (Blinkit,
    Instamart, ...): PO lookup, retry/401/429 handling, draft credit-note creation, and PDF
    attachment are identical across marketplaces. Subclasses supply only ITEM_KEY_FIELD (the
    DN line-item key used to look up invoice line items) plus _index_invoice_line_items() and
    _build_line_item()."""

    LOGGER_NAME = "marketplace_zoho"
    ITEM_KEY_FIELD: str

    def __init__(self):
        self._session = requests.Session()
        self.logger = logging.getLogger(self.LOGGER_NAME)

    def _get(self, url: str, params: dict) -> requests.Response:
        for attempt in range(MAX_RETRIES):
            headers = {"Authorization": f"Zoho-oauthtoken {token_manager.get_token()}"}
            resp = self._session.get(url, params=params, headers=headers, timeout=30)

            if resp.status_code == 401:
                self.logger.warning("401 — refreshing Zoho token")
                token_manager.force_refresh()
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if retry_after > MAX_WAIT_SECS:
                    self.logger.error("429 Retry-After=%s exceeds MAX_WAIT_SECS — aborting", retry_after)
                    return resp
                if attempt < MAX_RETRIES - 1:
                    self.logger.warning("429 — waiting %.0fs (attempt %d)", retry_after, attempt + 1)
                    time.sleep(retry_after)
                    continue

            return resp

        return resp

    def _post(self, url: str, params: dict, json_body: dict) -> requests.Response:
        for attempt in range(MAX_RETRIES):
            headers = {
                "Authorization": f"Zoho-oauthtoken {token_manager.get_token()}",
                "Content-Type": "application/json",
            }
            resp = self._session.post(url, params=params, json=json_body, headers=headers, timeout=30)

            if resp.status_code == 401:
                self.logger.warning("401 — refreshing Zoho token")
                token_manager.force_refresh()
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if retry_after > MAX_WAIT_SECS:
                    self.logger.error("429 Retry-After=%s exceeds MAX_WAIT_SECS — aborting", retry_after)
                    return resp
                if attempt < MAX_RETRIES - 1:
                    self.logger.warning("429 — waiting %.0fs (attempt %d)", retry_after, attempt + 1)
                    time.sleep(retry_after)
                    continue

            return resp

        return resp

    def _put(self, url: str, params: dict, json_body: dict) -> requests.Response:
        for attempt in range(MAX_RETRIES):
            headers = {
                "Authorization": f"Zoho-oauthtoken {token_manager.get_token()}",
                "Content-Type": "application/json",
            }
            resp = self._session.put(url, params=params, json=json_body, headers=headers, timeout=30)

            if resp.status_code == 401:
                self.logger.warning("401 — refreshing Zoho token")
                token_manager.force_refresh()
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if retry_after > MAX_WAIT_SECS:
                    self.logger.error("429 Retry-After=%s exceeds MAX_WAIT_SECS — aborting", retry_after)
                    return resp
                if attempt < MAX_RETRIES - 1:
                    self.logger.warning("429 — waiting %.0fs (attempt %d)", retry_after, attempt + 1)
                    time.sleep(retry_after)
                    continue

            return resp

        return resp

    def _set_item_cost_price(self, item_id: str, cost_price: float = 1.0) -> None:
        """Hardcodes purchase_rate ("Cost Price" under each line item in the Zoho UI) on the
        item master. Marketplace SKUs routed through this flow usually never get a cost price
        set, and Zoho wants some basis for COGS/valuation — best-effort, a failure here logs
        and moves on rather than blocking the credit note itself."""
        try:
            resp = self._put(
                f"{ZOHO_API_BASE}/items/{item_id}",
                params={"organization_id": settings.org_id},
                json_body={"purchase_rate": cost_price},
            )
            data = resp.json()
            if not resp.ok or data.get("code", 0) != 0:
                self.logger.warning("Failed to set cost price on item %s: %s", item_id, data)
                return
            # Zoho can return code 0 without the value sticking (e.g. item-group variants) —
            # confirm via a follow-up read instead of trusting the write response alone.
            check = self._get(f"{ZOHO_API_BASE}/items/{item_id}", params={"organization_id": settings.org_id}).json()
            actual = check.get("item", {}).get("purchase_rate")
            if actual != cost_price:
                self.logger.warning(
                    "Cost price PUT on item %s returned code 0 but purchase_rate reads back as %r (wanted %r) — "
                    "likely an item-group variant that ignores the plain /items update",
                    item_id, actual, cost_price,
                )
        except requests.RequestException:
            self.logger.exception("Failed to set cost price on item %s", item_id)

    def get_credit_note(self, creditnote_id: str) -> dict:
        """Full credit note JSON — carries `status` ("draft"/"open"/...) and `balance`."""
        resp = self._get(f"{ZOHO_API_BASE}/creditnotes/{creditnote_id}", params={"organization_id": settings.org_id})
        resp.raise_for_status()
        return resp.json()["creditnote"]

    def apply_credit_note_to_invoice(self, creditnote_id: str, invoice_id: str, amount: float | None = None) -> dict:
        """Applies a pushed credit note's balance against its invoice. Setting `invoice_id` at
        creation only references the invoice — it doesn't touch its balance, so this is a
        separate Zoho call. Only open (non-draft) credit notes can be applied, so drafts are
        opened first. Defaults to applying the note's full remaining balance."""
        creditnote = self.get_credit_note(creditnote_id)
        if creditnote["status"] == "draft":
            resp = self._post(
                f"{ZOHO_API_BASE}/creditnotes/{creditnote_id}/status/open",
                params={"organization_id": settings.org_id}, json_body={},
            )
            data = resp.json()
            if not resp.ok or data.get("code", 0) != 0:
                raise RuntimeError(f"Zoho credit note open failed [{resp.status_code}]: {data}")

        amount = creditnote["balance"] if amount is None else amount
        resp = self._post(
            f"{ZOHO_API_BASE}/creditnotes/{creditnote_id}/invoices",
            params={"organization_id": settings.org_id},
            json_body={"invoices": [{"invoice_id": invoice_id, "amount_applied": amount}]},
        )
        data = resp.json()
        if not resp.ok or data.get("code", 0) != 0:
            raise RuntimeError(f"Zoho apply-credit-note failed [{resp.status_code}]: {data}")
        self.logger.info("Applied %.2f from credit note %s to invoice %s", amount, creditnote_id, invoice_id)
        return data

    def find_invoices_by_po(self, po_number: str) -> list[dict]:
        """Zoho invoices where the cf_customer_po_no custom field matches the buyer's PO number
        (reference_number is always blank on real invoices — the PO lives in this custom field)."""
        resp = self._get(
            f"{ZOHO_API_BASE}/invoices",
            params={"organization_id": settings.org_id, "cf_customer_po_no": po_number},
        )
        resp.raise_for_status()
        invoices = resp.json().get("invoices", [])
        self.logger.info("PO %r → %d invoice(s)", po_number, len(invoices))
        return invoices

    def find_invoice_number_by_po(self, po_number: str) -> str | None:
        """Convenience for the UI flow: PO number in, invoice_number out (None if no match)."""
        invoices = self.find_invoices_by_po(po_number)
        if not invoices:
            self.logger.warning("No invoice found for PO %r", po_number)
            return None
        if len(invoices) > 1:
            self.logger.warning("PO %r matched %d invoices — using first: %s",
                                 po_number, len(invoices), invoices[0]["invoice_number"])
        return invoices[0]["invoice_number"]

    def get_invoice(self, invoice_id: str) -> dict:
        """Full invoice JSON by internal invoice_id (not invoice_number)."""
        resp = self._get(f"{ZOHO_API_BASE}/invoices/{invoice_id}", params={"organization_id": settings.org_id})
        resp.raise_for_status()
        return resp.json()["invoice"]

    def get_contact(self, customer_id: str) -> dict:
        """Full contact JSON — includes the stored `addresses` book, distinct from the
        invoice's own billing_address / customer_default_billing_address fields."""
        resp = self._get(f"{ZOHO_API_BASE}/contacts/{customer_id}", params={"organization_id": settings.org_id})
        resp.raise_for_status()
        return resp.json()["contact"]

    def attach_pdf(self, creditnote_id: str, pdf_bytes: bytes, filename: str) -> None:
        """Attaches a file (the source discrepancy note PDF) to an existing credit note."""
        for attempt in range(MAX_RETRIES):
            headers = {"Authorization": f"Zoho-oauthtoken {token_manager.get_token()}"}
            resp = self._session.post(
                f"{ZOHO_API_BASE}/creditnotes/{creditnote_id}/attachment",
                params={"organization_id": settings.org_id},
                files={"attachment": (filename, pdf_bytes, "application/pdf")},
                headers=headers,
                timeout=30,
            )

            if resp.status_code == 401:
                self.logger.warning("401 — refreshing Zoho token")
                token_manager.force_refresh()
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if retry_after > MAX_WAIT_SECS:
                    self.logger.error("429 Retry-After=%s exceeds MAX_WAIT_SECS — aborting attachment", retry_after)
                    return
                if attempt < MAX_RETRIES - 1:
                    self.logger.warning("429 — waiting %.0fs (attempt %d)", retry_after, attempt + 1)
                    time.sleep(retry_after)
                    continue

            data = resp.json()
            if not resp.ok or data.get("code", 0) != 0:
                raise RuntimeError(f"Zoho attachment upload failed [{resp.status_code}]: {data}")
            self.logger.info("Attached %s to credit note %s", filename, creditnote_id)
            return

    @abstractmethod
    def _index_invoice_line_items(self, invoice_line_items: list[dict]) -> dict:
        """Maps invoice line items to the key found on DN items under ITEM_KEY_FIELD (EAN for
        Blinkit, the cf_id custom field for Instamart)."""

    @abstractmethod
    def _build_line_item(self, item: dict, match: dict, location_id: str) -> dict:
        """Builds one Zoho creditnote line-item dict from a DN item + its matched invoice line
        item."""

    def build_credit_note_payload(
        self, dn: dict, invoice: dict, items: list[dict], location_id: str, billing_address_id: str | None = None,
    ) -> dict:
        """Maps one warehouse's worth of DN line items (see _split_dn_items) onto Zoho
        creditnote fields — no speculative fields beyond the API docs' full example body."""
        index = self._index_invoice_line_items(invoice.get("line_items", []))

        line_items = []
        unmatched = []
        for item in items:
            key = item.get(self.ITEM_KEY_FIELD)
            match = index.get(key) if key else None
            if not match:
                unmatched.append(key)
                continue
            line_items.append(self._build_line_item(item, match, location_id))

        if unmatched:
            raise ValueError(
                f"DN {dn['dn_id']}: {self.ITEM_KEY_FIELD}(s) not found on invoice {invoice['invoice_number']}: {unmatched}"
            )

        # Notes = parsed reason/remark only, one line per item.
        notes_lines = [f"{i['reason']} — {i['remark']}" for i in items]

        # Goods Status: "Shortage" for the shortage-reason bucket (United WH), "Pending" for
        # everything else (Returns WH) — mirrors the warehouse split in _split_dn_items.
        goods_status = "Shortage" if location_id == UNITED_WH_LOCATION_ID else "Pending"

        payload = {
            "customer_id": invoice["customer_id"],
            "invoice_id": invoice["invoice_id"],
            "is_draft": True,
            "location_id": KARNATAKA_LOCATION_ID,
            "notes": "\n".join(notes_lines),
            "custom_fields": [
                {"api_name": "cf_org_debit_note_no", "value": dn["dn_id"]},
                {"api_name": "cf_goods_status", "value": goods_status},
            ],
            "line_items": line_items,
            # Shipping recipient GST details: use the invoice's own shipping GST if it has one,
            # else fall back to its billing GST — same customer/address in practice, and Zoho
            # otherwise leaves the shipping GST section blank on the credit note.
            "shipping_gst_no": invoice.get("shipping_gst_no") or invoice.get("gst_no", ""),
            "shipping_trader_name": invoice.get("shipping_trader_name") or invoice.get("company_name", ""),
            "shipping_legal_name": invoice.get("shipping_legal_name") or invoice.get("legal_name", ""),
        }
        if billing_address_id:
            payload["billing_address_id"] = billing_address_id
            # Credit note's shipping address always mirrors billing, regardless of what the
            # source invoice's own shipping address was.
            payload["shipping_address_id"] = billing_address_id
        return payload

    def create_credit_note(
        self, dn: dict, invoice: dict, pdf_bytes: bytes | None = None, pdf_filename: str | None = None,
    ) -> list[dict]:
        """Creates one draft Zoho credit note per warehouse from a parsed discrepancy note
        (pdf_parser output) against the matching Zoho invoice: shortage items go on a credit
        note against United WH, everything else goes on a separate credit note against Returns
        WH — a DN with items in only one bucket creates just one. Raises on any DN item that
        doesn't match an invoice line item before creating either note, so a bad key in the
        second bucket can't leave the first bucket's credit note as an orphaned partial.
        Attaches the source PDF to each created note, if given."""
        contact = self.get_contact(invoice["customer_id"])
        billing_address_id = _match_billing_address_id(contact.get("addresses", []), invoice.get("billing_address"))

        short_items, other_items = _split_dn_items(dn["line_items"])
        buckets = [(short_items, UNITED_WH_LOCATION_ID), (other_items, RETURNS_WH_LOCATION_ID)]
        payloads = [
            self.build_credit_note_payload(dn, invoice, items, location_id, billing_address_id)
            for items, location_id in buckets if items
        ]

        item_ids = {li["item_id"] for payload in payloads for li in payload["line_items"]}
        for item_id in item_ids:
            self._set_item_cost_price(item_id)

        created = []
        for payload in payloads:
            self.logger.info("Creating credit note for DN %s (%d item(s)) against invoice %s",
                              dn["dn_id"], len(payload["line_items"]), invoice["invoice_number"])
            resp = self._post(f"{ZOHO_API_BASE}/creditnotes", params={"organization_id": settings.org_id}, json_body=payload)
            data = resp.json()
            if not resp.ok or data.get("code", 0) != 0:
                raise RuntimeError(f"Zoho credit note creation failed [{resp.status_code}]: {data}")
            creditnote = data["creditnote"]
            self.logger.info("Credit note created: %s", creditnote["creditnote_id"])

            if pdf_bytes:
                self.attach_pdf(creditnote["creditnote_id"], pdf_bytes, pdf_filename or f"{dn['dn_id']}.pdf")

            created.append(creditnote)

        return created


def demo() -> None:
    """ponytail self-check: retry/401 handling + generic PO-lookup/payload plumbing via a
    minimal concrete subclass, mocked session, no live network call. Marketplace-specific
    matching/line-item logic is covered by each subclass's own demo()."""

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self.ok = status_code < 400
            self.headers: dict = {}
            self._payload = payload or {}

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.ok:
                raise requests.HTTPError(str(self.status_code))

    class DemoClient(MarketplaceZohoClient):
        LOGGER_NAME = "demo_zoho"
        ITEM_KEY_FIELD = "code"

        def _index_invoice_line_items(self, invoice_line_items):
            return {li["code"]: li for li in invoice_line_items}

        def _build_line_item(self, item, match, location_id):
            return {
                "item_id": match["item_id"], "name": match["name"], "quantity": item["qty"],
                "rate": match["rate"], "tax_id": match.get("tax_id"), "location_id": location_id,
            }

    client = DemoClient()
    state = {"calls": 0, "refreshed": False}
    orig_force_refresh = token_manager.force_refresh
    token_manager.force_refresh = lambda: state.__setitem__("refreshed", True)

    try:
        def fake_get(url, params, headers, timeout):
            state["calls"] += 1
            if state["calls"] == 1:
                return FakeResponse(401)
            return FakeResponse(200, {"invoices": [{"invoice_id": "1", "invoice_number": "INV-001"}]})

        client._session.get = fake_get
        assert client.find_invoice_number_by_po("PO123") == "INV-001"
        assert state["calls"] == 2 and state["refreshed"] is True

        client._session.get = lambda *a, **k: FakeResponse(200, {"invoices": []})
        assert client.find_invoice_number_by_po("NOPE") is None

        dn = {"dn_id": "DN1", "line_items": [
            {"code": "A1", "reason": "Short quantity", "remark": "SHORT", "qty": 5},
            {"code": "A2", "reason": "Damage", "remark": "MRP wrong", "qty": 1},
        ]}
        invoice = {
            "invoice_id": "INV-ID-1", "invoice_number": "INV-001", "customer_id": "CUST-1",
            "billing_address": {"zip": "560095"},
            "shipping_gst_no": "27AAFCG9846E1ZB", "shipping_trader_name": "Acme Trading",
            "shipping_legal_name": "Acme Pvt Ltd",
            "line_items": [
                {"code": "A1", "item_id": "ITEM-1", "name": "Item A1", "rate": 10.0, "tax_id": "TAX-1"},
                {"code": "A2", "item_id": "ITEM-2", "name": "Item A2", "rate": 20.0, "tax_id": "TAX-1"},
            ],
        }
        short_items, other_items = _split_dn_items(dn["line_items"])

        short_payload = client.build_credit_note_payload(dn, invoice, short_items, UNITED_WH_LOCATION_ID)
        assert short_payload["line_items"][0]["item_id"] == "ITEM-1"
        assert {"api_name": "cf_goods_status", "value": "Shortage"} in short_payload["custom_fields"]
        assert "billing_address_id" not in short_payload
        assert "shipping_address_id" not in short_payload  # no billing_address_id matched → nothing to mirror
        assert short_payload["shipping_gst_no"] == "27AAFCG9846E1ZB"
        assert short_payload["shipping_trader_name"] == "Acme Trading"
        assert short_payload["shipping_legal_name"] == "Acme Pvt Ltd"

        other_payload = client.build_credit_note_payload(
            dn, invoice, other_items, RETURNS_WH_LOCATION_ID, billing_address_id="addr-1",
        )
        assert other_payload["billing_address_id"] == "addr-1"
        assert other_payload["shipping_address_id"] == "addr-1"  # shipping mirrors billing
        assert {"api_name": "cf_goods_status", "value": "Pending"} in other_payload["custom_fields"]

        # no shipping GST on invoice -> falls back to billing GST fields, not left blank
        no_shipping_gst_invoice = {**invoice, "shipping_gst_no": "", "shipping_trader_name": "",
                                    "shipping_legal_name": "", "gst_no": "36AAKCC0275A1Z2",
                                    "company_name": "Some Co", "legal_name": "Some Co Pvt Ltd"}
        fallback_payload = client.build_credit_note_payload(
            dn, no_shipping_gst_invoice, short_items, UNITED_WH_LOCATION_ID,
        )
        assert fallback_payload["shipping_gst_no"] == "36AAKCC0275A1Z2"
        assert fallback_payload["shipping_trader_name"] == "Some Co"
        assert fallback_payload["shipping_legal_name"] == "Some Co Pvt Ltd"

        bad_items = [{**short_items[0], "code": "NOPE"}]
        try:
            client.build_credit_note_payload(dn, invoice, bad_items, UNITED_WH_LOCATION_ID)
            raise AssertionError("expected ValueError for unmatched item")
        except ValueError:
            pass

        assert _match_billing_address_id([{"address_id": "a1", "zip": "560095"}], {"zip": "560095"}) == "a1"
        assert _match_billing_address_id([{"address_id": "a1", "zip": "560095"}], {"zip": "000000"}) is None
        assert _match_billing_address_id([], None) is None

        # create_credit_note: one credit note per non-empty bucket, PDF attached to each
        def fake_get(url, params, headers, timeout):
            if "/items/" in url:
                return FakeResponse(200, {"item": {"purchase_rate": 1.0}})
            return FakeResponse(200, {"contact": {"addresses": [{"address_id": "a1", "zip": "560095"}]}})

        client._session.get = fake_get
        post_state = {"cn_calls": 0, "attached_filenames": []}

        def fake_post(url, params, headers, timeout, json=None, files=None):
            if url.endswith("/attachment"):
                post_state["attached_filenames"].append(files["attachment"][0])
                return FakeResponse(200, {"code": 0})
            post_state["cn_calls"] += 1
            return FakeResponse(200, {"code": 0, "creditnote": {"creditnote_id": f"cn{post_state['cn_calls']}"}})

        put_calls = []
        client._session.put = lambda url, params, headers, timeout, json=None: (
            put_calls.append((url, json)) or FakeResponse(200, {"code": 0})
        )
        client._session.post = fake_post
        created = client.create_credit_note(dn, invoice, pdf_bytes=b"%PDF-fake", pdf_filename="DN1.pdf")
        assert post_state["cn_calls"] == 2
        assert {c["creditnote_id"] for c in created} == {"cn1", "cn2"}
        assert post_state["attached_filenames"] == ["DN1.pdf", "DN1.pdf"]
        # cost price hardcoded to 1 on every distinct item used, once each
        assert {url for url, _ in put_calls} == {
            f"{ZOHO_API_BASE}/items/ITEM-1", f"{ZOHO_API_BASE}/items/ITEM-2",
        }
        assert all(body == {"purchase_rate": 1.0} for _, body in put_calls)

        # apply_credit_note_to_invoice: draft note gets opened, then applied for its balance
        get_calls = []
        client._session.get = lambda url, params, headers, timeout: (
            get_calls.append(url) or FakeResponse(200, {"creditnote": {"status": "draft", "balance": 150.0}})
        )
        apply_calls = []

        def fake_apply_post(url, params, headers, timeout, json=None, files=None):
            apply_calls.append((url, json))
            return FakeResponse(200, {"code": 0})

        client._session.post = fake_apply_post
        client.apply_credit_note_to_invoice("cn1", "INV-ID-1")
        assert apply_calls[0][0].endswith("/creditnotes/cn1/status/open")
        assert apply_calls[1] == (
            f"{ZOHO_API_BASE}/creditnotes/cn1/invoices",
            {"invoices": [{"invoice_id": "INV-ID-1", "amount_applied": 150.0}]},
        )

        # already-open note: no status/open call, explicit amount overrides balance
        client._session.get = lambda url, params, headers, timeout: FakeResponse(
            200, {"creditnote": {"status": "open", "balance": 150.0}},
        )
        apply_calls.clear()
        client.apply_credit_note_to_invoice("cn2", "INV-ID-1", amount=75.0)
        assert len(apply_calls) == 1
        assert apply_calls[0] == (
            f"{ZOHO_API_BASE}/creditnotes/cn2/invoices",
            {"invoices": [{"invoice_id": "INV-ID-1", "amount_applied": 75.0}]},
        )
    finally:
        token_manager.force_refresh = orig_force_refresh

    print("creditnote zoho_client (shared base) demo OK")


if __name__ == "__main__":
    demo()
