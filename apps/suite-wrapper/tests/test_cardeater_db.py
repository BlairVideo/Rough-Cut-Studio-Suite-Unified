"""Card Eater SQLite layer (backend/cardeater_db.py): favorites CRUD, naming
template upsert-by-name, job-history summaries + CSV export. Ported schema
from CardEater/src-tauri/src/db.rs (see that module's own docstring)."""

import sqlite3

import pytest

from backend import cardeater_db as db


@pytest.fixture
def conn(tmp_path):
    d = db.Db(str(tmp_path / "cardeater.sqlite3"))
    return d.conn


def test_favorites_add_list_remove_round_trip(conn):
    fav1 = db.add_favorite(conn, "Editing Drive", "/Volumes/Editing")
    fav2 = db.add_favorite(conn, "Backup Drive", "/Volumes/Backup")
    assert fav1["sort_order"] == 0
    assert fav2["sort_order"] == 1

    listed = db.list_favorites(conn)
    assert [f["label"] for f in listed] == ["Editing Drive", "Backup Drive"]

    db.remove_favorite(conn, fav1["id"])
    listed_after = db.list_favorites(conn)
    assert [f["label"] for f in listed_after] == ["Backup Drive"]


def test_favorite_path_must_be_unique(conn):
    db.add_favorite(conn, "Editing Drive", "/Volumes/Editing")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_favorite(conn, "Editing Drive Again", "/Volumes/Editing")


def _template(name, **overrides):
    tpl = {
        "name": name,
        "folder_template": "{YYYYMMDD}_{Name}",
        "file_template": "{YYYYMMDD}_{Name}_{Seq}.{ext}",
        "date_source": "card_insert",
        "seq_start": None,
        "seq_padding": 3,
        "no_subfolder": False,
        "use_source_filename": False,
        "no_sequence": False,
    }
    tpl.update(overrides)
    return tpl


def test_naming_template_save_and_list(conn):
    saved = db.save_naming_template(conn, _template("Standard"))
    assert saved["name"] == "Standard"
    assert saved["seq_padding"] == 3
    assert saved["no_subfolder"] is False

    listed = db.list_naming_templates(conn)
    assert len(listed) == 1
    assert listed[0]["name"] == "Standard"


def test_naming_template_save_is_upsert_by_name_not_duplicate(conn):
    """`name` is UNIQUE with an ON CONFLICT DO UPDATE -- re-saving a template
    under the same name must overwrite it in place, not create a second row
    (a real-world "edit an existing template" action, not a rename)."""
    db.save_naming_template(conn, _template("Standard", seq_padding=3))
    db.save_naming_template(conn, _template("Standard", seq_padding=5, no_subfolder=True))

    listed = db.list_naming_templates(conn)
    assert len(listed) == 1, f"expected exactly one row after upsert, got {listed}"
    assert listed[0]["seq_padding"] == 5
    assert listed[0]["no_subfolder"] is True


def test_naming_template_delete(conn):
    saved = db.save_naming_template(conn, _template("Standard"))
    db.delete_naming_template(conn, saved["id"])
    assert db.list_naming_templates(conn) == []


def _insert_job(conn, source_path, label, status="complete", dest_paths=(("/dest", 3, 3, 3, 12345),)):
    conn.execute(
        "INSERT INTO jobs (source_card_label, source_path, status, started_at, finished_at) "
        "VALUES (?, ?, ?, '2026-07-14T10:00:00Z', '2026-07-14T10:05:00Z')",
        (label, source_path, status),
    )
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for dest_path, files_total, files_copied, files_verified, bytes_total in dest_paths:
        conn.execute(
            "INSERT INTO job_destinations (job_id, dest_path, files_total, files_copied, "
            "files_verified, bytes_total, status) VALUES (?, ?, ?, ?, ?, ?, 'complete')",
            (job_id, dest_path, files_total, files_copied, files_verified, bytes_total),
        )
    conn.commit()
    return job_id


def test_list_job_summaries_rolls_up_destinations(conn):
    _insert_job(conn, "/Volumes/CARD1", "CARD1",
                dest_paths=(("/dest/a", 3, 3, 3, 1000), ("/dest/b", 3, 3, 3, 1000)))
    summaries = db.list_job_summaries(conn)
    assert len(summaries) == 1
    row = summaries[0]
    assert row["destination_count"] == 2
    assert row["file_count"] == 6
    assert row["bytes_total"] == 2000
    assert row["destination_paths"] == "/dest/a, /dest/b"


def test_list_job_summaries_filters_by_card_id(conn):
    _insert_job(conn, "/Volumes/CARD1", "CARD1")
    _insert_job(conn, "/Volumes/CARD2", "CARD2")
    filtered = db.list_job_summaries(conn, card_id="/Volumes/CARD2")
    assert len(filtered) == 1
    assert filtered[0]["source_card_label"] == "CARD2"

    unfiltered = db.list_job_summaries(conn, card_id=None)
    assert len(unfiltered) == 2


def test_list_job_summaries_newest_first(conn):
    _insert_job(conn, "/Volumes/CARD1", "First")
    _insert_job(conn, "/Volumes/CARD2", "Second")
    summaries = db.list_job_summaries(conn)
    assert [r["source_card_label"] for r in summaries] == ["Second", "First"]


@pytest.mark.parametrize("field,expected", [
    ("plain", "plain"),
    ("has,comma", '"has,comma"'),
    ('has"quote', '"has""quote"'),
    ("has\nnewline", '"has\nnewline"'),
])
def test_csv_escape_field(field, expected):
    assert db.csv_escape_field(field) == expected


def test_job_summaries_to_csv_header_and_row_shape():
    rows = [{
        "id": 1, "source_card_label": "CARD1, Reel A", "started_at": "2026-07-14T10:00:00Z",
        "finished_at": "2026-07-14T10:05:00Z", "status": "complete", "destination_count": 1,
        "destination_paths": "/dest/a", "file_count": 3, "bytes_total": 1000,
    }]
    csv_text = db.job_summaries_to_csv(rows)
    lines = csv_text.strip("\n").split("\n")
    assert lines[0] == "Job ID,Card,Started,Finished,Status,Destinations,Destination Paths,Files,Bytes"
    assert lines[1] == (
        '1,"CARD1, Reel A",2026-07-14T10:00:00Z,2026-07-14T10:05:00Z,'
        "Complete,1,/dest/a,3,1000"
    )
    assert csv_text.endswith("\n")


def test_job_summaries_to_csv_empty_rows_is_header_only():
    csv_text = db.job_summaries_to_csv([])
    assert csv_text == "Job ID,Card,Started,Finished,Status,Destinations,Destination Paths,Files,Bytes\n"
