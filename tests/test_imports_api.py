"""Tests for api/imports.py.

Preview endpoint is tested end-to-end via TestClient since it doesn't touch
the stores (file staging + mapping inference is self-contained).

The commit endpoint requires MongoDB + BatchProcessor so its happy path is
exercised manually via the live server; here we only verify the error
paths that don't require a store (expired file_id, unknown file_id).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api.imports import router


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Redirect the staging dir into tmp_path so tests don't pollute .omc/imports.
    from beever_atlas.infra.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "file_import_staging_dir", str(tmp_path / "imports"))
    # Turn off the LLM by default so tests are deterministic and offline.
    monkeypatch.setattr(settings, "file_import_llm_mapping_enabled", False)
    app = FastAPI()
    app.include_router(router)
    # Bypass `require_user` on this isolated app — RES-177 H1 adds the
    # dependency directly on preview/commit handlers, so the global
    # conftest override (which targets the top-level app) does not cover
    # this ad-hoc test app.
    from beever_atlas.infra.auth import Principal, require_user

    app.dependency_overrides[require_user] = lambda: Principal("user:test", kind="user")
    return TestClient(app)


DISCORD_CSV = (
    "AuthorID,Author,Date,Content,Attachments,Reactions\n"
    '"111","alice","2024-01-01T10:00:00+00:00","hello world","",""\n'
    '"222","bob","2024-01-01T11:30:00+00:00","酒干倘賣無","",""\n'
)


def test_preview_discord_csv_detects_preset(client: TestClient) -> None:
    resp = client.post(
        "/api/imports/preview",
        files={"file": ("chat.csv", DISCORD_CSV.encode("utf-8"), "text/csv")},
        data={"use_llm": "false"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["preset"] == "discord_chat_exporter"
    assert body["mapping"]["content"] == "Content"
    assert body["mapping"]["author_name"] == "Author"
    assert body["needs_review"] is False
    assert body["overall_confidence"] == 1.0
    # Sample messages round-trip CJK content.
    assert any("酒干倘賣無" == m["content"] for m in body["sample_messages"])
    assert body["encoding"] == "utf-8"
    assert body["format"] == "csv"


def test_preview_unknown_headers_needs_review(client: TestClient) -> None:
    csv = "x,y,z\n1,2,3\n"
    resp = client.post(
        "/api/imports/preview",
        files={"file": ("weird.csv", csv.encode("utf-8"), "text/csv")},
        data={"use_llm": "false"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["preset"] is None
    assert body["needs_review"] is True


def test_preview_undecodable_file_returns_400(client: TestClient) -> None:
    bad_bytes = b"\x81\x00\x82\x00\x83\x00" * 200
    resp = client.post(
        "/api/imports/preview",
        files={"file": ("bad.csv", bad_bytes, "text/csv")},
        data={"use_llm": "false"},
    )
    assert resp.status_code == 400
    assert "decode" in resp.json()["detail"].lower()


def test_commit_malformed_file_id_returns_400(client: TestClient) -> None:
    # CodeQL py/path-injection (alerts #39, #40, #41): non-UUID file_ids
    # are rejected at the path-construction boundary before any filesystem
    # access, preventing `../` traversal out of the staging dir.
    resp = client.post(
        "/api/imports/commit",
        json={
            "file_id": "does-not-exist",
            "channel_name": "x",
            "mapping": {"content": "Content"},
        },
    )
    assert resp.status_code == 400
    assert "Invalid file_id" in resp.json()["detail"]


def test_commit_unknown_uuid_file_id_returns_404(client: TestClient) -> None:
    import uuid as _uuid

    resp = client.post(
        "/api/imports/commit",
        json={
            "file_id": str(_uuid.uuid4()),
            "channel_name": "x",
            "mapping": {"content": "Content"},
        },
    )
    assert resp.status_code == 404
    assert "Unknown file_id" in resp.json()["detail"]


def test_commit_expired_file_id_returns_410(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force TTL to 0 so any stage is immediately expired.
    from beever_atlas.infra.config import get_settings

    settings = get_settings()

    # First, create a valid stage by calling preview.
    resp = client.post(
        "/api/imports/preview",
        files={"file": ("chat.csv", DISCORD_CSV.encode("utf-8"), "text/csv")},
        data={"use_llm": "false"},
    )
    assert resp.status_code == 200
    file_id = resp.json()["file_id"]

    # Now set TTL to something negative via monkeypatch so the check reports expired.
    monkeypatch.setattr(settings, "file_import_staging_ttl_seconds", -1)

    resp = client.post(
        "/api/imports/commit",
        json={
            "file_id": file_id,
            "channel_name": "x",
            "mapping": {"content": "Content"},
        },
    )
    assert resp.status_code == 410
    assert "expired" in resp.json()["detail"].lower()


def test_commit_invalid_mapping_returns_422(client: TestClient) -> None:
    # First preview to stage a file.
    resp = client.post(
        "/api/imports/preview",
        files={"file": ("chat.csv", DISCORD_CSV.encode("utf-8"), "text/csv")},
        data={"use_llm": "false"},
    )
    file_id = resp.json()["file_id"]

    # Commit with a mapping that references a non-existent column.
    resp = client.post(
        "/api/imports/commit",
        json={
            "file_id": file_id,
            "channel_name": "x",
            "mapping": {"content": "NotARealColumn"},
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "mapping_errors" in detail
    assert any("NotARealColumn" in e for e in detail["mapping_errors"])
