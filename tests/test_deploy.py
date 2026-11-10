"""Deployment contract tests (Render free plan + Supabase + GitHub Pages).

Five areas are pinned here:

1. dburl normalization — hosted Postgres URLs (Render's bare ``postgres://``,
   Supabase's ``postgresql://`` + mandatory TLS) must reach SQLAlchemy WITH
   the psycopg dialect and raw psycopg WITHOUT it, idempotently.
2. The root "API alive" card — served at ``/`` ahead of the MCP catch-all,
   linking the GitHub Pages docs, dashboard, OpenAPI, health, and repo.
3. In-process worker gating — ``WORKER_IN_PROCESS`` opt-in, and the
   deliberate decline-to-start when the agent key does not exist yet (the
   first deploy provisions that key through this very service, so crashing
   would deadlock it).
4. Docs-site self-containment — no scripts, no external assets, every
   section present (the site must render identically offline and on Pages).
5. render.yaml — free plan, in-process worker on, every secret
   ``sync: false``, and the Supabase password absent from every file git
   tracks (or could track).
"""

import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dburl

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. dburl normalization
# ---------------------------------------------------------------------------

def test_for_sqlalchemy_rewrites_bare_postgres_scheme():
    assert (dburl.for_sqlalchemy("postgres://u:p@h/db")
            == "postgresql+psycopg://u:p@h/db")


def test_for_sqlalchemy_rewrites_postgresql_scheme():
    assert (dburl.for_sqlalchemy("postgresql://u:p@h/db")
            == "postgresql+psycopg://u:p@h/db")


def test_for_sqlalchemy_passes_through_existing_dialect():
    url = "postgresql+psycopg://u:p@h/db"
    assert dburl.for_sqlalchemy(url) == url


def test_for_sqlalchemy_appends_sslmode_for_supabase_direct_host():
    out = dburl.for_sqlalchemy(
        "postgres://u:p@db.abc123.supabase.co:5432/postgres")
    assert out == ("postgresql+psycopg://u:p@db.abc123.supabase.co:5432"
                   "/postgres?sslmode=require")


def test_for_sqlalchemy_supabase_pooler_host_uses_ampersand():
    # The real prod shape: plain postgresql:// session-pooler URL that
    # already carries a query string.
    out = dburl.for_sqlalchemy(
        "postgresql://u:p@aws-1-ap-southeast-2.pooler.supabase.com:5432"
        "/postgres?opt=1")
    assert out.startswith("postgresql+psycopg://")
    assert out.endswith("&sslmode=require")


def test_for_sqlalchemy_does_not_duplicate_existing_sslmode():
    for pinned in ("require", "prefer"):
        url = f"postgres://u:p@h.supabase.co/db?sslmode={pinned}"
        out = dburl.for_sqlalchemy(url)
        assert out.count("sslmode=") == 1
        assert out.endswith(f"?sslmode={pinned}")


def test_for_sqlalchemy_leaves_non_supabase_urls_alone():
    url = "postgres://tollgate:tollgate@localhost:5433/tollgate"
    out = dburl.for_sqlalchemy(url)
    assert out == "postgresql+psycopg://tollgate:tollgate@localhost:5433/tollgate"
    assert "sslmode" not in out


def test_for_sqlalchemy_strips_whitespace():
    assert (dburl.for_sqlalchemy("  postgres://u:p@h/db\n")
            == "postgresql+psycopg://u:p@h/db")


def test_for_psycopg_never_adds_the_dialect_suffix():
    # psycopg rejects postgresql+psycopg:// — for_psycopg must never add it.
    for url in ("postgres://u:p@h/db", "postgresql://u:p@h/db"):
        out = dburl.for_psycopg(url)
        assert "+psycopg" not in out
        assert out == url  # non-Supabase URLs pass through completely


def test_for_psycopg_adds_supabase_sslmode():
    out = dburl.for_psycopg(
        "postgresql://u:p@db.abc123.supabase.co:5432/postgres")
    assert out == ("postgresql://u:p@db.abc123.supabase.co:5432/postgres"
                   "?sslmode=require")
    assert "+psycopg" not in out


def test_both_helpers_are_idempotent():
    once = dburl.for_sqlalchemy("postgres://u:p@h.supabase.co/db")
    assert dburl.for_sqlalchemy(once) == once
    once_p = dburl.for_psycopg("postgresql://u:p@h.supabase.co/db")
    assert dburl.for_psycopg(once_p) == once_p


# ---------------------------------------------------------------------------
# 2. Root "API alive" card
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def admin_env(monkeypatch):
    # Same pattern as tests/test_dashboard_static.py: a deterministic admin
    # key makes the guard answer 401 (not 503 ADMIN_DISABLED) so route-order
    # checks prove the guard was REACHED.
    monkeypatch.setenv("ADMIN_API_KEY", "tga_" + "deploy_" + "9" * 40)


@pytest.fixture()
def client():
    from main import app

    with TestClient(app) as c:
        yield c


def test_root_serves_the_api_alive_card(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Toll Gate" in r.text
    assert "rajnair06.github.io/tollgate" in r.text


def test_root_card_links_docs_dashboard_openapi_health_repo(client):
    body = client.get("/").text
    for href in ("/dashboard/", "/docs", "/health",
                 "https://rajnair06.github.io/tollgate/",
                 "https://github.com/RajNair06/tollgate"):
        assert f'href="{href}"' in body, href


def test_root_route_does_not_shadow_the_mounts(client):
    # The card is registered BEFORE the MCP catch-all; everything around it
    # must keep working: health, and unknown paths still fall through to the
    # catch-all mount (404 from the MCP sub-app, not from the card route).
    assert client.get("/health").status_code == 200
    assert client.get("/definitely-not-a-route").status_code == 404


# ---------------------------------------------------------------------------
# 3. In-process worker gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("1", True),
    ("true", True),
    ("YES", True),
    ("On", True),
    ("0", False),
    ("false", False),
    ("", False),
    ("off", False),
])
def test_worker_requested_reads_the_env(monkeypatch, value, expected):
    from worker.inprocess import worker_requested

    monkeypatch.setenv("WORKER_IN_PROCESS", value)
    assert worker_requested() is expected


def test_worker_requested_default_off(monkeypatch):
    from worker.inprocess import worker_requested

    monkeypatch.delenv("WORKER_IN_PROCESS", raising=False)
    assert worker_requested() is False


def test_start_declines_without_agent_key(monkeypatch):
    # Chicken-and-egg by design: AGENT_API_KEY is provisioned through the
    # deployed service's admin API, so a missing key must log loudly and
    # return None INSTEAD of crashing the first deploy. The check happens
    # before any engine/runner is built — no DB fixtures needed.
    from worker.inprocess import start_in_process_worker

    monkeypatch.setenv("WORKER_IN_PROCESS", "1")
    monkeypatch.setenv("AGENT_API_KEY", "")
    assert start_in_process_worker() is None


# ---------------------------------------------------------------------------
# 4. Docs-site self-containment (GitHub Pages)
# ---------------------------------------------------------------------------

@pytest.fixture()
def docs_html():
    return (REPO_ROOT / "docs" / "index.html").read_text(encoding="utf-8")


def test_docs_have_no_scripts_or_external_assets(docs_html):
    assert "<script" not in docs_html
    assert 'src="http' not in docs_html
    # No external stylesheets — all CSS is inline (no <link> tags at all):
    assert "<link" not in docs_html


@pytest.mark.parametrize("section_id", [
    "overview", "how", "architecture", "agent", "webhook-worker",
    "dashboard", "getting-started", "api", "configuration", "deployment",
])
def test_docs_contain_every_section(docs_html, section_id):
    assert f'id="{section_id}"' in docs_html
    assert f'href="#{section_id}"' in docs_html  # and the sidebar links it


# ---------------------------------------------------------------------------
# 5. render.yaml blueprint + zero secrets in git
# ---------------------------------------------------------------------------

@pytest.fixture()
def render_yaml():
    return (REPO_ROOT / "render.yaml").read_text(encoding="utf-8")


def test_render_yaml_pins_free_plan_and_in_process_worker(render_yaml):
    assert "plan: free" in render_yaml
    assert "WORKER_IN_PROCESS" in render_yaml


@pytest.mark.parametrize("key", [
    "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "DATABASE_URL",
    "AGENT_DATABASE_URL", "ADMIN_API_KEY", "GEMINI_API_KEY",
    "AGENT_API_KEY", "AGENT_MCP_URL", "PAYPAL_WEBHOOK_ID",
])
def test_render_yaml_secrets_are_dashboard_only(render_yaml, key):
    # Every secret-ish env var must be `sync: false` (value set in the
    # Render dashboard at deploy time) and must NOT carry a committed value.
    blocks = re.split(r"\n\s*- key: ", render_yaml)[1:]
    block = next((b for b in blocks if b.startswith(key + "\n")), None)
    assert block is not None, f"{key} missing from render.yaml"
    assert "sync: false" in block, f"{key} must be sync:false"
    assert "value:" not in block, f"{key} must not carry a committed value"


def test_supabase_password_appears_in_no_git_file():
    # The password is assembled from two fragments so the literal never
    # appears in THIS file either (it is itself git-tracked). Then every
    # file git tracks — or could track (untracked but not ignored) — is
    # scanned byte-wise. .env is gitignored and stays local.
    password = "7pI1O" + "osY1uSsJlJK"
    needle = password.encode()

    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT, capture_output=True, check=True,
    ).stdout.decode()
    files = [f for f in listing.split("\0") if f]
    assert len(files) > 50, "git ls-files returned suspiciously little"

    leaks = [
        rel for rel in files
        if (REPO_ROOT / rel).is_file() and needle in (REPO_ROOT / rel).read_bytes()
    ]
    assert leaks == [], f"Supabase password found in tracked files: {leaks}"
