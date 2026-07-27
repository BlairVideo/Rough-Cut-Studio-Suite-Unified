"""
tests/test_lut.py -- .cube/.3dl parsing, trilinear sampling, and the
WebGL preview JSON / write-back round trip.
"""

import os

import pytest

from lut import (CubeLut, LutParseError, identity_lut, parse_cube, parse_3dl,
                  parse_lut_file, sample_lut, to_preview_json, write_cube)


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


def test_parse_cube_basic(tmp_path):
    # A trivial 2x2x2 identity-ish cube.
    content = (
        'TITLE "Test LUT"\n'
        "LUT_3D_SIZE 2\n"
        "0.0 0.0 0.0\n"
        "1.0 0.0 0.0\n"
        "0.0 1.0 0.0\n"
        "1.0 1.0 0.0\n"
        "0.0 0.0 1.0\n"
        "1.0 0.0 1.0\n"
        "0.0 1.0 1.0\n"
        "1.0 1.0 1.0\n"
    )
    path = _write(tmp_path, "test.cube", content)
    parsed = parse_cube(path)
    assert parsed.size == 2
    assert parsed.title == "Test LUT"
    assert len(parsed.data) == 8
    assert parsed.data[0] == (0.0, 0.0, 0.0)
    assert parsed.data[7] == (1.0, 1.0, 1.0)


def test_parse_cube_1d_size_raises(tmp_path):
    content = "LUT_1D_SIZE 4\n0 0 0\n0.33 0.33 0.33\n0.66 0.66 0.66\n1 1 1\n"
    path = _write(tmp_path, "flat.cube", content)
    with pytest.raises(LutParseError):
        parse_cube(path)


def test_parse_cube_row_count_mismatch_raises(tmp_path):
    content = "LUT_3D_SIZE 2\n0 0 0\n1 0 0\n"  # only 2 rows, needs 8
    path = _write(tmp_path, "short.cube", content)
    with pytest.raises(LutParseError):
        parse_cube(path)


def test_parse_cube_missing_size_raises(tmp_path):
    content = "0 0 0\n1 1 1\n"
    path = _write(tmp_path, "nosize.cube", content)
    with pytest.raises(LutParseError):
        parse_cube(path)


def test_parse_3dl_basic(tmp_path):
    # 2x2x2, 8-bit-ish values (0/255), size inferred from row count.
    rows = [
        (0, 0, 0), (255, 0, 0), (0, 255, 0), (255, 255, 0),
        (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
    ]
    content = "\n".join(f"{r} {g} {b}" for r, g, b in rows) + "\n"
    path = _write(tmp_path, "test.3dl", content)
    parsed = parse_3dl(path)
    assert parsed.size == 2
    assert parsed.data[0] == pytest.approx((0.0, 0.0, 0.0))
    assert parsed.data[7] == pytest.approx((1.0, 1.0, 1.0))


def test_parse_3dl_non_cube_row_count_raises(tmp_path):
    content = "0 0 0\n1 1 1\n2 2 2\n"  # 3 rows -- not a perfect cube
    path = _write(tmp_path, "bad.3dl", content)
    with pytest.raises(LutParseError):
        parse_3dl(path)


def test_parse_lut_file_dispatches_by_extension(tmp_path):
    cube_path = _write(tmp_path, "a.cube", "LUT_3D_SIZE 2\n" + "0 0 0\n" * 8)
    assert parse_lut_file(cube_path).size == 2

    threedl_path = _write(tmp_path, "b.3dl", "0 0 0\n" * 8)
    assert parse_lut_file(threedl_path).size == 2

    bogus_path = str(tmp_path / "c.txt")
    open(bogus_path, "w").close()
    with pytest.raises(LutParseError):
        parse_lut_file(bogus_path)


def test_identity_lut_samples_to_input():
    lut = identity_lut(size=17)
    for r, g, b in [(0.0, 0.0, 0.0), (0.5, 0.5, 0.5), (1.0, 1.0, 1.0), (0.3, 0.7, 0.9)]:
        out = sample_lut(lut, r, g, b)
        assert out == pytest.approx((r, g, b), abs=1e-6)


def test_sample_lut_trilinear_interpolates():
    # 2x2x2 LUT that maps R -> R*2 (clamped) at the corners; sampling at
    # the midpoint should land between the two known corners.
    data = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
    ]
    lut = CubeLut(size=2, data=data)
    out = sample_lut(lut, 0.5, 0.0, 0.0)
    assert out[0] == pytest.approx(0.5, abs=1e-6)


def test_to_preview_json_flattens_in_order():
    lut = identity_lut(size=2)
    payload = to_preview_json(lut)
    assert payload["size"] == 2
    assert len(payload["data"]) == 2 ** 3 * 3
    # First lattice point (0,0,0) -> first 3 floats.
    assert payload["data"][:3] == list(lut.data[0])


def test_write_cube_round_trips(tmp_path):
    original = identity_lut(size=3)
    out_path = str(tmp_path / "roundtrip.cube")
    write_cube(original, out_path, title="Round Trip")
    reparsed = parse_cube(out_path)
    assert reparsed.size == original.size
    assert reparsed.title == "Round Trip"
    for a, b in zip(original.data, reparsed.data):
        assert a == pytest.approx(b, abs=1e-5)
