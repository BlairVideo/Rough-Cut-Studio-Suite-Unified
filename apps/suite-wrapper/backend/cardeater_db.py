"""
cardeater_db.py — SQLite schema + CRUD for the Card Eater workspace.

Python port of Card Eater's own db.rs (see CardEater/src-tauri/src/db.rs and
its migrations/*.sql) — same table shapes, same upsert-by-name behavior for
naming templates, same CSV export format for job history. One difference
from the original: there's no versioned-migrations system here (this schema
was ported wholesale, not evolved in place), so it's a single CREATE-TABLE
pass rather than the original's three migration files.

One `Connection` per SuiteApi instance, guarded by a single RLock (pywebview
dispatches every js_api call on a worker thread — same rationale as jobs.py).
"""

import os
import sqlite3
import threading

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS favorites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    path        TEXT NOT NULL UNIQUE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS naming_templates (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL UNIQUE,
    folder_template      TEXT NOT NULL,
    file_template        TEXT NOT NULL,
    date_source          TEXT NOT NULL DEFAULT 'card_insert'
                           CHECK (date_source IN ('card_insert','file_metadata','manual')),
    seq_start            INTEGER,
    seq_padding          INTEGER NOT NULL DEFAULT 3,
    no_subfolder         INTEGER NOT NULL DEFAULT 0,
    use_source_filename  INTEGER NOT NULL DEFAULT 0,
    no_sequence          INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_card_label  TEXT NOT NULL,
    source_path        TEXT NOT NULL,
    naming_template_id INTEGER REFERENCES naming_templates(id) ON DELETE SET NULL,
    event_name         TEXT,
    manual_date        TEXT,
    started_at         TEXT,
    finished_at        TEXT,
    status             TEXT NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued','running','paused','complete','failed','cancelled'))
);

CREATE TABLE IF NOT EXISTS job_destinations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    dest_path       TEXT NOT NULL,
    files_total     INTEGER NOT NULL DEFAULT 0,
    files_copied    INTEGER NOT NULL DEFAULT 0,
    files_verified  INTEGER NOT NULL DEFAULT 0,
    bytes_total     INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued','running','paused','complete','failed','cancelled')),
    resolved_path   TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_destinations_job_id ON job_destinations(job_id);

CREATE TABLE IF NOT EXISTS job_files (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_destination_id  INTEGER NOT NULL REFERENCES job_destinations(id) ON DELETE CASCADE,
    original_name       TEXT NOT NULL,
    new_name            TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL,
    hash_source         TEXT,
    hash_dest           TEXT,
    verified            INTEGER NOT NULL DEFAULT 0,
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_files_job_destination_id ON job_files(job_destination_id);
"""


class Db:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.lock = threading.RLock()


# ---------------------------------------------------------------------------
# Favorites (destination bookmarks -- pinned *destination* folders, not
# sources; see architecture doc section 3.2)
# ---------------------------------------------------------------------------

def list_favorites(conn):
    cur = conn.execute(
        "SELECT id, label, path, sort_order, created_at FROM favorites "
        "ORDER BY sort_order ASC, id ASC"
    )
    return [
        {"id": r[0], "label": r[1], "path": r[2], "sort_order": r[3], "created_at": r[4]}
        for r in cur.fetchall()
    ]


def add_favorite(conn, label, path):
    conn.execute(
        "INSERT INTO favorites (label, path, sort_order) VALUES (?, ?, "
        "(SELECT COALESCE(MAX(sort_order), -1) + 1 FROM favorites))",
        (label, path),
    )
    conn.commit()
    fav_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    row = conn.execute(
        "SELECT id, label, path, sort_order, created_at FROM favorites WHERE id = ?",
        (fav_id,),
    ).fetchone()
    return {"id": row[0], "label": row[1], "path": row[2], "sort_order": row[3], "created_at": row[4]}


def remove_favorite(conn, fav_id):
    conn.execute("DELETE FROM favorites WHERE id = ?", (fav_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Naming templates
# ---------------------------------------------------------------------------

_TEMPLATE_COLUMNS = (
    "id, name, folder_template, file_template, date_source, seq_start, "
    "seq_padding, no_subfolder, use_source_filename, no_sequence, created_at"
)


def _row_to_template(row):
    return {
        "id": row[0],
        "name": row[1],
        "folder_template": row[2],
        "file_template": row[3],
        "date_source": row[4],
        "seq_start": row[5],
        "seq_padding": row[6],
        "no_subfolder": bool(row[7]),
        "use_source_filename": bool(row[8]),
        "no_sequence": bool(row[9]),
        "created_at": row[10],
    }


def list_naming_templates(conn):
    cur = conn.execute(f"SELECT {_TEMPLATE_COLUMNS} FROM naming_templates ORDER BY name ASC")
    return [_row_to_template(r) for r in cur.fetchall()]


def save_naming_template(conn, tpl):
    """Inserts a new naming template, or -- since `name` is unique --
    overwrites an existing one of the same name in place (upsert) rather
    than erroring or duplicating."""
    conn.execute(
        """INSERT INTO naming_templates
               (name, folder_template, file_template, date_source, seq_start,
                seq_padding, no_subfolder, use_source_filename, no_sequence)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET
               folder_template = excluded.folder_template,
               file_template = excluded.file_template,
               date_source = excluded.date_source,
               seq_start = excluded.seq_start,
               seq_padding = excluded.seq_padding,
               no_subfolder = excluded.no_subfolder,
               use_source_filename = excluded.use_source_filename,
               no_sequence = excluded.no_sequence""",
        (
            tpl["name"], tpl["folder_template"], tpl["file_template"], tpl["date_source"],
            tpl.get("seq_start"), tpl["seq_padding"], int(bool(tpl["no_subfolder"])),
            int(bool(tpl["use_source_filename"])), int(bool(tpl.get("no_sequence", False))),
        ),
    )
    conn.commit()
    row = conn.execute(
        f"SELECT {_TEMPLATE_COLUMNS} FROM naming_templates WHERE name = ?", (tpl["name"],)
    ).fetchone()
    return _row_to_template(row)


def delete_naming_template(conn, tpl_id):
    conn.execute("DELETE FROM naming_templates WHERE id = ?", (tpl_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Job history (read-only summaries for the history/export view -- the
# authoritative per-destination progress lives in cardeater_copy.py /
# api_cardeater.py's get_job_status; this is a coarser rollup for browsing
# past jobs)
# ---------------------------------------------------------------------------

def list_job_summaries(conn, card_id=None):
    cur = conn.execute(
        """SELECT j.id, j.source_card_label, j.started_at, j.finished_at, j.status,
                  (SELECT COUNT(*) FROM job_destinations d WHERE d.job_id = j.id) AS dest_count,
                  (SELECT COALESCE(SUM(d.files_total), 0) FROM job_destinations d WHERE d.job_id = j.id) AS file_count,
                  (SELECT COALESCE(SUM(d.bytes_total), 0) FROM job_destinations d WHERE d.job_id = j.id) AS bytes_total,
                  (SELECT COALESCE(GROUP_CONCAT(d.dest_path, ', '), '') FROM job_destinations d WHERE d.job_id = j.id) AS destination_paths
           FROM jobs j
           WHERE ?1 IS NULL OR j.source_path = ?1
           ORDER BY j.id DESC""",
        (card_id,),
    )
    return [
        {
            "id": r[0], "source_card_label": r[1], "started_at": r[2], "finished_at": r[3],
            "status": r[4], "destination_count": r[5], "file_count": r[6],
            "bytes_total": r[7], "destination_paths": r[8],
        }
        for r in cur.fetchall()
    ]


_STATUS_LABELS = {
    "queued": "Queued", "running": "Running", "paused": "Paused",
    "complete": "Complete", "failed": "Failed", "cancelled": "Cancelled",
}


def csv_escape_field(field):
    """Quotes a CSV field per RFC 4180 whenever it contains a comma, quote,
    or newline (doubling any embedded quotes); left bare otherwise."""
    field = str(field)
    if "," in field or '"' in field or "\n" in field:
        return '"' + field.replace('"', '""') + '"'
    return field


def job_summaries_to_csv(rows):
    """Renders job summaries as CSV text (header row + one row per job),
    for the history view's "Export CSV" action."""
    lines = ["Job ID,Card,Started,Finished,Status,Destinations,Destination Paths,Files,Bytes"]
    for row in rows:
        lines.append(",".join([
            str(row["id"]),
            csv_escape_field(row["source_card_label"]),
            csv_escape_field(row["started_at"] or ""),
            csv_escape_field(row["finished_at"] or ""),
            _STATUS_LABELS.get(row["status"], row["status"]),
            str(row["destination_count"]),
            csv_escape_field(row["destination_paths"]),
            str(row["file_count"]),
            str(row["bytes_total"]),
        ]))
    return "\n".join(lines) + "\n"
