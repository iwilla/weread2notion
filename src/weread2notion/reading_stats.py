"""Optional daily reading history, keyed by Shanghai calendar date."""
import math
from collections import Counter
from datetime import datetime, timedelta, timezone

from .incremental import property_changes, property_value, text

SHANGHAI = timezone(timedelta(hours=8))
SCHEMA = {"Name": "title", "Date": "date", "SyncKey": "rich_text",
          "ReadingSeconds": "number", "ReadingMinutes": "number"}


def daily_records(response, month):
    buckets = response.get("readTimes")
    if not isinstance(buckets, dict):
        raise RuntimeError("月度统计缺少 readTimes 对象；保留已有记录")
    records = {}
    for timestamp, raw_seconds in buckets.items():
        try:
            seconds = float(raw_seconds)
            day = datetime.fromtimestamp(int(timestamp), SHANGHAI).date()
        except (ValueError, TypeError, OverflowError, OSError):
            raise RuntimeError("每日阅读统计包含无效日期或时长") from None
        if not math.isfinite(seconds) or seconds < 0 or day.strftime("%Y-%m") != month:
            raise RuntimeError("每日阅读统计日期超出请求月份或时长无效")
        key = "daily:" + day.isoformat()
        if key in records:
            raise RuntimeError("月度统计包含重复日期，停止写入")
        records[key] = {
            "Name": {"title": [text(day.isoformat())]},
            "Date": {"date": {"start": day.isoformat()}},
            "SyncKey": {"rich_text": [text(key)]},
            "ReadingSeconds": {"number": seconds},
            "ReadingMinutes": {"number": round(seconds / 60, 2)},
        }
    return records


def page_index(store, source_id):
    result, seen = {}, set()
    body = {"page_size": 100, "filter": {
        "property": "SyncKey", "rich_text": {"starts_with": "daily:"}}}
    while True:
        response = store.request(f"data_sources/{source_id}/query", "POST", body)
        for page in response.get("results", []):
            key = property_value(page.get("properties", {}).get("SyncKey", {}))
            if key in result:
                raise RuntimeError("每日统计存在重复 SyncKey，未自动修改")
            result[key] = page
        if not response.get("has_more"):
            return result
        cursor = response.get("next_cursor")
        if not cursor or cursor in seen:
            raise RuntimeError("每日统计分页游标未前进")
        seen.add(cursor)
        body["start_cursor"] = cursor


def sync_daily_reading(gateway, store, source_id, now=None):
    schema = store.request(f"data_sources/{source_id}").get("properties", {})
    if any(schema.get(name, {}).get("type") != kind for name, kind in SCHEMA.items()):
        raise RuntimeError("每日统计数据库需要 Name 标题、Date 日期、SyncKey 文本和 ReadingSeconds/ReadingMinutes 数字属性")
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous = (first - timedelta(days=1)).replace(day=1)
    desired = {}
    # Re-read both months to recover late uploads and month-boundary interruptions.
    # Never manufacture zero rows for dates missing from the API response.
    for month in (previous, first):
        response = gateway.request("/readdata/detail", mode="monthly", baseTime=int(month.timestamp()))
        desired.update(daily_records(response, month.strftime("%Y-%m")))
    index = page_index(store, source_id)
    totals = Counter()
    for key, properties in sorted(desired.items()):
        page = index.get(key)
        if page:
            changes = property_changes(page.get("properties", {}), properties)
            if changes:
                totals["daily_updated"] += 1
                if not store.dry_run:
                    store.request(f"pages/{page['id']}", "PATCH", {"properties": changes})
            else:
                totals["daily_unchanged"] += 1
        else:
            totals["daily_added"] += 1
            if not store.dry_run:
                try:
                    created = store.request("pages", "POST", {
                        "parent": {"type": "data_source_id", "data_source_id": source_id},
                        "properties": properties,
                    })
                    index[key] = created
                except Exception:
                    # Observe an ambiguous create before any future retry.
                    found = page_index(store, source_id).get(key)
                    if not found or property_changes(found.get("properties", {}), properties):
                        raise
                    index[key] = found
    print(f"{'预览' if store.dry_run else '同步'}每日阅读统计: {dict(totals)}")
    return totals
