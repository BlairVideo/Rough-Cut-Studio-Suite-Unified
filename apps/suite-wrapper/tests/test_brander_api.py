"""Graphics-workspace (Blair Brander bridge) API surface: scene defaults,
a real still-frame render, the logo-placement contract, Gemini key
storage (via the OS keychain -- faked here), and the AI response
validator."""


def test_brander_defaults_shape(api):
    defaults = api.brander_defaults()
    assert defaults.get("ok"), f"brander_defaults: {defaults}"
    assert isinstance(defaults["scene"].get("canvas_size"), list)
    assert defaults["options"]["fps"] == 30


def test_brander_defaults_logo_placements_match_standalone_app(api):
    # C1: the seven logo placements, exactly as the standalone app lists them.
    defaults = api.brander_defaults()
    assert defaults["options"].get("logo_placements") == [
        "top-left", "top-center", "top-right", "center",
        "bottom-left", "bottom-center", "bottom-right",
    ], f"logo_placements: {defaults['options'].get('logo_placements')}"


def test_brander_still_preview_renders_a_real_png(api):
    defaults = api.brander_defaults()
    still = api.brander_still_preview(defaults["scene"], max_width=320)
    assert still.get("ok") and still["data_uri"].startswith("data:image/png;base64,"), \
        f"brander_still_preview: {still.get('error')}"


def test_brander_gemini_key_lifecycle(api, fake_keyring):
    # C4 / addendum v8: the key lives in the system keychain, separate
    # from RCS's shared .env-based key -- no api_key argument or
    # load_saved_api_key fallback.
    status0 = api.brander_gemini_key_status()
    assert status0.get("ok") and status0.get("present") is False, \
        f"brander_gemini_key_status (no key yet): {status0}"

    defaults = api.brander_defaults()
    no_key = api.brander_ai_generate("make it blue", defaults["scene"])
    assert no_key.get("ok") is False and no_key.get("error") == "no_api_key", \
        f"brander_ai_generate without key: {no_key}"

    saved = api.brander_save_gemini_key("fake-test-key-123")
    assert saved.get("ok"), f"brander_save_gemini_key: {saved}"
    status1 = api.brander_gemini_key_status()
    assert status1.get("ok") and status1.get("present") is True, \
        f"brander_gemini_key_status (after save): {status1}"

    removed = api.brander_save_gemini_key("")
    assert removed.get("ok"), f"brander_save_gemini_key (clear): {removed}"
    status2 = api.brander_gemini_key_status()
    assert status2.get("ok") and status2.get("present") is False, \
        f"brander_gemini_key_status (after clear): {status2}"

    # Clearing an already-absent key must not raise (PasswordDeleteError
    # path in brander_save_gemini_key).
    removed_again = api.brander_save_gemini_key("")
    assert removed_again.get("ok"), f"brander_save_gemini_key (double clear): {removed_again}"


def test_brander_gemini_validate_update_cleans_and_annotates(api):
    # C4: the Gemini request path's validator, exercised with a FAKE
    # response dict (no live call): valid fields survive, the invalid
    # font and bad hex are dropped with notes, the out-of-range size is
    # clamped with a note, and the model's own notes ride along.
    from backend import brander_gemini

    defaults = api.brander_defaults()
    fake_response = {
        "title": "SPRING GALA",
        "title_font": "Comic Sans",          # not a FONTS key -> dropped
        "title_size": 9999,                   # out of range -> clamped to 300
        "bg_color": "#12345",                 # bad hex -> dropped
        "accent_color": "#DA1A32",            # valid -> normalized lowercase
        "logo_placement": "top-right",        # valid enum
        "divider": True,
        "sneaky_field": "ignored",            # not whitelisted -> dropped
        "notes": ["Set the title for the gala."],
    }
    clean, notes = brander_gemini.validate_update(
        fake_response, defaults["scene"], defaults["options"])
    assert clean.get("title") == "SPRING GALA", f"validate_update clean: {clean}"
    assert "title_font" not in clean and "bg_color" not in clean \
        and "sneaky_field" not in clean, f"validate_update clean: {clean}"
    assert clean.get("title_size") == 300, f"validate_update clean: {clean}"
    assert clean.get("accent_color") == "#da1a32", f"validate_update clean: {clean}"
    assert clean.get("logo_placement") == "top-right" and clean.get("divider") is True
    assert "Set the title for the gala." in notes, f"validate_update notes: {notes}"
    assert any("title_font" in n for n in notes), f"validate_update notes: {notes}"
    assert any("bg_color" in n for n in notes), f"validate_update notes: {notes}"
    assert any("title_size" in n for n in notes), f"validate_update notes: {notes}"
