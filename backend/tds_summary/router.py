import io
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel

from tds_summary.summary_fetcher import fetch_entity

router = APIRouter(prefix="/tds", tags=["tds"])


class TdsRequest(BaseModel):
    organization_ids: list[str]
    from_date: str
    to_date: str


def _fetch_all_entities(body: TdsRequest) -> list[dict]:
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(lambda org_id: fetch_entity(org_id, body.from_date, body.to_date), body.organization_ids))


def _consolidated(entities: list[dict]) -> dict:
    totals: dict[str, float] = defaultdict(float)
    meta: dict[str, dict] = {}
    for entity in entities:
        for section in entity["sections"]:
            key = section["tds_section"]
            totals[key] += section["tds_bcyamount"]
            meta[key] = {"section_code": section["section_code"], "tds_section_description": section["tds_section_description"]}
    return {
        "sections": [{"tds_section": k, "tds_bcyamount": v, **meta[k]} for k, v in totals.items()],
        "total_tds": sum(totals.values()),
    }


@router.post("/summary")
def get_summary(body: TdsRequest):
    entities = _fetch_all_entities(body)
    return {"entities": entities, "consolidated": _consolidated(entities)}


@router.post("/xlsx")
def get_xlsx(body: TdsRequest) -> StreamingResponse:
    entities = _fetch_all_entities(body)

    rows: list[dict] = []
    columns: list[str] = []
    for entity in entities:
        for section in entity["sections"]:
            for detail in section["details"]:
                branch = detail.pop("branch", None)
                row = {
                    "entity": entity["entity_name"],
                    "section_code": section["section_code"],
                    "section_description": section["tds_section_description"],
                    "section": section["tds_section"],
                    **detail,
                    "branch_name": (branch or {}).get("branch_name"),
                }
                rows.append(row)
                for key in row:
                    if key not in columns:
                        columns.append(key)

    wb = Workbook()
    ws = wb.active
    ws.append(columns)
    for row in rows:
        ws.append([row.get(col) for col in columns])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=tds_summary_{body.from_date}_to_{body.to_date}.xlsx"},
    )
