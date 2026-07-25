"""
cardeater_naming.py — naming/collision engine for the Card Eater workspace.

Python port of Card Eater's own naming.rs — see
CardEater/src-tauri/src/naming.rs for the original (including its test
suite, which is what the scenarios in cardeater tests here mirror). Behavior
is a line-for-line port, not a reinterpretation:

- {YYYYMMDD}/{YYYY}/{Name}/{OriginalName}/{Seq}/{ext} tokens.
- Per-date-group {Seq} sequencing: files are grouped by their *resolved*
  date, and sequence numbers are independent within each group.
- Destination collision scan resumes numbering from the highest EXISTING
  matching sequence number across every distinct literal template in a date
  group (not just the first file's) -- a mixed-extension batch (.mov + .jpg
  sharing one {Seq} counter) must not undercount by only checking one
  extension, or a re-import silently overwrites higher-numbered files of a
  different extension.
- Folder names always use the card-insert date for {YYYYMMDD}, even when
  the per-file date source is `file_metadata` (a single job produces one
  destination folder; per-file dates can legitimately differ across the
  batch, and there's no sane single folder date to derive from that -- see
  naming.rs's resolve_folder_name for the same deliberate carve-out).
- Suite-only addition (no Rust equivalent): a job copying exactly one file,
  with no explicit seq_start and no existing colliding file at the
  destination, drops the {Seq} token entirely rather than manufacturing a
  "_001" suffix nothing needs to disambiguate from -- see
  resolve_file_names's single_file_job handling and _drop_seq_token.
"""

import os
import re
from datetime import datetime, timezone

VALID_FILE_TOKENS = {"YYYYMMDD", "YYYY", "Name", "OriginalName", "Seq", "ext"}
VALID_FOLDER_TOKENS = {"YYYYMMDD", "YYYY", "Name"}

_TOKEN_RE = re.compile(r"\{([^{}]*)\}")


class NamingError(Exception):
    pass


def _extract_tokens(template):
    return _TOKEN_RE.findall(template)


def validate_template(tpl):
    for tok in _extract_tokens(tpl["file_template"]):
        if tok not in VALID_FILE_TOKENS:
            raise NamingError(f"Unknown token {{{tok}}} in file naming template")
    if not tpl["no_subfolder"]:
        for tok in _extract_tokens(tpl["folder_template"]):
            if tok not in VALID_FOLDER_TOKENS:
                raise NamingError(f"Unknown or unsupported token {{{tok}}} in folder naming template")


_ILLEGAL_NAME_CHARS = str.maketrans({c: "_" for c in '/:*?"<>|'})


def sanitize_name(name):
    """Sanitize a user-entered event/shoot name for filesystem use by
    replacing characters illegal (or awkward) on macOS/Windows volumes
    with an underscore. Used for the folder name -- unlike
    sanitize_file_name, spaces are left alone."""
    return name.translate(_ILLEGAL_NAME_CHARS)


def sanitize_file_name(name):
    """Like sanitize_name, but additionally closes up whitespace entirely
    for use in individual file names -- "Game Day" becomes "GameDay", not
    "Game_Day" (substituting an underscore) or "Game Day" (leaving it, fine
    for a folder but awkward inside a bare file name)."""
    return re.sub(r"\s+", "", sanitize_name(name))


def _parse_rfc3339(value):
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    return datetime.fromisoformat(v)


def format_yyyymmdd(rfc3339):
    """Parse an RFC3339 timestamp and format it as YYYYMMDD."""
    try:
        dt = _parse_rfc3339(rfc3339)
    except ValueError as e:
        raise NamingError(f"Invalid date '{rfc3339}': {e}") from e
    return dt.strftime("%Y%m%d")


def _strip_extension(file_name, ext):
    """Strip the trailing .{ext} suffix from a file name, matching only the
    final extension (so multi-dot names like 'clip.final.mov' with ext
    'mov' correctly yield 'clip.final', not 'clip')."""
    if ext and file_name.lower().endswith("." + ext.lower()):
        return file_name[: -(len(ext) + 1)]
    return file_name


def _resolve_file_date(file, date_source, card_insert_date, manual_date):
    """Returns (yyyymmdd, warning_or_none)."""
    if date_source == "card_insert":
        return format_yyyymmdd(card_insert_date), None
    if date_source == "manual":
        if not manual_date:
            raise NamingError("Manual date source selected but no manual_date was provided")
        return format_yyyymmdd(manual_date), None
    if date_source == "file_metadata":
        created_at = file.get("created_at")
        if created_at:
            return format_yyyymmdd(created_at), None
        return (
            format_yyyymmdd(card_insert_date),
            f"{file['name']} had no file metadata date; used card-insert date as fallback",
        )
    raise NamingError(f"Unknown date source: {date_source}")


def _substitute_non_seq_tokens(template, yyyymmdd, sanitized_name, file):
    original_name = _strip_extension(file["name"], file["ext"])
    yyyy = yyyymmdd[0:4]
    return (
        template
        .replace("{YYYYMMDD}", yyyymmdd)
        .replace("{YYYY}", yyyy)
        .replace("{Name}", sanitized_name)
        .replace("{OriginalName}", original_name)
        .replace("{ext}", file["ext"])
    )


def _format_seq(seq, padding):
    padding = max(0, padding)
    return str(seq).zfill(padding)


def _drop_seq_token(literal):
    """Removes a resolved file name's {Seq} placeholder, along with one
    adjacent separator character so `_{Seq}` or `{Seq}_` collapses cleanly
    rather than leaving a stray underscore/dash/dot behind."""
    for sep in ("_", "-", ".", " "):
        if sep + "{Seq}" in literal:
            return literal.replace(sep + "{Seq}", "", 1)
        if "{Seq}" + sep in literal:
            return literal.replace("{Seq}" + sep, "", 1)
    return literal.replace("{Seq}", "", 1)


def _scan_max_existing_seq(dest_path, resolved_template_with_seq_placeholder):
    """Scans dest_path (non-recursive) for existing entries matching the
    resolved template for one date group (with {Seq} as the only remaining
    variable), and returns the highest existing sequence number found."""
    if "{Seq}" not in resolved_template_with_seq_placeholder:
        # No token to extract a number from (use_source_filename, or any
        # other template that omits {Seq}) -- there's no sequence to scan
        # for, and the regex below would have no capture group to match
        # against a same-named existing file.
        return None
    try:
        entries = os.listdir(dest_path)
    except OSError:
        return None  # dest doesn't exist yet / unreadable: no collisions

    escaped = re.escape(resolved_template_with_seq_placeholder)
    pattern = escaped.replace(re.escape("{Seq}"), r"(\d+)")
    regex = re.compile(f"^{pattern}$", re.IGNORECASE)

    max_seq = None
    for name in entries:
        m = regex.match(name)
        if m:
            n = int(m.group(1))
            max_seq = n if max_seq is None else max(max_seq, n)
    return max_seq


def resolve_file_names(files, template, event_name, card_insert_date, manual_date=None, dest_path=None):
    """Resolves final output file names for an entire batch of files,
    applying per-date-group {Seq} sequencing. Returns (names, warnings)."""
    sanitized_name = sanitize_file_name(event_name)
    warnings = []
    single_file_job = len(files) == 1

    # group_key -> ordered list of (original index, literal template with {Seq} placeholder)
    groups = []
    group_index = {}

    for idx, file in enumerate(files):
        yyyymmdd, warning = _resolve_file_date(
            file, template["date_source"], card_insert_date, manual_date)
        if warning:
            warnings.append(warning)
        literal = _substitute_non_seq_tokens(
            template["file_template"], yyyymmdd, sanitized_name, file)

        if yyyymmdd not in group_index:
            group_index[yyyymmdd] = len(groups)
            groups.append((yyyymmdd, []))
        groups[group_index[yyyymmdd]][1].append((idx, literal))

    results = [None] * len(files)

    for _date_key, members in groups:
        seq_start = template.get("seq_start")
        overall_max = None
        if seq_start is not None:
            start = seq_start
        elif dest_path is not None:
            # Members of a group do NOT always share the same literal
            # template: {ext} (and {OriginalName}) vary per file, so a
            # mixed-extension batch sharing one {Seq} counter (the common
            # case) produces a distinct literal per extension. Scan every
            # distinct literal in the group and take the overall max,
            # rather than just the first member's -- otherwise re-imports
            # can collide with and overwrite existing higher-numbered files
            # of a different extension.
            distinct_literals = sorted({literal for _idx, literal in members})
            for literal in distinct_literals:
                m = _scan_max_existing_seq(dest_path, literal)
                if m is not None:
                    overall_max = m if overall_max is None else max(overall_max, m)
            start = (overall_max + 1) if overall_max is not None else 1
        else:
            start = 1

        if (single_file_job and seq_start is None and overall_max is None
                and "{Seq}" in template["file_template"]):
            idx, literal = members[0]
            results[idx] = _drop_seq_token(literal)
            continue

        for offset, (idx, literal) in enumerate(members):
            seq = start + offset
            seq_str = _format_seq(seq, template["seq_padding"])
            results[idx] = literal.replace("{Seq}", seq_str)

    return results, warnings


def resolve_folder_name(template, event_name, card_insert_date, manual_date=None):
    """Resolves the folder name for a job, restricted to the
    {YYYYMMDD}/{Name} tokens supported by folder templates. Always uses the
    card-insert date for {YYYYMMDD}, even under date_source=file_metadata
    (see module docstring)."""
    sanitized_name = sanitize_name(event_name)
    date_source = template["date_source"]
    if date_source == "manual":
        if not manual_date:
            raise NamingError("Manual date source selected but no manual_date was provided")
        yyyymmdd = format_yyyymmdd(manual_date)
    else:
        yyyymmdd = format_yyyymmdd(card_insert_date)

    yyyy = yyyymmdd[0:4]
    return (
        template["folder_template"]
        .replace("{YYYYMMDD}", yyyymmdd)
        .replace("{YYYY}", yyyy)
        .replace("{Name}", sanitized_name)
    )


def preview_names(req):
    """req: {card_insert_date, event_name, manual_date, template, files,
    dest_path}. Returns {folder_name, sample_file_names, warnings}."""
    template = req["template"]
    validate_template(template)

    folder_name = None
    if not template["no_subfolder"]:
        folder_name = resolve_folder_name(
            template, req["event_name"], req["card_insert_date"], req.get("manual_date"))

    all_names, warnings = resolve_file_names(
        req["files"], template, req["event_name"], req["card_insert_date"],
        req.get("manual_date"), req.get("dest_path"))

    return {
        "folder_name": folder_name,
        "sample_file_names": all_names[:3],
        "warnings": warnings,
    }


def check_folder_collision(dest_path, folder_name):
    resolved_path = os.path.join(dest_path, folder_name)
    if not os.path.exists(resolved_path):
        status = "no_conflict"
    elif os.path.isdir(resolved_path):
        try:
            is_empty = len(os.listdir(resolved_path)) == 0
        except OSError:
            is_empty = False
        status = "exists_empty" if is_empty else "exists_non_empty"
    else:
        status = "exists_non_empty"  # a file already occupies this path
    return {"status": status, "resolved_path": resolved_path}
