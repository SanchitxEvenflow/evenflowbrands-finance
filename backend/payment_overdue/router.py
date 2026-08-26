from fastapi import APIRouter

from config import settings
from payment_overdue.fetcher import get_overdue_summary

router = APIRouter(prefix="/payment-overdue", tags=["payment-overdue"])


@router.get("/summary")
def summary(organization_id: str = settings.org_id, force_refresh: bool = False):
    return get_overdue_summary(organization_id, force_refresh=force_refresh)
