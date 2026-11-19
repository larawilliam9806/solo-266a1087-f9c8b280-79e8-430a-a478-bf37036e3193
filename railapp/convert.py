"""Strict CSV parsing and per-downstream conversion.

Parsing rules (whole batch is rejected together):
  * file must be valid UTF-8 (a UTF-8 BOM is tolerated)
  * header must exist and contain no blank / duplicate names
  * every data row must have exactly as many fields as the header
  * required columns must all be present (extra source columns are fine)
  * integer columns must contain canonical base-10 integers
"""

import csv
import hashlib
import io
import re
from collections import Counter

from .errors import BadRequest

MAX_FILE_BYTES = 1024 * 1024
MAX_ROWS = 200_000

_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_INT_MIN = -(2**63)
_INT_MAX = 2**63 - 1
_PREVIEW_ROWS = 50
_PREVIEW_ISSUES = 200


class CsvProblem(Exception):
    """A batch-level problem that rejects the whole upload."""

    def __init__(self, kind, message, rows=None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.rows = rows or []

    def to_dict(self):
        payload = {"kind": self.kind, "message": self.message}
        if self.rows:
            payload["rows"] = self.rows
        return payload


def _detect_delimiter(text):
    """Standard comma CSV; ';' only if a quoted first record makes it plain."""
    first_line = text.split("\n", 1)[0]
    if ";" in first_line and '"' not in first_line:
        # Only switch when the header becomes multi-column under ';' and the
        # row is a single column under ','.  This keeps ordinary data rows
        # from ever flipping the decision.
        try:
            semi_fields = next(csv.reader([first_line], delimiter=";", strict=True))
            comma_fields = next(csv.reader([first_line], delimiter=",", strict=True))
        except (csv.Error, StopIteration):
            return ","
        if len(semi_fields) > 1 and len(comma_fields) <= 1:
            return ";"
    return ","


class ParsedCsv:
    def __init__(self, header, rows, raw_bytes, delimiter):
        self.header = header
        self.rows = rows
        self.raw_bytes = raw_bytes
        self.delimiter = delimiter
        self.sha256 = hashlib.sha256(raw_bytes).hexdigest()

    @property
    def row_count(self):
        return len(self.rows)


def parse_csv_bytes(raw):
    if not isinstance(raw, (bytes, bytearray)):
        raise BadRequest("上传内容无效")
    raw = bytes(raw)
    if len(raw) == 0:
        raise CsvProblem("empty_file", "上传的 CSV 为空文件")
    if len(raw) > MAX_FILE_BYTES:
        raise CsvProblem("too_large", f"文件超过 1 MB 上限（{len(raw)} 字节）")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvProblem(
            "encoding", f"文件不是合法的 UTF-8 编码（字节偏移 {exc.start}）"
        ) from None

    if "\x00" in text:
        raise CsvProblem("encoding", "文件包含 NUL 字节，不是文本 CSV")

    if not text.strip():
        raise CsvProblem("empty_file", "上传的 CSV 只有空白字符")

    delimiter = _detect_delimiter(text)
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
    except csv.Error:
        raise CsvProblem("parse", "CSV 解析失败") from None
    header = None
    rows = []
    header_line = 0
    try:
        for fields in reader:
            line_no = reader.line_num
            if not fields:
                if header is None:
                    raise CsvProblem("bad_header", "第 1 行表头为空")
                raise CsvProblem(
                    "blank_row",
                    f"第 {line_no} 行为空行；请删除空行后重新上传",
                    [{"line": line_no, "reason": "空行"}],
                )
            if header is None:
                header = fields
                header_line = line_no
                continue
            rows.append((line_no, fields))
            if len(rows) > MAX_ROWS:
                raise CsvProblem("too_many_rows", f"数据行超过 {MAX_ROWS} 行上限")
    except csv.Error as exc:
        raise CsvProblem("parse", f"CSV 解析失败：{exc}") from None

    if header is None:
        raise CsvProblem("empty_file", "CSV 没有表头行")

    # ----- header checks -------------------------------------------------
    trimmed = [h.strip() for h in header]
    if any(h == "" for h in trimmed):
        bad = [i + 1 for i, h in enumerate(header) if h.strip() == ""]
        raise CsvProblem("bad_header", f"表头存在空列名（第 {bad} 列）")
    counts = Counter(trimmed)
    dupes = sorted(name for name, count in counts.items() if count > 1)
    if dupes:
        raise CsvProblem("duplicate_header", f"表头存在重复列名: {', '.join(dupes)}")
    header = trimmed
    if any(ord(ch) < 32 and ch not in "\t" for ch in "".join(header)):
        raise CsvProblem("bad_header", "表头列名包含控制字符")

    # ----- row width checks ---------------------------------------------
    width = len(header)
    bad_rows = []
    total_bad = 0
    for line_no, fields in rows:
        if len(fields) != width:
            total_bad += 1
            if len(bad_rows) < 200:
                bad_rows.append(
                    {
                        "line": line_no,
                        "reason": "列数不一致",
                        "expected": width,
                        "actual": len(fields),
                    }
                )
    if bad_rows:
        raise CsvProblem(
            "row_width",
            f"{total_bad} 行字段数与表头（{width} 列）不一致，整批拒绝",
            bad_rows,
        )

    parsed = ParsedCsv(header, rows, raw, delimiter)
    parsed.header_line = header_line
    return parsed


def _normalise_int(value):
    stripped = value.strip()
    if not _INT_RE.match(stripped) or len(stripped) > 32:
        return None
    number = int(stripped)
    if number < _INT_MIN or number > _INT_MAX:
        return None
    return str(number)


def convert(parsed, contract_columns):
    """Convert a parsed CSV for one downstream contract.

    Returns a dict with status ("ok" or "incompatible"), preview rows, column
    and row problems, and (when fully convertible) the output bytes.  Never
    raises for data problems; batch-level problems are caught in parse_csv.
    """
    header_index = {}
    for i, name in enumerate(parsed.header):
        header_index[name] = i

    missing = [c["name"] for c in contract_columns if c["name"] not in header_index]
    if missing:
        return {
            "status": "incompatible",
            "missing_columns": missing,
            "issues": [],
            "preview": [],
            "columns": [c["name"] for c in contract_columns],
            "column_types": [c["type"] for c in contract_columns],
            "row_count": len(parsed.rows),
            "bad_row_count": 0,
            "output_bytes": b"",
            "output_sha256": None,
            "issues_truncated": False,
        }

    positions = [header_index[c["name"]] for c in contract_columns]
    issues = []
    bad_rows = set()
    out_rows = []
    preview_rows = []
    for row_no, (line_no, fields) in enumerate(parsed.rows, start=1):
        out = []
        row_problems = []
        for col, pos in zip(contract_columns, positions):
            value = fields[pos]
            if col["type"] == "integer":
                normalised = _normalise_int(value)
                if normalised is None:
                    row_problems.append(
                        {
                            "row": row_no,
                            "line": line_no,
                            "column": col["name"],
                            "value": value[:200],
                            "reason": "不是合法整数",
                        }
                    )
                    out.append(value)
                    continue
                out.append(normalised)
            else:
                out.append(value)

        if row_problems:
            bad_rows.add(row_no)
            if len(issues) < _PREVIEW_ISSUES:
                issues.extend(row_problems[: _PREVIEW_ISSUES - len(issues)])
        else:
            out_rows.append(out)

        if len(preview_rows) < _PREVIEW_ROWS:
            preview_rows.append(
                {
                    "row": row_no,
                    "line": line_no,
                    "values": out,
                    "bad_columns": [p["column"] for p in row_problems],
                }
            )

    incompatible = bool(bad_rows)
    output = b""
    output_sha = None
    if not incompatible:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow([c["name"] for c in contract_columns])
        writer.writerows(out_rows)
        output = buf.getvalue().encode("utf-8")
        output_sha = hashlib.sha256(output).hexdigest()

    return {
        "status": "incompatible" if incompatible else "ok",
        "missing_columns": [],
        "issues": issues,
        "preview": preview_rows,
        "columns": [c["name"] for c in contract_columns],
        "column_types": [c["type"] for c in contract_columns],
        "row_count": len(parsed.rows),
        "bad_row_count": len(bad_rows),
        "output_bytes": output,
        "output_sha256": output_sha,
        "issues_truncated": len(issues) >= _PREVIEW_ISSUES,
    }
