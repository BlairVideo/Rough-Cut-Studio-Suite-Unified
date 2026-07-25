"""
jobs.py — thread-safe background job manager for the suite.

Two job flavors:

  * Subprocess jobs ("transcribe", "broll") — heavy work that needs a
    DIFFERENT interpreter (the sibling app's own venv, with mlx/pyannote or
    cv2/torch installed). Spawned as `interpreter script '<json params>'`
    with cwd set to the owning app's folder; the worker reports back over a
    one-JSON-object-per-line stdout protocol:
        {"type":"progress","progress":0-100,"detail":"..."}
        {"type":"result","data":{...}}
        {"type":"error","message":"..."}
    stderr is captured into a small ring buffer so a crash without a
    protocol-level error still yields a useful message.

  * Thread jobs ("brander_video", "brander_send", "braw_proxy") — work
    that runs fine in-process on a daemon thread rather than needing a
    different interpreter. Cancellation for these is cooperative (a flag
    the callable may check between steps); an ffmpeg export that is
    already streaming frames is NOT interruptible mid-run — the job is
    marked cancelled immediately but the underlying export may still
    finish writing its file. That's an accepted limitation: the output
    lands in a suite-owned folder or a user-chosen path and simply goes
    unused. "braw_proxy" (braw_bridge.py) is a thread job even though its
    real work happens in a CHILD PROCESS (the compiled BRAW proxy tool,
    a proprietary-SDK dependency isolated in its own process like every
    other heavy dependency in this suite) — the thread just owns that
    subprocess's lifecycle so cancellation/progress reporting reuse this
    same mechanism rather than a third one.

Concurrency: jobs of different kinds always run simultaneously.
"transcribe" and "braw_proxy" jobs are additionally throttled by a
per-kind running limit — "transcribe" stays at 1 (mlx-whisper/pyannote
contend for the single Metal/MPS device, see the transcriber's own
docs); "braw_proxy" is 2 (addendum v48: a folder with several BRAW
files serializing entirely behind one proxy at a time made a B-Roll
analyze job's per-file proxy wait (braw_bridge.wait_for_decode_path)
time out on files that were still legitimately queued, not stuck —
2 gives real parallelism without assuming the machine can sustain much
more concurrent decode+encode work than that). Excess jobs sit in a
FIFO queue with status "queued" and start automatically as running ones
finish. The limit is adjustable at runtime (JobManager.set_kind_limit).

pywebview dispatches every js_api call on a worker thread, so ALL shared
state here is guarded by one RLock. Job dicts handed out to the frontend
are snapshots (copies), never live references.
"""

import os
import json
import time
import uuid
import threading
import traceback
import subprocess
from collections import deque
from dataclasses import dataclass, field

# The canonical wire-format definition shared with the workers themselves
# (contract A-2) — message shapes/coercion live THERE, not here.
try:
    from .workers import worker_protocol
except ImportError:  # pragma: no cover — direct script import in tests
    from workers import worker_protocol

STDERR_RING_SIZE = 50          # last N stderr lines kept per subprocess job
KILL_GRACE_SECONDS = 5.0       # terminate() -> kill() escalation window

TERMINAL_STATUSES = ("done", "error", "cancelled")


@dataclass
class Job:
    id: str
    kind: str                   # "transcribe" | "broll" | "brander_video" | "brander_send" | "braw_proxy"
    label: str
    status: str = "queued"      # queued | running | done | error | cancelled
    progress: float = 0.0       # 0..100
    detail: str = ""
    error: str = None
    result: dict = None
    created_at: float = field(default_factory=time.time)
    finished_at: float = None

    # --- internal (never serialized to the frontend) ---
    _proc: object = None                 # subprocess.Popen while running
    _cancel_event: object = None         # threading.Event for thread jobs
    _stderr_lines: object = None         # deque ring buffer
    _launch: object = None               # zero-arg callable that actually starts the job (for queued jobs)

    def to_dict(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "progress": round(float(self.progress), 2),
            "detail": self.detail,
            "error": self.error,
            "result": self.result,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class JobManager:
    """Owns every Job. One instance per process (see get_job_manager())."""

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs = {}                       # id -> Job
        self._order = []                      # ids, oldest first
        self._kind_limits = {"transcribe": 1, "braw_proxy": 2}  # kind -> max simultaneously running
        self._queues = {}                     # kind -> deque of queued job ids

    # ---------- public queries ----------

    def list_jobs(self):
        """Snapshot of every job, newest first."""
        with self._lock:
            return [self._jobs[jid].to_dict() for jid in reversed(self._order)]

    def get_job(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def get_job_dict(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def clear_finished(self):
        with self._lock:
            keep = []
            for jid in self._order:
                if self._jobs[jid].status in TERMINAL_STATUSES:
                    del self._jobs[jid]
                else:
                    keep.append(jid)
            self._order = keep

    def set_kind_limit(self, kind, limit):
        """Adjust how many jobs of `kind` may run at once; immediately
        starts queued jobs if the limit was raised."""
        limit = max(1, int(limit))
        with self._lock:
            self._kind_limits[kind] = limit
        self._pump_queue(kind)

    def get_kind_limit(self, kind):
        with self._lock:
            return self._kind_limits.get(kind)

    # ---------- job creation ----------

    def start_subprocess_job(self, kind, label, interpreter, script, params,
                             cwd=None, on_done=None):
        """Register (and, capacity permitting, immediately start) a
        subprocess job. Returns the job id. `params` is JSON-serialized
        into argv[1] — that's the whole input contract with the worker.
        `on_done(job)` runs on the reader thread after a successful result
        is captured but before status flips to "done"; if it raises, the
        job is marked "error" instead (used for post-processing that is
        logically part of the job, e.g. handoff ingestion)."""
        job = self._new_job(kind, label)

        def launch():
            thread = threading.Thread(
                target=self._run_subprocess,
                args=(job, interpreter, script, params, cwd, on_done),
                daemon=True,
                name=f"job-{kind}-{job.id[:8]}",
            )
            thread.start()

        job._launch = launch
        self._start_or_queue(job)
        return job.id

    def start_thread_job(self, kind, label, fn, on_done=None):
        """Register a thread job. `fn(progress_cb, cancel_event)` runs on a
        daemon thread; it returns the result dict or raises. progress_cb
        signature: (percent_0_100, detail_str)."""
        job = self._new_job(kind, label)
        job._cancel_event = threading.Event()

        def launch():
            thread = threading.Thread(
                target=self._run_thread_job,
                args=(job, fn, on_done),
                daemon=True,
                name=f"job-{kind}-{job.id[:8]}",
            )
            thread.start()

        job._launch = launch
        self._start_or_queue(job)
        return job.id

    # ---------- cancellation ----------

    def cancel(self, job_id):
        """Cancel a queued or running job. Queued: simply dropped from its
        queue. Subprocess: terminate(), escalating to kill() after a grace
        period. Thread: cooperative flag (see module docstring). Always
        flips status to "cancelled" right away so the UI reflects intent
        even while a stubborn process winds down."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return {"ok": False, "error": f"Unknown job id: {job_id}"}
            if job.status in TERMINAL_STATUSES:
                return {"ok": True}  # already settled — cancelling is a no-op

            was_queued = job.status == "queued"
            job.status = "cancelled"
            job.finished_at = time.time()
            job.detail = "Cancelled"
            if was_queued:
                q = self._queues.get(job.kind)
                if q is not None:
                    try:
                        q.remove(job.id)
                    except ValueError:
                        pass
            proc = job._proc
            if job._cancel_event is not None:
                job._cancel_event.set()

        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

            def escalate():
                try:
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass

            timer = threading.Timer(KILL_GRACE_SECONDS, escalate)
            timer.daemon = True
            timer.start()

        if was_queued:
            self._pump_queue(job.kind)
        return {"ok": True}

    # ---------- internals ----------

    def _new_job(self, kind, label):
        job = Job(id=uuid.uuid4().hex, kind=kind, label=label)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        return job

    def _running_count(self, kind):
        return sum(1 for j in self._jobs.values()
                   if j.kind == kind and j.status == "running")

    def _start_or_queue(self, job):
        with self._lock:
            limit = self._kind_limits.get(job.kind)
            if limit is not None and self._running_count(job.kind) >= limit:
                self._queues.setdefault(job.kind, deque()).append(job.id)
                job.detail = "Waiting for an earlier job to finish…"
                return
            job.status = "running"
        job._launch()

    def _pump_queue(self, kind):
        """Start as many queued jobs of `kind` as the limit now allows."""
        to_launch = []
        with self._lock:
            q = self._queues.get(kind)
            limit = self._kind_limits.get(kind)
            while q:
                if limit is not None and self._running_count(kind) >= limit:
                    break
                jid = q.popleft()
                job = self._jobs.get(jid)
                if job is None or job.status != "queued":
                    continue  # cancelled/cleared while waiting
                job.status = "running"
                job.detail = ""
                to_launch.append(job)
        for job in to_launch:
            job._launch()

    def _finish(self, job, status, error=None):
        """Flip a job to a terminal state — but never overwrite an earlier
        terminal state (e.g. a cancel that raced the natural exit)."""
        with self._lock:
            if job.status not in TERMINAL_STATUSES:
                job.status = status
                job.error = error
                job.finished_at = time.time()
                if status == "done":
                    job.progress = 100.0
        self._pump_queue(job.kind)

    # ---------- subprocess runner ----------

    def _run_subprocess(self, job, interpreter, script, params, cwd, on_done):
        stderr_ring = deque(maxlen=STDERR_RING_SIZE)
        job._stderr_lines = stderr_ring
        try:
            proc = subprocess.Popen(
                [interpreter, script, json.dumps(params)],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            self._finish(job, "error", f"Couldn't start worker process: {e}")
            return

        with self._lock:
            if job.status in TERMINAL_STATUSES:
                # Cancelled between queue and spawn — kill immediately.
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            job._proc = proc

        def drain_stderr():
            try:
                for line in proc.stderr:
                    line = line.rstrip("\n")
                    if line.strip():
                        stderr_ring.append(line)
            except Exception:
                pass

        err_thread = threading.Thread(target=drain_stderr, daemon=True)
        err_thread.start()

        result_data = None
        worker_error = None
        try:
            for line in proc.stdout:
                msg = worker_protocol.parse_line(line)
                if msg is None:
                    continue  # stray non-protocol output — ignore
                if msg["type"] == worker_protocol.PROGRESS:
                    with self._lock:
                        if job.status == "running":
                            # progress None = "no usable number in this
                            # line" — keep the current percentage.
                            if msg["progress"] is not None:
                                job.progress = msg["progress"]
                            job.detail = msg["detail"] or job.detail
                elif msg["type"] == worker_protocol.RESULT:
                    result_data = msg["data"]
                elif msg["type"] == worker_protocol.ERROR:
                    worker_error = msg["message"]
        except Exception:
            pass  # pipe closed by termination — exit code handling below decides

        proc.wait()
        err_thread.join(timeout=2.0)
        job._proc = None

        if job.status == "cancelled":
            return  # user cancelled; whatever the worker managed to say is moot

        if worker_error is not None:
            self._finish(job, "error", worker_error)
            return
        if result_data is None:
            tail = "\n".join(stderr_ring) or f"Worker exited with code {proc.returncode} and no result."
            self._finish(job, "error", tail[-4000:])
            return

        with self._lock:
            job.result = result_data
        if on_done is not None:
            try:
                on_done(job)
            except Exception as e:
                traceback.print_exc()
                self._finish(job, "error", f"Post-processing failed: {e}")
                return
        self._finish(job, "done")

    # ---------- thread runner ----------

    def _run_thread_job(self, job, fn, on_done):
        def progress_cb(pct, detail=""):
            with self._lock:
                if job.status == "running":
                    try:
                        job.progress = max(0.0, min(100.0, float(pct)))
                    except (TypeError, ValueError):
                        pass
                    if detail:
                        job.detail = str(detail)

        try:
            result = fn(progress_cb, job._cancel_event)
        except Exception as e:
            traceback.print_exc()
            if job._cancel_event.is_set():
                return  # already marked cancelled; the failure is expected fallout
            self._finish(job, "error", str(e) or repr(e))
            return

        if job._cancel_event.is_set() or job.status == "cancelled":
            return

        with self._lock:
            job.result = result if isinstance(result, dict) else {"value": result}
        if on_done is not None:
            try:
                on_done(job)
            except Exception as e:
                traceback.print_exc()
                self._finish(job, "error", f"Post-processing failed: {e}")
                return
        self._finish(job, "done")


_manager = None
_manager_lock = threading.Lock()


def get_job_manager():
    """Process-wide JobManager singleton — SuiteApi and any helper module
    always talk to the same job table."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = JobManager()
        return _manager
