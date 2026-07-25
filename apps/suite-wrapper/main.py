"""
main.py — Rough Cut Studio Suite

Entry point for the unified suite window. At EVERY launch it composes a
fresh frontend/_generated/index.html by embedding Rough Cut Studio's
untouched frontend (its <head> links and <body> markup, with relative URLs
rewritten to absolute file:// URIs) into the suite's shell.html template,
then opens one pywebview window whose js_api is SuiteApi — a subclass of
RCS's own Api, so the embedded RCS UI keeps working unchanged while the
Transcribe/B-Roll/Graphics workspaces get their new suite_* methods.

Run:
    .venv/bin/python main.py             # open the app window
    .venv/bin/python main.py --selftest  # headless smoke test (no window)
"""

import hashlib
import os
import re
import sys
import shutil
import subprocess
import traceback

SUITE_DIR = os.path.dirname(os.path.abspath(__file__))
if SUITE_DIR not in sys.path:
    sys.path.insert(0, SUITE_DIR)

from backend import paths  # noqa: E402

# Minimal stand-in used ONLY while frontend/shell.html doesn't exist yet
# (the frontend is built in parallel against the same contract). It carries
# the exact same placeholders, so page composition is fully exercisable —
# once shell.html exists it is ALWAYS used instead.
FALLBACK_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Rough Cut Studio Suite</title>
{{RCS_HEAD_LINKS}}
<link rel="stylesheet" href="{{SUITE_CSS_HREF}}" />
</head>
<body>
<div id="suiteTopbar">ROUGH CUT STUDIO SUITE (fallback shell — frontend/shell.html not found)</div>
<div id="workspace-transcribe" hidden></div>
<div id="workspace-broll" hidden></div>
<div id="workspace-graphics" hidden></div>
<div id="workspace-edit">
{{RCS_BODY}}
</div>
<script src="{{RCS_APPJS_SRC}}"></script>
<script src="{{SUITE_JS_SRC}}"></script>
</body>
</html>
"""

_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL)
_HEAD_RE = re.compile(r"<head[^>]*>(.*)</head>", re.IGNORECASE | re.DOTALL)
_APPJS_SCRIPT_RE = re.compile(
    r'<script[^>]*\bsrc="[^"]*app\.js"[^>]*>\s*</script>', re.IGNORECASE)


def _content_hash(path):
    """Short content hash for cache-busting query strings (see
    _versioned_name's docstring for why this exists)."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


def _versioned_name(name, src_path):
    """`name?v=<hash>` — pywebview serves the composed page over its
    built-in bottle HTTP server (webview/http.py's BottleServer), not
    file://, so WebKit applies ordinary HTTP heuristic caching to
    suite.js/suite.css/rcs-app.js/etc. bottle's static-file route ignores
    the query string when resolving the filesystem path, so this is a
    no-op for serving — but it DOES change the URL WebKit caches under,
    which is the whole point: without it, WebKit's on-disk NetworkCache
    (shared across every plain-`python`-hosted webview app under the
    `org.python.python` bundle id, confirmed via ~/Library/Caches — NOT
    scoped by webview.start(storage_path=...), which only affects
    cookies/localStorage/IndexedDB) can and does keep serving a script
    body from a PREVIOUS launch even after a full quit-and-relaunch and
    even though compose_page() re-copies the current file to disk every
    time — confirmed live: a real edit to suite.js was invisible in the
    running app across two full quit/relaunch cycles until this fix.
    Content-hashed (not mtime-based) so an unrelated same-second git
    checkout/copy2 can't accidentally produce a URL WebKit already has a
    stale entry for."""
    return f"{name}?v={_content_hash(src_path)}"


def _rewrite_link_hrefs(link_tags, base_dir, copies):
    """Relative hrefs in RCS's <link> tags (e.g. style.css) only resolve
    from RCS's own frontend folder. They CANNOT simply become absolute
    file:// URIs: pywebview auto-starts its built-in HTTP server whenever a
    window's URL is a local file (webview/__init__.py, `has_local_urls`),
    and WKWebView blocks file:// subresources on an http-origin page — the
    window would render with no CSS/JS at all. Instead, each local target
    is registered in `copies` (src -> filename) to be copied INTO
    _generated/ and referenced by bare relative filename, which works under
    both file:// and http:// serving. Absolute URLs (Google Fonts etc.)
    pass through untouched. The href itself additionally gets a
    _versioned_name cache-busting suffix (see that function's docstring)."""
    rewritten = []
    for tag in link_tags:
        m = _HREF_RE.search(tag)
        if m:
            href = m.group(1)
            if "://" not in href and not href.startswith(("data:", "//", "#")):
                src = os.path.normpath(os.path.join(base_dir, href))
                local_name = "rcs-" + os.path.basename(src)
                copies[src] = local_name
                tag = tag[:m.start(1)] + _versioned_name(local_name, src) + tag[m.end(1):]
        rewritten.append(tag)
    return rewritten


def extract_rcs_parts(copies):
    """Pull (head_links_html, body_html) out of Rough Cut Studio's
    index.html — the body minus its own app.js <script> tag(s), which the
    shell loads itself in the contract-mandated order (app.js before
    suite.js). Local <link> targets are registered in `copies` for
    duplication into _generated/ (see _rewrite_link_hrefs)."""
    index_path = os.path.join(paths.RCS_FRONTEND_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()

    head_match = _HEAD_RE.search(html)
    head_html = head_match.group(1) if head_match else html
    links = _rewrite_link_hrefs(_LINK_TAG_RE.findall(head_html), paths.RCS_FRONTEND_DIR, copies)

    body_match = _BODY_RE.search(html)
    if not body_match:
        raise RuntimeError(f"Couldn't find a <body> in {index_path}")
    body_html = _APPJS_SCRIPT_RE.sub("", body_match.group(1))

    return "\n".join(links), body_html


def compose_page():
    """Build frontend/_generated/index.html from shell.html (or the
    fallback template) and return its path. Every local asset the page
    needs (RCS's style.css/app.js, the suite's css/js) is COPIED into
    _generated/ and referenced by bare relative filename — pywebview serves
    local windows through its built-in HTTP server, under which any
    file:// subresource would be blocked (see _rewrite_link_hrefs).
    Regenerated at every launch so changes to either app's frontend are
    always picked up."""
    paths.ensure_suite_dirs()
    copies = {}  # absolute source path -> filename inside _generated/
    head_links, body_html = extract_rcs_parts(copies)

    rcs_appjs_path = os.path.join(paths.RCS_FRONTEND_DIR, "app.js")
    suite_css_path = os.path.join(paths.FRONTEND_DIR, "suite.css")
    suite_js_path = os.path.join(paths.FRONTEND_DIR, "suite.js")
    copies[rcs_appjs_path] = "rcs-app.js"
    copies[suite_css_path] = "suite.css"
    copies[suite_js_path] = "suite.js"

    shell_path = os.path.join(paths.FRONTEND_DIR, "shell.html")
    if os.path.exists(shell_path):
        with open(shell_path, "r", encoding="utf-8") as f:
            shell = f.read()
    else:
        shell = FALLBACK_SHELL

    # Cache-busting query strings (see _versioned_name's docstring) — WITHOUT
    # these, WebKit's shared NetworkCache can keep serving an old launch's
    # suite.js/suite.css/rcs-app.js body indefinitely, surviving even a full
    # quit-and-relaunch, since compose_page() always writes these under the
    # exact same bare filenames every time.
    page = (shell
            .replace("{{RCS_HEAD_LINKS}}", head_links)
            .replace("{{RCS_BODY}}", body_html)
            .replace("{{RCS_APPJS_SRC}}", _versioned_name("rcs-app.js", rcs_appjs_path))
            .replace("{{SUITE_CSS_HREF}}", _versioned_name("suite.css", suite_css_path))
            .replace("{{SUITE_JS_SRC}}", _versioned_name("suite.js", suite_js_path)))

    for src, name in copies.items():
        shutil.copy2(src, os.path.join(paths.GENERATED_DIR, name))

    out_path = os.path.join(paths.GENERATED_DIR, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    return out_path


def selftest():
    """Headless smoke test. Two parts:

    1. compose_page() + a placeholder check, inline, with zero extra
       dependencies -- this must work in an environment with only
       requirements.txt installed, since it exercises the exact code path
       real app launches depend on.
    2. The full pytest suite under tests/ (what used to be ~700 more
       lines in this function, one giant sequential assertion chain --
       see tests/conftest.py's docstring). Requires requirements-dev.txt
       (pytest) to be installed in THIS venv, same convention as every
       sibling app's own test suite.

    Prints SELFTEST OK and returns 0 on success; prints a traceback (part
    1) or pytest's own failure report (part 2) and returns 1 otherwise --
    same CLI contract as before this was extracted into tests/.
    """
    try:
        page = compose_page()
        assert os.path.isfile(page), "composed index.html missing"
        with open(page, "r", encoding="utf-8") as f:
            content = f.read()
        for placeholder in ("{{RCS_HEAD_LINKS}}", "{{RCS_BODY}}", "{{RCS_APPJS_SRC}}",
                            "{{SUITE_CSS_HREF}}", "{{SUITE_JS_SRC}}"):
            assert placeholder not in content, f"placeholder left unreplaced: {placeholder}"
    except Exception:
        traceback.print_exc()
        return 1

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", os.path.join(SUITE_DIR, "tests")],
        cwd=SUITE_DIR)
    if proc.returncode != 0:
        return 1

    print("SELFTEST OK")
    return 0


def main():
    import webview
    from backend.suite_api import SuiteApi

    index_path = compose_page()
    api = SuiteApi()
    window = webview.create_window(
        "Rough Cut Studio Suite",
        url=index_path,
        js_api=api,
        width=1440,
        height=900,
        min_size=(1100, 720),
        background_color="#101116",
        text_select=True,
    )
    # Same bootstrap order as Rough Cut Studio's own main.py: the Api needs
    # the window handle for native dialogs, and it only exists after
    # create_window returns.
    api.window = window
    # private_mode=False + a fixed storage_path: pywebview 6.x defaults to
    # private_mode=True (an ephemeral WebKit data store wiped every launch),
    # which silently discarded every localStorage-backed setting suite.js
    # writes (Transcriber/B-Roll/RCS settings, column widths) on relaunch.
    webview.start(debug=False, private_mode=False, storage_path=paths.WEBVIEW_STORAGE_DIR)


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(selftest())
    main()
