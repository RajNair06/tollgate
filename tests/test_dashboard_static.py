"""Static dashboard serving tests (increment 9).

The dashboard is a plain directory of files mounted at /dashboard — no
build step, no CDN, no network fetches from the browser beyond the JSON
APIs. These tests pin the contract:
- index.html + app.js + styles.css are served,
- the VENDORED AG Grid assets are served (the offline guarantee),
- the mount does not shadow (or get shadowed by) the other routes:
  /health and /v1/* keep working, and the MCP catch-all still owns
  everything else.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def admin_env(monkeypatch):
    # Deterministic 401 (not 503 ADMIN_DISABLED) for the route-shadowing
    # check below — the guard must be REACHED, proving the mount order.
    monkeypatch.setenv("ADMIN_API_KEY", "tga_" + "static_" + "9" * 40)


@pytest.fixture()
def client():
    from main import app

    with TestClient(app) as c:
        yield c


def test_index_served_at_dashboard_root(client):
    r = client.get("/dashboard/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "Toll Gate" in body
    assert 'src="app.js"' in body
    # Assets are referenced LOCALLY — no CDN urls anywhere in the page.
    assert "cdn" not in body.lower()
    assert "vendor/ag-grid-community.min.noStyle.js" in body
    assert "vendor/ag-theme-quartz.css" in body


def test_dashboard_without_slash_redirects(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith("/dashboard/")


def test_styles_and_app_js_served(client):
    css = client.get("/dashboard/styles.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "--bg" in css.text  # the theme variables are really there

    js = client.get("/dashboard/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "X-Admin-Key" in js.text
    assert "/v1/admin/dashboard" in js.text


@pytest.mark.parametrize("asset,marker", [
    ("ag-grid-community.min.noStyle.js", "agGrid"),
    ("ag-grid.css", "ag-root"),
    ("ag-theme-quartz.css", "ag-theme-quartz"),
])
def test_vendored_ag_grid_assets_served(client, asset, marker):
    r = client.get(f"/dashboard/vendor/{asset}")
    assert r.status_code == 200
    assert len(r.content) > 10_000  # real bundles, not stubs
    assert marker in r.text


def test_other_routes_survive_the_mount(client):
    # The static mount must not swallow the API or health routes, and the
    # MCP catch-all must still own unknown paths.
    assert client.get("/health").status_code == 200
    assert client.get("/dashboard/nope.html").status_code == 404
    # /v1 routes still authenticate (401 without a key), not 404:
    assert client.get("/v1/admin/dashboard/metrics").status_code == 401
