"""
cardeater_copy.py — copy queue manager + verification engine for Card Eater.

Python port of Card Eater's own copy_engine.rs (chunked streaming copy,
verify-worker pool overlapping subsequent copies, pause/resume/cancel,
multi-destination concurrency cap) and verify.rs (consumed via
cardeater_verify.py). See that file's own module docstring for the
behavioral contract this mirrors — this port keeps the same shape:

- Disk space preflight before a job is queued (one check, not just a
  mid-copy failure).
- A single process-wide semaphore caps how many DESTINATIONS copy
  concurrently (default 8) -- copy work is sequential *within* one
  destination (the source card's own read speed is the real bottleneck),
  but multiple destinations legitimately run at once.
- Each destination's copy loop overlaps hashing verification with the next
  file's copy via a small worker pool consuming a queue.
- One retry on a hash mismatch before flagging a file as failed.
- Pause/cancel checked once per file boundary (never mid-file) -- a
  partially-copied file is never rolled back or deleted.

One difference from the original: there's no Tauri event bus here, so
per-destination live progress (MB/s, ETA, current filename, any
destination-level error message) is kept in an in-memory dict on
`CardEaterState.live` rather than emitted as events — `get_job_status`
(api_cardeater.py) merges it in on each poll, mirroring the same
information the original pushed to the frontend as copy-progress/
verify-progress/job-error events.
"""

import os
import queue
import shutil
import threading
import time

try:
    from . import cardeater_naming as naming
    from . import cardeater_verify as verify
except ImportError:  # pragma: no cover — direct script import in tests
    import cardeater_naming as naming
    import cardeater_verify as verify

DEST_CONCURRENCY = 8
VERIFY_WORKERS_PER_DEST = 2
PROGRESS_EMIT_INTERVAL = 0.25  # seconds
PAUSE_POLL_INTERVAL = 0.2      # seconds

_dest_semaphore = threading.Semaphore(DEST_CONCURRENCY)


class JobControl:
    RUNNING = 0
    PAUSED = 1
    CANCELLED = 2

    def __init__(self):
        self._lock = threading.Lock()
        self._state = self.RUNNING

    @property
    def state(self):
        with self._lock:
            return self._state

    @state.setter
    def state(self, value):
        with self._lock:
            self._state = value


def _wait_for_turn(state, dest_id, control):
    """Blocks while `control` is paused. Returns True if cancelled (while
    waiting or already), False otherwise. Checked once per file boundary
    only, never mid-file.

    Also mirrors the pause into `job_destinations.status` for as long as
    it lasts (reverting to "running" on resume/cancel) — the JobControl
    atomic alone drives the actual blocking, but the DB status column is
    what `get_job_status`/`list_as_generic_jobs` report to the frontend,
    so without this a paused job would silently keep showing "running"
    (progress just stops advancing) with no way for the UI to ever offer
    a Resume button."""
    paused_marked = False
    while True:
        s = control.state
        if s == JobControl.CANCELLED:
            if paused_marked:
                _set_dest_status(state, dest_id, "running")
            return True
        if s == JobControl.PAUSED:
            if not paused_marked:
                _set_dest_status(state, dest_id, "paused")
                paused_marked = True
            time.sleep(PAUSE_POLL_INTERVAL)
            continue
        if paused_marked:
            _set_dest_status(state, dest_id, "running")
        return False


def _buffer_size_for(size_bytes):
    mb = 1024 * 1024
    if size_bytes < 100 * mb:
        return 8 * mb
    if size_bytes < 1024 * mb:
        return 16 * mb
    if size_bytes < 10 * 1024 * mb:
        return 32 * mb
    return 64 * mb


# ---------------------------------------------------------------------------
# Disk space preflight
# ---------------------------------------------------------------------------

def _existing_ancestor(path):
    """Walks up from `path` to the nearest existing ancestor directory --
    the destination's own subfolder may not exist yet (it's created at
    copy time), but disk_usage needs a real path."""
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return "/"
        p = parent
    return p


def check_disk_space(dest_paths, bytes_needed):
    results = []
    for dest_path in dest_paths:
        try:
            usage = shutil.disk_usage(_existing_ancestor(dest_path))
            available_bytes = usage.free
        except OSError:
            available_bytes = 0
        results.append({
            "dest_path": dest_path,
            "available_bytes": available_bytes,
            "required_bytes": bytes_needed,
            "ok": available_bytes >= bytes_needed,
        })
    return results


# ---------------------------------------------------------------------------
# Job control (pause/resume/cancel)
# ---------------------------------------------------------------------------

def set_job_control(state, job_id, value):
    with state.db.lock:
        rows = state.db.conn.execute(
            "SELECT id FROM job_destinations WHERE job_id = ?", (job_id,)
        ).fetchall()
    dest_ids = [r[0] for r in rows]
    if not dest_ids:
        raise ValueError(f"No destinations found for job {job_id}")
    with state.lock:
        for dest_id in dest_ids:
            ctrl = state.job_controls.get(dest_id)
            if ctrl is not None:
                ctrl.state = value


# ---------------------------------------------------------------------------
# start_job
# ---------------------------------------------------------------------------

def start_job(state, req):
    """req: {source_card_label, source_path, card_insert_date, event_name,
    manual_date, template, files, destinations}. Returns {"job_id": int}."""
    destinations = req["destinations"]
    if not destinations:
        raise ValueError("At least one destination is required")

    template = req["template"]
    # Resolve the subfolder name once up front (fail-fast template validity
    # check too, before any DB rows or threads are created).
    folder_name = None
    if not template["no_subfolder"]:
        preview = naming.preview_names({
            "card_insert_date": req["card_insert_date"],
            "event_name": req["event_name"],
            "manual_date": req.get("manual_date"),
            "template": template,
            "files": req["files"],
            "dest_path": None,
        })
        folder_name = preview["folder_name"]

    bytes_total = sum(f["size_bytes"] for f in req["files"])

    with state.db.lock:
        conn = state.db.conn
        conn.execute(
            """INSERT INTO jobs (source_card_label, source_path, naming_template_id,
                                  event_name, manual_date, status)
               VALUES (?, ?, NULL, ?, ?, 'queued')""",
            (req["source_card_label"], req["source_path"], req["event_name"], req.get("manual_date")),
        )
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        dest_ids = []
        for dest_path in destinations:
            conn.execute(
                """INSERT INTO job_destinations
                       (job_id, dest_path, files_total, files_copied, files_verified,
                        bytes_total, status)
                   VALUES (?, ?, ?, 0, 0, ?, 'queued')""",
                (job_id, dest_path, len(req["files"]), bytes_total),
            )
            dest_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            dest_ids.append(dest_id)

        conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
            (_now_iso(), job_id),
        )
        conn.commit()

    with state.lock:
        for dest_id in dest_ids:
            state.job_controls[dest_id] = JobControl()
            state.live[dest_id] = _blank_live()

    for dest_path, dest_id in zip(destinations, dest_ids):
        thread = threading.Thread(
            target=_run_destination,
            args=(state, job_id, dest_id, dest_path, req["files"], template,
                  req["event_name"], req["card_insert_date"], req.get("manual_date"), folder_name),
            daemon=True,
            name=f"cardeater-copy-{job_id}-{dest_id}",
        )
        thread.start()

    return {"job_id": job_id}


def _now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _blank_live():
    return {"mb_per_sec": 0.0, "eta_secs": None, "current_file_name": None, "error_message": None}


# ---------------------------------------------------------------------------
# Per-destination task
# ---------------------------------------------------------------------------

class _CopySourceError(Exception):
    pass


class _CopyDestError(Exception):
    pass


def _copy_chunked(src, dest, size_bytes, on_chunk=None):
    """Chunked streaming copy: moves data through a tiered-size buffer
    (never the whole file at once), calling on_chunk(n) with each chunk's
    length so callers can track progress."""
    try:
        src_f = open(src, "rb")
    except OSError as e:
        raise _CopySourceError(str(e)) from e
    try:
        try:
            dest_f = open(dest, "wb")
        except OSError as e:
            raise _CopyDestError(str(e)) from e
        try:
            buf_size = _buffer_size_for(size_bytes)
            while True:
                try:
                    chunk = src_f.read(buf_size)
                except OSError as e:
                    raise _CopySourceError(str(e)) from e
                if not chunk:
                    break
                try:
                    dest_f.write(chunk)
                except OSError as e:
                    raise _CopyDestError(str(e)) from e
                if on_chunk:
                    on_chunk(len(chunk))
            try:
                dest_f.flush()
                os.fsync(dest_f.fileno())
            except OSError as e:
                raise _CopyDestError(str(e)) from e
        finally:
            dest_f.close()
    finally:
        src_f.close()


def _run_destination(state, job_id, dest_id, dest_path, files, template,
                      event_name, card_insert_date, manual_date, folder_name):
    with _dest_semaphore:
        with state.lock:
            control = state.job_controls.get(dest_id)
        if control is None:
            return  # should never happen: inserted in start_job before spawn

        _set_dest_status(state, dest_id, "running")

        # Resolve/create the destination subfolder. Create-if-missing,
        # merge into it if it already exists (richer interactive collision
        # UX -- auto-suffix/rename/cancel -- is a frontend-side concern,
        # same as the original's own TODO carve-out).
        target_dir = os.path.join(dest_path, folder_name) if folder_name else dest_path
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as e:
            _set_live_error(state, dest_id, f"Failed to create destination folder: {e}")
            _set_dest_status(state, dest_id, "failed")
            _finalize_job_if_all_destinations_done(state, job_id)
            return
        _set_dest_resolved_path(state, dest_id, target_dir)

        # Resolve output file names against the *actual* target folder so
        # sequence-number collision scanning reflects what's really there.
        try:
            resolved_names, _warnings = naming.resolve_file_names(
                files, template, event_name, card_insert_date, manual_date, target_dir)
        except naming.NamingError as e:
            _set_live_error(state, dest_id, f"Failed to resolve file names: {e}")
            _set_dest_status(state, dest_id, "failed")
            _finalize_job_if_all_destinations_done(state, job_id)
            return

        # Insert one job_files row per file up front so progress/verification
        # can update rows in place rather than racing to create them later.
        job_file_ids = []
        with state.db.lock:
            conn = state.db.conn
            for f, new_name in zip(files, resolved_names):
                conn.execute(
                    """INSERT INTO job_files (job_destination_id, original_name, new_name, size_bytes)
                       VALUES (?, ?, ?, ?)""",
                    (dest_id, f["name"], new_name, f["size_bytes"]),
                )
                job_file_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.commit()

        files_total = len(files)
        bytes_total = sum(f["size_bytes"] for f in files)

        # Verify-worker pool: hashing overlaps the NEXT file's copy rather
        # than blocking it.
        verify_q = queue.Queue()
        verify_workers = []
        for _ in range(VERIFY_WORKERS_PER_DEST):
            t = threading.Thread(
                target=_verify_worker_loop,
                args=(state, job_id, dest_id, files_total, verify_q),
                daemon=True,
            )
            t.start()
            verify_workers.append(t)

        files_copied = 0
        bytes_copied_cumulative = 0
        bytes_since_emit = 0
        last_emit = time.monotonic()
        cancelled = False
        hard_error = None  # (message, resulting_dest_status)

        for idx, (file, new_name) in enumerate(zip(files, resolved_names)):
            if _wait_for_turn(state, dest_id, control):
                cancelled = True
                break

            src_path = file["path"]
            dest_file_path = os.path.join(target_dir, new_name)

            # Overwrite protection: never clobber a file already sitting at
            # the destination. The {Seq}-based naming collision scan
            # (cardeater_naming.py) is the normal guard against this, but a
            # template that omits {Seq} (use_source_filename, or the
            # single-file auto-drop) or a destination folder reused outside
            # this app can still resolve to a name that's already there --
            # skip and flag it rather than silently truncating someone
            # else's file.
            if os.path.exists(dest_file_path):
                _mark_file_failed(
                    state, dest_id, job_file_ids[idx], None, None,
                    f'A file named "{new_name}" already exists at the destination — skipped to avoid overwriting it.')
                continue

            def on_chunk(n):
                nonlocal bytes_copied_cumulative, bytes_since_emit, last_emit
                bytes_copied_cumulative += n
                bytes_since_emit += n
                now = time.monotonic()
                elapsed = now - last_emit
                if elapsed >= PROGRESS_EMIT_INTERVAL:
                    secs = max(elapsed, 0.001)
                    bytes_per_sec = bytes_since_emit / secs
                    mb_per_sec = bytes_per_sec / 1_000_000.0
                    remaining = max(0, bytes_total - bytes_copied_cumulative)
                    eta_secs = int(remaining / bytes_per_sec) if bytes_per_sec > 0 else None
                    with state.lock:
                        state.live[dest_id].update({
                            "mb_per_sec": mb_per_sec, "eta_secs": eta_secs,
                            "current_file_name": file["name"],
                        })
                    bytes_since_emit = 0
                    last_emit = now

            try:
                _copy_chunked(src_path, dest_file_path, file["size_bytes"], on_chunk)
            except _CopySourceError as e:
                # Card likely removed mid-copy: pause (resumable) rather
                # than failing the job outright.
                control.state = JobControl.PAUSED
                hard_error = (f"Lost access to source card: {e}", "paused")
                break
            except _CopyDestError as e:
                hard_error = (f"Destination write failed: {e}", "failed")
                break

            files_copied += 1
            _update_dest_files_copied(state, dest_id, files_copied)
            verify_q.put(_VerifyJob(job_file_ids[idx], file["name"], src_path, dest_file_path))

        for _ in range(VERIFY_WORKERS_PER_DEST):
            verify_q.put(None)  # one sentinel per worker
        for t in verify_workers:
            t.join()

        if hard_error is not None:
            message, dest_status = hard_error
            _set_live_error(state, dest_id, message)
            _set_dest_status(state, dest_id, dest_status)
            if dest_status != "paused":
                _finalize_job_if_all_destinations_done(state, job_id)
            return

        if cancelled:
            final_status = "cancelled"
        else:
            with state.db.lock:
                failed_count = state.db.conn.execute(
                    "SELECT COUNT(*) FROM job_files WHERE job_destination_id = ? AND verified = 0",
                    (dest_id,),
                ).fetchone()[0]
            final_status = "failed" if failed_count > 0 else "complete"

        _set_dest_status(state, dest_id, final_status)
        _finalize_job_if_all_destinations_done(state, job_id)


class _VerifyJob:
    __slots__ = ("job_file_id", "original_name", "source_path", "dest_path")

    def __init__(self, job_file_id, original_name, source_path, dest_path):
        self.job_file_id = job_file_id
        self.original_name = original_name
        self.source_path = source_path
        self.dest_path = dest_path


def _verify_worker_loop(state, job_id, dest_id, files_total, verify_q):
    while True:
        vjob = verify_q.get()
        if vjob is None:
            return
        _process_verify_job(state, job_id, dest_id, files_total, vjob)


def _process_verify_job(state, job_id, dest_id, files_total, vjob):
    try:
        result = verify.verify_pair(vjob.source_path, vjob.dest_path)
    except OSError as e:
        result = None
        error = str(e)
    else:
        error = None

    # One retry: re-copy (streamed, never loaded fully into memory) and
    # re-hash once before giving up on this file.
    if result is None or not result["matched"]:
        try:
            size_bytes = os.path.getsize(vjob.source_path)
        except OSError:
            size_bytes = 0
        try:
            _copy_chunked(vjob.source_path, vjob.dest_path, size_bytes)
            result = verify.verify_pair(vjob.source_path, vjob.dest_path)
            error = None
        except (_CopySourceError, _CopyDestError, OSError) as e:
            result = None
            error = str(e)

    if result is not None and result["matched"]:
        _mark_file_verified(state, dest_id, vjob.job_file_id, result["hash_source"], result["hash_dest"])
    elif result is not None:
        _mark_file_failed(state, dest_id, vjob.job_file_id, result["hash_source"], result["hash_dest"],
                           "Hash mismatch after retry")
    else:
        _mark_file_failed(state, dest_id, vjob.job_file_id, None, None, error or "Verification failed")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _set_dest_status(state, dest_id, status):
    with state.db.lock:
        state.db.conn.execute("UPDATE job_destinations SET status = ? WHERE id = ?", (status, dest_id))
        state.db.conn.commit()


def _set_dest_resolved_path(state, dest_id, resolved_path):
    with state.db.lock:
        state.db.conn.execute(
            "UPDATE job_destinations SET resolved_path = ? WHERE id = ?", (resolved_path, dest_id))
        state.db.conn.commit()


def _update_dest_files_copied(state, dest_id, files_copied):
    with state.db.lock:
        state.db.conn.execute(
            "UPDATE job_destinations SET files_copied = ? WHERE id = ?", (files_copied, dest_id))
        state.db.conn.commit()


def _mark_file_verified(state, dest_id, job_file_id, hash_source, hash_dest):
    with state.db.lock:
        conn = state.db.conn
        conn.execute(
            "UPDATE job_files SET hash_source = ?, hash_dest = ?, verified = 1, error = NULL WHERE id = ?",
            (hash_source, hash_dest, job_file_id),
        )
        conn.execute(
            "UPDATE job_destinations SET files_verified = files_verified + 1 WHERE id = ?", (dest_id,))
        conn.commit()


def _mark_file_failed(state, dest_id, job_file_id, hash_source, hash_dest, error):
    with state.db.lock:
        state.db.conn.execute(
            "UPDATE job_files SET hash_source = ?, hash_dest = ?, verified = 0, error = ? WHERE id = ?",
            (hash_source, hash_dest, error, job_file_id),
        )
        state.db.conn.commit()


def _set_live_error(state, dest_id, message):
    with state.lock:
        live = state.live.get(dest_id)
        if live is not None:
            live["error_message"] = message


def _finalize_job_if_all_destinations_done(state, job_id):
    """Once every destination for a job reaches a terminal state
    (complete/failed/cancelled), roll the parent jobs row up to one final
    status: failed > cancelled > complete, in that priority order."""
    with state.db.lock:
        conn = state.db.conn
        rows = conn.execute(
            "SELECT status FROM job_destinations WHERE job_id = ?", (job_id,)
        ).fetchall()
        statuses = [r[0] for r in rows]
        if not statuses or not all(s in ("complete", "failed", "cancelled") for s in statuses):
            return
        if "failed" in statuses:
            final_status = "failed"
        elif "cancelled" in statuses:
            final_status = "cancelled"
        else:
            final_status = "complete"
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?",
            (final_status, _now_iso(), job_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Status query
# ---------------------------------------------------------------------------

def get_job_status(state, job_id):
    with state.db.lock:
        conn = state.db.conn
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown job id: {job_id}")
        status = row[0]
        dest_rows = conn.execute(
            """SELECT id, dest_path, files_total, files_copied, files_verified, bytes_total,
                      status, resolved_path
               FROM job_destinations WHERE job_id = ?""",
            (job_id,),
        ).fetchall()

    destinations = []
    with state.lock:
        for r in dest_rows:
            dest_id = r[0]
            live = state.live.get(dest_id) or _blank_live()
            destinations.append({
                "id": dest_id, "dest_path": r[1], "files_total": r[2], "files_copied": r[3],
                "files_verified": r[4], "bytes_total": r[5], "status": r[6], "resolved_path": r[7],
                "mb_per_sec": live["mb_per_sec"], "eta_secs": live["eta_secs"],
                "current_file_name": live["current_file_name"], "error_message": live["error_message"],
            })

    return {"job_id": job_id, "status": status, "destinations": destinations}


def mark_card_safe_check(state, card_path):
    """True once every job destination for `card_path` has copied and
    verified (status == 'complete')."""
    with state.db.lock:
        incomplete = state.db.conn.execute(
            """SELECT COUNT(*) FROM job_destinations d
               JOIN jobs j ON j.id = d.job_id
               WHERE j.source_path = ? AND d.status != 'complete'""",
            (card_path,),
        ).fetchone()[0]
    return incomplete == 0


# ---------------------------------------------------------------------------
# Jobs-drawer integration — the Copy workspace has no queue panel of its own;
# every in-flight/recent destination is instead surfaced as a generic job
# dict (one per destination, matching jobs.py's Job.to_dict() shape plus a
# few cardeater_* extras) inside Studio Suite's existing suite_list_jobs /
# Jobs drawer, so there's exactly one place in the UI that shows background
# work. "Session-visible" == present in state.job_controls, which start_job
# populates and clear_finished (below) prunes; the underlying DB rows are
# never deleted (job history / CSV export still sees everything).
# ---------------------------------------------------------------------------

_STATUS_TO_GENERIC = {
    "queued": "queued", "running": "running", "paused": "paused",
    "complete": "done", "failed": "error", "cancelled": "cancelled",
}


def list_as_generic_jobs(state):
    with state.lock:
        dest_ids = list(state.job_controls.keys())
    if not dest_ids:
        return []

    placeholders = ",".join("?" * len(dest_ids))
    with state.db.lock:
        rows = state.db.conn.execute(
            f"""SELECT jd.id, jd.job_id, jd.dest_path, jd.files_total, jd.files_copied,
                       jd.files_verified, jd.bytes_total, jd.status, jd.resolved_path,
                       j.source_card_label, j.started_at, j.finished_at
                FROM job_destinations jd JOIN jobs j ON j.id = jd.job_id
                WHERE jd.id IN ({placeholders})""",
            dest_ids,
        ).fetchall()

    results = []
    with state.lock:
        for (dest_id, job_id, dest_path, files_total, files_copied, files_verified,
             bytes_total, status, resolved_path, card_label, started_at, finished_at) in rows:
            live = state.live.get(dest_id) or _blank_live()
            # Blend copy+verify into one 0..100 figure (two roughly equal
            # phases per file) so the bar reflects real progress even
            # before the first file finishes verifying.
            progress = ((files_copied + files_verified) / (2 * files_total) * 100.0) if files_total else 0.0
            detail = f"{files_copied}/{files_total} copied, {files_verified}/{files_total} verified"
            if live["current_file_name"]:
                detail += f" — {live['current_file_name']}"
            if live["mb_per_sec"]:
                detail += f" — {live['mb_per_sec']:.1f} MB/s"
            results.append({
                "id": f"cardeater-{dest_id}",
                "kind": "cardeater_copy",
                "label": f"{card_label} → {os.path.basename(dest_path.rstrip('/'))}",
                "status": _STATUS_TO_GENERIC.get(status, status),
                "progress": round(progress, 2),
                "detail": detail,
                "error": live["error_message"],
                "result": {
                    "resolved_path": resolved_path, "dest_path": dest_path,
                    "files_total": files_total, "files_verified": files_verified,
                    "bytes_total": bytes_total,
                },
                "created_at": started_at,
                "finished_at": finished_at,
                # cardeater-specific extras, ignored by every other job kind's
                # rendering but read by the drawer's cardeater_copy branch.
                "cardeater_job_id": job_id,
                "cardeater_dest_id": dest_id,
            })
    return results


def clear_finished(state):
    """Drops every terminal (complete/failed/cancelled) destination from
    the session-visible set — the Card-Eater half of the Jobs drawer's
    "Clear Finished" button. DB rows are untouched."""
    with state.lock:
        dest_ids = list(state.job_controls.keys())
    if not dest_ids:
        return
    placeholders = ",".join("?" * len(dest_ids))
    with state.db.lock:
        rows = state.db.conn.execute(
            f"SELECT id, status FROM job_destinations WHERE id IN ({placeholders})", dest_ids
        ).fetchall()
    terminal_ids = {r[0] for r in rows if r[1] in ("complete", "failed", "cancelled")}
    with state.lock:
        for dest_id in terminal_ids:
            state.job_controls.pop(dest_id, None)
            state.live.pop(dest_id, None)
