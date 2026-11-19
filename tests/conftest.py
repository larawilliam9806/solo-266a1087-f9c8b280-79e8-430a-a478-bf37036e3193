import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from railapp import create_app  # noqa: E402

DEFAULT_CSV = (
    "ticket_id,passenger_id,train_no,amount,travel_date,depart_station,arrive_station,passenger_count\n"
    "T1,P1,G101,55,2026-11-01,北京南,上海虹桥,1\n"
    "T2,P2,G101,120,2026-11-01,北京南,上海虹桥,1\n"
    "T3,P3,G22,88,2026-11-01,天津,北京南,2\n"
)


@pytest.fixture
def app(tmp_path):
    application = create_app({"DATA_DIR": str(tmp_path / "data"), "TESTING": True})
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csv_bytes():
    return DEFAULT_CSV.encode("utf-8")


def upload(client, day="2026-11-01", data=None, filename="tickets.csv"):
    return client.post(
        "/api/previews",
        data={
            "day": day,
            "file": (__import__("io").BytesIO(data if data is not None else DEFAULT_CSV.encode()), filename),
        },
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )


def preview_and_confirm(client, day="2026-11-01", data=None):
    response = upload(client, day=day, data=data)
    assert response.status_code == 201, response.get_json()
    preview = response.get_json()
    confirmed = client.post(
        f"/api/previews/{preview['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    return preview, confirmed
