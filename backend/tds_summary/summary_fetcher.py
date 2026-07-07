"""Fetch TDS payable summary (by section) then per-section detail rows."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from zoho_client import ZOHO_API_BASE, token_manager

SELECT_COLUMNS = json.dumps([
    {"field": "tds_section", "group": "report"},
    {"field": "tds_bcyamount", "group": "report"},
    {"field": "bcyamount_without_tds_deduction", "group": "report"},
    {"field": "bcyamount", "group": "report"},
])

# Zoho's report API returns the internal slug + a plain description, but not
# the official Income Tax Act section code shown in the Books UI heading.
# No API exposes that mapping, so it's kept here (extend as new sections show up).
SECTION_CODES = {
    "rent_land_furniture": "Section 393(1) Sl2(ii)D(b)",
    "contract_payments_individual_or_huf": "Section 393(1) Sl6(i)D(a)",
    "contract_payments_other_individual_or_huf": "Section 393(1) Sl6(i)D(b)",
    "technical_services": "Section 393(1) Sl6(iii)D(a)",
    "purchase_of_goods": "Section 393(1) Sl8(ii)",
}

ENTITY_NAMES = {
    "60009401567": "Evenflow Brands Tech Private Limited",
    "60014528875": "Everlong Brands Private Limited",
    "60014753441": "Fourth Second Private Limited",
    "60014871731": "Pepmart Brands International Private Limited",
}


def _get(url: str, params: dict) -> dict:
    headers = {"Authorization": f"Zoho-oauthtoken {token_manager.get_token()}"}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_summary(organization_id: str, from_date: str, to_date: str) -> list[dict]:
    data = _get(f"{ZOHO_API_BASE}/reports/tdspayablesummary", {
        "page": 1,
        "per_page": 500,
        "sort_column": "vendor_name",
        "sort_order": "A",
        "filter_by": "PaymentDate.CustomDate",
        "from_date": from_date,
        "to_date": to_date,
        "cash_based": "false",
        "group_by": "tds_section",
        "select_columns": SELECT_COLUMNS,
        "response_option": 1,
        "organization_id": organization_id,
    })
    sections = data["tdspayablesummary"]
    for section in sections:
        section["section_code"] = SECTION_CODES.get(section["tds_section"], section["tds_section"])
    return sections


def fetch_section_details(organization_id: str, section_code: str, from_date: str, to_date: str) -> list[dict]:
    data = _get(f"{ZOHO_API_BASE}/reports/tdspayablesummarydetails", {
        "page": 1,
        "per_page": 500,
        "filter_by": "PaymentDate.CustomDate",
        "from_date": from_date,
        "to_date": to_date,
        "cash_based": "false",
        "group_by": "tds_section",
        "section_code": section_code,
        "response_option": 1,
        "organization_id": organization_id,
    })
    return data.get("tdspayablesummarydetails", [])


def fetch_entity(organization_id: str, from_date: str, to_date: str) -> dict:
    sections = fetch_summary(organization_id, from_date, to_date)
    for section in sections:
        section["details"] = fetch_section_details(organization_id, section["tds_section"], from_date, to_date)
    total = sum(s["tds_bcyamount"] for s in sections)
    return {
        "organization_id": organization_id,
        "entity_name": ENTITY_NAMES.get(organization_id, organization_id),
        "sections": sections,
        "total_tds": total,
    }
