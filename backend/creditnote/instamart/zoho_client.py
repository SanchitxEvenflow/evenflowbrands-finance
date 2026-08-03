import logging
import time

import requests

from config import settings
from creditnote.blinkit.zoho_client import KARNATAKA_LOCATION_ID, RETURNS_WH_LOCATION_ID, UNITED_WH_LOCATION_ID
from zoho_client import MAX_RETRIES, BACKOFF_BASE, MAX_WAIT_SECS, ZOHO_API_BASE, token_manager

logger = logging.getLogger("instamart_zoho")


class InstamartZohoClient:
    """Looks up Zoho Books invoices by PO number, for the Instamart credit-note flow."""

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
        (pdf_parser output) against the matching Zoho invoice: shortage items go on a credit
        note against United WH, everything else goes on a separate credit note against
        Returns WH — a DN with items in only one bucket creates just one. Raises on any DN
        item whose SKU doesn't match an invoice line item before creating either note."""
        contact = self.get_contact(invoice["customer_id"])
        billing_address_id = _match_billing_address_id(contact.get("addresses", []), invoice.get("billing_address"))

        short_items, other_items = _split_dn_items(dn["line_items"])
        buckets = [(short_items, UNITED_WH_LOCATION_ID), (other_items, RETURNS_WH_LOCATION_ID)]
        payloads = [
            build_credit_note_payload(dn, invoice, items, location_id, billing_address_id)
            for items, location_id in buckets if items
        ]

        created = []
        for payload in payloads:
            logger.info("Creating credit note for DN %s (%d item(s)) against invoice %s",
                        dn["dn_no"], len(payload["line_items"]), invoice["invoice_number"])
            resp = self._post(f"{ZOHO_API_BASE}/creditnotes", params={"organization_id": settings.org_id}, json_body=payload)
            data = resp.json()
            if not resp.ok or data.get("code", 0) != 0:
                raise RuntimeError(f"Zoho credit note creation failed [{resp.status_code}]: {data}")
            creditnote = data["creditnote"]
            logger.info("Credit note created: %s", creditnote["creditnote_id"])

            if pdf_bytes:
                self.attach_pdf(creditnote["creditnote_id"], pdf_bytes, pdf_filename or f"{dn['dn_no']}.pdf")

            created.append(creditnote)

        return created

    def attach_pdf(self, creditnote_id: str, pdf_bytes: bytes, filename: str) -> None:
        """Attaches a file (the Instamart discrepancy note PDF) to an existing credit note."""
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


def _line_item_cf_id(line_item: dict) -> str | None:
    """The Instamart SKU code parsed off the PDF (see instamart/pdf_parser.py) matches
    item_custom_fields' cf_id ("ID") field — Zoho's own built-in `sku` field is a
    differently-formatted internal code (e.g. "CIN-HOP-LLF-L5") and doesn't match at all."""
    for cf in line_item.get("item_custom_fields", []):
        if cf.get("api_name") == "cf_id":
            return cf.get("value")
    return None


def _match_invoice_line_item(invoice_line_items: list[dict], sku: str) -> dict | None:
    return next((li for li in invoice_line_items if _line_item_cf_id(li) == sku), None)


def _match_billing_address_id(addresses: list[dict], target: dict | None) -> str | None:
    """Matches the invoice's real billing_address to one of the customer's stored address-book
    entries by zip (unique enough on its own) — the resulting address_id is the only thing
    that reliably persists as a creditnote's billing_address_id."""
    if not target or not target.get("zip"):
        return None
    return next((a["address_id"] for a in addresses if a.get("zip") == target["zip"]), None)


def _split_dn_items(line_items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Shortage-reason items go on one credit note (United WH); everything else — wrong SKU,
    damage, etc. — goes on a separate credit note (Returns WH), per warehouse routing."""
    short = [i for i in line_items if "SHORT" in i["reason"].strip().upper()]
    other = [i for i in line_items if "SHORT" not in i["reason"].strip().upper()]
    return short, other


def build_credit_note_payload(
    dn: dict, invoice: dict, items: list[dict], location_id: str, billing_address_id: str | None = None,
) -> dict:
    """Maps one warehouse's worth of DN line items (see _split_dn_items) onto Zoho creditnote
    fields. Only dn_no/sku/reason/remark/dn_qty are scraped from the PDF (see
    instamart/pdf_parser.py) — item name, rate and tax_id come from the matched invoice line
    item, not the PDF."""
    invoice_line_items = invoice.get("line_items", [])

    line_items = []
    unmatched = []
    for item in items:
        match = _match_invoice_line_item(invoice_line_items, item["sku"]) if item["sku"] else None
        if not match:
            unmatched.append(item["sku"])
            continue
        line_items.append({
            "item_id": match["item_id"],
            "name": match["name"],
            "description": f"Reason: {item['reason']} | Remark: {item['remark']}",
            "quantity": item["dn_qty"],
            "rate": match["rate"],
            "tax_id": match.get("tax_id"),
            "location_id": location_id,
            # Zoho auto-fills ASIN/FSN from the item master's defaults when only item_id is
            # given — blank them explicitly, this credit note flow doesn't need them.
            "item_custom_fields": [
                {"api_name": "cf_asin", "value": ""},
                {"api_name": "cf_fsin", "value": ""},
            ],
        })

    if unmatched:
        raise ValueError(f"DN {dn['dn_no']}: SKU(s) not found on invoice {invoice['invoice_number']}: {unmatched}")

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
            {"api_name": "cf_org_debit_note_no", "value": dn["dn_no"]},
            {"api_name": "cf_goods_status", "value": goods_status},
        ],
        "line_items": line_items,
    }
    if billing_address_id:
        payload["billing_address_id"] = billing_address_id
    return payload


instamart_zoho = InstamartZohoClient()


def demo() -> None:
    """ponytail self-check: PO lookup + payload building, mocked session, no live network call."""

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

    dn = {
        "dn_no": "FC5-DN772809",
        "line_items": [
            {"sku": "117407", "reason": "Wrong SKU", "remark": "sku not add in po", "dn_qty": 50},
            {"sku": "884213", "reason": "Short quantity", "remark": "SHORT", "dn_qty": 10},
        ],
    }
    invoice = {
        "invoice_id": "90300000079426",
        "invoice_number": "EL-KA-IN27-05151",
        "customer_id": "903000000000099",
        "billing_address": {"city": "Jhajjar", "state": "Haryana", "zip": "124108"},
        "line_items": [
            {"item_id": "90300000081501", "name": "Xtrim Unisex Bandana", "rate": 132.67,
             "tax_id": "903000000000356", "sku": "CIN-XTR-BND-1",
             "item_custom_fields": [{"api_name": "cf_id", "value": "117407"}]},
            {"item_id": "90300000081502", "name": "Some Other Item", "rate": 45.0,
             "tax_id": "903000000000356", "sku": "CIN-OTH-002",
             "item_custom_fields": [{"api_name": "cf_id", "value": "884213"}]},
        ],
    }

    short_items, other_items = _split_dn_items(dn["line_items"])
    assert [i["sku"] for i in short_items] == ["884213"]
    assert [i["sku"] for i in other_items] == ["117407"]

    other_payload = build_credit_note_payload(dn, invoice, other_items, RETURNS_WH_LOCATION_ID)
    assert "creditnote_number" not in other_payload
    assert other_payload["location_id"] == KARNATAKA_LOCATION_ID
    assert other_payload["custom_fields"] == [
        {"api_name": "cf_org_debit_note_no", "value": "FC5-DN772809"},
        {"api_name": "cf_goods_status", "value": "Pending"},
    ]
    [wrong_sku_li] = other_payload["line_items"]
    assert wrong_sku_li["item_id"] == "90300000081501" and wrong_sku_li["location_id"] == RETURNS_WH_LOCATION_ID
    assert wrong_sku_li["quantity"] == 50 and wrong_sku_li["rate"] == 132.67  # rate from invoice, not PDF
    assert wrong_sku_li["name"] == "Xtrim Unisex Bandana"  # name from invoice, not PDF
    assert wrong_sku_li["item_custom_fields"] == [
        {"api_name": "cf_asin", "value": ""}, {"api_name": "cf_fsin", "value": ""},
    ]  # ASIN/FSN explicitly blanked, not left for Zoho to auto-fill from the item master

    short_payload = build_credit_note_payload(dn, invoice, short_items, UNITED_WH_LOCATION_ID, billing_address_id="addr-jhajjar")
    assert short_payload["billing_address_id"] == "addr-jhajjar"
    assert {"api_name": "cf_goods_status", "value": "Shortage"} in short_payload["custom_fields"]
    [short_li] = short_payload["line_items"]
    assert short_li["item_id"] == "90300000081502" and short_li["location_id"] == UNITED_WH_LOCATION_ID

    # Unmatched SKU must raise, not silently drop a line item on a money path
    bad_items = [{**short_items[0], "sku": "000000"}]
    try:
        build_credit_note_payload(dn, invoice, bad_items, UNITED_WH_LOCATION_ID)
        raise AssertionError("expected ValueError for unmatched SKU")
    except ValueError:
        pass

    # create_credit_note: resolves billing_address_id via get_contact, creates one credit
    # note per non-empty warehouse bucket, attaching the source PDF to each
    client = InstamartZohoClient()
    client._session.get = lambda url, params, headers, timeout: FakeResponse(
        200, {"contact": {"addresses": [{"address_id": "addr-jhajjar", "zip": "124108"}]}},
    )
    post_state = {"cn_calls": 0, "attached_filenames": []}

    def fake_post(url, params, headers, timeout, json=None, files=None):
        if url.endswith("/attachment"):
            post_state["attached_filenames"].append(files["attachment"][0])
            return FakeResponse(200, {"code": 0})
        post_state["cn_calls"] += 1
        return FakeResponse(200, {"code": 0, "creditnote": {"creditnote_id": f"cn{post_state['cn_calls']}"}})

    client._session.post = fake_post
    created = client.create_credit_note(dn, invoice, pdf_bytes=b"%PDF-fake", pdf_filename="FC5-DN772809.pdf")
    assert post_state["cn_calls"] == 2
    assert {c["creditnote_id"] for c in created} == {"cn1", "cn2"}
    assert post_state["attached_filenames"] == ["FC5-DN772809.pdf", "FC5-DN772809.pdf"]

    print("instamart zoho_client demo OK")


if __name__ == "__main__":
    demo()
