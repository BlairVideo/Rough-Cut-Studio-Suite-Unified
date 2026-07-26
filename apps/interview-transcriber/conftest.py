"""
Root conftest.py -- deliberately empty except for this docstring.

Its only job is to exist: pytest's default ("prepend") import mode adds
the directory containing any conftest.py it discovers to sys.path
(since this directory has no __init__.py, making it a plain top-level
directory, not a package). That's what lets tests/ `import app` directly,
without this project needing to be pip installed or packaged, and
without every test file hand-rolling its own sys.path hack. Mirrors the
same pattern already used by B-Roll Analyzer's conftest.py.

Importing app.py itself is safe and side-effect-free: mlx_whisper and
pyannote.audio (both heavy, Apple-Silicon/GPU-specific) are imported
lazily inside the functions that need them, not at module level, and
every st.* call that needs a real Streamlit script run lives inside
main() (guarded by `if __name__ == "__main__":`), not at import time.
"""

import pytest


class FakeSessionState(dict):
    """Minimal stand-in for st.session_state: supports both attribute
    access (st.session_state.speaker_names) and dict-style access
    (st.session_state.get(...)/[...]/pop(...)), same as the real thing.

    Used instead of Streamlit's own bare-mode session_state fallback
    (which works standalone but prints "missing ScriptRunContext"
    warnings to stderr on every access outside `streamlit run`) so tests
    stay deterministic and quiet regardless of Streamlit version."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value


@pytest.fixture
def fake_session_state(monkeypatch):
    """Patches app.st.session_state with a fresh FakeSessionState seeded
    with the keys app.py assumes already exist by the time these
    functions run (speaker_names/last_export_paths are initialized once
    at app startup in the real app)."""
    import app as app_module

    state = FakeSessionState(speaker_names={}, last_export_paths={})
    monkeypatch.setattr(app_module.st, "session_state", state)
    return state
