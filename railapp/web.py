"""Flask web application."""

import os
import sqlite3

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import convert, db, domain
from .errors import AppError, NotFound, Unprocessable
from .publisher import Service
from .storage import BlobStore, verify_publications


def create_app(config=None):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
        static_folder=os.path.join(base_dir, "static"),
    )
    app.json.ensure_ascii = False
    app.config.update(
        MAX_CONTENT_LENGTH=convert.MAX_FILE_BYTES + 256 * 1024,
        DATA_DIR=os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data")),
        MAX_FILE_BYTES=convert.MAX_FILE_BYTES,
        JSON_AS_ASCII=False,
        SECRET_KEY=os.environ.get("SECRET_KEY", "rail-settlement-dev"),
    )
    if config:
        app.config.update(config)

    data_dir = os.path.abspath(app.config["DATA_DIR"])
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "app.db")
    store = BlobStore(os.path.join(data_dir, "blobs"))
    app.config["DB_PATH"] = db_path
    app.config["STORE"] = store

    store.clean_tmp()
    bootstrap_conn = db.connect(db_path)
    try:
        db.init_db(bootstrap_conn)
        app.config["STARTUP_PROBLEMS"] = verify_publications(bootstrap_conn, store)
    finally:
        bootstrap_conn.close()
    if app.config["STARTUP_PROBLEMS"]:
        app.logger.error(
            "startup self-check found %d damaged revision(s); affected downloads "
            "will fail closed with 503",
            len(app.config["STARTUP_PROBLEMS"]),
        )

    def get_conn():
        return db.connect(db_path)

    service = Service(get_conn, store)
    app.extensions["service"] = service
    app.extensions["get_conn"] = get_conn

    # ------------------------------------------------------------------
    # error handling
    # ------------------------------------------------------------------
    @app.errorhandler(AppError)
    def handle_app_error(err):
        if _wants_json():
            return jsonify(err.to_dict()), err.status
        return (
            render_template(
                "error.html",
                status=err.status,
                error=err.code,
                message=err.message,
                details=err.details,
            ),
            err.status,
        )

    @app.errorhandler(413)
    def handle_413(err):
        message = "上传文件超过 1 MB 上限"
        if _wants_json():
            return jsonify({"error": "file_too_large", "message": message}), 413
        return (
            render_template(
                "error.html", status=413, error="file_too_large", message=message
            ),
            413,
        )

    @app.errorhandler(404)
    def handle_404(err):
        if _wants_json():
            return jsonify({"error": "not_found", "message": "页面或资源不存在"}), 404
        return (
            render_template(
                "error.html", status=404, error="not_found", message="页面或资源不存在"
            ),
            404,
        )

    @app.errorhandler(sqlite3.Error)
    def handle_sqlite(err):
        app.logger.exception("database error: %s", err)
        message = "数据库繁忙或写入失败，请稍后重试；已发布版本不受影响"
        if _wants_json():
            return jsonify({"error": "database_error", "message": message}), 503
        return render_template("error.html", status=503, error="database_error", message=message), 503

    @app.errorhandler(Exception)
    def handle_unexpected(err):
        from werkzeug.exceptions import HTTPException

        if isinstance(err, HTTPException):
            return err
        app.logger.exception("unexpected error: %s", err)
        message = "服务器内部错误，已发布版本不受影响"
        if _wants_json():
            return jsonify({"error": "internal_error", "message": message}), 500
        return render_template("error.html", status=500, error="internal_error", message=message), 500

    def _wants_json():
        if request.path.startswith("/api/"):
            return True
        accept = request.headers.get("Accept", "")
        return "application/json" in accept and "text/html" not in accept

    app.jinja_env.globals["downstreams"] = domain.DOWNSTREAMS
    app.jinja_env.globals["downstream_order"] = domain.DOWNSTREAM_ORDER
    app.jinja_env.globals["max_file_bytes"] = convert.MAX_FILE_BYTES

    # ------------------------------------------------------------------
    # HTML pages
    # ------------------------------------------------------------------
    @app.get("/")
    def index():
        conn = get_conn()
        try:
            contracts = db.get_contracts(conn)
            days = db.list_days(conn, limit=20)
        finally:
            conn.close()
        return render_template(
            "index.html",
            contracts=contracts,
            days=days,
            startup_problems=app.config.get("STARTUP_PROBLEMS", []),
        )

    @app.get("/contracts")
    def contracts_page():
        conn = get_conn()
        try:
            contracts = db.get_contracts(conn)
        finally:
            conn.close()
        return render_template(
            "contracts.html",
            contracts=contracts,
            contract_text={
                name: domain.format_contract_text(contracts[name]["columns"])
                for name in domain.DOWNSTREAM_ORDER
            },
        )

    @app.post("/contracts")
    def update_contracts():
        payload = {}
        for name in domain.DOWNSTREAM_ORDER:
            text = request.form.get(name + "_contract")
            if text is None or not text.strip():
                # Blank textarea means "leave this downstream untouched".
                continue
            try:
                columns = domain.parse_contract_text(text)
            except AppError as err:
                return (
                    render_template(
                        "error.html",
                        status=400,
                        error=err.code,
                        message=f"{domain.label_for(name)}：{err.message}",
                        details=None,
                    ),
                    400,
                )
            payload[name] = columns
        if not payload:
            raise AppError("没有提交任何契约", code="no_contract")

        conn = get_conn()
        try:
            with db.transaction(conn, immediate=True):
                for name, columns in payload.items():
                    db.update_contract(conn, name, columns)
        finally:
            conn.close()
        return redirect(url_for("contracts_page"))

    @app.get("/upload")
    def upload_page():
        conn = get_conn()
        try:
            contracts = db.get_contracts(conn)
            days = db.list_days(conn, limit=10)
        finally:
            conn.close()
        return render_template("upload.html", contracts=contracts, days=days)

    @app.get("/days/<day>")
    def day_page(day):
        try:
            detail = service.day_detail(day)
        except AppError:
            abort(404)
        if detail["current_rev"] == 0:
            abort(404)
        return render_template("day.html", detail=detail)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    @app.post("/api/previews")
    def api_create_preview():
        day = request.form.get("day") or request.args.get("day")
        upload = request.files.get("file")
        if upload is None:
            raise AppError("请选择要上传的 CSV 文件", code="missing_file")
        raw = upload.read(convert.MAX_FILE_BYTES + 1)
        if len(raw) > convert.MAX_FILE_BYTES:
            raise AppError(
                "文件超过 1 MB 上限", status=413, code="file_too_large"
            )
        if not raw:
            raise Unprocessable(
                "上传的 CSV 为空文件",
                code="batch_rejected",
                details={"kind": "empty_file", "message": "上传的 CSV 为空文件"},
            )
        filename = upload.filename or "tickets.csv"
        status, payload = service.preview(day, filename, raw)
        return jsonify(payload), status

    @app.get("/api/previews/<preview_id>")
    def api_get_preview(preview_id):
        return jsonify(service.get_preview(preview_id))

    @app.delete("/api/previews/<preview_id>")
    def api_delete_preview(preview_id):
        status, payload = service.discard_preview(preview_id)
        return jsonify(payload), status

    @app.post("/api/previews/<preview_id>/confirm")
    def api_confirm(preview_id):
        status, payload = service.confirm(preview_id)
        return jsonify(payload), status

    @app.get("/api/days")
    def api_days():
        return jsonify({"days": service.list_days()})

    @app.get("/api/days/<day>")
    def api_day(day):
        return jsonify(service.day_detail(day))

    @app.get("/api/contracts")
    def api_contracts():
        conn = get_conn()
        try:
            contracts = db.get_contracts(conn)
        finally:
            conn.close()
        return jsonify({name: contracts[name] for name in domain.DOWNSTREAM_ORDER})

    @app.put("/api/contracts/<name>")
    def api_update_contract(name):
        if name not in domain.DOWNSTREAMS:
            raise NotFound("未知的下游系统", code="unknown_downstream")
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or "columns" not in body:
            raise AppError("请求体必须包含 columns 列表", code="bad_contract")
        columns = domain.validate_columns(body["columns"])
        conn = get_conn()
        try:
            with db.transaction(conn, immediate=True):
                updated = db.update_contract(conn, name, columns)
        finally:
            conn.close()
        return jsonify(updated)

    @app.get("/api/health")
    def api_health():
        conn = get_conn()
        try:
            conn.execute("SELECT 1")
            damaged = verify_publications(conn, store)
        finally:
            conn.close()
        # Damaged revisions do not make the service unhealthy (unaffected
        # revisions remain downloadable), but they are surfaced for operators.
        return jsonify({"status": "ok", "damaged_revisions": damaged})

    # ------------------------------------------------------------------
    # downloads
    # ------------------------------------------------------------------
    @app.get("/download/<day>/<rev>/<kind>")
    def download(day, rev, kind):
        info = service.resolve_download(day, rev, kind)
        # Open the blob before constructing the response so a missing file is
        # reported as 503 rather than a truncated transfer.  The open file
        # descriptor keeps the already-resolved revision readable to the end
        # even if the store is modified concurrently; blobs themselves are
        # immutable content-addressed files.
        handle = store.open_read(info["digest"])
        # Always advertise the immutable, verified blob hash rather than the
        # upload's own name, so a client can check it got exactly this revision.
        response = Response(
            _stream(handle),
            mimetype="text/csv; charset=utf-8",
            direct_passthrough=True,
        )
        response.headers["Content-Length"] = str(info["size"])
        response.headers["Content-Disposition"] = _content_disposition(info["name"])
        response.headers["ETag"] = '"%s"' % info["digest"]
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-SHA256"] = info["digest"]
        response.headers["X-Revision"] = f"{day}-r{info['rev']}"
        response.call_on_close(handle.close)
        return response

    def _stream(handle):
        try:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            handle.close()

    def _content_disposition(name):
        from urllib.parse import quote

        ascii_fallback = "".join(
            ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in name
        )
        suffix = ".csv"
        if not ascii_fallback.lower().endswith(".csv"):
            ascii_fallback = ascii_fallback + suffix
        return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
            ascii_fallback,
            quote(name, safe=""),
        )

    return app
