"""Bucket keys for cached messages — shared by /api/aggregates and /api/cache/messages."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

GroupBy = Literal["domain", "sender", "age", "newsletter"]


def domain_from_from_addr(addr: str) -> str:
    if not addr:
        return "(unknown)"
    m = re.search(r"@([\w.-]+\.[a-zA-Z]{2,})", addr)
    if m:
        return m.group(1).lower()
    m2 = re.search(r"<([^>]+)>", addr)
    if m2:
        return domain_from_from_addr(m2.group(1))
    return "(unknown)"


def age_bucket(ts_ms: int) -> str:
    if not ts_ms:
        return "unknown"
    age_s = time.time() - ts_ms / 1000.0
    if age_s < 86400 * 7:
        return "< 7d"
    if age_s < 86400 * 30:
        return "< 30d"
    if age_s < 86400 * 365:
        return "< 1y"
    return "> 1y"


def parse_label_ids(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return [str(x) for x in data] if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    return []


def bucket_key(
    group_by: GroupBy,
    *,
    from_addr: str,
    internal_date: int,
    label_ids: list[str],
) -> str:
    if group_by == "domain":
        return domain_from_from_addr(from_addr)
    if group_by == "sender":
        return (from_addr or "")[:200] if from_addr else "(unknown)"
    if group_by == "age":
        return age_bucket(internal_date)
    promo = "CATEGORY_PROMOTIONS" in label_ids or "CATEGORY_SOCIAL" in label_ids
    return "likely_newsletter" if promo else "other"


def row_bucket_key(group_by: GroupBy, row: dict[str, Any]) -> str:
    from_addr = row.get("from_addr") or ""
    internal_date = int(row.get("internal_date") or 0)
    labels = parse_label_ids(row.get("label_ids"))
    return bucket_key(
        group_by,
        from_addr=from_addr,
        internal_date=internal_date,
        label_ids=labels,
    )
