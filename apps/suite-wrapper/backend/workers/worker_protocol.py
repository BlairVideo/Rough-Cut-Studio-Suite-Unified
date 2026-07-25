"""
worker_protocol.py — the ONE definition of the suite<->worker wire format
(contract A-2). Before this module, the schema lived implicitly in every
worker's emit() helper on one side and jobs.py's hand-rolled parse loop on
the other; drift between them was caught only at runtime.

Transport: params go to the worker as a single JSON object in argv[1];
the worker answers with ONE JSON object PER LINE on stdout. stderr is
free-form (ring-buffered for error reporting). Non-JSON stdout lines are
NOT an error — they're ignored by parse_line() returning None (the
workers' "--selfcheck" mode prints a human-readable "WORKER OK", and a
sibling library could print anything).

Message shapes:
    {"type": "progress", "progress": <float 0-100>, "detail": <str>}
    {"type": "result",   "data": <kind-specific payload dict>}
    {"type": "error",    "message": <str>}

This file must stay STDLIB-ONLY and free of suite imports: the workers
run it inside each sibling app's own venv. It sits in backend/workers/ so
a worker launched as a plain script (sys.path[0] == this directory) can
`import worker_protocol` bare, while suite-side code imports it as
backend.workers.worker_protocol — same file, both routes.
"""

import json

PROGRESS = "progress"
RESULT = "result"
ERROR = "error"
MESSAGE_TYPES = (PROGRESS, RESULT, ERROR)


# ---------- build side (workers) ----------

def make_progress(progress, detail=""):
    """Clamped/coerced at BUILD time so a worker bug (progress=None, a
    numpy float, a percentage string) can't emit an out-of-contract
    message that the receive side then has to defend against."""
    try:
        pct = round(max(0.0, min(100.0, float(progress))), 2)
    except (TypeError, ValueError):
        pct = 0.0
    return {"type": PROGRESS, "progress": pct, "detail": str(detail or "")}


def make_result(data):
    return {"type": RESULT, "data": data}


def make_error(message):
    return {"type": ERROR, "message": str(message or "Worker reported an error.")}


def encode(msg):
    """One protocol line, newline-terminated, ready to write."""
    return json.dumps(msg) + "\n"


# ---------- parse side (jobs.py) ----------

def parse_line(line):
    """Decode one stdout line into a normalized protocol message, or None
    for anything that isn't one (blank lines, stray prints, non-dict JSON,
    unknown types). The returned dict always has the full field set for
    its type, already coerced — progress is a clamped float, detail and
    message are strings — so consumers never re-validate."""
    line = (line or "").strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    mtype = msg.get("type")
    if mtype == PROGRESS:
        out = make_progress(msg.get("progress"), msg.get("detail", ""))
        # A progress line with no usable number should leave the job's
        # current percentage alone rather than snapping it back to 0 —
        # signal that with None (make_progress's build-side clamp is the
        # wrong default here on the READ side).
        try:
            float(msg.get("progress"))
        except (TypeError, ValueError):
            out["progress"] = None
        return out
    if mtype == RESULT:
        return {"type": RESULT, "data": msg.get("data")}
    if mtype == ERROR:
        return make_error(msg.get("message"))
    return None
