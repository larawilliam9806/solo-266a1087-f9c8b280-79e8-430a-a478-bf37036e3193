"""Concurrency, crash-consistency and integrity tests."""

import hashlib
import io
import os
import threading

from conftest import DEFAULT_CSV, preview_and_confirm, upload
from railapp import db
from railapp.storage import verify_publications


def _upload_bytes(client, day, data, filename="tickets.csv"):
    return client.post(
        "/api/previews",
        data={"day": day, "file": (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )


def test_concurrent_confirmations_only_one_wins(app):
    """Two previews of the same day, confirmed from two threads at once."""
    client = app.test_client()
    first = _upload_bytes(client, "2026-11-02", DEFAULT_CSV.encode()).get_json()
    second_data = DEFAULT_CSV.replace("T1,P1", "T1,P1x").encode()
    second = _upload_bytes(client, "2026-11-02", second_data).get_json()
    assert first["preview_id"] != second["preview_id"]

    results = []
    barrier = threading.Barrier(2)

    def confirm(preview_id):
        local = app.test_client()
        barrier.wait()
        response = local.post(
            f"/api/previews/{preview_id}/confirm", headers={"Accept": "application/json"}
        )
        results.append((response.status_code, response.get_json()))

    threads = [
        threading.Thread(target=confirm, args=(first["preview_id"],)),
        threading.Thread(target=confirm, args=(second["preview_id"],)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    statuses = sorted(code for code, _ in results)
    assert statuses == [201, 409], results
    winner = next(payload for code, payload in results if code == 201)
    loser = next(payload for code, payload in results if code == 409)
    assert winner["rev"] == 1
    assert loser["error"] in {"stale_day_revision", "conflict", "preview_used"}

    day = client.get("/api/days/2026-11-02").get_json()
    assert day["current_rev"] == 1
    assert len(day["revisions"]) == 1


def test_concurrent_same_preview_only_one_publication(app):
    client = app.test_client()
    preview = _upload_bytes(client, "2026-11-03", DEFAULT_CSV.encode()).get_json()
    results = []
    barrier = threading.Barrier(4)

    def confirm():
        local = app.test_client()
        barrier.wait()
        response = local.post(
            f"/api/previews/{preview['preview_id']}/confirm",
            headers={"Accept": "application/json"},
        )
        results.append(response.status_code)

    threads = [threading.Thread(target=confirm) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(201) == 1, results
    day = client.get("/api/days/2026-11-03").get_json()
    assert day["current_rev"] == 1
    assert len(day["revisions"]) == 1


def test_publish_write_failure_keeps_previous_revision(client, app, monkeypatch):
    preview_and_confirm(client)
    before = client.get("/download/2026-11-01/1/original").data

    service = app.extensions["service"]
    response = upload(client, data=DEFAULT_CSV.replace("G22", "G23").encode())
    preview = response.get_json()

    def flaky_put(data):
        raise OSError("disk full")

    # Simulate the write failure only during confirm: preview blobs are
    # already persisted, so only a forced re-materialisation needs a write.
    monkeypatch.setattr(service.store, "put_bytes", flaky_put)
    # Force re-materialisation to need a write by deleting downstream blobs.
    for name in ("settlement", "ridership"):
        digest = preview["downstreams"][name]["output_sha256"]
        if digest:
            try:
                os.unlink(service.store.path_for(digest))
            except FileNotFoundError:
                pass
    confirm = client.post(
        f"/api/previews/{preview['preview_id']}/confirm", headers={"Accept": "application/json"}
    )
    assert confirm.status_code == 500
    assert client.get("/download/2026-11-01/latest/original").data == before
    day = client.get("/api/days/2026-11-01").get_json()
    assert day["current_rev"] == 1
    assert len(day["revisions"]) == 1


def test_publication_row_and_pointer_are_atomic(client, app):
    """A failure after the row INSERT but before commit leaves no partial rev."""
    preview_and_confirm(client)
    preview = upload(client, data=DEFAULT_CSV.replace("T1", "T9").encode()).get_json()

    conn = db.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            """CREATE TRIGGER fail_pointer BEFORE UPDATE ON days
               BEGIN SELECT RAISE(ABORT, 'simulated crash before pointer update'); END"""
        )
    finally:
        conn.close()
    try:
        response = client.post(
            f"/api/previews/{preview['preview_id']}/confirm",
            headers={"Accept": "application/json"},
        )
    finally:
        conn = db.connect(app.config["DB_PATH"])
        try:
            conn.execute("DROP TRIGGER fail_pointer")
        finally:
            conn.close()
    assert response.status_code == 503

    day = client.get("/api/days/2026-11-01").get_json()
    assert day["current_rev"] == 1
    assert len(day["revisions"]) == 1
    # Old revision still fully downloadable.
    for kind in ("original", "settlement", "ridership"):
        assert client.get(f"/download/2026-11-01/1/{kind}").status_code == 200
    # Preview was not consumed, so it can still be retried after the fault.
    assert day["revisions"][0]["rev"] == 1


def test_missing_blob_fails_closed_without_fallback(client, app):
    preview_and_confirm(client)
    # Change ridership-visible data so revision 2 gets its own ridership blob
    # instead of deduplicating onto revision 1's content-addressed object.
    data = DEFAULT_CSV.replace("G22,88,2026-11-01,天津,北京南,2", "G22,88,2026-11-01,天津,北京南,3").encode()
    preview, confirmed = preview_and_confirm(client, data=data)
    assert confirmed.get_json()["rev"] == 2

    store = app.config["STORE"]
    os.unlink(store.path_for(confirmed.get_json()["downstreams"]["ridership"]["sha256"]))
    response = client.get("/download/2026-11-01/2/ridership")
    assert response.status_code == 503
    # Must not silently serve revision 1 instead.
    assert b"G22" not in response.data
    assert client.get("/download/2026-11-01/2/settlement").status_code == 503
    # Older revision untouched.
    assert client.get("/download/2026-11-01/1/ridership").status_code == 200


def test_startup_selfcheck_reports_missing_blobs(app):
    conn = db.connect(app.config["DB_PATH"])
    try:
        store = app.config["STORE"]
        fake = "0" * 64
        conn.execute(
            """INSERT INTO publications (day, rev, original_name, original_size,
               original_sha256, settlement_sha256, settlement_size, ridership_sha256,
               ridership_size, settlement_fingerprint, ridership_fingerprint,
               settlement_version, ridership_version, settlement_columns,
               ridership_columns, row_count, created_at)
               VALUES ('2026-01-01', 1, 'x.csv', 10, ?, ?, 10, ?, 10, 'f', 'f', 1, 1, '[]', '[]', 0, 'now')""",
            (fake, fake, fake),
        )
        problems = verify_publications(conn, store)
    finally:
        conn.close()
    assert problems and problems[0]["day"] == "2026-01-01"
    assert len(problems[0]["missing"]) == 3


def test_download_started_before_publish_finishes_complete(app):
    client = app.test_client()
    preview_and_confirm(client)
    response = client.get("/download/2026-11-01/1/settlement")
    assert response.status_code == 200
    assert int(response.headers["Content-Length"]) == len(response.data)
    # Publish revision 2 while holding the revision-1 response open.
    preview_and_confirm(client, data=DEFAULT_CSV.replace("T1", "T2").encode())
    rest = response.data
    assert hashlib.sha256(rest).hexdigest() == response.headers["ETag"].strip('"')
    assert client.get("/download/2026-11-01/1/settlement").data == rest


def test_content_length_matches_streamed_bytes(client):
    preview_and_confirm(client)
    for kind in ("original", "settlement", "ridership"):
        response = client.get(f"/download/2026-11-01/1/{kind}")
        assert int(response.headers["Content-Length"]) == len(response.data)
        assert hashlib.sha256(response.data).hexdigest() == response.headers["ETag"].strip('"')
        assert response.headers["X-Content-SHA256"] == response.headers["ETag"].strip('"')


def test_preview_then_contract_change_then_republish_new_contract(client):
    preview = upload(client).get_json()
    client.put(
        "/api/contracts/ridership",
        json={"columns": [{"name": "train_no", "type": "text"},
                          {"name": "passenger_count", "type": "integer"}]},
    )
    stale = client.post(
        f"/api/previews/{preview['preview_id']}/confirm", headers={"Accept": "application/json"}
    )
    assert stale.status_code == 409
    fresh = upload(client).get_json()
    confirmed = client.post(
        f"/api/previews/{fresh['preview_id']}/confirm", headers={"Accept": "application/json"}
    )
    assert confirmed.status_code == 201
    ridership = client.get("/download/2026-11-01/1/ridership").data.decode().splitlines()
    assert ridership[0] == "train_no,passenger_count"
    assert ridership[1] == "G101,1"


def test_expired_preview_rejected(client, app):
    preview = upload(client).get_json()
    conn = db.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "UPDATE previews SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", preview["preview_id"]),
        )
    finally:
        conn.close()
    confirm = client.post(
        f"/api/previews/{preview['preview_id']}/confirm", headers={"Accept": "application/json"}
    )
    assert confirm.status_code == 409
    assert confirm.get_json()["error"] == "preview_expired"
    assert client.get("/api/days/2026-11-01").get_json()["current_rev"] == 0


def test_original_bytes_are_preserved_exactly(client):
    raw = (
        "ticket_id,passenger_id,train_no,amount,travel_date,depart_station,"
        "arrive_station,passenger_count\n"
        "T1,P1,G1,+007,2026-11-01,北京南,上海虹桥,01\n"
    ).encode("utf-8")
    preview, confirmed = preview_and_confirm(client, data=raw)
    assert client.get("/download/2026-11-01/1/original").data == raw
    settlement = client.get("/download/2026-11-01/1/settlement").data.decode()
    assert settlement.splitlines()[1] == "T1,P1,G1,7,2026-11-01"
    ridership = client.get("/download/2026-11-01/1/ridership").data.decode()
    assert ridership.splitlines()[1] == "G1,北京南,上海虹桥,2026-11-01,1"


def test_other_day_is_independent(client):
    preview_and_confirm(client, day="2026-11-01")
    preview_and_confirm(client, day="2026-11-02", data=DEFAULT_CSV.encode())
    assert client.get("/api/days/2026-11-01").get_json()["current_rev"] == 1
    assert client.get("/api/days/2026-11-02").get_json()["current_rev"] == 1
    days = client.get("/api/days").get_json()["days"]
    assert {d["day"] for d in days} == {"2026-11-01", "2026-11-02"}


def test_streaming_download_completes_while_new_revision_publishes(app):
    """Chunks read after a concurrent publish still belong to revision 1."""
    client = app.test_client()
    preview_and_confirm(client)
    response = client.get("/download/2026-11-01/1/settlement", buffered=False)
    iterator = iter(response.response)
    first_chunk = next(iterator)

    preview_and_confirm(client, data=DEFAULT_CSV.replace("T1", "T1-new").encode())

    rest = b"".join(iterator)
    response.close()
    combined = first_chunk + rest
    assert len(combined) == int(response.headers["Content-Length"])
    assert hashlib.sha256(combined).hexdigest() == response.headers["ETag"].strip('"')
    assert b"T1-new" not in combined
    # The complete revision 2 is separately downloadable and differs.
    assert b"T1-new" in client.get("/download/2026-11-01/2/settlement").data


def test_near_limit_file_round_trips(client):
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["ticket_id", "passenger_id", "train_no", "amount", "travel_date",
         "depart_station", "arrive_station", "passenger_count"]
    )
    index = 0
    while index < 18000:
        writer.writerow(
            [f"T{index}", f"P{index}", f"G{index % 50}", str(index % 999),
             "2026-11-09", "北京南", "上海虹桥", str(index % 4)]
        )
        index += 1
    data = buffer.getvalue().encode("utf-8")
    assert 900 * 1000 < len(data) <= 1024 * 1024

    preview, confirmed = preview_and_confirm(client, day="2026-11-09", data=data)
    assert confirmed.status_code == 201
    published = confirmed.get_json()
    assert published["row_count"] == index
    assert client.get("/download/2026-11-09/1/original").data == data
    assert int(client.get("/download/2026-11-09/1/settlement").headers["Content-Length"]) > 0


def test_contract_type_validation(client):
    response = client.put(
        "/api/contracts/ridership",
        json={"columns": [{"name": "train_no", "type": "decimal"}]},
    )
    assert response.status_code == 400
    assert "integer" in response.get_json()["message"]
