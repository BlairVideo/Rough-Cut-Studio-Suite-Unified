"""colorize_bridge.py's preview-token lifecycle -- mirrors
test_braw_proxy_cache.py's forget test. Confirms `forget_preview` actually
drops a clip's loopback-URL token (Section: removeClip/clearAllClips in
colorize.js call this on removal so a deleted clip's media doesn't stay
servable via its old token for the rest of the process's life -- see
apps/rough-cut-studio/backend/sources.py's remove_source, which does the
same thing for Rough Cut Studio's own preview server)."""

import os

from backend import colorize_bridge


def make_source(tmp_path, name="clip.mov", data=b"\x00" * 32):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_get_preview_url_registers_a_token_for_the_path(tmp_path):
    source = make_source(tmp_path)
    url = colorize_bridge.get_preview_url(source)
    assert url is not None
    assert "/media/" in url


def test_forget_preview_drops_the_token_without_touching_the_file(tmp_path):
    source = make_source(tmp_path)
    url = colorize_bridge.get_preview_url(source)
    assert url is not None

    colorize_bridge.forget_preview(source)

    # A fresh request re-registers a NEW token rather than reusing the old
    # one -- proves the old path->token mapping was actually dropped, not
    # left servable under its previous URL.
    new_url = colorize_bridge.get_preview_url(source)
    assert new_url is not None
    assert new_url != url
    assert os.path.isfile(source)  # the file itself is left alone


def test_forget_preview_on_an_unregistered_path_is_a_no_op(tmp_path):
    source = make_source(tmp_path)
    colorize_bridge.forget_preview(source)  # never registered -- must not raise
