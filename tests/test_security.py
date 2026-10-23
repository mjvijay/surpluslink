import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "pytest-secret-key"
os.environ["ADMIN_PASSWORD"] = "pytest-admin-password"
os.environ["DATABASE_PATH"] = str(
    Path(tempfile.gettempdir()) / f"surpluslink-pytest-import-{os.getpid()}.db"
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import app as backend


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "test.db"))
    backend.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    backend.init_db()
    return backend.app.test_client()


def register_user(client, role, email):
    response = client.post(
        "/api/register",
        json={
            "name": "Test User",
            "email": email,
            "phone": "1234567890",
            "role": role,
            "password": "unique-test-password",
        },
        environ_base={"REMOTE_ADDR": f"198.51.100.{len(email) % 250 + 1}"},
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json['token']}"}


def create_listing(client, donor_headers):
    response = client.post(
        "/api/listings",
        headers=donor_headers,
        json={
            "title": "Fresh surplus meals",
            "description": "Ready for pickup",
            "food_type": "cooked",
            "quantity": 20,
            "address": "123 Main Street",
            "lat": 13.0674,
            "lng": 80.2376,
            "pickup_by": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
        },
    )
    assert response.status_code == 201
    return response.json["listing"]["id"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/listings"),
        ("PUT", "/api/listings"),
        ("PUT", "/api/listings/1"),
        ("DELETE", "/api/listings"),
        ("DELETE", "/api/listings/1"),
        ("POST", "/api/listings/1/claim"),
        ("POST", "/api/listings/1/release"),
        ("POST", "/api/listings/1/complete"),
        ("POST", "/api/listings/1/cancel"),
        ("GET", "/api/admin/users"),
    ],
)
def test_role_routes_require_authentication(client, method, path):
    response = client.open(path, method=method, json={})

    assert response.status_code == 401


def test_receiver_cannot_edit_another_donors_listing(client):
    donor_headers = register_user(client, "donor", "owner@example.invalid")
    receiver_headers = register_user(client, "receiver", "receiver@example.invalid")
    listing_id = create_listing(client, donor_headers)

    response = client.put(
        f"/api/listings/{listing_id}", headers=receiver_headers, json={}
    )

    assert response.status_code == 403


def test_donor_cannot_claim_own_listing(client):
    donor_headers = register_user(client, "donor", "claimant@example.invalid")
    listing_id = create_listing(client, donor_headers)

    response = client.post(
        f"/api/listings/{listing_id}/claim", headers=donor_headers
    )

    assert response.status_code == 403


def test_admin_password_rotation_revokes_existing_tokens(client, monkeypatch):
    response = client.post(
        "/api/login",
        json={"email": "admin@surpluslink.org", "password": "pytest-admin-password"},
    )
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json['token']}"}

    monkeypatch.setattr(backend, "ADMIN_PASSWORD", "rotated-admin-password")
    backend.init_db()

    assert client.get("/api/admin/users", headers=headers).status_code == 401
    assert client.post(
        "/api/login",
        json={"email": "admin@surpluslink.org", "password": "rotated-admin-password"},
    ).status_code == 200


def test_only_one_receiver_wins_a_double_claim(client):
    donor_headers = register_user(client, "donor", "race-donor@example.invalid")
    receiver_headers = [
        register_user(client, "receiver", "race-one@example.invalid"),
        register_user(client, "receiver", "race-two@example.invalid"),
    ]
    listing_id = create_listing(client, donor_headers)
    barrier = Barrier(2)

    def claim(headers):
        with backend.app.test_client() as concurrent_client:
            barrier.wait(timeout=5)
            response = concurrent_client.post(
                f"/api/listings/{listing_id}/claim", headers=headers
            )
            return response.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(claim, receiver_headers))

    assert sorted(statuses) == [200, 409]