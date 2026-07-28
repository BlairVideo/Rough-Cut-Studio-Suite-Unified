"""Mirrored sibling constants must MATCH (addendum v10 / A-3).

The suite hand-copies a handful of sibling-app values it can't import
directly (their modules need streamlit / are Tkinter entry points).
These checks assert the VALUES still agree with the real sibling source,
so a sibling update can't drift silently."""

import ast
import json
import os
import subprocess

import pytest

from backend import paths


def test_transcriber_whisper_models_match_worker_selfcheck():
    # Via the worker's own venv/interpreter: its --selfcheck dumps
    # {"whisper_models", "cache_suffix"} as a JSON line before "WORKER OK".
    # Skipped if the transcriber venv isn't set up on this machine -- the
    # equality is then unverifiable, which shouldn't fail an otherwise-
    # green run.
    if not os.path.exists(paths.IVT_PYTHON):
        pytest.skip("transcriber venv missing -- WHISPER_MODELS equality not verifiable")

    from backend import suite_api as sa

    worker_py = os.path.join(paths.SUITE_DIR, "backend", "workers", "transcribe_worker.py")
    proc = subprocess.run(
        [paths.IVT_PYTHON, worker_py, "--selfcheck"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"worker selfcheck failed: {proc.stderr[-800:]}"
    json_line = next(ln for ln in proc.stdout.splitlines() if ln.strip().startswith("{"))
    dumped = json.loads(json_line)
    assert dumped["whisper_models"] == sa.WHISPER_MODELS, \
        ("suite WHISPER_MODELS drifted from transcriber app.py:\n"
         f"  theirs: {dumped['whisper_models']}\n  ours:   {sa.WHISPER_MODELS}")
    assert dumped["cache_suffix"] == sa.IVT_CACHE_SUFFIX, \
        f"cache suffix drift: theirs {dumped['cache_suffix']!r} vs ours {sa.IVT_CACHE_SUFFIX!r}"


def _fn_body_dump(source, fn_name):
    """AST dump of a function's body minus its docstring: robust to
    comments/whitespace, trips on any real change to keys/values."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]  # drop the docstring
            return [ast.dump(n) for n in body]
    raise AssertionError(f"{fn_name} not found in source")


@pytest.fixture
def brander_source():
    with open(os.path.join(paths.BRANDER_DIR, "app.py"), "r", encoding="utf-8") as f:
        return f.read()


def test_brander_default_scene_matches_bridge(brander_source):
    # Blair Brander's app.py is a Tkinter entry point -- never imported --
    # so this compares the two default_scene() function bodies via ast.
    with open(os.path.join(paths.SUITE_DIR, "backend", "brander_bridge.py"),
              "r", encoding="utf-8") as f:
        bridge_src = f.read()
    assert _fn_body_dump(brander_source, "default_scene") == \
           _fn_body_dump(bridge_src, "default_scene"), \
        "brander_bridge.default_scene() drifted from Blair Brander app.py's"


def test_brander_logo_placements_match_standalone_app(brander_source):
    from backend import brander_bridge

    # LOGO_PLACEMENTS: the standalone builds the list inline in its
    # Placement OptionMenu code (a local `placements = [...]` literal).
    placements_lists = [
        ast.literal_eval(node.value)
        for node in ast.walk(ast.parse(brander_source))
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "placements" for t in node.targets)
        and isinstance(node.value, ast.List)
    ]
    assert placements_lists, "couldn't find `placements = [...]` in Brander app.py"
    assert brander_bridge.LOGO_PLACEMENTS in placements_lists, \
        (f"bridge LOGO_PLACEMENTS {brander_bridge.LOGO_PLACEMENTS} no longer "
         f"matches Brander app.py's inline list(s): {placements_lists}")
    assert brander_bridge.DEFAULT_LOGO_PLACEMENT in brander_bridge.LOGO_PLACEMENTS


def test_no_module_basename_collides_across_sibling_dirs_on_sys_path():
    """Four sibling dirs sit on `sys.path` simultaneously in-process (see
    api_shared.py/brander_bridge.py/colorize_bridge.py/harmonizer_bridge.py's
    `sys.path.insert` calls). No basenames collide today, but nothing stops
    a future addition (e.g. colorize's project.py/grade.py/lut.py vs.
    blair-brander's export.py/assets.py/timeline.py -- all generic enough
    names) from silently shadowing a sibling's module instead of failing
    loudly. This guards importability, not just constant values (the tests
    above only check that mirrored constants agree)."""
    sibling_dirs = {
        "rcs": paths.RCS_BACKEND_DIR,
        "brander": paths.BRANDER_DIR,
        "colorize": paths.COLORIZE_DIR,
        "harmonizer": paths.HARMONIZER_BACKEND_DIR,
    }

    basenames_by_dir = {}
    for label, dir_path in sibling_dirs.items():
        if not os.path.isdir(dir_path):
            continue  # e.g. a sibling app not present on this checkout
        basenames_by_dir[label] = {
            fname[:-3]
            for fname in os.listdir(dir_path)
            if fname.endswith(".py")
            # conftest.py is pytest's own auto-discovery convention, not a
            # module any sibling bridge bare-imports -- collisions there
            # are not a shadowing risk.
            and fname != "conftest.py"
        }

    seen = {}
    collisions = []
    for label, basenames in basenames_by_dir.items():
        for name in basenames:
            if name in seen:
                collisions.append(f"{name}.py in both {seen[name]!r} and {label!r}")
            else:
                seen[name] = label

    assert not collisions, (
        "module basename collides across dirs simultaneously on sys.path -- "
        "whichever is inserted last will silently shadow the other's import "
        "instead of failing:\n  " + "\n  ".join(collisions)
    )
