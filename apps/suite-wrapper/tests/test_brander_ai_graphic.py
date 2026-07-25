"""Blair Brander AI-generated background graphics (Gemini image
generation, not the earlier text-only scene-update mode). No real
network calls -- requests.post is monkeypatched to return a synthetic
base64 PNG, so this exercises the real request-building, response-
parsing, file-saving, and preview-rendering code paths without hitting
the real API or costing anything."""

import base64
import io
import json

import pytest
from PIL import Image

from backend import brander_gemini


def _fake_png_base64(color=(0, 75, 141), size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        return self._json_data


def _fake_success_body():
    return {
        "candidates": [{
            "content": {"parts": [
                {"inlineData": {"mimeType": "image/png", "data": _fake_png_base64()}},
            ]},
        }],
    }


def test_generate_graphic_image_returns_decoded_bytes(monkeypatch):
    def fake_post(url, headers=None, data=None, timeout=None):
        assert "x-goog-api-key" in headers
        assert headers["x-goog-api-key"] == "fake-key"
        body = json.loads(data)
        assert body["generationConfig"]["responseModalities"] == ["IMAGE"]
        return _FakeResponse(200, _fake_success_body())

    monkeypatch.setattr("requests.post", fake_post)
    image_bytes, mime_type = brander_gemini.generate_graphic_image(
        "fake-key", "a moody navy gradient", {"primary_colors": {"Blair Blue": "#004b8d"}})
    assert mime_type == "image/png"
    img = Image.open(io.BytesIO(image_bytes))
    assert img.size == (64, 64)


def test_generate_graphic_image_no_key_raises():
    with pytest.raises(brander_gemini.BranderGeminiError):
        brander_gemini.generate_graphic_image("", "prompt", {})


def test_generate_graphic_image_retries_on_503_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, headers=None, data=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            return _FakeResponse(503, text="overloaded")
        return _FakeResponse(200, _fake_success_body())

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr(brander_gemini, "_wait_before_retry", lambda attempt: None)
    image_bytes, mime_type = brander_gemini.generate_graphic_image("fake-key", "prompt", {})
    assert calls["n"] == 2
    assert mime_type == "image/png"


def test_generate_graphic_image_no_image_in_response_raises(monkeypatch):
    def fake_post(url, headers=None, data=None, timeout=None):
        return _FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "declined"}]}}]})

    monkeypatch.setattr("requests.post", fake_post)
    with pytest.raises(brander_gemini.BranderGeminiError):
        brander_gemini.generate_graphic_image("fake-key", "prompt", {})


def test_brander_ai_generate_graphic_end_to_end(api, fake_keyring, monkeypatch, tmp_path):
    # Full path through SuiteApi: keychain key -> Gemini call (mocked) ->
    # file saved under GRAPHICS_DIR -> scene field set -> real composite
    # preview rendered (title text over the generated background).
    from backend import paths
    monkeypatch.setattr(paths, "GRAPHICS_DIR", str(tmp_path / "graphics"))
    import os
    os.makedirs(paths.GRAPHICS_DIR, exist_ok=True)

    api.brander_save_gemini_key("fake-test-key")

    def fake_post(url, headers=None, data=None, timeout=None):
        return _FakeResponse(200, _fake_success_body())

    monkeypatch.setattr("requests.post", fake_post)

    defaults = api.brander_defaults()
    res = api.brander_ai_generate_graphic("a clean navy gradient backdrop", defaults["scene"])
    assert res.get("ok"), res
    assert res["scene"]["ai_background_path"]
    assert os.path.isfile(res["scene"]["ai_background_path"])
    assert res["scene"]["transparent_bg"] is False
    assert res["data_uri"].startswith("data:image/png;base64,")

    # brander_clear_ai_background reverts the field without deleting the file.
    cleared = api.brander_clear_ai_background(res["scene"])
    assert cleared.get("ok")
    assert "ai_background_path" not in cleared["scene"]
    assert os.path.isfile(res["scene"]["ai_background_path"]), "clearing must not delete the file"


def test_brander_ai_generate_graphic_without_key_errors(api, fake_keyring):
    # fake_keyring is REQUIRED here, not optional: without it this would
    # read the real system keychain, and if a real key is saved there,
    # the "no key" branch would never trigger -- the request would fall
    # through and hit the real Gemini API with real credentials. Found
    # this the hard way (see incident note in the test suite's history).
    defaults = api.brander_defaults()
    res = api.brander_ai_generate_graphic("anything", defaults["scene"])
    assert res.get("ok") is False
    assert res.get("error") == "no_api_key"
