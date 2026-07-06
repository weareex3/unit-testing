"""Route-level tests for ui/server.py: auth enforcement, role gating, lab-save
gating, and run-file path traversal. TestClient is used without its context
manager on purpose — startup events (canonical-task seeding) must not run and
write to real storage during tests."""

import importlib

import pytest
from fastapi.testclient import TestClient

import ui.server as server


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def auth_client(monkeypatch):
    monkeypatch.setenv("TESTOPS_PIN", "test-pin-1234")
    monkeypatch.setenv("TESTOPS_AUTH_TOKEN", "a3f8c2d94b1e6570a3f8c2d94b1e6570a3f8c2d94b1e6570")
    importlib.reload(server)
    yield TestClient(server.app)
    monkeypatch.undo()
    importlib.reload(server)


def test_match_unknown_script_is_graceful(client):
    r = client.get("/api/match", params={"script": "does-not-exist.xlsx"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_lab_save_requires_name(client):
    r = client.post("/api/lab/save", json={})
    assert r.status_code == 400
    assert "Name the task" in r.json()["error"]


def test_lab_save_requires_learned_moves(client, monkeypatch):
    monkeypatch.setitem(server._LAB_LEARNED, "commands", [])
    r = client.post("/api/lab/save", json={"task_name": "t"})
    assert r.status_code == 400
    assert "No confirmed moves" in r.json()["error"]


def test_run_file_traversal_is_blocked(client):
    r = client.get("/runs/%2e%2e/secret.txt")
    assert r.status_code == 404
    r = client.get("/runs/nope/%2e%2e%2f%2e%2e%2fsecret.txt")
    assert r.status_code == 404


def test_unauthenticated_page_redirects_to_login(auth_client):
    r = auth_client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_unauthenticated_api_is_401(auth_client):
    r = auth_client.post("/api/lab/save", json={"task_name": "t"})
    assert r.status_code == 401


def test_forged_cookie_is_rejected(auth_client):
    auth_client.cookies.set(server.AUTH_COOKIE, "bm90LWEtcmVhbC1jb29raWU=")
    r = auth_client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_viewer_cannot_write(auth_client):
    auth_client.cookies.set(server.AUTH_COOKIE, server._sign_auth("v", "viewer"))
    r = auth_client.post("/api/library/rescan")
    assert r.status_code == 403


def test_valid_cookie_passes(auth_client):
    auth_client.cookies.set(server.AUTH_COOKIE, server._sign_auth("louie", "owner"))
    r = auth_client.get("/api/match", params={"script": "does-not-exist.xlsx"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
