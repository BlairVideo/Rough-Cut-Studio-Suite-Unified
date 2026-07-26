"""
Root conftest.py.

Unlike B-Roll Analyzer/Suite Wrapper (whose modules sit at the app root
and rely on pytest's own rootdir-insertion), this app's real modules live
in backend/, and backend/*.py files import each other with flat, bare
names (e.g. `from transcript_parser import ...` inside api.py) rather
than as a `backend.` package -- there's no backend/__init__.py. main.py
handles this the same way: `sys.path.insert(0, .../backend)` before
importing `api`. This conftest.py does the identical insertion so
tests/ can `import transcript_parser`, `import xml_builder`, etc. the
same way the app's own entry point does, without installing this
project as a package.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
