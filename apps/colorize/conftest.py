"""
Root conftest.py -- deliberately empty except for this docstring.

Its only job is to exist: pytest's default ("prepend") import mode adds
the directory containing any conftest.py it discovers to sys.path (since
this directory has no __init__.py, making it a plain top-level directory,
not a package). That's what lets tests/ import `grade`, `lut`,
`ffmpeg_graph`, and `project` directly, without this app needing to be
pip installed or packaged. Mirrors blair-brander's conftest.py.
"""
