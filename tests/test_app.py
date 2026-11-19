import hashlib
import io

from conftest import DEFAULT_CSV, preview_and_confirm, upload


def test_preview_reports_both_downstreams(client):
    response = upload(client)
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["convertible"] is True
    assert payload["base_rev"] == 0
    settlement = payload["downstreams"]["settlement"]
    assert settlement["status"] == "ok"
    assert settlement["columns"] == ["ticket_id", "passenger_id", "train_no", "amount", "travel_date"]
    assert settlement["preview"][0]["values"] == ["T1", "P1", "G101", "55", "2026-11-01"]
    ridership = payload["downstreams"]["ridership"]
    assert ridership["columns"] == [
        "train_no", "depart_station", "arrive_station", "travel_date", "passenger_count",
    ]
    assert ridership["preview"][2]["values"] == ["G22", "天津", "北京南", "2026-11-01", "2"]
    assert payload["original"]["row_count"] == 3


def test_upload_accepts_extra_columns_and_reordered_source(client):
    data = (
        "passenger_count,travel_date,amount,train_no,extra,arrive_station,passenger_id,"
        "depart_station,ticket_id\n"
        "3,2026-11-02,90,G9,ignore,广州南,P9,北京西,T9\n"
    )
    response = upload(client, data=data.encode())
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["convertible"] is True
    assert payload["downstreams"]["settlement"]["preview"][0]["values"][:4] == [
        "T9", "P9", "G9", "90",
    ]


def test_row_width_mismatch_rejects_whole_batch(client):
    data = "ticket_id,passenger_id,train_no,amount\nT1,P1,G1,10,extra\n"
    response = upload(client, data=data.encode())
    assert response.status_code == 422
    payload = response.get_json()
    assert payload["error"] == "batch_rejected"
    assert payload["details"]["kind"] == "row_width"
    assert payload["details"]["rows"][0]["line"] == 2


def test_missing_required_column_incompatible_not_rejected(client):
    data = "ticket_id,train_no,amount,travel_date\nT1,G1,10,2026-11-01\n"
    response = upload(client, data=data.encode())
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["convertible"] is False
    settlement = payload["downstreams"]["settlement"]
    assert settlement["status"] == "incompatible"
    assert "passenger_id" in settlement["missing_columns"]
    assert payload["downstreams"]["ridership"]["status"] == "incompatible"


def test_duplicate_header_rejected(client):
    data = (
        "ticket_id,passenger_id,train_no,amount,travel_date,ticket_id\n"
        "T1,P1,G1,10,2026-11-01,T9\n"
    )
    response = upload(client, data=data.encode())
    assert response.status_code == 422
    payload = response.get_json()
    assert payload["details"]["kind"] == "duplicate_header"


def test_type_error_marks_rows_and_blocks_publish(client):
    data = DEFAULT_CSV.replace("T2,P2,G101,120", "T2,P2,G101,12x")
    response = upload(client, data=data.encode())
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["convertible"] is False
    settlement = payload["downstreams"]["settlement"]
    assert settlement["bad_row_count"] == 1
    assert settlement["issues"][0]["row"] == 2
    assert settlement["issues"][0]["column"] == "amount"
    assert payload["downstreams"]["ridership"]["status"] == "ok"
    confirm = client.post(
        f"/api/previews/{payload['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 422
    assert confirm.get_json()["error"] == "incompatible"


def test_classic_csv_case_does_not_shift_columns(client):
    data = (
        "ticket_id,passenger_id,train_no,amount,travel_date,depart_station,arrive_station,passenger_count\n"
        'T1,P1,G101,55,2026-11-01,"北京南","上海虹桥",1\n'
        'T2,P2,G101,120,2026-11-01,"北京南","上海虹桥",1\n'
    )
    response = upload(client, data=data.encode())
    assert response.status_code == 201
    assert response.get_json()["convertible"] is True


def test_confirm_publishes_three_files_with_same_rev(client):
    preview, confirmed = preview_and_confirm(client)
    assert confirmed.status_code == 201, confirmed.get_json()
    published = confirmed.get_json()
    assert published["rev"] == 1
    assert published["day"] == "2026-11-01"
    assert published["row_count"] == 3

    for kind in ("original", "settlement", "ridership"):
        download = client.get(f"/download/2026-11-01/1/{kind}")
        assert download.status_code == 200
        assert download.headers["X-Revision"] == "2026-11-01-r1"
    original = client.get("/download/2026-11-01/1/original").data
    assert original == DEFAULT_CSV.encode()
    settlement = client.get("/download/2026-11-01/1/settlement").data.decode()
    assert settlement.splitlines()[0] == "ticket_id,passenger_id,train_no,amount,travel_date"
    assert settlement.splitlines()[1] == "T1,P1,G101,55,2026-11-01"
    ridership = client.get("/download/2026-11-01/1/ridership").data.decode()
    assert ridership.splitlines()[0] == (
        "train_no,depart_station,arrive_station,travel_date,passenger_count"
    )


def test_latest_alias_and_bad_rev(client):
    preview_and_confirm(client)
    latest = client.get("/download/2026-11-01/latest/settlement")
    assert latest.status_code == 200
    assert latest.headers["X-Revision"] == "2026-11-01-r1"
    assert client.get("/download/2026-11-01/9/settlement").status_code == 404
    assert client.get("/download/2026-11-01/0/settlement").status_code == 400


def test_second_publish_increments_revision_and_keeps_first(client):
    preview_and_confirm(client)
    data = DEFAULT_CSV.replace("G22,88", "G22,99").encode()
    preview, confirmed = preview_and_confirm(client, data=data)
    assert confirmed.status_code == 201
    assert confirmed.get_json()["rev"] == 2
    first = client.get("/download/2026-11-01/1/settlement").data.decode()
    second = client.get("/download/2026-11-01/2/settlement").data.decode()
    assert "G22,88" in first and "G22,99" not in first
    assert "G22,99" in second
    assert client.get("/download/2026-11-01/latest/settlement").headers["X-Revision"] \
        == "2026-11-01-r2"


def test_same_preview_cannot_confirm_twice(client):
    response = upload(client)
    preview_id = response.get_json()["preview_id"]
    first = client.post(f"/api/previews/{preview_id}/confirm", headers={"Accept": "application/json"})
    second = client.post(f"/api/previews/{preview_id}/confirm", headers={"Accept": "application/json"})
    assert first.status_code == 201
    assert second.status_code == 404
    assert second.get_json()["error"] == "preview_not_found"


def test_contract_change_invalidates_pending_preview(client):
    response = upload(client)
    preview = response.get_json()
    changed = client.put(
        "/api/contracts/settlement",
        json={"columns": [{"name": "ticket_id", "type": "text"},
                          {"name": "amount", "type": "integer"}]},
    )
    assert changed.status_code == 200
    confirm = client.post(
        f"/api/previews/{preview['preview_id']}/confirm", headers={"Accept": "application/json"}
    )
    assert confirm.status_code == 409
    assert confirm.get_json()["error"] == "stale_contract"


def test_contract_change_does_not_touch_published_files(client):
    preview_and_confirm(client)
    before = {
        kind: hashlib.sha256(client.get(f"/download/2026-11-01/1/{kind}").data).hexdigest()
        for kind in ("original", "settlement", "ridership")
    }
    client.put(
        "/api/contracts/settlement",
        json={"columns": [{"name": "train_no", "type": "text"},
                          {"name": "amount", "type": "integer"}]},
    )
    after = {
        kind: hashlib.sha256(client.get(f"/download/2026-11-01/1/{kind}").data).hexdigest()
        for kind in ("original", "settlement", "ridership")
    }
    assert before == after
    # New publishes use the new contract, frozen at publish time.
    preview, confirmed = preview_and_confirm(client)
    assert confirmed.status_code == 201
    published = confirmed.get_json()
    assert published["downstreams"]["settlement"]["columns"] == [
        {"name": "train_no", "type": "text"},
        {"name": "amount", "type": "integer"},
    ]


def test_stale_day_revision_blocks_old_confirmation(client):
    preview_and_confirm(client)
    stale = upload(client).get_json()
    preview_and_confirm(client, data=DEFAULT_CSV.replace("T1", "T1x").encode())
    confirm = client.post(
        f"/api/previews/{stale['preview_id']}/confirm", headers={"Accept": "application/json"}
    )
    assert confirm.status_code == 409
    assert confirm.get_json()["error"] == "stale_day_revision"


def test_purge_expired_previews_only_removes_expired(client, app):
    from railapp import db

    preview = upload(client).get_json()
    conn = db.connect(app.config["DB_PATH"])
    try:
        db.purge_expired_previews(conn)
        assert db.get_preview(conn, preview["preview_id"]) is not None
        conn.execute(
            "UPDATE previews SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (preview["preview_id"],),
        )
        removed = db.purge_expired_previews(conn)
        assert removed == [preview["preview_id"]]
        assert db.get_preview(conn, preview["preview_id"]) is None
    finally:
        conn.close()
    # Expired preview no longer confirmable.
    confirm = client.post(
        f"/api/previews/{preview['preview_id']}/confirm", headers={"Accept": "application/json"}
    )
    assert confirm.status_code == 404


def test_oversized_upload_rejected(client):
    big = b"a,b\n" + b"x,y\n" * (1024 * 1024 // 4 + 10)
    response = client.post(
        "/api/previews",
        data={"day": "2026-11-01", "file": (io.BytesIO(big), "big.csv")},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 413


def test_non_utf8_rejected(client):
    response = upload(client, data="ticket_id,passenger_id\nT1,P1\n".encode("gbk") + b"\xff\xfe")
    assert response.status_code == 422
    assert response.get_json()["details"]["kind"] == "encoding"


def test_invalid_day_rejected(client):
    assert upload(client, day="2026-13-01").status_code == 400
    assert upload(client, day="20261101").status_code == 400


def test_html_pages_render(client):
    for path in ("/", "/upload", "/contracts"):
        response = client.get(path)
        assert response.status_code == 200, path
    preview_and_confirm(client)
    assert client.get("/days/2026-11-01").status_code == 200
    assert client.get("/days/2020-01-01").status_code == 404


def test_duplicate_contract_columns_rejected(client):
    response = client.put(
        "/api/contracts/settlement",
        json={"columns": [{"name": "a", "type": "text"}, {"name": "a", "type": "integer"}]},
    )
    assert response.status_code == 400


def test_contract_form_post_updates(client):
    response = client.post(
        "/contracts",
        data={"settlement_contract": "ticket_id,text\namount,integer", "ridership_contract": ""},
        follow_redirects=False,
    )
    assert response.status_code == 302
    contracts = client.get("/api/contracts").get_json()
    assert [c["name"] for c in contracts["settlement"]["columns"]] == ["ticket_id", "amount"]
    assert contracts["settlement"]["version"] == 2
    # Untouched downstream keeps its version.
    assert contracts["ridership"]["version"] == 1
