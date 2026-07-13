# ponytail: single JSON file, no locking across processes — fine for one
# backend instance; move to a real DB if this ever needs multi-instance writes.
import json
import logging
import os
import threading
from datetime import datetime, timezone

from grnpush.mapper import BillGroup, merge_bill_groups

logger = logging.getLogger("grn_push.store")

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_STORE_PATH = os.path.join(_DATA_DIR, "pending_bills.json")

_lock = threading.Lock()


def _read() -> dict:
    if not os.path.exists(_STORE_PATH):
        return {}
    with open(_STORE_PATH, "r") as f:
        return json.load(f)


def _write(data: dict) -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_STORE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_all() -> dict[str, dict]:
    with _lock:
        return _read()


def get(bill_number: str) -> dict | None:
    with _lock:
        return _read().get(bill_number)


def upsert_merged(group: BillGroup) -> BillGroup:
    """Merge `group` into any existing pending entry sharing its bill_number, else insert fresh."""
    with _lock:
        data = _read()
        existing = data.get(group.bill_number)
        if existing:
            existing_group = BillGroup(**{k: v for k, v in existing.items() if k in BillGroup.model_fields})
            merged = merge_bill_groups(existing_group, group)
        else:
            merged = group

        record = merged.model_dump()
        record["status"] = "pending"
        record["issues"] = []
        record["updated_at"] = datetime.now(timezone.utc).isoformat()

        data[group.bill_number] = record
        _write(data)
        logger.info("Queue upsert: %s (%d GRN(s))", group.bill_number, len(merged.grn_codes))
        return merged


def patch(bill_number: str, **fields) -> dict:
    with _lock:
        data = _read()
        if bill_number not in data:
            raise KeyError(bill_number)
        data[bill_number].update(fields)
        data[bill_number]["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write(data)
        return data[bill_number]


def delete(bill_number: str) -> None:
    with _lock:
        data = _read()
        data.pop(bill_number, None)
        _write(data)
        logger.info("Queue delete: %s", bill_number)
