"""SuiteApi's cross-workspace additions for the Pipeline/Settings/
notifications features: suite_native_notify, suite_proxy_cache_info,
suite_clear_proxy_cache. Each just forwards to notify.py/braw_bridge.py
(themselves covered by their own test files) with the {"ok": ...} error
contract every SuiteApi method follows."""

import subprocess

from backend import paths


def _redirect_proxies_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PROXIES_DIR", str(tmp_path / "proxies"))


def test_suite_native_notify_forwards_to_osascript(api, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a[0]))

    res = api.suite_native_notify("Transcribe", "Transcription finished")

    assert res == {"ok": True}
    assert len(calls) == 1
    assert "Transcription finished" in calls[0][2]


def test_suite_native_notify_never_raises_on_failure(api, monkeypatch):
    def boom(*a, **k):
        raise OSError("no osascript")
    monkeypatch.setattr(subprocess, "run", boom)

    res = api.suite_native_notify("title", "message")

    assert res == {"ok": True}  # notify.py itself swallows the error


def test_suite_proxy_cache_info_on_empty_cache(api, monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)

    res = api.suite_proxy_cache_info()

    assert res["ok"] is True
    assert res["bytes_used"] == 0
    assert res["file_count"] == 0
    assert res["bytes_cap"] > 0


def test_suite_clear_proxy_cache_removes_files_and_reports_count(api, monkeypatch, tmp_path):
    _redirect_proxies_dir(monkeypatch, tmp_path)
    proxies_dir = tmp_path / "proxies"
    proxies_dir.mkdir(parents=True)
    (proxies_dir / "a.mov").write_bytes(b"\x00" * 10)
    (proxies_dir / "b.mov").write_bytes(b"\x00" * 10)

    res = api.suite_clear_proxy_cache()

    assert res == {"ok": True, "removed": 2}
    assert list(proxies_dir.glob("*.mov")) == []
