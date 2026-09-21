import argparse
import logging
import os
import re
import time
from notion_client import Client
import requests
from datetime import datetime, timezone
import hashlib
from dotenv import load_dotenv
from notion_client.errors import APIResponseError
from .incremental import (
    NotionStore, source_records, find_area, ensure_area, reconcile,
    clean_body, marker, property_changes, property_value,
)
from collections import Counter
from .blocks import (
    get_callout,
    get_date,
    get_heading,
    get_icon,
    get_multi_select,
    get_number,
    get_quote,
    get_rich_text,
    get_select,
    get_status,
    get_title,
    get_url,
)

client = None
data_source_id = None
data_source_property_types = {}
data_source_property_configs = {}
title_property_name = None
skipped_property_names = set()
weread = None

load_dotenv()
WEREAD_URL = "https://weread.qq.com/"
WEREAD_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
WEREAD_SKILL_VERSION = "1.0.4"
NOTION_VERSION = "2026-03-11"
BOOKMARK_CALLOUT_ICON = "〰️"
NOTE_CALLOUT_ICON = "✍️"
NOTION_TOKEN_PATTERN = re.compile(r"^(secret|ntn)_[A-Za-z0-9_-]{20,}$")
WEREAD_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{10,}$")
NOTION_ID_PATTERN = re.compile(
    r"^[a-f0-9]{32}$|^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
NOTION_ID_IN_TEXT_PATTERN = re.compile(
    r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    re.IGNORECASE,
)


PROPERTY_ALIASES = {
    "作者": "Author", "分类": "Categories", "评分": "Rating",
    "链接": "URL", "阅读时长": "ReadingTime", "阅读进度": "Progress",
    "状态": "Status", "时间": "Date",
}


def resolve_property_name(name):
    if name in data_source_property_types:
        return name
    return PROPERTY_ALIASES.get(name, name)


class ConfigError(Exception):
    pass


class UpgradeRequired(RuntimeError):
    pass


def emit_error(message):
    if os.getenv("GITHUB_ACTIONS") == "true":
        safe = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{safe}")
    else:
        print(f"配置错误: {message}")


def fail_config(message):
    emit_error(message)
    raise ConfigError(message)


def clean_secret_value(name, required=False):
    raw = os.getenv(name)
    if raw is None:
        if required:
            fail_config(f"缺少 {name}，请在 GitHub Actions Secrets 中配置")
        return None
    value = re.sub(r"\s+", "", raw)
    if value:
        os.environ[name] = value
        return value
    if required:
        fail_config(f"{name} 为空，请检查 GitHub Actions Secrets")
    os.environ.pop(name, None)
    return None


def validate_regex(name, value, pattern, hint):
    if value and not pattern.search(value):
        fail_config(f"{name} 格式不正确：{hint}")
    return value


def validate_secret_inputs():
    weread_api_key = clean_secret_value("WEREAD_API_KEY", required=True)
    notion_token = clean_secret_value("NOTION_TOKEN", required=True)
    notion_page = clean_secret_value("NOTION_PAGE")
    notion_database_id = clean_secret_value("NOTION_DATABASE_ID")
    notion_data_source_id = clean_secret_value("NOTION_DATA_SOURCE_ID")

    validate_regex(
        "WEREAD_API_KEY",
        weread_api_key,
        WEREAD_API_KEY_PATTERN,
        "应为微信读书 Gateway API Key，不能包含空格或换行",
    )
    validate_regex(
        "NOTION_TOKEN",
        notion_token,
        NOTION_TOKEN_PATTERN,
        "应以 secret_ 或 ntn_ 开头，不能包含空格或换行",
    )
    for name, value in (
        ("NOTION_DATA_SOURCE_ID", notion_data_source_id),
        ("NOTION_DATABASE_ID", notion_database_id),
    ):
        validate_regex(
            name,
            value,
            NOTION_ID_PATTERN,
            "应为 32 位 Notion ID 或带连字符的 UUID",
        )
    if notion_page and not NOTION_ID_IN_TEXT_PATTERN.search(notion_page):
        fail_config("NOTION_PAGE 格式不正确：请填写 Notion 页面链接、数据库链接或 ID")
    if not (notion_data_source_id or notion_page or notion_database_id):
        fail_config(
            "缺少 NOTION_PAGE / NOTION_DATA_SOURCE_ID / NOTION_DATABASE_ID，"
            "请至少配置其中一个"
        )
    return {
        "weread_api_key": weread_api_key,
        "notion_token": notion_token,
    }


class WeReadGatewayClient:
    def __init__(self, api_key):
        if not api_key:
            fail_config("没有找到 WEREAD_API_KEY，请在 GitHub Actions Secrets 中配置")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def request(self, api_name, **kwargs):
        payload = {"api_name": api_name, "skill_version": WEREAD_SKILL_VERSION, **kwargs}
        for attempt in range(4):
            try:
                response = self.session.post(WEREAD_GATEWAY_URL, json=payload, timeout=30)
                response.raise_for_status()
                data = response.json()
                if data.get("upgrade_info"):
                    raise UpgradeRequired("微信读书接口要求升级 skill_version，请更新程序后重试")
                if data.get("errcode", 0) != 0:
                    raise RuntimeError(f"微信读书请求失败: {api_name}, errcode={data.get('errcode')}")
                return data
            except requests.HTTPError:
                if response.status_code != 429 and response.status_code < 500:
                    raise
                if attempt == 3:
                    raise
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 3:
                    raise
            time.sleep(2 ** attempt)


def get_range_start(item):
    note_range = item.get("range") or ""
    try:
        return int(note_range.split("-")[0] or 0)
    except (ValueError, TypeError):
        return 0


def get_note_sort_key(item, chapter=None):
    chapter_uid = item.get("chapterUid", 1)
    chapter_info = None
    if chapter:
        chapter_info = chapter.get(chapter_uid) or chapter.get(str(chapter_uid))
    chapter_idx = (
        chapter_info.get("chapterIdx", 1000000)
        if chapter_info
        else chapter_uid
    )
    return (chapter_idx, get_range_start(item))


def get_bookmark_list(bookId):
    """获取我的划线"""
    data = weread.request("/book/bookmarklist", bookId=bookId)
    updated = data.get("updated") or []
    return sorted(updated, key=get_note_sort_key)


def get_read_info(bookId):
    data = weread.request("/book/getprogress", bookId=bookId)
    book = data.get("book")
    if not isinstance(book, dict) or to_number(book.get("progress")) is None:
        raise RuntimeError("微信读书阅读进度响应不完整，保留已有阅读信息")
    progress = to_number(book["progress"])
    reading_progress = normalize_reading_progress(progress)
    finish_time = book.get("finishTime") or 0
    update_time = book.get("updateTime") or 0
    if finish_time or progress >= 100:
        marked_status = 4
    elif update_time or book.get("isStartReading") or progress > 0:
        marked_status = 2
    else:
        marked_status = 1
    return {
        "markedStatus": marked_status,
        "readingTime": to_number(book.get("recordReadingTime")),
        "readingProgress": reading_progress,
        "finishedDate": finish_time,
    }


def normalize_reading_progress(value):
    value = to_number(value) or 0
    # Gateway progress is always 0..100; 1 means one percent.
    return round(min(max(value / 100, 0), 1), 4)


def normalize_rating(value):
    value = value or 0
    if value > 100:
        return value / 1000
    if value > 10:
        return value / 10
    return value


def get_bookinfo(bookId):
    """获取书的详情"""
    data = weread.request("/book/info", bookId=bookId)
    isbn = data.get("isbn")
    newRating = normalize_rating(data["newRating"]) if data.get("newRating") is not None else None
    return (isbn, newRating)


def get_review_list(bookId):
    """获取笔记"""
    reviews_data = []
    hasMore = 1
    synckey = 0
    while hasMore:
        data = weread.request("/review/list/mine", bookid=bookId, synckey=synckey, count=100)
        hasMore = data.get("hasMore", 0)
        synckey = data.get("synckey", 0)
        batch = data.get("reviews") or []
        reviews_data.extend(batch)
        if not batch:
            hasMore = 0
    summary = list(filter(lambda x: (x.get("review") or {}).get("type") == 4, reviews_data))
    reviews = list(filter(lambda x: (x.get("review") or {}).get("type") == 1, reviews_data))
    reviews = list(map(lambda x: x.get("review") or {}, reviews))
    reviews = list(
        map(
            lambda x: {
                **x,
                "markText": x.pop("content", ""),
                "_callout_icon": NOTE_CALLOUT_ICON,
            },
            reviews,
        )
    )
    return summary, reviews


def get_chapter_info(bookId):
    """获取章节信息"""
    data = weread.request("/book/chapterinfo", bookId=bookId)
    chapters = data.get("chapters") or []
    return {item["chapterUid"]: item for item in chapters if "chapterUid" in item}


def book_properties(bookName, bookId, sort, author, isbn, rating, categories):
    raw_properties = {
        title_property_name: bookName,
        "BookId": bookId,
        "ISBN": isbn,
        "链接": f"https://weread.qq.com/web/reader/{calculate_book_str_id(bookId)}",
        "作者": author,
        "Sort": sort,
        "评分": rating,
    }
    if categories != None:
        raw_properties["分类"] = categories
    read_info = (
        get_read_info(bookId=bookId)
        if has_any_property(("状态", "阅读时长", "阅读进度", "时间"))
        else None
    )
    if read_info != None:
        markedStatus = read_info.get("markedStatus", 0)
        readingTime = read_info.get("readingTime")
        readingProgress = read_info.get("readingProgress", 0)
        if readingTime is not None:
            minutes = max(0, int(readingTime)) // 60
            hours, minutes = divmod(minutes, 60)
            raw_properties["阅读时长"] = (
                f"{hours}时{minutes}分" if hours else f"{minutes}分"
            )
        raw_properties["状态"] = "读完" if markedStatus == 4 else "在读"
        raw_properties["阅读进度"] = readingProgress
        if read_info.get("finishedDate"):
            raw_properties["时间"] = datetime.fromtimestamp(
                read_info["finishedDate"], timezone.utc
            ).isoformat()

    return build_notion_properties(raw_properties)


def create_book_page(properties, cover):
    """Create a stable page without advancing the note synchronization cursor."""
    if not cover or not cover.startswith("http"):
        cover = "https://www.notion.so/icons/book_gray.svg"
    properties = {name: value for name, value in properties.items() if name != "Sort"}
    icon = get_icon(cover)
    response = client.pages.create(
        parent={"type": "data_source_id", "data_source_id": data_source_id},
        icon=icon, cover=icon, properties=properties,
    )
    return response["id"]


def list_page_blocks(page_id):
    params = {"block_id": page_id, "page_size": 100}
    while True:
        response = client.blocks.children.list(**params)
        yield from response.get("results", [])
        if not response.get("has_more"):
            return
        params["start_cursor"] = response["next_cursor"]


def add_children(id, children):
    results = []
    for i in range(0, len(children), 100):
        time.sleep(0.3)
        response = client.blocks.children.append(
            block_id=id, children=children[i : i + 100]
        )
        results.extend(response.get("results"))
    if len(results) != len(children):
        raise RuntimeError("Notion 返回的笔记块数量不完整，请重新运行同步")
    return results


def add_grandchild(grandchild, results):
    for key, value in grandchild.items():
        time.sleep(0.3)
        id = results[key].get("id")
        response = client.blocks.children.append(block_id=id, children=[value])
        if len(response.get("results", [])) != 1:
            raise RuntimeError("Notion 返回的批注块数量不完整，请重新运行同步")


def get_notebooklist():
    """获取笔记本列表"""
    books = []
    hasMore = 1
    lastSort = None
    seen_sorts = set()
    while hasMore:
        params = {"count": 100}
        if lastSort is not None:
            params["lastSort"] = lastSort
        data = weread.request("/user/notebooks", **params)
        hasMore = data.get("hasMore", 0)
        if not isinstance(data.get("books"), list):
            raise RuntimeError("微信读书笔记本响应不完整")
        batch = data["books"]
        books.extend(batch)
        if batch:
            lastSort = batch[-1].get("sort")
            if hasMore and (lastSort is None or lastSort in seen_sorts):
                raise RuntimeError("微信读书笔记本分页游标未前进")
            seen_sorts.add(lastSort)
        elif hasMore:
            raise RuntimeError("微信读书笔记本分页返回空页")
    books.sort(key=lambda x: x.get("sort") or 0)
    return books


def get_sort():
    """获取 data source 中的最新时间"""
    filter = build_is_not_empty_filter("Sort")
    sorts = [
        {
            "property": "Sort",
            "direction": "descending",
        }
    ]
    response = query_data_source(filter=filter, sorts=sorts, page_size=1)
    if len(response.get("results")) == 1:
        return get_number_property_value(
            response.get("results")[0].get("properties").get("Sort")
        )
    return 0


def get_children(chapter, summary, bookmark_list):
    children = []
    grandchild = {}
    all_chapters = []
    if chapter:
        for uid, info in chapter.items():
            item = dict(info)
            item["chapterUid"] = item.get("chapterUid", uid)
            all_chapters.append(item)
        all_chapters.sort(key=lambda x: x.get("chapterIdx", 0))
    chapter_nodes = {node.get("chapterUid"): node for node in all_chapters}

    def get_ancestor_chain(current_chapter_info):
        if not current_chapter_info:
            return []
        try:
            current_pos = all_chapters.index(current_chapter_info)
        except ValueError:
            return [current_chapter_info]

        chain = []
        target_level = current_chapter_info.get("level", 1)
        for index in range(current_pos - 1, -1, -1):
            candidate = all_chapters[index]
            if candidate.get("level", 1) < target_level:
                chain.insert(0, candidate)
                target_level = candidate.get("level", 1)
                if target_level <= 1:
                    break
        chain.append(current_chapter_info)
        return chain

    if chapter:
        grouped_bookmarks = []
        last_uid = None
        current_group = None

        for data in bookmark_list:
            uid = data.get("chapterUid", 1)
            if uid != last_uid:
                if current_group:
                    grouped_bookmarks.append(current_group)
                info = chapter.get(uid) or chapter.get(str(uid))
                current_group = {
                    "chapterUid": uid,
                    "bookmarks": [],
                    "chapterInfo": info,
                }
                last_uid = uid
            current_group["bookmarks"].append(data)
        if current_group:
            grouped_bookmarks.append(current_group)

        previous_path_uids = []
        for group in grouped_bookmarks:
            info = group["chapterInfo"]
            if info:
                current_info = chapter_nodes.get(group["chapterUid"]) or chapter_nodes.get(
                    str(group["chapterUid"])
                )
                if current_info is None:
                    current_info = dict(info)
                    current_info["chapterUid"] = current_info.get("chapterUid", group["chapterUid"])
                path = get_ancestor_chain(current_info)

                divergence_index = 0
                min_len = min(len(path), len(previous_path_uids))
                while divergence_index < min_len:
                    path_uid = path[divergence_index].get("chapterUid")
                    if path_uid != previous_path_uids[divergence_index]:
                        break
                    divergence_index += 1

                for chapter_node in path[divergence_index:]:
                    children.append(
                        get_heading(
                            chapter_node.get("level"), chapter_node.get("title")
                        )
                    )
                previous_path_uids = [node.get("chapterUid") for node in path]
            else:
                previous_path_uids = []

            for i in group["bookmarks"]:
                markText = i.get("markText") or ""
                if not markText:
                    continue
                callout_icon = i.get("_callout_icon") or BOOKMARK_CALLOUT_ICON
                for j in range(0, len(markText) // 2000 + 1):
                    children.append(
                        get_callout(
                            markText[j * 2000 : (j + 1) * 2000],
                            icon=callout_icon,
                        )
                    )
                if i.get("abstract") != None and i.get("abstract") != "":
                    quote = get_quote(i.get("abstract"))
                    grandchild[len(children) - 1] = quote

    else:
        # 如果没有章节信息
        for data in bookmark_list:
            markText = data.get("markText") or ""
            if not markText:
                continue
            for i in range(0, len(markText) // 2000 + 1):
                children.append(
                    get_callout(
                        markText[i * 2000 : (i + 1) * 2000],
                        icon=BOOKMARK_CALLOUT_ICON,
                    )
                )
    if summary != None and len(summary) > 0:
        children.append(get_heading(1, "点评"))
        for i in summary:
            content = (i.get("review") or {}).get("content") or ""
            if not content:
                continue
            for j in range(0, len(content) // 2000 + 1):
                children.append(
                    get_callout(
                        content[j * 2000 : (j + 1) * 2000],
                        icon=NOTE_CALLOUT_ICON,
                    )
                )
    return children, grandchild


def transform_id(book_id):
    id_length = len(book_id)

    if re.match(r"^\d*$", book_id):
        ary = []
        for i in range(0, id_length, 9):
            ary.append(format(int(book_id[i : min(i + 9, id_length)]), "x"))
        return "3", ary

    result = ""
    for i in range(id_length):
        result += format(ord(book_id[i]), "x")
    return "4", [result]


def calculate_book_str_id(book_id):
    md5 = hashlib.md5()
    md5.update(book_id.encode("utf-8"))
    digest = md5.hexdigest()
    result = digest[0:3]
    code, transformed_ids = transform_id(book_id)
    result += code + "2" + digest[-2:]

    for i in range(len(transformed_ids)):
        hex_length_str = format(len(transformed_ids[i]), "x")
        if len(hex_length_str) == 1:
            hex_length_str = "0" + hex_length_str

        result += hex_length_str + transformed_ids[i]

        if i < len(transformed_ids) - 1:
            result += "g"

    if len(result) < 20:
        result += digest[0 : 20 - len(result)]

    md5 = hashlib.md5()
    md5.update(result.encode("utf-8"))
    result += md5.hexdigest()[0:3]
    return result


def extract_notion_id():
    url_or_id = (
        os.getenv("NOTION_DATA_SOURCE_ID")
        or os.getenv("NOTION_PAGE")
        or os.getenv("NOTION_DATABASE_ID")
    )
    if not url_or_id:
        fail_config("没有找到 NOTION_PAGE / NOTION_DATA_SOURCE_ID，请按照文档填写")
    match = NOTION_ID_IN_TEXT_PATTERN.search(url_or_id)
    if match:
        return match.group(0)

    fail_config("获取 Notion ID 失败，请检查 NOTION_PAGE / NOTION_DATA_SOURCE_ID")


def query_data_source(**body):
    return client.request(
        path=f"data_sources/{data_source_id}/query",
        method="POST",
        body=body,
    )


def load_data_source_schema():
    """读取当前 data source 的真实属性，只强制要求同步游标需要的字段。"""
    global data_source_property_types, data_source_property_configs, title_property_name, skipped_property_names
    response = client.request(path=f"data_sources/{data_source_id}", method="GET")
    properties = response.get("properties") or {}
    data_source_property_configs = properties
    data_source_property_types = {
        name: (config or {}).get("type") for name, config in properties.items()
    }
    title_property_name = next(
        (
            name
            for name, prop_type in data_source_property_types.items()
            if prop_type == "title"
        ),
        None,
    )
    skipped_property_names = set()
    if not title_property_name:
        raise Exception("Notion data source 缺少标题属性，请保留一个 Title 类型属性")

    missing = [
        name for name in ("BookId", "Sort") if name not in data_source_property_types
    ]
    if missing:
        raise Exception(
            f"Notion data source 缺少必填属性: {', '.join(missing)}。"
            "请在模板中补充后重试"
        )

    print(
        f"已读取 Notion 属性 {len(data_source_property_types)} 个，"
        f"标题属性: {title_property_name}"
    )


def get_property_type(name):
    return data_source_property_types.get(resolve_property_name(name))


def has_any_property(names):
    return any(get_property_type(name) for name in names)


def build_equals_filter(name, value):
    prop_type = get_property_type(name)
    if prop_type in {"title", "rich_text", "url", "email", "phone_number"}:
        return {"property": name, prop_type: {"equals": str(value)}}
    if prop_type == "number":
        return {"property": name, "number": {"equals": to_number(value)}}
    if prop_type == "select":
        return {"property": name, "select": {"equals": str(value)}}
    if prop_type == "status":
        return {"property": name, "status": {"equals": str(value)}}
    raise Exception(f"Notion 属性 {name} 的类型 {prop_type} 暂不支持用于查询")


def build_is_not_empty_filter(name):
    prop_type = get_property_type(name)
    if prop_type in {
        "title",
        "rich_text",
        "url",
        "email",
        "phone_number",
        "number",
        "select",
        "status",
        "date",
    }:
        return {"property": name, prop_type: {"is_not_empty": True}}
    raise Exception(f"Notion 属性 {name} 的类型 {prop_type} 暂不支持用于查询")


def to_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(to_text(item) for item in value if item is not None)
    return str(value)


def to_name_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [to_text(item) for item in value if to_text(item)]
    text = to_text(value)
    return [text] if text else []


def to_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def normalize_date_value(value):
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    return value


def build_option_property(prop_type, value):
    names = to_name_list(value)
    if not names:
        return None
    if prop_type == "status":
        return get_status(names[0])
    if prop_type == "select":
        return get_select(names[0])
    return get_multi_select(names)


def build_notion_property(name, value):
    prop_type = get_property_type(name)
    if not prop_type:
        if name not in skipped_property_names:
            print(f"属性 {name} 在 Notion 模板中不存在，自动跳过")
            skipped_property_names.add(name)
        return None
    if value is None:
        return None

    if prop_type == "title":
        return get_title(to_text(value))
    if prop_type == "rich_text":
        return get_rich_text(to_text(value))
    if prop_type == "number":
        number = to_number(value)
        return get_number(number) if number is not None else None
    if prop_type == "url":
        return get_url(to_text(value))
    if prop_type in {"multi_select", "status", "select"}:
        return build_option_property(prop_type, value)
    if prop_type == "date":
        return {"date": {"start": normalize_date_value(value)}}
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}

    if name not in skipped_property_names:
        print(f"属性 {name} 的类型 {prop_type} 暂不支持写入，自动跳过")
        skipped_property_names.add(name)
    return None


def build_notion_properties(raw_properties):
    result = {}
    for logical_name, value in raw_properties.items():
        name = resolve_property_name(logical_name)
        config = data_source_property_configs.get(name, {})
        number_format = config.get("number", {}).get("format")
        if logical_name == "阅读进度" and name == "Progress" and number_format != "percent":
            value = value * 100 if value is not None else None
        if logical_name == "评分" and number_format == "percent":
            value = value / 10 if value is not None else None
        prop = build_notion_property(name, value)
        if prop is not None:
            result[name] = prop
    return result


def find_book_pages(book_id):
    pages = []
    body = {"filter": build_equals_filter("BookId", book_id)}
    while True:
        response = query_data_source(**body)
        pages.extend(response.get("results", []))
        if not response.get("has_more"):
            return pages
        body["start_cursor"] = response["next_cursor"]


def get_number_property_value(property_value):
    if not property_value:
        return 0
    prop_type = property_value.get("type")
    value = property_value.get(prop_type)
    if prop_type == "number":
        return value or 0
    if prop_type in {"title", "rich_text"} and value:
        return to_number(value[0].get("plain_text")) or 0
    if prop_type in {"select", "status"} and value:
        return to_number(value.get("name")) or 0
    return 0


def resolve_data_source_id(notion_id):
    if os.getenv("NOTION_DATA_SOURCE_ID"):
        return notion_id

    try:
        client.request(path=f"data_sources/{notion_id}", method="GET")
        return notion_id
    except APIResponseError as error:
        code = getattr(error.code, "value", error.code)
        if code not in {"object_not_found", "validation_error"}:
            raise

    database = client.request(path=f"databases/{notion_id}", method="GET")
    sources = database.get("data_sources") or []
    if not sources:
        raise Exception(f"数据库 {notion_id} 下没有可用的 data source")
    if len(sources) > 1:
        print(
            f"数据库 {notion_id} 包含 {len(sources)} 个 data sources，默认使用第一个: {sources[0].get('id')}"
        )
    return sources[0]["id"]


def load_page_index(store):
    index = {}
    body = {"page_size": 100}
    seen = set()
    while True:
        response = store.request(f"data_sources/{data_source_id}/query", "POST", body)
        for page in response.get("results", []):
            book_id = property_value(page.get("properties", {}).get("BookId", {}))
            if book_id is not None and str(book_id):
                index.setdefault(str(book_id), []).append(page)
        if not response.get("has_more"):
            return index
        cursor = response.get("next_cursor")
        if not cursor or cursor in seen:
            raise RuntimeError("Notion 书籍分页游标未前进")
        seen.add(cursor)
        body["start_cursor"] = cursor


def get_note_records(book_id):
    data = weread.request("/book/bookmarklist", bookId=book_id)
    if not isinstance(data.get("updated"), list):
        raise RuntimeError("微信读书划线响应不完整，未推进同步进度")
    chapters_by_id = {str(key): value for key, value in get_chapter_info(book_id).items()}
    for chapter in data.get("chapters") or []:
        key = str(chapter["chapterUid"])
        chapters_by_id[key] = {**chapters_by_id.get(key, {}), **chapter}
    chapters = list(chapters_by_id.values())
    reviews = []
    cursor = 0
    seen = set()
    while True:
        response = weread.request("/review/list/mine", bookid=book_id, synckey=cursor, count=100)
        if not isinstance(response.get("reviews"), list):
            raise RuntimeError("微信读书想法响应不完整，未推进同步进度")
        reviews.extend(response["reviews"])
        if not response.get("hasMore"):
            break
        next_cursor = response.get("synckey")
        if next_cursor is None or next_cursor == cursor or str(next_cursor) in seen:
            raise RuntimeError("微信读书想法分页游标未前进")
        seen.add(str(next_cursor))
        cursor = next_cursor
    url = f"{WEREAD_URL}web/reader/{calculate_book_str_id(book_id)}"
    records = source_records(book_id, data["updated"], reviews, chapters, url)
    # Only explicit deletions are trusted. Absence from a response isn't deletion.
    removed = []
    for item in data.get("removed") or []:
        identifier = item.get("bookmarkId") if isinstance(item, dict) else item
        if identifier is not None:
            removed.append(f"bookmark:{identifier}")
    return records, removed


def notebook_cursor(item):
    return {key: item.get(key) for key in ("sort", "noteCount", "reviewCount", "bookmarkCount")}


def sync(properties_only=False, dry_run=False, full_refresh=False, book_id=None):
    global client, data_source_id, weread
    secrets = validate_secret_inputs()
    weread = WeReadGatewayClient(secrets["weread_api_key"])
    # Disable SDK write retries: after a timeout reconcile identities first.
    client = Client(auth=secrets["notion_token"], log_level=logging.ERROR,
                    notion_version=NOTION_VERSION, retry=False)
    store = NotionStore(client, dry_run=dry_run)
    data_source_id = resolve_data_source_id(extract_notion_id())
    load_data_source_schema()
    pages_by_book = load_page_index(store)
    books = get_notebooklist() or []
    totals = Counter()
    failures = []
    matched = False
    for item in books:
        book = {**item, **(item.get("book") or {})}
        identifier = str(book.get("bookId") or "")
        if not identifier or (book_id and identifier != book_id):
            continue
        matched = True
        title = book.get("title") or identifier
        try:
            pages = pages_by_book.get(identifier, [])
            if len(pages) > 1:
                raise RuntimeError("BookId 对应多个页面，请人工确认；未修改任何副本")
            page = pages[0] if pages else None
            if properties_only and not page:
                totals["skipped_books"] += 1
                continue
            existing = (page or {}).get("properties", {})
            missing_details = any(
                get_property_type(name) and property_value(existing.get(resolve_property_name(name), {})) in (None, "")
                for name in ("ISBN", "评分")
            )
            isbn, rating = (get_bookinfo(identifier) if (missing_details or full_refresh)
                            and has_any_property(("ISBN", "评分")) else (None, None))
            categories = book.get("categories")
            if categories is not None:
                categories = [x["title"] for x in categories]
            desired = book_properties(title, identifier, item.get("sort") or 0,
                                      book.get("author"), isbn, rating, categories)
            desired.pop("Sort", None)
            changes = property_changes(existing, desired)
            if not page:
                totals["new_pages"] += 1
                if not dry_run:
                    cover = (book.get("cover") or "").replace("/s_", "/t7_")
                    icon = get_icon(cover if cover.startswith("https://") else "https://www.notion.so/icons/book_gray.svg")
                    try:
                        page = store.request("pages", "POST", {
                            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
                            "properties": desired, "icon": icon, "cover": icon,
                        })
                    except Exception:
                        candidates = load_page_index(store).get(identifier, [])
                        if len(candidates) != 1:
                            raise
                        page = candidates[0]
                    pages_by_book[identifier] = [page]
                    changes = property_changes(page.get("properties", {}), desired)
            if changes:
                totals["property_updates"] += 1
                print(f"{'计划更新' if dry_run else '更新'}属性: {title} — {', '.join(changes)}")
                if not dry_run:
                    store.request(f"pages/{page['id']}", "PATCH", {"properties": changes})
            if properties_only:
                continue
            area = find_area(store, page["id"], identifier) if page else None
            checkpoint = area[2] if area else {}
            cursor = notebook_cursor(item)
            # Sort is a notebook timestamp, not a reliable change feed. Periodic
            # audits catch edits/deletions which didn't advance the timestamp.
            needs_notes = (full_refresh or not area or checkpoint.get("cursor") != cursor
                           or checkpoint.get("pending")
                           or time.time() - checkpoint.get("audited", 0) >= 7 * 86400)
            if not needs_notes:
                totals["skipped_notes"] += 1
                continue
            records, removed = get_note_records(identifier)
            if not dry_run:
                area = ensure_area(store, page["id"], identifier, area)
            stats = reconcile(store, area[0] if area else None, records, removed)
            totals.update(stats)
            print(f"{'预览' if dry_run else '笔记'}: {title} — {dict(stats)}")
            if not dry_run:
                # Atomic per-block baselines already survive partial runs. The
                # root checkpoint is committed only after the entire book.
                root, state = area[1], area[2]
                state = {**state, "cursor": cursor, "audited": int(time.time()),
                         "pending": bool(stats["conflicts"])}
                body = clean_body(root)
                body["rich_text"].append(marker(state))
                if not stats["conflicts"]:
                    sort_prop = build_notion_properties({"Sort": item.get("sort") or 0})
                    changed_sort = property_changes(existing, sort_prop)
                    if changed_sort:
                        store.request(f"pages/{page['id']}", "PATCH", {"properties": changed_sort})
                store.update(root, {"type": root["type"], root["type"]: body})
        except UpgradeRequired:
            raise
        except Exception as error:
            # No raw API responses, note text, or credentials in error logs.
            failures.append(identifier)
            reason = str(error) if type(error) in (RuntimeError, ValueError) else type(error).__name__
            print(f"书籍 {identifier} 同步失败: {reason}；原页面和内容已保留，下次可续传。")
    if book_id and not matched:
        raise RuntimeError("指定 BookId 不在微信读书笔记本列表中")
    print(f"{'预览完成，未写入' if dry_run else '同步完成'}: {dict(totals)}；Notion 请求: {dict(store.counts)}；失败 {len(failures)} 本")
    if failures or totals["conflicts"]:
        raise RuntimeError("存在失败或内容冲突，请查看日志及微信读书摘录中的新版本提示")
    return totals


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="weread2notion",
        description="Sync WeRead highlights and notes to Notion.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="sync",
        choices=["sync"],
        help="Command to run. Defaults to sync.",
    )
    parser.add_argument("--properties-only", action="store_true",
                        help="Refresh existing book properties without changing notes.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without any Notion writes.")
    parser.add_argument("--full-refresh", action="store_true", help="Audit all source notes and refresh static metadata; never delete pages.")
    parser.add_argument("--book-id", help="Limit synchronization to one WeRead book ID.")
    args = parser.parse_args(argv)
    try:
        sync(properties_only=args.properties_only, dry_run=args.dry_run,
             full_refresh=args.full_refresh, book_id=args.book_id)
    except (ConfigError, RuntimeError) as error:
        print(str(error))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
