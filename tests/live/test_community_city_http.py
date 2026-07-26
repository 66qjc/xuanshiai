"""Real-HTTP community city matrix.

Run only when a server is already up and LIVE_API_BASE is set, e.g.:

  LIVE_API_BASE=http://127.0.0.1:8000 \\
  SMS_PROVIDER=mock SMS_MOCK_CODE=123456 \\
  pytest tests/live/test_community_city_http.py -v

Uses httpx against the network stack (not TestClient).
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

BASE = (os.environ.get("LIVE_API_BASE") or "").rstrip("/")
PHONE = os.environ.get("LIVE_PHONE") or "13800001001"
SMS_CODE = os.environ.get("SMS_MOCK_CODE") or "123456"

pytestmark = pytest.mark.skipif(
    not BASE,
    reason="LIVE_API_BASE not set; skip real-HTTP city matrix",
)


def _api(path: str) -> str:
    if path.startswith("http"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    if path.startswith("/api/"):
        return BASE + path
    return BASE + "/api/v1" + path


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(timeout=20.0) as c:
        yield c


@pytest.fixture(scope="module")
def auth_headers(client: httpx.Client) -> dict[str, str]:
    send = client.post(
        _api("/auth/sms/send"),
        json={"phone": PHONE, "purpose": "login"},
    )
    # 60s resend: 202 or 429 both ok if code still valid / prior login path
    assert send.status_code in (200, 202, 429), send.text

    login = client.post(
        _api("/auth/phone/login"),
        json={
            "phone": PHONE,
            "purpose": "login",
            "code": SMS_CODE,
        },
    )
    assert login.status_code == 200, login.text
    body = login.json()
    token = body.get("access_token") or (body.get("data") or {}).get("access_token")
    assert token, body
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def city_preference_headers(client: httpx.Client) -> dict[str, str]:
    phone = f"139{time.time_ns() % 100_000_000:08d}"
    send = client.post(
        _api("/auth/sms/send"),
        json={"phone": phone, "purpose": "login"},
    )
    assert send.status_code in (200, 202, 429), send.text
    login = client.post(
        _api("/auth/phone/login"),
        json={"phone": phone, "purpose": "login", "code": SMS_CODE},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    token = body.get("access_token") or (body.get("data") or {}).get("access_token")
    assert token, body
    headers = {"Authorization": f"Bearer {token}"}
    stored = client.put(
        _api("/community/city"),
        headers=headers,
        json={"name": "南京", "code": "320100"},
    )
    assert stored.status_code == 200, stored.text
    assert str(stored.json().get("code") or "").startswith("3201")
    return headers


def test_l1_login_returns_bearer(auth_headers: dict[str, str]) -> None:
    assert auth_headers["Authorization"].startswith("Bearer ")


def test_l11_city_requires_auth(client: httpx.Client) -> None:
    r = client.get(_api("/community/city"))
    assert r.status_code == 401


def test_l2_put_get_city_roundtrip(
    client: httpx.Client, auth_headers: dict[str, str]
) -> None:
    put = client.put(
        _api("/community/city"),
        headers=auth_headers,
        json={"name": "南京", "code": "320100"},
    )
    # First set 200; if already 南京 same-city also 200; if cooldown from other city 429
    assert put.status_code in (200, 429), put.text
    if put.status_code == 429:
        pytest.skip("city cooldown active; cannot roundtrip to 南京 this run")
    data = put.json()
    assert data.get("name") == "南京"
    code = data.get("code")
    assert code in ("320100", "3201", None) or str(code).startswith("3201")

    got = client.get(_api("/community/city"), headers=auth_headers)
    assert got.status_code == 200, got.text
    g = got.json()
    assert g.get("name") == "南京"


def test_l3_put_invalid_city_422(
    client: httpx.Client, auth_headers: dict[str, str]
) -> None:
    for payload in ({"name": "未设置"}, {"name": ""}, {"name": "   "}):
        r = client.put(_api("/community/city"), headers=auth_headers, json=payload)
        assert r.status_code == 422, (payload, r.text)


def test_l4_cooldown_rejects_switch(
    client: httpx.Client, auth_headers: dict[str, str]
) -> None:
    # Ensure Nanjing is current (or skip if cannot set)
    put_nj = client.put(
        _api("/community/city"),
        headers=auth_headers,
        json={"name": "南京", "code": "320100"},
    )
    if put_nj.status_code == 429:
        # Already on another city within cooldown — switching to Hangzhou should also 429
        put_hz = client.put(
            _api("/community/city"),
            headers=auth_headers,
            json={"name": "杭州", "code": "330100"},
        )
        assert put_hz.status_code == 429, put_hz.text
        return

    assert put_nj.status_code == 200, put_nj.text
    put_hz = client.put(
        _api("/community/city"),
        headers=auth_headers,
        json={"name": "杭州", "code": "330100"},
    )
    assert put_hz.status_code == 429, put_hz.text
    got = client.get(_api("/community/city"), headers=auth_headers)
    assert got.status_code == 200
    assert got.json().get("name") == "南京"


def test_l5_l6_l7_feed_location_only(
    client: httpx.Client, auth_headers: dict[str, str]
) -> None:
    # Publish a Nanjing-located post
    create = client.post(
        _api("/community/posts"),
        headers=auth_headers,
        json={
            "content": f"live-city-e2e {int(time.time())}",
            "location": "南京",
        },
    )
    assert create.status_code in (200, 201), create.text
    post = create.json()
    post_id = post.get("id") or (post.get("data") or {}).get("id")
    assert post_id, post

    feed_nj = client.get(
        _api("/community/posts"),
        headers=auth_headers,
        params={
            "mode": "city",
            "city": "南京",
            "city_code": "320100",
            "page": 1,
            "page_size": 20,
        },
    )
    assert feed_nj.status_code == 200, feed_nj.text
    body_nj = feed_nj.json()
    items_nj = body_nj.get("items") or body_nj.get("list") or []
    ids_nj = [it.get("id") for it in items_nj]
    assert post_id in ids_nj, (post_id, ids_nj[:10], body_nj.get("total"))
    for it in items_nj:
        loc = (it.get("location") or "").strip()
        assert loc == "南京" or loc.startswith("南京"), loc

    feed_hz = client.get(
        _api("/community/posts"),
        headers=auth_headers,
        params={
            "mode": "city",
            "city": "杭州",
            "city_code": "330100",
            "page": 1,
            "page_size": 20,
        },
    )
    assert feed_hz.status_code == 200, feed_hz.text
    body_hz = feed_hz.json()
    items_hz = body_hz.get("items") or body_hz.get("list") or []
    ids_hz = [it.get("id") for it in items_hz]
    assert post_id not in ids_hz, "location=南京 post must not appear in 杭州 feed"


def test_l9_feed_unset_city_422(
    client: httpx.Client, auth_headers: dict[str, str]
) -> None:
    r = client.get(
        _api("/community/posts"),
        headers=auth_headers,
        params={"mode": "city", "city": "未设置", "page": 1, "page_size": 10},
    )
    assert r.status_code == 422, r.text


def test_l10_feed_mode_city_uses_preference(
    client: httpx.Client, city_preference_headers: dict[str, str]
) -> None:
    r = client.get(
        _api("/community/posts"),
        headers=city_preference_headers,
        params={"mode": "city", "page": 1, "page_size": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    items = body.get("items") or body.get("list") or []
    for it in items:
        loc = (it.get("location") or "").strip()
        assert loc == "南京" or loc.startswith("南京"), loc


def test_l11_feed_without_request_or_community_preference_is_422(
    client: httpx.Client,
) -> None:
    phone = os.environ.get("LIVE_UNSET_CITY_PHONE") or f"139{int(time.time()) % 10_000_000:08d}"
    send = client.post(_api("/auth/sms/send"), json={"phone": phone, "purpose": "login"})
    assert send.status_code in (200, 202, 429), send.text
    login = client.post(
        _api("/auth/phone/login"),
        json={"phone": phone, "purpose": "login", "code": SMS_CODE},
    )
    assert login.status_code == 200, login.text
    body = login.json()
    token = body.get("access_token") or (body.get("data") or {}).get("access_token")
    assert token, body

    response = client.get(
        _api("/community/posts"),
        headers={"Authorization": f"Bearer {token}"},
        params={"mode": "city", "page": 1, "page_size": 10},
    )
    assert response.status_code == 422, response.text
