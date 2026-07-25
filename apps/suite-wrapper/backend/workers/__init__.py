# Worker scripts run with the sibling apps' own venv interpreters, never
# imported by the suite process itself. This __init__.py exists only so
# the directory is a proper package for tooling; the workers are executed
# as plain scripts (python transcribe_worker.py '<json>').
