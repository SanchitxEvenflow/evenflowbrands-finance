import logging
import time

import requests

from config import settings
from zoho_client import MAX_RETRIES, BACKOFF_BASE, MAX_WAIT_SECS, ZOHO_API_BASE, token_manager

logger = logging.getLogger("blinkit_zoho")

KARNATAKA_LOCATION_ID = "727927000000014347"
UNITED_WH_LOCATION_ID = "727927000099392821"
RETURNS_WH_LOCATION_ID = "727927000024898475"


class BlinkitZohoClient:
    """Looks up Zoho Books invoices by PO number, for the Blinkit credit-note flow."""

    def __init__(self):
        self._session = requests.Session()

    def _get(self, url: str, params: dict) -> requests.Response:
        for attempt in range(MAX_RETRIES):
            headers = {"Authorization": f"Zoho-oauthtoken {token_manager.get_token()}"}
            resp = self._session.get(url, params=params, headers=headers, timeout=30)

            if resp.status_code == 401:
                logger.warning("401 — refreshing Zoho token")
                token_manager.force_refresh()
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if retry_after > MAX_WAIT_SECS:
                    logger.error("429 Retry-After=%s exceeds MAX_WAIT_SECS — aborting", retry_after)
                    return resp
                if attempt < MAX_RETRIES - 1:
                    logger.warning("429 — waiting %.0fs (attempt %d)", retry_after, attempt + 1)
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
                logger.warning("401 — refreshing Zoho token")
                token_manager.force_refresh()
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if retry_after > MAX_WAIT_SECS:
                    logger.error("429 Retry-After=%s exceeds MAX_WAIT_SECS — aborting", retry_after)
                    return resp
                if attempt < MAX_RETRIES - 1:
                    logger.warning("429 — waiting %.0fs (attempt %d)", retry_after, attempt + 1)
                    time.sleep(retry_after)
                    continue

            return resp

        return resp

    def find_invoices_by_po(self, po_number: str) -> list[dict]:
        """Zoho invoices where the cf_customer_po_no custom field matches the buyer's PO number
        (reference_number is always blank on real invoices — the PO lives in this custom field)."""
        resp = self._get(
            f"{ZOHO_API_BASE}/invoices",
            params={"organization_id": settings.org_id, "cf_customer_po_no": po_number},
        )
        resp.raise_for_status()
        invoices = resp.json().get("invoices", [])
        logger.info("PO %r → %d invoice(s)", po_number, len(invoices))
        return invoices

    def find_invoice_number_by_po(self, po_number: str) -> str | None:
        """Convenience for the UI flow: PO number in, invoice_number out (None if no match)."""
        invoices = self.find_invoices_by_po(po_number)
        if not invoices:
            logger.warning("No invoice found for PO %r", po_number)
            return None
        if len(invoices) > 1:
            logger.warning("PO %r matched %d invoices — using first: %s",
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

    def create_credit_note(
        self, dn: dict, invoice: dict, pdf_bytes: bytes | None = None, pdf_filename: str | None = None,
    ) -> list[dict]:
        """Creates one draft Zoho credit note per warehouse from a parsed discrepancy note
        (pdf_parser output) against the matching Zoho invoice (get_invoice output): shortage
        items go on a credit note against United WH, everything else (damage, EAN mismatch,
        etc.) goes on a separate credit note against Returns WH — a DN with items in only one
        bucket creates just one. Raises on any DN item whose UPC doesn't match an invoice line
        item — a partial credit note would misstate the refund — before creating either note.

        creditnote_number is left to Zoho's own auto-generation — this org has Auto Number
        Generation enabled for credit notes, and rejects any custom number that doesn't
        exactly match its own counter (code 4097), even a dash-only reformat of the same
        digits (confirmed live).

        billing_address is resolved as a billing_address_id pointing at the customer's stored
        address book, matched to the invoice's real billing_address by zip — sending a raw
        billing_address object 400s ("less than 100 characters", regardless of actual length)
        or silently fails to persist on this org's creditnotes endpoint (confirmed live);
        billing_address_id is the one form that actually sticks.

        If pdf_bytes is given, the source discrepancy note is attached to each credit note
        created."""
        contact = self.get_contact(invoice["customer_id"])
        billing_address_id = _match_billing_address_id(contact.get("addresses", []), invoice.get("billing_address"))

        short_items, other_items = _split_dn_items(dn["line_items"])
        buckets = [(short_items, UNITED_WH_LOCATION_ID), (other_items, RETURNS_WH_LOCATION_ID)]
        # Build (and validate) every payload before creating anything, so a bad UPC in the
        # second bucket can't leave the first bucket's credit note as an orphaned partial.
        payloads = [
            build_credit_note_payload(dn, invoice, items, location_id, billing_address_id)
            for items, location_id in buckets if items
        ]

        created = []
        for payload in payloads:
            logger.info("Creating credit note for DN %s (%d item(s)) against invoice %s",
                        dn["dn_id"], len(payload["line_items"]), invoice["invoice_number"])
            resp = self._post(f"{ZOHO_API_BASE}/creditnotes", params={"organization_id": settings.org_id}, json_body=payload)
            data = resp.json()
            if not resp.ok or data.get("code", 0) != 0:
                raise RuntimeError(f"Zoho credit note creation failed [{resp.status_code}]: {data}")
            creditnote = data["creditnote"]
            logger.info("Credit note created: %s", creditnote["creditnote_id"])

            if pdf_bytes:
                self.attach_pdf(creditnote["creditnote_id"], pdf_bytes, pdf_filename or f"{dn['dn_id']}.pdf")

            created.append(creditnote)

        return created

    def attach_pdf(self, creditnote_id: str, pdf_bytes: bytes, filename: str) -> None:
        """Attaches a file (the Blinkit discrepancy note PDF) to an existing credit note."""
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
                logger.warning("401 — refreshing Zoho token")
                token_manager.force_refresh()
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", BACKOFF_BASE ** (attempt + 1)))
                if retry_after > MAX_WAIT_SECS:
                    logger.error("429 Retry-After=%s exceeds MAX_WAIT_SECS — aborting attachment", retry_after)
                    return
                if attempt < MAX_RETRIES - 1:
                    logger.warning("429 — waiting %.0fs (attempt %d)", retry_after, attempt + 1)
                    time.sleep(retry_after)
                    continue

            data = resp.json()
            if not resp.ok or data.get("code", 0) != 0:
                raise RuntimeError(f"Zoho attachment upload failed [{resp.status_code}]: {data}")
            logger.info("Attached %s to credit note %s", filename, creditnote_id)
            return


def _line_item_ean(line_item: dict) -> str | None:
    """EAN/UPC lives in item_custom_fields (api_name=cf_ean) — sku is an unrelated internal code."""
    for cf in line_item.get("item_custom_fields", []):
        if cf.get("api_name") == "cf_ean":
            return cf.get("value")
    return None


def _match_billing_address_id(addresses: list[dict], target: dict | None) -> str | None:
    """Matches the invoice's real billing_address to one of the customer's stored address-book
    entries by zip (unique enough on its own) — the resulting address_id is the only thing
    that reliably persists as a creditnote's billing_address_id (see create_credit_note)."""
    if not target or not target.get("zip"):
        return None
    return next((a["address_id"] for a in addresses if a.get("zip") == target["zip"]), None)


def _split_dn_items(line_items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Shortage-reason items go on one credit note (United WH); everything else — damage,
    EAN mismatch, etc. — goes on a separate credit note (Returns WH), per warehouse routing."""
    short = [i for i in line_items if "SHORT" in i["reason"].strip().upper()]
    other = [i for i in line_items if "SHORT" not in i["reason"].strip().upper()]
    return short, other


def build_credit_note_payload(
    dn: dict, invoice: dict, items: list[dict], location_id: str, billing_address_id: str | None = None,
) -> dict:
    """Maps one warehouse's worth of DN line items (see _split_dn_items) + the source invoice
    onto only the Zoho creditnote fields we actually have data for — no speculative fields
    from the API docs' full example body."""
    invoice_line_items = invoice.get("line_items", [])
    by_ean = {_line_item_ean(li): li for li in invoice_line_items if _line_item_ean(li)}

    line_items = []
    unmatched = []
    for item in items:
        match = by_ean.get(item["upc"])
        if not match:
            unmatched.append(item["upc"])
            continue
        qty = item["qty"]
        line_items.append({
            "item_id": match["item_id"],
            "name": item["name"],
            "description": f"Reason: {item['reason']} | Remark: {item['remark']}",
            "quantity": qty,
            "rate": round(item["subtotal_excl_tax"] / qty, 2) if qty else 0,
            "tax_id": match.get("tax_id"),
            "location_id": location_id,
        })

    if unmatched:
        raise ValueError(f"DN {dn['dn_id']}: UPC(s) not found on invoice {invoice['invoice_number']}: {unmatched}")

    # Notes = parsed reason/remark only, one line per item — no DN id, warehouse, name,
    # UPC, or postal address (explicitly rejected).
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
    }
    if billing_address_id:
        payload["billing_address_id"] = billing_address_id
    return payload


blinkit_zoho = BlinkitZohoClient()


def demo() -> None:
    """ponytail self-check: 401-retry + PO lookup branches, mocked session, no live network call."""

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

    client = BlinkitZohoClient()
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
        invoices = client.find_invoices_by_po("PO123")
        assert state["calls"] == 2, "expected a retry after the 401"
        assert state["refreshed"] is True
        assert invoices[0]["invoice_number"] == "INV-001"
        assert client.find_invoice_number_by_po("PO123") == "INV-001"

        client._session.get = lambda *a, **k: FakeResponse(200, {"invoices": []})
        assert client.find_invoice_number_by_po("NOPE") is None

        # build_credit_note_payload: matched EAN → line item with computed rate/tax_id/location carried over
        dn = {
            "dn_id": "D25497DN26001725",
            "warehouse_name": "BCPL - Nagpur N1 Feeder Warehouse",
            "warehouse_address": "Plot 8/9 MIDC Kalmeshwar, Nagpur, Maharashtra 441501",
            "original_invoice_id": "EL-KA-IN27-00609",
            "line_items": [
                {"upc": "8906157933838", "name": "Ice Gel Pad", "qty": 50,
                 "subtotal_excl_tax": 6159.50, "gst_percent": 5.0, "reason": "Short quantity", "remark": "SHORT"},
                {"upc": "8906158357510", "name": "Neem Oil", "qty": 2,
                 "subtotal_excl_tax": 100.0, "gst_percent": 5.0, "reason": "Product / Box / UPC Damage", "remark": "MRP = 299"},
            ],
        }
        invoice = {
            "invoice_id": "90300000079426",
            "invoice_number": "INV-384",
            "customer_id": "903000000000099",
            "billing_address": {"city": "Pune", "state": "Maharashtra", "zip": "410506"},
            "line_items": [
                {"item_id": "90300000081501", "tax_id": "903000000000356",
                 "item_custom_fields": [{"api_name": "cf_ean", "value": "8906157933838"}]},
                {"item_id": "90300000081502", "tax_id": "903000000000356",
                 "item_custom_fields": [{"api_name": "cf_ean", "value": "8906158357510"}]},
            ],
        }
        # _split_dn_items: shortage reason vs everything else
        short_items, other_items = _split_dn_items(dn["line_items"])
        assert [i["upc"] for i in short_items] == ["8906157933838"]
        assert [i["upc"] for i in other_items] == ["8906158357510"]

        short_payload = build_credit_note_payload(dn, invoice, short_items, UNITED_WH_LOCATION_ID)
        assert "creditnote_number" not in short_payload  # left to Zoho's own auto-generation
        assert "billing_address_id" not in short_payload  # not passed → omitted, not defaulted
        assert short_payload["customer_id"] == "903000000000099"
        assert short_payload["invoice_id"] == "90300000079426"
        assert short_payload["location_id"] == KARNATAKA_LOCATION_ID
        assert short_payload["is_draft"] is True
        assert short_payload["custom_fields"] == [
            {"api_name": "cf_org_debit_note_no", "value": "D25497DN26001725"},
            {"api_name": "cf_goods_status", "value": "Shortage"},
        ]
        [short_li] = short_payload["line_items"]
        assert short_li["item_id"] == "90300000081501" and short_li["location_id"] == UNITED_WH_LOCATION_ID
        assert short_li["quantity"] == 50 and short_li["rate"] == round(6159.50 / 50, 2)
        assert short_li["description"] == "Reason: Short quantity | Remark: SHORT"
        assert "Short quantity — SHORT" in short_payload["notes"]
        assert "Neem Oil" not in short_payload["notes"]  # damage item is on the other credit note
        assert dn["warehouse_address"] not in short_payload["notes"]  # address dropped, only reason/remark wanted

        other_payload = build_credit_note_payload(dn, invoice, other_items, RETURNS_WH_LOCATION_ID, billing_address_id="addr-pune")
        assert other_payload["billing_address_id"] == "addr-pune"
        assert {"api_name": "cf_goods_status", "value": "Pending"} in other_payload["custom_fields"]
        [damage_li] = other_payload["line_items"]
        assert damage_li["item_id"] == "90300000081502" and damage_li["location_id"] == RETURNS_WH_LOCATION_ID
        assert "Product / Box / UPC Damage — MRP = 299" in other_payload["notes"]

        # _match_billing_address_id: matches the invoice's billing_address to a stored
        # contact address by zip; a raw billing_address object is never sent (it 400s / no-ops)
        stored_addresses = [
            {"address_id": "addr-pune", "zip": "410506"},
            {"address_id": "addr-lucknow", "zip": "227101"},
        ]
        assert _match_billing_address_id(stored_addresses, {"zip": "410506"}) == "addr-pune"
        assert _match_billing_address_id(stored_addresses, {"zip": "000000"}) is None
        assert _match_billing_address_id(stored_addresses, None) is None

        # Unmatched UPC must raise, not silently drop a line item on a money path
        bad_items = [{**short_items[0], "upc": "0000000000000"}]
        try:
            build_credit_note_payload(dn, invoice, bad_items, UNITED_WH_LOCATION_ID)
            raise AssertionError("expected ValueError for unmatched UPC")
        except ValueError:
            pass

        # create_credit_note: resolves billing_address_id via get_contact, creates one credit
        # note per non-empty warehouse bucket (with a 401 retry on the first), attaching the
        # source PDF to each
        client._session.get = lambda url, params, headers, timeout: FakeResponse(
            200, {"contact": {"addresses": stored_addresses}},
        )

        post_state = {"cn_calls": 0, "attached_filenames": []}

        def fake_post(url, params, headers, timeout, json=None, files=None):
            if url.endswith("/attachment"):
                post_state["attached_filenames"].append(files["attachment"][0])
                return FakeResponse(200, {"code": 0})
            post_state["cn_calls"] += 1
            if post_state["cn_calls"] == 1:
                return FakeResponse(401)
            assert "creditnote_number" not in json
            assert json["billing_address_id"] == "addr-pune"
            return FakeResponse(200, {"code": 0, "creditnote": {"creditnote_id": f"cn{post_state['cn_calls'] - 1}"}})

        client._session.post = fake_post
        created = client.create_credit_note(dn, invoice, pdf_bytes=b"%PDF-fake", pdf_filename="D25497DN26001725.pdf")
        assert post_state["cn_calls"] == 3  # 1 failed (401) + 2 successful creates
        assert {c["creditnote_id"] for c in created} == {"cn1", "cn2"}
        assert post_state["attached_filenames"] == ["D25497DN26001725.pdf", "D25497DN26001725.pdf"]

        # A DN with items in only one bucket creates just one credit note
        short_only_dn = {**dn, "line_items": [dn["line_items"][0]]}
        post_state["cn_calls"] = 0
        client._session.post = lambda url, params, headers, timeout, json=None, files=None: (
            FakeResponse(200, {"code": 0}) if url.endswith("/attachment")
            else FakeResponse(200, {"code": 0, "creditnote": {"creditnote_id": "cn-solo"}})
        )
        created_solo = client.create_credit_note(short_only_dn, invoice)
        assert len(created_solo) == 1 and created_solo[0]["creditnote_id"] == "cn-solo"
    finally:
        token_manager.force_refresh = orig_force_refresh

    print("blinkit zoho_client demo OK")


if __name__ == "__main__":
    demo()
