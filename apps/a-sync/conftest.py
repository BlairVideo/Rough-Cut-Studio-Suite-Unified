"""
Root conftest.py -- deliberately empty except for this docstring.

Its only job is to exist: pytest's default ("prepend") import mode adds
the directory containing any conftest.py it discovers to sys.path
(since this directory has no __init__.py, making it a plain top-level
directory, not a package). That's what lets tests/ import `sync_core`
and `waveform_view` directly, without this project needing to be pip
installed or packaged, and without every test file hand-rolling its own
sys.path hack. Mirrors the same pattern already used by B-Roll
Analyzer's conftest.py.
"""
