from creditnote.zoho_client import (
    KARNATAKA_LOCATION_ID,
    RETURNS_WH_LOCATION_ID,
    UNITED_WH_LOCATION_ID,
    MarketplaceZohoClient,
    _split_dn_items,
)


def _line_item_ean(line_item: dict) -> str | None:
    """EAN/UPC lives in item_custom_fields (api_name=cf_ean) — sku is an unrelated internal code."""
    for cf in line_item.get("item_custom_fields", []):
        if cf.get("api_name") == "cf_ean":
            return cf.get("value")
    return None


class BlinkitZohoClient(MarketplaceZohoClient):
    """Blinkit/PartnersBiz discrepancy-note credit-note flow: DN items are matched to invoice
    line items by EAN, and the line rate is derived from the DN's own scraped subtotal — the
    PDF gives amounts directly (Instamart's doesn't, so it takes rate/name off the invoice)."""

    LOGGER_NAME = "blinkit_zoho"
    ITEM_KEY_FIELD = "upc"

    def _index_invoice_line_items(self, invoice_line_items: list[dict]) -> dict:
        return {_line_item_ean(li): li for li in invoice_line_items if _line_item_ean(li)}

    def _build_line_item(self, item: dict, match: dict, location_id: str) -> dict:
        qty = item["qty"]
        return {
            "item_id": match["item_id"],
            "name": item["name"],
            "description": f"Reason: {item['reason']} | Remark: {item['remark']}",
            "quantity": qty,
            "rate": round(item["subtotal_excl_tax"] / qty, 2) if qty else 0,
            "tax_id": match.get("tax_id"),
            "location_id": location_id,
        }


blinkit_zoho = BlinkitZohoClient()


def demo() -> None:
    """ponytail self-check: Blinkit-specific EAN matching + rate-from-subtotal derivation,
    mocked session, no live network call. Shared retry/PO-lookup/create plumbing is covered
    by creditnote/zoho_client.py's own demo()."""

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
        "dn_id": "D25497DN26001725",
        "warehouse_name": "BCPL - Nagpur N1 Feeder Warehouse",
        "warehouse_address": "8/9 MIDC Kalmeshwar 441501",
        "original_invoice_id": "EL-KA-IN27-00609",
        "line_items": [
            {"upc": "8906157933838", "name": "Almond Pad", "qty": 50,
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

    client = BlinkitZohoClient()
    short_items, other_items = _split_dn_items(dn["line_items"])
    assert [i["upc"] for i in short_items] == ["8906157933838"]
    assert [i["upc"] for i in other_items] == ["8906158357510"]

    short_payload = client.build_credit_note_payload(dn, invoice, short_items, UNITED_WH_LOCATION_ID)
    assert "billing_address_id" not in short_payload  # not passed -> omitted, not defaulted
    assert short_payload["customer_id"] == "903000000000099"
    assert short_payload["invoice_id"] == "90300000079426"
    assert short_payload["location_id"] == KARNATAKA_LOCATION_ID
    assert short_payload["custom_fields"] == [
        {"api_name": "cf_org_debit_note_no", "value": "D25497DN26001725"},
        {"api_name": "cf_goods_status", "value": "Shortage"},
    ]
    [li] = short_payload["line_items"]
    assert li["item_id"] == "90300000081501" and li["location_id"] == UNITED_WH_LOCATION_ID
    assert li["quantity"] == 50 and li["rate"] == round(6159.50 / 50, 2)
    assert li["description"] == "Reason: Short quantity | Remark: SHORT"

    other_payload = client.build_credit_note_payload(
        dn, invoice, other_items, RETURNS_WH_LOCATION_ID, billing_address_id="addr-pune",
    )
    assert other_payload["billing_address_id"] == "addr-pune"
    assert {"api_name": "cf_goods_status", "value": "Pending"} in other_payload["custom_fields"]
    [li2] = other_payload["line_items"]
    assert li2["item_id"] == "90300000081502" and li2["location_id"] == RETURNS_WH_LOCATION_ID
    assert li2["description"] == "Reason: Product / Box / UPC Damage | Remark: MRP = 299"

    # Unmatched UPC raises, doesn't silently drop a line item on the money path
    bad_items = [{**short_items[0], "upc": "0000000000000"}]
    try:
        client.build_credit_note_payload(dn, invoice, bad_items, UNITED_WH_LOCATION_ID)
        raise AssertionError("expected ValueError for unmatched UPC")
    except ValueError:
        pass

    # create_credit_note: resolves billing_address_id via get_contact, one credit note per
    # non-empty warehouse bucket, PDF attached to each
    stored_addresses = [{"address_id": "addr-pune", "zip": "410506"}, {"address_id": "addr-lucknow", "zip": "227101"}]
    client._session.get = lambda url, params, headers, timeout: FakeResponse(
        200, {"contact": {"addresses": stored_addresses}},
    )
    post_state = {"cn_calls": 0, "attached_filenames": []}

    def fake_post(url, params, headers, timeout, json=None, files=None):
        if url.endswith("/attachment"):
            post_state["attached_filenames"].append(files["attachment"][0])
            return FakeResponse(200, {"code": 0})
        post_state["cn_calls"] += 1
        assert json["billing_address_id"] == "addr-pune"
        return FakeResponse(200, {"code": 0, "creditnote": {"creditnote_id": f"cn{post_state['cn_calls']}"}})

    client._session.post = fake_post
    client._session.put = lambda url, params, headers, timeout, json=None: FakeResponse(200, {"code": 0})
    created = client.create_credit_note(dn, invoice, pdf_bytes=b"%PDF-fake", pdf_filename="D25497DN26001725.pdf")
    assert post_state["cn_calls"] == 2
    assert {c["creditnote_id"] for c in created} == {"cn1", "cn2"}
    assert post_state["attached_filenames"] == ["D25497DN26001725.pdf", "D25497DN26001725.pdf"]

    # DN with items in only one bucket creates just one credit note
    short_only_dn = {**dn, "line_items": [dn["line_items"][0]]}
    post_state["cn_calls"] = 0
    client._session.post = lambda url, params, headers, timeout, json=None, files=None: (
        FakeResponse(200, {"code": 0}) if url.endswith("/attachment")
        else FakeResponse(200, {"code": 0, "creditnote": {"creditnote_id": "cn-solo"}})
    )
    created_solo = client.create_credit_note(short_only_dn, invoice, pdf_bytes=b"%PDF-fake", pdf_filename="solo.pdf")
    assert len(created_solo) == 1 and created_solo[0]["creditnote_id"] == "cn-solo"

    print("blinkit zoho_client demo OK")


if __name__ == "__main__":
    demo()
