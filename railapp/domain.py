"""Domain definitions for the two downstream systems and contract validation."""

import hashlib
import json
import re

from .errors import BadRequest

DOWNSTREAM_ORDER = ("settlement", "ridership")

DOWNSTREAMS = {
    "settlement": {
        "name": "settlement",
        "label": "结算系统",
        "short": "结算",
        "description": "按配置的列序与类型生成结算用 CSV，整数列规范化后输出。",
    },
    "ridership": {
        "name": "ridership",
        "label": "客流系统",
        "short": "客流",
        "description": "按配置的列序与类型生成客流用 CSV，整数列规范化后输出。",
    },
}

# Seeded once for a fresh database; edits are never overwritten afterwards.
DEFAULT_CONTRACTS = {
    "settlement": [
        {"name": "ticket_id", "type": "text"},
        {"name": "passenger_id", "type": "text"},
        {"name": "train_no", "type": "text"},
        {"name": "amount", "type": "integer"},
        {"name": "travel_date", "type": "text"},
    ],
    "ridership": [
        {"name": "train_no", "type": "text"},
        {"name": "depart_station", "type": "text"},
        {"name": "arrive_station", "type": "text"},
        {"name": "travel_date", "type": "text"},
        {"name": "passenger_count", "type": "integer"},
    ],
}

_TYPES = {
    "int": "integer",
    "integer": "integer",
    "str": "text",
    "string": "text",
    "text": "text",
}
_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f,]")
MAX_COLUMNS = 100
MAX_NAME_LENGTH = 128


def label_for(name):
    spec = DOWNSTREAMS.get(name)
    return spec["label"] if spec else name


def normalize_type(value):
    if not isinstance(value, str):
        raise BadRequest(f"列类型必须是 integer 或 text，收到 {value!r}")
    key = value.strip().lower()
    if key not in _TYPES:
        raise BadRequest(f"不支持的列类型 {value!r}，只允许 integer 或 text")
    return _TYPES[key]


def validate_columns(raw):
    """Validate and normalise a contract column list.

    Accepted item forms:
      {"name": "ticket_id", "type": "text"}
      "ticket_id"                      (convenience: text column)
    """
    if not isinstance(raw, (list, tuple)):
        raise BadRequest("契约必须是列的列表")
    if not raw:
        raise BadRequest("契约至少需要一列")
    if len(raw) > MAX_COLUMNS:
        raise BadRequest(f"契约最多支持 {MAX_COLUMNS} 列")
    columns = []
    seen = set()
    for index, item in enumerate(raw, start=1):
        if isinstance(item, str):
            name, column_type = item, "text"
        elif isinstance(item, dict):
            name = item.get("name")
            column_type = item.get("type", "text")
        else:
            raise BadRequest(f"第 {index} 列格式无效")
        if not isinstance(name, str):
            raise BadRequest(f"第 {index} 列缺少列名")
        name = name.strip()
        if not name:
            raise BadRequest(f"第 {index} 列列名为空")
        if len(name) > MAX_NAME_LENGTH:
            raise BadRequest(f"列名过长（最多 {MAX_NAME_LENGTH} 字符）: {name[:20]}…")
        if _NAME_FORBIDDEN.search(name):
            raise BadRequest(f"列名不能包含控制字符或逗号: {name!r}")
        if name in seen:
            raise BadRequest(f"契约中存在重复列名: {name}")
        seen.add(name)
        columns.append({"name": name, "type": normalize_type(column_type)})
    return columns


def fingerprint(columns):
    canonical = json.dumps(columns, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_contract_text(text):
    """Parse the textarea format used by the contract editor.

    One column per line: ``name,type``.  Blank lines and ``#`` comments are
    ignored.  The order of lines is the downstream column order.
    """
    if text is None:
        raise BadRequest("契约内容为空")
    items = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "," in stripped:
            name, _, type_part = stripped.rpartition(",")
            if not name.strip() or not type_part.strip():
                raise BadRequest(f"第 {lineno} 行格式应为 列名,integer|text")
            items.append({"name": name.strip(), "type": type_part.strip()})
        else:
            items.append({"name": stripped, "type": "text"})
    return validate_columns(items)


def format_contract_text(columns):
    return "\n".join(f'{c["name"]},{c["type"]}' for c in columns)
