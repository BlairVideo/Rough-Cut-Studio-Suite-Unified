"""
Launcher for the Local Interview Transcriber.

Runs the Streamlit app as a background server (headless, no browser tab)
and displays it inside a native macOS window using pywebview — so the
app looks and feels like a regular desktop app instead of a browser page.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
import atexit

import webview

HOST = "127.0.0.1"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def wait_for_server(host: str, port: int, proc: subprocess.Popen, timeout: float = 150.0) -> bool:
    """Poll until the Streamlit server accepts connections, or give up if
    it exits early (crashed) or the timeout elapses. The timeout is very
    generous (rather than Streamlit's typical ~2-5s startup) because this
    app's files live on an external volume — measured cold-import time for
    Streamlit alone (before any of this app's own code runs) has been
    observed to take 30-60+ seconds on that drive, with no fixed upper
    bound, since it's gated by that drive's per-file access latency rather
    than anything this app does. See CLAUDE.md for the profiling behind
    this; moving the project (or at least its .venv) to internal storage
    is the real fix for the slowness itself, not a bigger number here."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False  # the subprocess already exited (crashed) - no point waiting further
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def main():
    port = find_free_port()

    # Captured to a temp file (rather than swallowed via DEVNULL) so that if
    # startup fails, the actual reason is included in the error instead of
    # just "did not start in time" with no clue why.
    log_path = os.path.join(tempfile.gettempdir(), "ivt_streamlit_server.log")
    log_file = open(log_path, "w", encoding="utf-8")

    proc = subprocess.Popen(
        [
            # "-u": unbuffered stdout/stderr. Without this, Python fully
            # block-buffers output once it's redirected to a file (rather
            # than a terminal), so if this subprocess is killed (e.g. by
            # `cleanup()` after a timeout) before that buffer happens to
            # flush on its own, whatever it had already printed is lost -
            # which is exactly what produced a "(no output was captured)"
            # error with no actual diagnostic content in it.
            sys.executable, "-u", "-m", "streamlit", "run", "app.py",
            "--server.headless", "true",
            "--server.address", HOST,
            "--server.port", str(port),
            "--browser.gatherUsageStats", "false",
            # This is the packaged/end-user launch path (not `streamlit run
            # app.py` directly, which SETUP.md documents separately for dev
            # use) - live source-file watching is dev-only overhead here,
            # not a feature end users need.
            "--server.fileWatcherType", "none",
        ],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    def cleanup():
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log_file.close()

    atexit.register(cleanup)

    if not wait_for_server(HOST, port, proc):
        cleanup()
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                server_output = f.read().strip()
        except OSError:
            server_output = ""
        detail = f"\n\nServer output:\n{server_output}" if server_output else " (no output was captured)"
        raise RuntimeError(f"Streamlit server did not start.{detail}")

    window = webview.create_window(
        "Local Interview Transcriber",
        url=f"http://{HOST}:{port}",
        width=1280,
        height=860,
        min_size=(900, 600),
    )
    webview.start()

    # webview.start() blocks until the window is closed
    cleanup()


if __name__ == "__main__":
    main()
