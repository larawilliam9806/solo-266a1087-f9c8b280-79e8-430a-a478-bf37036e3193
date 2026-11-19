"""Preview and publish orchestration.

Consistency model
-----------------
* A ``publication`` row names exactly three immutable blobs (original +
  settlement + ridership) and is inserted in one SQLite transaction together
  with the day's ``current_rev`` pointer.  Readers therefore never observe a
  revision with files from another revision.
* Blobs are written and fsynced *before* the transaction commits.  A crash or
  failed write leaves unreferenced blobs only; the previous revision remains
  current and fully downloadable.
* Confirmations take ``BEGIN IMMEDIATE`` and re-validate the preview snapshot
  (day revision + both contract versions) inside the lock, so out of two
  concurrent confirmations for one day exactly one succeeds.
* Publishing never reads the contract at download time: a revision's column
  contract is frozen into the row and content-addressed files.
"""

import hashlib
import json
import os
import uuid

from . import convert, db, domain
from .errors import AppError, Conflict, NotFound, Unprocessable, Unavailable
from .storage import BlobStore

KINDS = ("original", "settlement", "ridership")


def _check_day(day):
    if not isinstance(day, str):
        raise AppError("日期格式必须为 YYYY-MM-DD", code="invalid_day")
    from datetime import date

    if len(day) != 10:
        raise AppError("日期格式必须为 YYYY-MM-DD", code="invalid_day")
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        raise AppError("日期不是有效日期", code="invalid_day") from None
    if parsed.isoformat() != day:
        raise AppError("日期格式必须为 YYYY-MM-DD", code="invalid_day")
    return day


def _safe_name(name):
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n")
    if not name:
        name = "tickets.csv"
    return name[:255]


def _publication_payload(row):
    return {
        "day": row["day"],
        "rev": row["rev"],
        "created_at": row["created_at"],
        "row_count": row["row_count"],
        "original": {
            "name": row["original_name"],
            "size": row["original_size"],
            "sha256": row["original_sha256"],
        },
        "downstreams": {
            "settlement": {
                "sha256": row["settlement_sha256"],
                "size": row["settlement_size"],
                "fingerprint": row["settlement_fingerprint"],
                "contract_version": row["settlement_version"],
                "columns": json.loads(row["settlement_columns"]),
            },
            "ridership": {
                "sha256": row["ridership_sha256"],
                "size": row["ridership_size"],
                "fingerprint": row["ridership_fingerprint"],
                "contract_version": row["ridership_version"],
                "columns": json.loads(row["ridership_columns"]),
            },
        },
        "downloads": {
            kind: f"/download/{row['day']}/{row['rev']}/{kind}" for kind in KINDS
        },
    }


def _preview_public_payload(preview):
    result = preview["result"]
    return {
        "preview_id": preview["id"],
        "day": preview["day"],
        "base_rev": preview["base_rev"],
        "convertible": bool(preview["convertible"]),
        "original": result["original"],
        "contracts": {
            name: {
                "version": result["contracts"][name]["version"],
                "fingerprint": result["contracts"][name]["fingerprint"],
                "columns": result["contracts"][name]["columns"],
            }
            for name in domain.DOWNSTREAM_ORDER
        },
        "downstreams": result["downstreams"],
        "created_at": preview["created_at"],
        "expires_at": preview["expires_at"],
    }


class Service:
    def __init__(self, conn_factory, store: BlobStore):
        self.conn_factory = conn_factory
        self.store = store

    # ------------------------------------------------------------------
    # preview
    # ------------------------------------------------------------------
    def preview(self, day, filename, raw_bytes):
        day = _check_day(day)
        max_bytes = convert.MAX_FILE_BYTES
        if raw_bytes is None:
            raise AppError("缺少文件", code="missing_file")
        if len(raw_bytes) > max_bytes:
            raise AppError(
                f"文件超过 1 MB 上限（{len(raw_bytes)} 字节）", status=413, code="file_too_large"
            )
        try:
            parsed = convert.parse_csv_bytes(raw_bytes)
        except convert.CsvProblem as problem:
            raise Unprocessable(
                problem.message, code="batch_rejected", details=problem.to_dict()
            ) from None

        conn = self.conn_factory()
        try:
            # One read transaction gives a consistent snapshot of both
            # contracts and the day pointer even if they change mid-preview.
            conn.execute("BEGIN")
            try:
                contracts = db.get_contracts(conn)
                base_rev = db.day_revision(conn, day)
            finally:
                conn.execute("COMMIT")
            db.purge_expired_previews(conn)
            results = {}
            convertible = True
            for name in domain.DOWNSTREAM_ORDER:
                outcome = convert.convert(parsed, contracts[name]["columns"])
                results[name] = outcome
                if outcome["status"] != "ok":
                    convertible = False
            original_name = _safe_name(filename)
            original_sha = self.store.put_bytes(raw_bytes)
            for name in domain.DOWNSTREAM_ORDER:
                outcome = results[name]
                outcome["output_size"] = len(outcome["output_bytes"])
                if outcome["output_bytes"]:
                    self.store.put_bytes(outcome["output_bytes"])
                outcome.pop("output_bytes", None)

            snapshot = {
                "original": {
                    "name": original_name,
                    "size": len(raw_bytes),
                    "sha256": original_sha,
                    "row_count": parsed.row_count,
                    "header": parsed.header,
                },
                "contracts": {
                    name: {
                        "version": contracts[name]["version"],
                        "fingerprint": contracts[name]["fingerprint"],
                        "columns": contracts[name]["columns"],
                    }
                    for name in domain.DOWNSTREAM_ORDER
                },
                "downstreams": results,
                "convertible": convertible,
            }
            preview_id = uuid.uuid4().hex
            with db.transaction(conn):
                db.create_preview(
                    conn,
                    preview_id=preview_id,
                    day=day,
                    base_rev=base_rev,
                    contract_versions={
                        name: contracts[name]["version"] for name in domain.DOWNSTREAM_ORDER
                    },
                    original_name=original_name,
                    original_size=len(raw_bytes),
                    original_sha256=original_sha,
                    result=snapshot,
                    convertible=convertible,
                )
            preview = db.get_preview(conn, preview_id)
            return 201, _preview_public_payload(preview)
        finally:
            conn.close()

    def get_preview(self, preview_id):
        conn = self.conn_factory()
        try:
            preview = db.get_preview(conn, preview_id)
            if preview is None:
                raise NotFound("预览不存在或已失效，请重新上传", code="preview_not_found")
            return _preview_public_payload(preview)
        finally:
            conn.close()

    def discard_preview(self, preview_id):
        conn = self.conn_factory()
        try:
            with db.transaction(conn, immediate=True):
                preview = db.get_preview(conn, preview_id)
                if preview is None:
                    raise NotFound("预览不存在或已失效", code="preview_not_found")
                db.delete_preview(conn, preview_id)
            return 200, {"discarded": preview_id}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # confirm / publish
    # ------------------------------------------------------------------
    def confirm(self, preview_id):
        conn = self.conn_factory()
        try:
            with db.transaction(conn, immediate=True):
                preview = db.get_preview(conn, preview_id)
                if preview is None:
                    raise NotFound("预览不存在或已失效，请重新上传并预览", code="preview_not_found")
                if preview["consumed_at"]:
                    raise Conflict("该预览已用于发布，不能重复确认", code="preview_used")
                now = db.iso(db.utcnow())
                if preview["expires_at"] < now:
                    db.delete_preview(conn, preview_id)
                    raise Conflict("预览已过期，请重新上传并预览", code="preview_expired")

                result = preview["result"]
                day = preview["day"]
                current_rev = db.day_revision(conn, day)
                if current_rev != preview["base_rev"]:
                    raise Conflict(
                        f"该日已发布到修订号 {current_rev}，预览基于修订号 "
                        f"{preview['base_rev']}，请重新预览后再确认",
                        code="stale_day_revision",
                    )

                contracts = db.get_contracts(conn)
                for name in domain.DOWNSTREAM_ORDER:
                    snapshot = result["contracts"][name]
                    if contracts[name]["version"] != preview[name + "_version"] or (
                        contracts[name]["fingerprint"] != snapshot["fingerprint"]
                    ):
                        raise Conflict(
                            f"{domain.label_for(name)} 契约在预览后已变更，"
                            "请重新预览后再确认",
                            code="stale_contract",
                        )

                if not preview["convertible"] or not result.get("convertible"):
                    raise Unprocessable(
                        "存在不兼容的列或行，不能发布；请修正 CSV 或契约后重新预览",
                        code="incompatible",
                    )

                # Re-materialise from the stored original bytes and the frozen
                # contract snapshot, and verify it matches what was previewed.
                original_bytes = self.store.read_bytes(preview["original_sha256"])
                if hashlib.sha256(original_bytes).hexdigest() != preview["original_sha256"]:
                    raise Unavailable("预览原件校验失败，请重新上传并预览")
                try:
                    parsed = convert.parse_csv_bytes(original_bytes)
                except convert.CsvProblem:
                    raise Unavailable(
                        "预览原件无法再次解析，请重新上传并预览"
                    ) from None

                outputs = {}
                for name in domain.DOWNSTREAM_ORDER:
                    snapshot = result["contracts"][name]
                    outcome = convert.convert(parsed, snapshot["columns"])
                    if outcome["status"] != "ok":
                        raise Conflict(
                            "当前契约与预览结果不一致，请重新预览", code="stale_contract"
                        )
                    expected = result["downstreams"][name]["output_sha256"]
                    actual = hashlib.sha256(outcome["output_bytes"]).hexdigest()
                    if expected != actual:
                        raise Unavailable("预览结果校验失败，请重新预览")
                    outputs[name] = (actual, len(outcome["output_bytes"]), outcome["output_bytes"])
                    if not self.store.exists(actual):
                        self.store.put_bytes(outcome["output_bytes"])

                # Blobs are durable; now commit the row + pointer atomically.
                new_rev = current_rev + 1
                created_at = db.iso(db.utcnow())
                settlement_cols = json.dumps(result["contracts"]["settlement"]["columns"], ensure_ascii=False)
                ridership_cols = json.dumps(result["contracts"]["ridership"]["columns"], ensure_ascii=False)
                conn.execute(
                    """INSERT INTO publications
                       (day, rev, original_name, original_size, original_sha256,
                        settlement_sha256, settlement_size, ridership_sha256, ridership_size,
                        settlement_fingerprint, ridership_fingerprint,
                        settlement_version, ridership_version,
                        settlement_columns, ridership_columns, row_count, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        day,
                        new_rev,
                        preview["original_name"],
                        preview["original_size"],
                        preview["original_sha256"],
                        outputs["settlement"][0],
                        outputs["settlement"][1],
                        outputs["ridership"][0],
                        outputs["ridership"][1],
                        result["contracts"]["settlement"]["fingerprint"],
                        result["contracts"]["ridership"]["fingerprint"],
                        result["contracts"]["settlement"]["version"],
                        result["contracts"]["ridership"]["version"],
                        settlement_cols,
                        ridership_cols,
                        parsed.row_count,
                        created_at,
                    ),
                )
                if current_rev == 0:
                    conn.execute(
                        "INSERT INTO days (day, current_rev, updated_at) VALUES (?, ?, ?)",
                        (day, new_rev, created_at),
                    )
                else:
                    conn.execute(
                        "UPDATE days SET current_rev = ?, updated_at = ? WHERE day = ?",
                        (new_rev, created_at, day),
                    )
                conn.execute(
                    "UPDATE previews SET consumed_at = ? WHERE id = ?",
                    (created_at, preview_id),
                )
                # The preview row is now consumed; publications are the only
                # thing downloads resolve against, so the preview can go.
                db.delete_preview(conn, preview_id)
                row = db.get_publication(conn, day, new_rev)
            return 201, _publication_payload(row)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def list_days(self):
        conn = self.conn_factory()
        try:
            return db.list_days(conn)
        finally:
            conn.close()

    def day_detail(self, day):
        day = _check_day(day)
        conn = self.conn_factory()
        try:
            revisions = db.list_publications(conn, day)
            return {
                "day": day,
                "current_rev": db.day_revision(conn, day),
                "revisions": [_publication_payload(r) for r in revisions],
            }
        finally:
            conn.close()

    def resolve_download(self, day, rev, kind):
        day = _check_day(day)
        if kind not in KINDS:
            raise NotFound("未知的下载类型", code="unknown_kind")
        # Resolve the revision against the database row, then open only the
        # blobs referenced by that row.  Directories are never scanned, so a
        # half-finished publish cannot leak into a reader.
        conn = self.conn_factory()
        try:
            if rev == "latest" or rev is None:
                current = db.day_revision(conn, day)
                if current == 0:
                    raise NotFound("该日尚无可下载版本", code="no_revision")
                row = db.get_publication(conn, day, current)
            else:
                try:
                    rev_int = int(rev)
                except (TypeError, ValueError):
                    raise AppError("修订号必须是数字", code="invalid_rev") from None
                if rev_int < 1:
                    raise AppError("修订号必须大于 0", code="invalid_rev")
                row = db.get_publication(conn, day, rev_int)
            if row is None:
                raise NotFound("该日该修订号不存在", code="revision_not_found")

            # Guard against a damaged revision: never fall back to another
            # revision or serve a partial set.
            for other in ("original_sha256", "settlement_sha256", "ridership_sha256"):
                if not self.store.exists(row[other]):
                    raise Unavailable(
                        "该版本文件缺失，无法提供一致的下载；请联系管理员",
                        code="revision_blob_missing",
                    )
            digest = row[kind + "_sha256"]
            size = row["original_size"] if kind == "original" else row[kind + "_size"]
            name = row["original_name"] if kind == "original" else f"{day}-{kind}.csv"
            path = self.store.path_for(digest)
            # The stored size is what Content-Length will promise; refuse to
            # start a transfer whose blob no longer matches it.
            try:
                if os.path.getsize(path) != size:
                    raise Unavailable(
                        "该版本文件大小与登记不一致，无法提供完整下载；请联系管理员",
                        code="revision_blob_corrupt",
                    )
            except OSError:
                raise Unavailable(
                    "该版本文件缺失，无法提供一致的下载；请联系管理员",
                    code="revision_blob_missing",
                ) from None
            return {
                "path": path,
                "digest": digest,
                "size": size,
                "day": row["day"],
                "rev": row["rev"],
                "kind": kind,
                "name": name,
            }
        finally:
            conn.close()


