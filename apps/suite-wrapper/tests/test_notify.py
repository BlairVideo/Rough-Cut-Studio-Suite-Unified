"""notify.py -- native macOS notification for job completion. Never
exercises a real osascript call (nothing to assert about Notification
Center from a test process); only that the right command is built, and
that any failure is swallowed rather than raised."""

import subprocess

from backend import notify


def test_send_native_notification_invokes_osascript(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append((a, k)))

    notify.send_native_notification("Transcribe", "Transcription finished")

    assert len(calls) == 1
    args, kwargs = calls[0]
    cmd = args[0]
    assert cmd[0] == "osascript"
    assert cmd[1] == "-e"
    assert "Transcribe" in cmd[2]
    assert "Transcription finished" in cmd[2]
    assert kwargs.get("check") is False


def test_escapes_quotes_and_backslashes_in_title_and_message(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a[0]))

    notify.send_native_notification('Say "hi"', "path\\to\\file")

    script = calls[0][2]
    assert '\\"hi\\"' in script
    assert "path\\\\to\\\\file" in script


def test_never_raises_when_subprocess_fails(monkeypatch):
    def boom(*a, **k):
        raise OSError("osascript not found")
    monkeypatch.setattr(subprocess, "run", boom)

    notify.send_native_notification("title", "message")  # must not raise
