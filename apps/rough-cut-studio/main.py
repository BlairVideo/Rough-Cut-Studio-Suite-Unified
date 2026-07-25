"""
main.py — Rough Cut Studio

Launches the app in its own native desktop window using pywebview, so it
opens as a standalone application rather than a browser tab. pywebview uses
your OS's built-in web renderer (WebKit on macOS/Linux, Edge WebView2 on
Windows) — no bundled Chromium, no extra background services.

Run with:
    python main.py

First-time setup (see README.md for full details):
    python -m venv .venv
    source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
    pip install -r requirements.txt
    python main.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

import webview  # noqa: E402
from api import Api  # noqa: E402

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")


def main():
    api = Api()
    window = webview.create_window(
        "Rough Cut Studio",
        url=os.path.join(FRONTEND_DIR, "index.html"),
        js_api=api,
        width=1280,
        height=820,
        min_size=(1000, 680),
        background_color="#101116",
        text_select=True,
    )
    api.window = window
    webview.start(debug=False)


if __name__ == "__main__":
    main()
