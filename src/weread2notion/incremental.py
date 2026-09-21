"""Non-destructive synchronization with state stored atomically in Notion blocks.

A linked marker on each managed block holds its source identity and baseline hash.
No local cache is authoritative. Unknown/legacy blocks are never adopted or deleted.
"""
import base64
import copy
import hashlib
import json
import time
from collections import Counter
from urllib.parse import urlsplit

import httpx
from notion_client.errors import APIResponseError, RequestTimeoutError

MARKER = "https://weread.qq.com/#w2n-v2-"
TITLE = "微信读书摘录 · WeRead2Notion"
DEFAULT_ANNOTATIONS = dict(bold=False, italic=False, strikethrough=False,
                           underline=False, code=False, color="default")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def text(content, url=None):
    item = {"type": "text", "text": {"content": content}}
    if url:
        item["text"]["link"] = {"url": url}
    return item


def rich_text(content):
    # Notion limits each rich-text item to 2,000 characters and arrays to 100 items.
    if len(content) > 180000:
        raise ValueError("笔记过长，保留原内容并停止该书同步")
    return [text(content[i:i + 2000]) for i in range(0, len(content), 2000)]


def marker(state, label=" · 同步记录"):
    encoded = base64.urlsafe_b64encode(json.dumps(state, separators=(",", ":"),
                                                 ensure_ascii=False).encode()).decode()
    url = MARKER + encoded
    if len(url) > 2000:
        raise ValueError("同步记录超过 Notion 链接长度限制")
    return text(label, url)


def read_state(block):
    body = block.get(block.get("type"), {})
    for item in body.get("rich_text", []):
        url = (item.get("text", {}).get("link") or {}).get("url", "")
        if url.startswith(MARKER):
            try:
                state = json.loads(base64.urlsafe_b64decode(url[len(MARKER):]))
                if not isinstance(state, dict) or state.get("v") != 2 or not isinstance(state.get("key"), str):
                    raise ValueError("未知同步记录版本")
                return state
            except (ValueError, TypeError) as error:
                raise ValueError("同步记录损坏，请保留该块并人工检查") from error
    return None


def writable_text(item):
    kind = item.get("type", "text")
    return {"type": kind, kind: copy.deepcopy(item[kind]),
            "annotations": {**DEFAULT_ANNOTATIONS, **item.get("annotations", {})}}


def clean_body(block):
    """Normalize only writable fields; include formatting in conflict detection."""
    kind = block["type"]
    body = block[kind]
    items = []
    for item in body.get("rich_text", []):
        url = (item.get("text", {}).get("link") or {}).get("url", "")
        if url.startswith(MARKER):
            continue
        value = writable_text(item)
        if value["type"] == "text":
            value["text"].setdefault("link", None)
        items.append(value)
    result = {"rich_text": items, "color": body.get("color", "default")}
    if kind == "callout":
        result["icon"] = copy.deepcopy(body.get("icon"))
    if kind.startswith("heading_"):
        result["is_toggleable"] = body.get("is_toggleable", False)
    return result


def fingerprint(block):
    return digest([block["type"], clean_body(block)])


def managed_block(key, block, extra=None, label=" · 同步记录"):
    block = copy.deepcopy(block)
    state = {"v": 2, "key": key, "hash": fingerprint(block), **(extra or {})}
    block[block["type"]]["rich_text"].append(marker(state, label))
    return block


def safe_deep_link(value):
    if not isinstance(value, str) or len(value) > 1800:
        return None
    parts = urlsplit(value)
    if parts.scheme == "weread" and parts.netloc in {"reading", "bestbookmark"}:
        return value
    if parts.scheme == "https" and parts.hostname in {"weread.qq.com", "i.weread.qq.com"}:
        return value
    return None


def source_records(book_id, bookmarks, reviews, chapters, book_url):
    """Return stable per-record keys, using official IDs; never text-as-identity."""
    result = []
    seen = set()
    chapter_map = {str(c["chapterUid"]): c for c in chapters}
    items = [("bookmark", b) for b in bookmarks]
    items += [("review", r.get("review") or r) for r in reviews]
    items.sort(key=lambda pair: (
        chapter_map.get(str(pair[1].get("chapterUid")), {}).get("chapterIdx", 10**9),
        int(str(pair[1].get("range") or "0").split("-")[0] or 0)
        if str(pair[1].get("range") or "0").split("-")[0].isdigit() else 0,
    ))
    used_chapters = set()
    paths = {}
    ancestors = []
    for chapter in sorted(chapters, key=lambda c: c.get("chapterIdx", 10**9)):
        level = chapter.get("level") or 1
        while ancestors and (ancestors[-1].get("level") or 1) >= level:
            ancestors.pop()
        ancestors.append(chapter)
        paths[str(chapter["chapterUid"])] = list(ancestors)
    for kind, item in items:
        identifier = item.get("bookmarkId" if kind == "bookmark" else "reviewId")
        if identifier is None or str(identifier) == "":
            raise ValueError("微信读书记录缺少稳定 ID，停止该书同步，避免错误匹配")
        key = f"{kind}:{identifier}"
        if key in seen:
            raise ValueError("微信读书返回重复笔记 ID，请重新运行")
        seen.add(key)
        chapter_id = str(item.get("chapterUid", ""))
        chapter = chapter_map.get(chapter_id, {})
        for section in paths.get(chapter_id, []):
            section_id = str(section["chapterUid"])
            if section_id in used_chapters:
                continue
            used_chapters.add(section_id)
            heading = f"heading_{min(3, max(1, section.get('level') or 1))}"
            result.append((f"chapter:{section_id}", {
                "object": "block", "type": heading,
                heading: {"rich_text": rich_text(section.get("title") or "章节")},
            }))
        content = item.get("markText" if kind == "bookmark" else "content") or ""
        if item.get("abstract"):
            content += "\n\n原文：" + item["abstract"]
        # A chapter label remains correct even if the API later reassigns a note.
        if chapter:
            content = chapter.get("title", "") + "\n" + content
        exact = safe_deep_link(item.get("deepLink"))
        chapter_link = safe_deep_link(chapter.get("deepLink"))
        label, url = (("在微信读书中查看", exact) if exact else
                      (("查看所在章节", chapter_link) if chapter_link else ("打开书籍", book_url)))
        body = {"rich_text": rich_text(content) + [text("\n↗ " + label, url)],
                "icon": {"type": "emoji", "emoji": "〰️" if kind == "bookmark" else "✍️"}}
        result.append((key, {"object": "block", "type": "callout", "callout": body}))
    return result


class NotionStore:
    def __init__(self, client, dry_run=False):
        self.client = client
        self.dry_run = dry_run
        self.counts = Counter()

    def request(self, path, method="GET", body=None, query=None):
        read = method == "GET" or path.endswith("/query")
        if self.dry_run and not read:
            raise RuntimeError("dry-run 禁止写入")
        for attempt in range(4):
            self.counts["read" if read else "write"] += 1
            try:
                return self.client.request(path=path, method=method, body=body, query=query)
            except APIResponseError as error:
                status = error.status
                # Ambiguous non-idempotent writes are never retried here.
                retryable = status == 429 or (read and status >= 500)
                if not retryable or attempt == 3:
                    raise
                delay = 2 ** attempt
                if status == 429:
                    try:
                        delay = min(60, max(1, float(error.headers.get("retry-after", delay))))
                    except (AttributeError, ValueError):
                        pass
                time.sleep(delay)
            except (RequestTimeoutError, httpx.TransportError):
                if not read or attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    def blocks(self, parent):
        query = {"page_size": 100}
        seen = set()
        while True:
            response = self.request(f"blocks/{parent}/children", query=query)
            yield from response.get("results", [])
            if not response.get("has_more"):
                return
            cursor = response.get("next_cursor")
            if not cursor or cursor in seen:
                raise RuntimeError("Notion 分页游标未前进")
            seen.add(cursor)
            query["start_cursor"] = cursor

    def append(self, parent, block, after=None):
        state = read_state(block)
        body = {"children": [block]}
        if after:
            body["position"] = {"type": "after_block", "after_block": {"id": after}}
        try:
            response = self.request(f"blocks/{parent}/children", "PATCH", body)
            if len(response.get("results", [])) != 1:
                raise RuntimeError("Notion 返回的块数量不完整")
            return response["results"][0]
        except Exception:
            # The server may have committed a timed-out append. Recover by key,
            # never issue a second append before observing the first result.
            found = [b for b in self.blocks(parent) if (read_state(b) or {}).get("key") == state["key"]]
            if len(found) == 1 and fingerprint(found[0]) == fingerprint(block):
                return found[0]
            raise

    def update(self, old, new):
        # Re-read immediately before writing to reduce the concurrent-edit window.
        current = self.request(f"blocks/{old['id']}")
        if fingerprint(current) != fingerprint(old) or read_state(current) != read_state(old):
            raise RuntimeError("同步过程中检测到 Notion 编辑，已停止该书写入，请重试")
        kind = new["type"]
        self.request(f"blocks/{old['id']}", "PATCH", {kind: new[kind]})


def managed_index(blocks):
    result = {}
    for block in blocks:
        state = read_state(block)
        if state:
            if state["key"] in result:
                raise RuntimeError("存在重复同步记录，保留内容并停止自动处理")
            result[state["key"]] = block
    return result


def find_area(store, page_id, book_id):
    found = []
    for block in store.blocks(page_id):
        if block.get("type") != "child_page":
            continue
        # Inspect child pages, including renamed managed pages. Titles aren't IDs.
        for child in store.blocks(block["id"]):
            state = read_state(child)
            if state and state["key"] == f"root:{book_id}":
                found.append((block["id"], child, state))
                break
    if len(found) > 1:
        raise RuntimeError("该书存在多个同步区域，请人工确认，未修改内容")
    return found[0] if found else None


def ensure_area(store, page_id, book_id, area=None):
    if area:
        return area
    checkpoint = managed_block(f"root:{book_id}", {
        "object": "block", "type": "paragraph", "paragraph": {"rich_text": [text(
            "此区域由微信读书同步维护。原页面、旧笔记和评论均保留；请勿删除同步记录链接。")]}},
        {"cursor": None, "audited": 0})
    try:
        page = store.request("pages", "POST", {
            "parent": {"type": "page_id", "page_id": page_id},
            "properties": {"title": {"type": "title", "title": [text(TITLE)]}},
            "children": [checkpoint],
        })
        blocks = list(store.blocks(page["id"]))
        root = next(b for b in blocks if (read_state(b) or {}).get("key") == f"root:{book_id}")
        return page["id"], root, read_state(root)
    except Exception:
        recovered = find_area(store, page_id, book_id)
        if recovered:
            return recovered
        raise


def reconcile(store, area_id, records, removed=()):
    """Never delete. Preserve local edits; publish source changes as conflict copies."""
    keys = [key for key, _ in records]
    if len(keys) != len(set(keys)) or set(keys).intersection(removed):
        raise RuntimeError("源端记录重复或同时返回更新与删除，未修改笔记")
    index = managed_index(store.blocks(area_id)) if area_id else {}
    stats = Counter()
    after = next((b["id"] for k, b in index.items() if k.startswith("root:")), None)
    for key, desired in records:
        old = index.get(key)
        expected = fingerprint(desired)
        if old is None:
            stats["added"] += 1
            if not store.dry_run:
                new = store.append(area_id, managed_block(key, desired), after=after)
                index[key] = new
                after = new["id"]
            continue
        state = read_state(old)
        after = old["id"]
        actual = fingerprint(old)
        if actual == expected and not state.get("deleted"):
            # If user manually copied the source version, adopt that as baseline.
            if state.get("hash") != expected and not store.dry_run:
                store.update(old, managed_block(key, desired))
            stats["unchanged"] += 1
        elif state.get("hash") == expected and not state.get("deleted"):
            # Source unchanged: user edits are authoritative; do not overwrite.
            stats["local_edits"] += 1
        elif actual == state.get("hash") and old["type"] == desired["type"]:
            stats["updated"] += 1
            if not store.dry_run:
                store.update(old, managed_block(key, desired))
        else:
            stats["conflicts"] += 1
            proposal_key = "conflict:" + digest([key, expected])
            if proposal_key not in index:
                proposal = copy.deepcopy(desired)
                proposal[proposal["type"]]["rich_text"].insert(0, text("⚠️ 微信读书新版本（原内容已保留）\n"))
                if not store.dry_run:
                    index[proposal_key] = store.append(area_id, managed_block(proposal_key, proposal), after=after)
    for key in removed:
        old = index.get(key)
        if not old or read_state(old).get("deleted"):
            continue
        stats["source_deleted"] += 1
        if not store.dry_run:
            state = {**read_state(old), "deleted": True}
            kind = old["type"]
            body = clean_body(old)
            # Preserve local edits and the original baseline; only mark deletion.
            body["rich_text"].append(marker(state, " · 源端已删除（保留内容）"))
            store.update(old, {"type": kind, kind: body})
    return stats


def property_value(prop):
    kind = prop.get("type") or next((k for k in (
        "title", "rich_text", "number", "url", "select", "status", "multi_select", "date", "checkbox"
    ) if k in prop), None)
    value = prop.get(kind)
    if kind in {"title", "rich_text"}:
        return "".join(i.get("plain_text", i.get("text", {}).get("content", "")) for i in value or [])
    if kind in {"select", "status"}:
        return (value or {}).get("name")
    if kind == "multi_select":
        return sorted(i["name"] for i in value or [])
    if kind == "date":
        # API may normalize UTC timestamps; compare instants, not spellings.
        from datetime import datetime
        if not value:
            return None
        def normalize(raw):
            if not raw:
                return raw
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
            except ValueError:
                return raw
        return (normalize(value.get("start")), normalize(value.get("end")))
    return value


def property_changes(current, desired):
    return {name: prop for name, prop in desired.items()
            if property_value(current.get(name, {})) != property_value(prop)}
