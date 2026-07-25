"""Card Eater workspace API surface (backend/api_cardeater.py: CardEaterMixin),
exercised through the composed SuiteApi -- the actual glue every other test
file in this suite tests at, per CONTRACT.md's own convention. Unlike
test_cardeater_copy.py (which drives cardeater_copy.py directly), these
tests confirm the js_api methods themselves parse args correctly, catch
exceptions into the {"ok": False, "error": ...} contract, and route into the
right lower-level module."""

import os
import time

import pytest


def _template(**overrides):
    tpl = {
        "name": "Test",
        "folder_template": "{YYYYMMDD}_{Name}",
        "file_template": "{YYYYMMDD}_{Name}_{Seq}.{ext}",
        "date_source": "card_insert",
        "seq_start": None,
        "seq_padding": 3,
        "no_subfolder": False,
        "use_source_filename": False,
        "no_sequence": False,
    }
    tpl.update(overrides)
    return tpl


def _wait_for_completion(api, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = api.suite_cardeater_get_job_status(job_id)
        if all(d["status"] in ("complete", "failed", "cancelled", "paused")
               for d in status["destinations"]):
            return status
        time.sleep(0.02)
    pytest.fail(f"job {job_id} did not reach a terminal state within {timeout}s")


# ---------------------------------------------------------------------------
# Favorites (destination bookmarks)
# ---------------------------------------------------------------------------

def test_favorite_add_list_remove_round_trip(api):
    added = api.suite_cardeater_add_favorite("Editing Drive", "/Volumes/Editing")
    assert added.get("ok"), added
    listed = api.suite_cardeater_list_favorites()
    assert listed.get("ok") and len(listed["favorites"]) == 1, listed

    removed = api.suite_cardeater_remove_favorite(added["favorite"]["id"])
    assert removed.get("ok"), removed
    assert api.suite_cardeater_list_favorites()["favorites"] == []


# ---------------------------------------------------------------------------
# Naming templates
# ---------------------------------------------------------------------------

def test_save_naming_template_rejects_unknown_token(api):
    result = api.suite_cardeater_save_naming_template(_template(file_template="{Nope}.{ext}"))
    assert result.get("ok") is False
    assert "error" in result


def test_save_list_delete_naming_template(api):
    saved = api.suite_cardeater_save_naming_template(_template(name="Standard"))
    assert saved.get("ok"), saved
    listed = api.suite_cardeater_list_naming_templates()
    assert listed.get("ok") and len(listed["templates"]) == 1

    deleted = api.suite_cardeater_delete_naming_template(saved["template"]["id"])
    assert deleted.get("ok"), deleted
    assert api.suite_cardeater_list_naming_templates()["templates"] == []


def test_preview_names_via_api(api):
    files = [{"path": "/card/clip1.mov", "relative_folder": "", "name": "clip1.mov",
              "ext": "mov", "size_bytes": 10, "created_at": None, "created_at_source": "unavailable"}]
    result = api.suite_cardeater_preview_names({
        "card_insert_date": "2026-07-14T00:00:00Z", "event_name": "GameDay",
        "manual_date": None, "template": _template(), "files": files, "dest_path": None,
    })
    assert result.get("ok"), result
    assert result["folder_name"] == "20260714_GameDay"
    # A single-file job with no destination collision drops the {Seq}
    # suffix -- nothing to disambiguate it from (see cardeater_naming.py).
    assert result["sample_file_names"] == ["20260714_GameDay.mov"]


def test_preview_names_via_api_surfaces_naming_error(api):
    result = api.suite_cardeater_preview_names({
        "card_insert_date": "2026-07-14T00:00:00Z", "event_name": "GameDay",
        "manual_date": None, "template": _template(file_template="{Nope}.{ext}"),
        "files": [], "dest_path": None,
    })
    assert result.get("ok") is False


def test_check_folder_collision_via_api(api, tmp_path):
    result = api.suite_cardeater_check_folder_collision(str(tmp_path), "no_such_folder")
    assert result.get("ok") and result["status"] == "no_conflict", result


# ---------------------------------------------------------------------------
# Destination preview ("Preview" button on a chosen destination)
# ---------------------------------------------------------------------------

def test_list_destination_files_folder_does_not_exist_yet(api, tmp_path):
    result = api.suite_cardeater_list_destination_files(str(tmp_path), "not_yet_created")
    assert result.get("ok"), result
    assert result["exists"] is False
    assert result["entries"] == []


def test_list_destination_files_lists_entries_sorted_and_skips_dotfiles(api, tmp_path):
    # A dedicated subfolder, not tmp_path itself -- the `api` fixture shares
    # this same tmp_path and already wrote cardeater.sqlite3/favorites.json
    # into it, which would otherwise show up as spurious destination contents.
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / ".DS_Store").write_bytes(b"x")
    (dest / "zeta.mov").write_bytes(b"12345")
    (dest / "alpha.jpg").write_bytes(b"x")
    (dest / "subfolder").mkdir()

    result = api.suite_cardeater_list_destination_files(str(dest))
    assert result.get("ok"), result
    assert result["exists"] is True
    assert result["resolved_path"] == str(dest)
    names = [e["name"] for e in result["entries"]]
    assert names == ["alpha.jpg", "subfolder", "zeta.mov"]

    by_name = {e["name"]: e for e in result["entries"]}
    assert by_name["alpha.jpg"]["is_dir"] is False
    assert by_name["alpha.jpg"]["size_bytes"] == 1
    assert by_name["subfolder"]["is_dir"] is True
    assert by_name["subfolder"]["size_bytes"] is None


def test_list_destination_files_resolves_subfolder_when_given(api, tmp_path):
    (tmp_path / "20260714_GameDay").mkdir()
    (tmp_path / "20260714_GameDay" / "clip.mov").write_bytes(b"x")

    result = api.suite_cardeater_list_destination_files(str(tmp_path), "20260714_GameDay")
    assert result.get("ok"), result
    assert result["resolved_path"] == str(tmp_path / "20260714_GameDay")
    assert [e["name"] for e in result["entries"]] == ["clip.mov"]


# ---------------------------------------------------------------------------
# Disk space / card scanning
# ---------------------------------------------------------------------------

def test_check_disk_space_via_api(api, tmp_path):
    result = api.suite_cardeater_check_disk_space([str(tmp_path)], 10)
    assert result.get("ok"), result
    assert result["checks"][0]["ok"] is True


def test_scan_card_files_via_api_missing_path_is_not_ok(api, tmp_path):
    result = api.suite_cardeater_scan_card_files(str(tmp_path / "nope"))
    assert result.get("ok") is False


def test_scan_card_files_via_api_real_folder(api, tmp_path, monkeypatch):
    # A dedicated subfolder, not tmp_path itself -- the `api` fixture shares
    # this same tmp_path and already wrote cardeater.sqlite3/favorites.json
    # into it, which would otherwise show up as spurious "card" contents.
    from backend import cardeater_card
    card_dir = tmp_path / "card"
    card_dir.mkdir()
    (card_dir / "clip.mov").write_bytes(b"x")
    monkeypatch.setattr(cardeater_card.metadata, "resolve_created_at_batch",
                         lambda paths: {p: (None, "unavailable") for p in paths})
    result = api.suite_cardeater_scan_card_files(str(card_dir))
    assert result.get("ok"), result
    assert len(result["files"]) == 1


# ---------------------------------------------------------------------------
# Preview URL gating
# ---------------------------------------------------------------------------

def test_preview_url_gates_on_extension_and_real_file(api, tmp_path):
    mov_path = tmp_path / "clip.mov"
    raw_path = tmp_path / "clip.cr2"
    mov_path.write_bytes(b"x")
    raw_path.write_bytes(b"x")

    ok_mov = api.suite_cardeater_preview_url(str(mov_path))
    assert ok_mov.get("ok") and ok_mov.get("url"), ok_mov

    unsupported = api.suite_cardeater_preview_url(str(raw_path))
    assert unsupported.get("ok") is False and unsupported.get("error") == "unsupported"

    missing = api.suite_cardeater_preview_url(str(tmp_path / "nope.mov"))
    assert missing.get("ok") is False


# ---------------------------------------------------------------------------
# Viewer panel file metadata
# ---------------------------------------------------------------------------

def test_file_metadata_missing_file_errors(api, tmp_path):
    result = api.suite_cardeater_file_metadata(str(tmp_path / "nope.mov"))
    assert result.get("ok") is False


def test_file_metadata_returns_backend_result_for_real_file(api, monkeypatch, tmp_path):
    from backend import cardeater_metadata

    p = tmp_path / "clip.mov"
    p.write_bytes(b"x")
    sentinel = {"available": True, "width": 1920, "height": 1080, "duration_secs": 12.5,
                "frame_rate": 29.97, "file_type": "MOV", "mime_type": "video/quicktime",
                "camera_make": "Sony", "camera_model": "FX3"}
    monkeypatch.setattr(cardeater_metadata, "resolve_extended_metadata", lambda path: sentinel)

    result = api.suite_cardeater_file_metadata(str(p))
    assert result.get("ok"), result
    assert result["metadata"] == sentinel


# ---------------------------------------------------------------------------
# Active card
# ---------------------------------------------------------------------------

def test_get_active_card_defaults_to_none(api):
    result = api.suite_cardeater_get_active_card()
    assert result.get("ok") and result["card"] is None, result


def test_get_active_card_reflects_registry_state(api):
    api._cardeater.card.active = {"id": "/Volumes/CARD1", "label": "CARD1"}
    result = api.suite_cardeater_get_active_card()
    assert result["card"]["label"] == "CARD1"


# ---------------------------------------------------------------------------
# Open a folder in the Finder -- backs both the "Open Source" button (the
# active card's own root) and the Jobs drawer's per-destination "Open
# Folder" button on a cardeater_copy job (its resolved destination folder).
# ---------------------------------------------------------------------------

def test_open_folder_missing_path_is_not_ok(api, tmp_path):
    result = api.suite_cardeater_open_folder(str(tmp_path / "nope"))
    assert result.get("ok") is False


def test_open_folder_opens_real_folder(api, tmp_path, monkeypatch):
    from backend import api_cardeater

    calls = []
    monkeypatch.setattr(api_cardeater.subprocess, "run", lambda args, **kw: calls.append(args))
    result = api.suite_cardeater_open_folder(str(tmp_path))
    assert result.get("ok"), result
    assert calls == [["open", str(tmp_path)]]


# ---------------------------------------------------------------------------
# Window-gated dialogs (no real pywebview window in tests -- self.window is
# None by default, same guard every other workspace's dialogs use)
# ---------------------------------------------------------------------------

def test_open_folder_as_card_without_window_is_not_ok(api):
    result = api.suite_cardeater_open_folder_as_card()
    assert result.get("ok") is False
    assert "window isn't ready" in result["error"]


def test_pick_destination_without_window_is_not_ok(api):
    result = api.suite_cardeater_pick_destination()
    assert result.get("ok") is False
    assert "window isn't ready" in result["error"]


def test_export_job_history_csv_without_window_is_not_ok(api):
    result = api.suite_cardeater_export_job_history_csv()
    assert result.get("ok") is False
    assert "window isn't ready" in result["error"]


# ---------------------------------------------------------------------------
# Copy job lifecycle through the API layer
# ---------------------------------------------------------------------------

def _start_real_job(api, tmp_path):
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    (source_dir / "clip1.mov").write_bytes(b"clip content")
    files = [{"path": str(source_dir / "clip1.mov"), "relative_folder": "", "name": "clip1.mov",
              "ext": "mov", "size_bytes": len(b"clip content"), "created_at": None,
              "created_at_source": "unavailable"}]
    req = {
        "source_card_label": "TESTCARD", "source_path": str(source_dir),
        "card_insert_date": "2026-07-14T00:00:00Z", "event_name": "GameDay",
        "manual_date": None, "template": _template(), "files": files,
        "destinations": [str(dest_dir)],
    }
    return api.suite_cardeater_start_job(req)


def test_start_job_and_get_status_via_api(api, tmp_path):
    started = _start_real_job(api, tmp_path)
    assert started.get("ok"), started
    status = _wait_for_completion(api, started["job_id"])
    assert status["status"] == "complete", status


def test_pause_resume_cancel_unknown_job_is_not_ok(api):
    for method in (api.suite_cardeater_pause_job, api.suite_cardeater_resume_job, api.suite_cardeater_cancel_job):
        result = method(999999)
        assert result.get("ok") is False
        assert "No destinations found" in result["error"]


def test_mark_card_safe_check_via_api(api, tmp_path):
    source_dir = tmp_path / "source"
    started = _start_real_job(api, tmp_path)
    _wait_for_completion(api, started["job_id"])
    result = api.suite_cardeater_mark_card_safe_check(str(tmp_path / "source"))
    assert result.get("ok") and result["safe"] is True, result


def test_suite_list_jobs_merges_cardeater_copy_jobs_and_clear_finished(api, tmp_path):
    started = _start_real_job(api, tmp_path)
    _wait_for_completion(api, started["job_id"])

    listed = api.suite_list_jobs()
    assert listed.get("ok"), listed
    kinds = [j["kind"] for j in listed["jobs"]]
    assert "cardeater_copy" in kinds, listed

    cleared = api.suite_clear_finished_jobs()
    assert cleared.get("ok"), cleared
    listed_after = api.suite_list_jobs()
    assert "cardeater_copy" not in [j["kind"] for j in listed_after["jobs"]]

    # Job history (the Copy workspace's own history view) is unaffected.
    history = api.suite_cardeater_list_jobs()
    assert history.get("ok") and len(history["jobs"]) == 1, history
