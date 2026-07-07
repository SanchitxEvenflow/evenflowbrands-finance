"""Fetch TDS payable summary (by section) then per-section detail rows."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from config import settings
from zoho_client import ZOHO_API_BASE, token_manager

SELECT_COLUMNS = json.dumps([
    {"field": "tds_section", "group": "report"},
    {"field": "tds_bcyamount", "group": "report"},
    {"field": "bcyamount_without_tds_deduction", "group": "report"},
    {"field": "bcyamount", "group": "report"},
])


def _get(url: str, params: dict) -> dict:
    headers = {"Authorization": f"Zoho-oauthtoken {token_manager.get_token()}"}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_summary(organization_id: str, filter_by: str = "PaymentDate.PreviousMonth") -> list[dict]:
    data = _get(f"{ZOHO_API_BASE}/reports/tdspayablesummary", {
        "page": 1,
        "per_page": 500,
        "sort_column": "vendor_name",
        "sort_order": "A",
        "filter_by": filter_by,
        "cash_based": "false",
        "group_by": "tds_section",
        "select_columns": SELECT_COLUMNS,
        "response_option": 1,
        "organization_id": organization_id,
    })
    return data["tdspayablesummary"]


def fetch_section_details(organization_id: str, section_code: str, filter_by: str = "PaymentDate.PreviousMonth") -> list[dict]:
    data = _get(f"{ZOHO_API_BASE}/reports/tdspayablesummarydetails", {
        "page": 1,
        "per_page": 500,
        "filter_by": filter_by,
        "cash_based": "false",
        "group_by": "tds_section",
        "section_code": section_code,
        "response_option": 1,
        "organization_id": organization_id,
    })
    return data.get("tdspayablesummarydetails", data.get("tds_payable_summary_details", []))


def fetch_all(organization_id: str, filter_by: str = "PaymentDate.PreviousMonth") -> dict:
    sections = fetch_summary(organization_id, filter_by)
    for section in sections:
        section["details"] = fetch_section_details(organization_id, section["tds_section"], filter_by)
    return {"sections": sections}


if __name__ == "__main__":
    result = fetch_all(settings.org_id)
    out_path = Path(__file__).resolve().parent / "tds_summary_output.json"
    out_path.write_text(json.dumps(result, indent=2))
    total = sum(s["tds_bcyamount"] for s in result["sections"])
    print(f"sections={len(result['sections'])}  total_tds={total:.2f}  -> {out_path}")
