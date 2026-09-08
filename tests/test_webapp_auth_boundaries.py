from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mms_shp_detection.webapp import WebAppConfig, create_app


@pytest.fixture
def secured_app(tmp_path: Path):
    root = tmp_path.resolve()
    data = root / "data"
    data.mkdir()
    static = root / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>test UI</html>", encoding="utf-8")
    (static / "assets" / "test.js").write_text("// test asset", encoding="utf-8")
    return create_app(WebAppConfig(
        allowed_roots=[data], state_dir=root / "state", static_dir=static,
        enable_run_worker=False, auth_username="operator", auth_password="fixture-only",
    ))


def test_every_registered_http_route_requires_basic_auth(secured_app):
    # Exercise the actual router inventory, including docs and state mutations.
    with TestClient(secured_app) as client:
        checked = set()
        for template, operations in secured_app.openapi()["paths"].items():
            for method in operations:
                if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                    continue
                method = method.upper()
                path = re.sub(r"\{[^}]+\}", "missing", template)
                response = client.request(method, path)
                assert response.status_code == 401, (method, path, response.text)
                assert "Basic" in response.headers["www-authenticate"]
                checked.add((method, template))
        assert ("GET", "/api/runs/{run_id}/events") in checked
        assert ("GET", "/api/runs/{run_id}/archive") in checked
        assert ("POST", "/api/uploads") in checked
        for path in ("/", "/assets/test.js", "/nested/workspace", "/unknown", "/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 401


def test_authentication_challenges_have_security_headers_and_are_not_cached(secured_app):
    with TestClient(secured_app) as client:
        for path in ("/", "/api/health", "/assets/test.js"):
            response = client.get(path)
            assert response.status_code == 401
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "SAMEORIGIN"
            assert response.headers["cache-control"] == "no-store"


def test_authenticated_ui_api_and_assets_keep_their_response_contract(secured_app):
    with TestClient(secured_app) as client:
        client.auth = ("operator", "fixture-only")
        assert client.get("/").text == "<html>test UI</html>"
        assert client.get("/api/health").json()["queue"]["worker_enabled"] is False
        assert client.get("/api/build").headers["cache-control"] == "no-store"
        asset = client.get("/assets/test.js")
        assert asset.text == "// test asset"
        assert asset.headers["cache-control"] == "private, max-age=31536000, immutable"
        assert client.get("/api/runs/missing/events").status_code == 404
        assert client.get("/api/runs/missing/archive").status_code == 404


@pytest.mark.parametrize("headers", [
    {"Origin": "https://other.example"},
    {"Origin": "http://testserver:81"},
    {"Origin": "http://testserver:0"},
    {"Origin": "https://testserver"},
    {"Origin": "null"},
    {"Origin": "http://testserver/path"},
    {"Origin": "http://user@testserver"},
    {"Origin": "http://testserver", "Host": "[invalid"},
    {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "cross-site"},
    [("Origin", "http://testserver"), ("Origin", "https://other.example")],
])
def test_cross_origin_mutation_is_rejected_before_upload_creation(secured_app, headers):
    with TestClient(secured_app) as client:
        response = client.post(
            "/api/uploads", auth=("operator", "fixture-only"), headers=headers,
            json={"name": "fixture", "files": [{"path": "tiny.txt", "size": 1}]},
        )
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
        assert not (secured_app.state.config.state_dir / "upload-staging").exists()


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "http://testserver"},
    {"Origin": "http://testserver:80", "Sec-Fetch-Site": "same-origin"},
])
def test_same_origin_and_non_browser_uploads_remain_supported(secured_app, headers):
    with TestClient(secured_app) as client:
        response = client.post(
            "/api/uploads", auth=("operator", "fixture-only"), headers=headers,
            json={"name": "fixture", "files": [{"path": "한글.txt", "size": 1}]},
        )
        assert response.status_code == 201, response.text


def test_remote_acknowledgement_does_not_enable_authentication(monkeypatch):
    from scripts.run_web import build_parser

    monkeypatch.delenv("MMS_WEB_USERNAME", raising=False)
    parser = build_parser()
    args = parser.parse_args(["--allow-remote-bind"])
    assert args.allow_remote_bind is True
    assert args.auth_username is None
    help_text = " ".join(parser.format_help().split())
    assert "does not configure authentication or TLS" in help_text
    assert "no built-in login" not in help_text
