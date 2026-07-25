"""Card Eater naming/collision engine (backend/cardeater_naming.py). Scenarios
mirror CardEater/src-tauri/src/naming.rs's own `#[test]` suite one-for-one --
that Rust suite is the behavioral contract this module is a line-for-line
port of, so a Python regression here means the port has drifted from it."""

import os

import pytest

from backend import cardeater_naming as naming


def _file(name, ext, created_at=None):
    return {
        "path": f"/card/{name}",
        "relative_folder": "",
        "name": name,
        "ext": ext,
        "size_bytes": 1024,
        "created_at": created_at,
        "created_at_source": "exif" if created_at else "unavailable",
    }


def _base_template(**overrides):
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


def test_basic_single_date_group_sequencing_no_dest():
    template = _base_template()
    files = [_file("clip1.mov", "mov"), _file("clip2.mov", "mov"), _file("clip3.mov", "mov")]
    names, warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T10:00:00Z")
    assert names == [
        "20260714_GameDay_001.mov",
        "20260714_GameDay_002.mov",
        "20260714_GameDay_003.mov",
    ]
    assert warnings == []


def test_per_date_group_sequencing_is_independent():
    template = _base_template(date_source="file_metadata")
    files = [
        _file("a.mov", "mov", "2026-07-14T09:00:00Z"),
        _file("b.mov", "mov", "2026-07-15T09:00:00Z"),
        _file("c.mov", "mov", "2026-07-14T10:00:00Z"),
        _file("d.mov", "mov", "2026-07-15T11:00:00Z"),
    ]
    names, warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z")
    # July 14 files: a, c -> 001, 002 independent of July 15 files.
    assert names[0] == "20260714_GameDay_001.mov"
    assert names[2] == "20260714_GameDay_002.mov"
    # July 15 files: b, d -> 001, 002.
    assert names[1] == "20260715_GameDay_001.mov"
    assert names[3] == "20260715_GameDay_002.mov"
    assert warnings == []


def test_file_metadata_fallback_to_card_insert_date_warns():
    template = _base_template(date_source="file_metadata")
    # Two files so the single-file seq-drop doesn't mask the date-group
    # fallback behavior this test is actually about.
    files = [_file("nodate.mov", "mov"), _file("nodate2.mov", "mov")]
    names, warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z")
    assert names[0] == "20260714_GameDay_001.mov"
    assert len(warnings) == 2
    assert "nodate.mov" in warnings[0]


def test_destination_scan_resumes_from_existing_max_sequence(tmp_path):
    dest = tmp_path / "resume"
    dest.mkdir()
    (dest / "20260714_GameDay_014.mov").write_bytes(b"x")
    (dest / "20260714_GameDay_015.mov").write_bytes(b"x")
    (dest / "readme.txt").write_bytes(b"x")  # unrelated, must not match

    template = _base_template()
    files = [_file("new1.mov", "mov"), _file("new2.mov", "mov")]
    names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z", dest_path=str(dest))
    assert names == ["20260714_GameDay_016.mov", "20260714_GameDay_017.mov"]


def test_destination_scan_uses_overall_max_across_mixed_extensions(tmp_path):
    """Regression test for a real bug the original Rust app hit: when a date
    group mixes extensions sharing one {Seq} counter (photos + video, the
    common real-world case), the collision scan must consider every
    extension's existing files, not just whichever file happens to be first
    in the batch -- otherwise a re-import collides with and silently
    overwrites existing higher-numbered files of a different extension."""
    dest = tmp_path / "mixed_ext_resume"
    dest.mkdir()
    # Simulate a prior run's output: .mov only reached 001, .jpg reached 003,
    # .mp4 reached 002.
    (dest / "20260714_GameDay_001.mov").write_bytes(b"x")
    (dest / "20260714_GameDay_001.jpg").write_bytes(b"x")
    (dest / "20260714_GameDay_002.jpg").write_bytes(b"x")
    (dest / "20260714_GameDay_003.jpg").write_bytes(b"x")
    (dest / "20260714_GameDay_002.mp4").write_bytes(b"x")

    template = _base_template()
    # .mov first in the list: if the scan only checked the first member's
    # (.mov) literal, it would wrongly start the next batch at 002.
    files = [
        _file("new_clip.mov", "mov"),
        _file("new_photo1.jpg", "jpg"),
        _file("new_photo2.jpg", "jpg"),
        _file("new_video.mp4", "mp4"),
    ]
    names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z", dest_path=str(dest))
    assert names == [
        "20260714_GameDay_004.mov",
        "20260714_GameDay_005.jpg",
        "20260714_GameDay_006.jpg",
        "20260714_GameDay_007.mp4",
    ]


def test_mixed_extension_reimport_end_to_end_no_data_loss(tmp_path):
    """Same regression, but exercising real file I/O across two successive
    'import' passes into the same destination -- the shape of test that
    surfaced the original overwrite bug during manual testing."""
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()

    specs = [
        ("CLIP_0001.mov", "mov", b"source content: CLIP_0001"),
        ("IMG_0001.jpg", "jpg", b"source content: IMG_0001"),
        ("IMG_0002.jpg", "jpg", b"source content: IMG_0002"),
        ("IMG_0003.jpg", "jpg", b"source content: IMG_0003"),
        ("BIGCLIP_0001.mp4", "mp4", b"source content: BIGCLIP_0001"),
    ]
    files = []
    for name, ext, content in specs:
        (source_dir / name).write_bytes(content)
        files.append(_file(name, ext))
        files[-1]["path"] = str(source_dir / name)
        files[-1]["size_bytes"] = len(content)

    template = _base_template()

    def do_pass():
        names, _warnings = naming.resolve_file_names(
            files, template, "GameDay", "2026-07-14T00:00:00Z", dest_path=str(dest_dir))
        for (orig_name, _ext, _content), new_name in zip(specs, names):
            (dest_dir / new_name).write_bytes((source_dir / orig_name).read_bytes())
        return names

    names_pass1 = do_pass()
    names_pass2 = do_pass()

    for n in names_pass2:
        assert n not in names_pass1, \
            f"pass 2 name {n} collided with a pass 1 name -- data would have been overwritten"

    expected_contents = {content for _n, _e, content in specs}
    for name in names_pass1 + names_pass2:
        content = (dest_dir / name).read_bytes()
        assert content in expected_contents, f"unexpected content for {name}"

    assert len(list(dest_dir.iterdir())) == 10


def test_single_file_job_drops_seq_when_no_dest_given():
    template = _base_template()
    files = [_file("clip1.mov", "mov")]
    names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z")
    assert names == ["20260714_GameDay.mov"]


def test_single_file_job_drops_seq_when_dest_has_no_collision(tmp_path):
    dest = tmp_path / "empty_dest"
    dest.mkdir()
    template = _base_template()
    files = [_file("clip1.mov", "mov")]
    names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z", dest_path=str(dest))
    assert names == ["20260714_GameDay.mov"]


def test_single_file_job_keeps_seq_when_dest_already_has_a_match(tmp_path):
    dest = tmp_path / "collision_dest"
    dest.mkdir()
    (dest / "20260714_GameDay_001.mov").write_bytes(b"x")
    template = _base_template()
    files = [_file("clip1.mov", "mov")]
    names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z", dest_path=str(dest))
    assert names == ["20260714_GameDay_002.mov"]


def test_single_file_job_respects_explicit_seq_start():
    template = _base_template(seq_start=5)
    files = [_file("clip1.mov", "mov")]
    names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z")
    assert names == ["20260714_GameDay_005.mov"]


def test_multi_file_job_never_drops_seq():
    template = _base_template()
    files = [_file("clip1.mov", "mov"), _file("clip2.mov", "mov")]
    names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z")
    assert names == ["20260714_GameDay_001.mov", "20260714_GameDay_002.mov"]


def test_single_file_job_with_no_seq_token_is_unaffected():
    template = _base_template(file_template="{OriginalName}.{ext}")
    files = [_file("clip1.mov", "mov")]
    names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z")
    assert names == ["clip1.mov"]


def test_dest_scan_with_no_seq_token_and_colliding_file_does_not_crash(tmp_path):
    """Regression test: a template with no {Seq} token (use_source_filename)
    has no capture group for the collision-scan regex to extract a number
    from -- scanning must not blow up just because a same-named file
    already exists at the destination."""
    dest = tmp_path / "no_seq_dest"
    dest.mkdir()
    (dest / "clip1.mov").write_bytes(b"x")

    template = _base_template(use_source_filename=True, file_template="{OriginalName}.{ext}")
    files = [_file("clip1.mov", "mov")]
    names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z", dest_path=str(dest))
    assert names == ["clip1.mov"]


def test_seq_start_override_wins_over_destination_scan(tmp_path):
    dest = tmp_path / "override"
    dest.mkdir()
    (dest / "20260714_GameDay_014.mov").write_bytes(b"x")

    template = _base_template(seq_start=100)
    files = [_file("new1.mov", "mov"), _file("new2.mov", "mov")]
    names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z", dest_path=str(dest))
    assert names == ["20260714_GameDay_100.mov", "20260714_GameDay_101.mov"]


def test_yyyy_token_resolves_to_year_only_in_file_and_folder_names():
    template = _base_template(
        folder_template="{Name} {YYYY}", file_template="{Name}_{YYYY}_{Seq}.{ext}")
    # Two files so the single-file seq-drop doesn't mask the {YYYY} token
    # substitution behavior this test is actually about.
    files = [_file("clip1.mov", "mov"), _file("clip2.mov", "mov")]
    names, _warnings = naming.resolve_file_names(
        files, template, "GameDay", "2026-07-14T00:00:00Z")
    assert names[0] == "GameDay_2026_001.mov"

    folder = naming.resolve_folder_name(template, "GameDay", "2026-07-14T00:00:00Z")
    assert folder == "GameDay 2026"


def test_folder_collision_no_conflict(tmp_path):
    result = naming.check_folder_collision(str(tmp_path), "does_not_exist")
    assert result["status"] == "no_conflict"


def test_folder_collision_exists_empty(tmp_path):
    (tmp_path / "20260714_GameDay").mkdir()
    result = naming.check_folder_collision(str(tmp_path), "20260714_GameDay")
    assert result["status"] == "exists_empty"


def test_folder_collision_exists_non_empty(tmp_path):
    target = tmp_path / "20260714_GameDay"
    target.mkdir()
    (target / "existing.mov").write_bytes(b"x")
    result = naming.check_folder_collision(str(tmp_path), "20260714_GameDay")
    assert result["status"] == "exists_non_empty"


def test_manual_date_source_missing_manual_date_is_hard_error():
    template = _base_template(date_source="manual")
    files = [_file("clip.mov", "mov")]
    with pytest.raises(naming.NamingError):
        naming.resolve_file_names(files, template, "GameDay", "2026-07-14T00:00:00Z")


def test_validate_template_rejects_unknown_file_token():
    template = _base_template(file_template="{Bogus}_{Seq}.{ext}")
    with pytest.raises(naming.NamingError):
        naming.validate_template(template)


def test_validate_template_rejects_unknown_folder_token():
    template = _base_template(folder_template="{Bogus}")
    with pytest.raises(naming.NamingError):
        naming.validate_template(template)


def test_validate_template_skips_folder_check_when_no_subfolder():
    # An otherwise-invalid folder_template is never even inspected when
    # no_subfolder is set (no folder gets created, so its token content is
    # irrelevant) -- must not raise.
    template = _base_template(folder_template="{Bogus}", no_subfolder=True)
    naming.validate_template(template)  # should not raise


@pytest.mark.parametrize("raw,expected", [
    ("a/b", "a_b"),
    ("a:b", "a_b"),
    ("a*b", "a_b"),
    ("a?b", "a_b"),
    ('a"b', "a_b"),
    ("a<b", "a_b"),
    ("a>b", "a_b"),
    ("a|b", "a_b"),
    ("Game Day", "Game Day"),  # spaces are left alone -- this is the folder-name sanitizer
])
def test_sanitize_name_replaces_illegal_filesystem_characters(raw, expected):
    assert naming.sanitize_name(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("a/b", "a_b"),
    ("a|b", "a_b"),
    ("Game Day", "GameDay"),  # whitespace is closed up, not substituted
    ("Football  vs  Exeter", "FootballvsExeter"),  # multiple/runs of spaces too
])
def test_sanitize_file_name_also_closes_up_whitespace(raw, expected):
    assert naming.sanitize_file_name(raw) == expected
