"""compose_page() -- the frontend/_generated/index.html builder that runs
at every app launch. Also exercised standalone (no pytest needed) by
main.py's own selftest(), since the app must be able to compose its page
even in an environment with only requirements.txt installed."""

import os

from main import compose_page


def test_compose_page_produces_a_file_with_no_leftover_placeholders():
    page = compose_page()
    assert os.path.isfile(page), "composed index.html missing"
    with open(page, "r", encoding="utf-8") as f:
        content = f.read()
    for placeholder in ("{{RCS_HEAD_LINKS}}", "{{RCS_BODY}}", "{{RCS_APPJS_SRC}}",
                         "{{SUITE_CSS_HREF}}", "{{SUITE_JS_SRC}}"):
        assert placeholder not in content, f"placeholder left unreplaced: {placeholder}"
