from creditnote.zoho_client import (
    RETURNS_WH_LOCATION_ID,
    UNITED_WH_LOCATION_ID,
    MarketplaceZohoClient,
    _split_dn_items,
)


def _line_item_cf_id(line_item: dict) -> str | None:
    """The Instamart SKU code parsed off the PDF (see instamart/pdf_parser.py) matches
    item_custom_fields' cf_id ("ID") field — Zoho's own built-in `sku` field is a
    differently-formatted internal code (e.g. "CIN-HOP-LLF-L5") and doesn't match at all."""
    for cf in line_item.get("item_custom_fields", []):
        if cf.get("api_name") == "cf_id":
            return cf.get("value")
    return None


class InstamartZohoClient(MarketplaceZohoClient):
    """Instamart discrepancy-note credit-note flow: DN items are matched to invoice line items
    by the cf_id custom field, and rate/name are taken straight off the invoice line item — the
    PDF doesn't carry amounts (Blinkit's does, so it derives rate from the DN's own subtotal)."""

    LOGGER_NAME = "instamart_zoho"
    ITEM_KEY_FIELD = "sku"

    def _index_invoice_line_items(self, invoice_line_items: list[dict]) -> dict:
        return {_line_item_cf_id(li): li for li in invoice_line_items if _line_item_cf_id(li)}

    def _build_line_item(self, item: dict, match: dict, location_id: str) -> dict:
        return {
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
        }


instamart_zoho = InstamartZohoClient()


def demo() -> None:
    """ponytail self-check: Instamart-specific cf_id matching + rate-from-invoice + ASIN/FSN
    blanking, mocked session, no live network call. Shared retry/PO-lookup/create plumbing is
    covered by creditnote/zoho_client.py's own demo()."""

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
                raise __import__("requests").HTTPError(str(self.status_code))

    dn = {
        "dn_id": "FC5-DN772809",
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

    client = InstamartZohoClient()
    short_items, other_items = _split_dn_items(dn["line_items"])
    assert [i["sku"] for i in short_items] == ["884213"]
    assert [i["sku"] for i in other_items] == ["117407"]

    other_payload = client.build_credit_note_payload(dn, invoice, other_items, RETURNS_WH_LOCATION_ID)
    assert "billing_address_id" not in other_payload
    [li] = other_payload["line_items"]
    assert li["item_id"] == "90300000081501" and li["location_id"] == RETURNS_WH_LOCATION_ID
    assert li["quantity"] == 50 and li["rate"] == 132.67  # rate/name come from invoice, not PDF
    # ASIN/FSN must be explicitly blanked, not left for Zoho to auto-fill from item master
    assert {"api_name": "cf_asin", "value": ""} in li["item_custom_fields"]
    assert {"api_name": "cf_fsin", "value": ""} in li["item_custom_fields"]

    short_payload = client.build_credit_note_payload(
        dn, invoice, short_items, UNITED_WH_LOCATION_ID, billing_address_id="addr-jhajjar",
    )
    assert short_payload["billing_address_id"] == "addr-jhajjar"
    [li2] = short_payload["line_items"]
    assert li2["item_id"] == "90300000081502" and li2["location_id"] == UNITED_WH_LOCATION_ID

    # Unmatched SKU raises, doesn't silently drop a line item on the money path
    bad_items = [{**short_items[0], "sku": "000000"}]
    try:
        client.build_credit_note_payload(dn, invoice, bad_items, UNITED_WH_LOCATION_ID)
        raise AssertionError("expected ValueError for unmatched SKU")
    except ValueError:
        pass

    # create_credit_note: one credit note per non-empty bucket, PDF attached to each
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
    client._session.put = lambda url, params, headers, timeout, json=None: FakeResponse(200, {"code": 0})
    created = client.create_credit_note(dn, invoice, pdf_bytes=b"%PDF-fake", pdf_filename="FC5-DN772809.pdf")
    assert post_state["cn_calls"] == 2
    assert {c["creditnote_id"] for c in created} == {"cn1", "cn2"}
    assert post_state["attached_filenames"] == ["FC5-DN772809.pdf", "FC5-DN772809.pdf"]

    print("instamart zoho_client demo OK")


if __name__ == "__main__":
    demo()
