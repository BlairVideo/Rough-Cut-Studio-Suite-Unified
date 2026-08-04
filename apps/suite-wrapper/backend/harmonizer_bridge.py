"""
harmonizer_bridge.py — in-process bridge to Harmonizer's FCPXML/Resolve
export scripts (make_fcpxml.py, import_to_resolve.py).

Both scripts are pure stdlib (no numpy/scipy/librosa — those are only needed
for align.py's own analysis, which runs out-of-process via
backend/workers/harmonize_worker.py instead), so they run directly inside
the suite venv, mirroring brander_bridge.py's in-process pattern rather than
sync_worker.py's subprocess one. Neither script is modified; their existing
functions are called directly.

sys.path insertion happens at module import time (same rationale as
brander_bridge.py, even though neither script here uses multiprocessing —
kept consistent so the sibling dir is reliably on sys.path before any call
into either module, not re-derived per call).

import_to_resolve's own functions raise SystemExit on failure (no Resolve
running, project not found, import rejected, etc.) rather than returning
error values — every call site below converts that into this bridge's own
{"ok": False, "error": ...} contract instead of letting a BaseException
escape into SuiteApi.
"""

import os
import sys

try:
    from . import paths
except ImportError:  # pragma: no cover — direct script import in tests
    import paths
from rcs_utils import ffprobe_util  # noqa: F401 — import for its PATH-fixup side effect

if paths.HARMONIZER_BACKEND_DIR not in sys.path:
    sys.path.insert(0, paths.HARMONIZER_BACKEND_DIR)

import make_fcpxml       # noqa: E402  (Harmonizer's own FCPXML writer — pure stdlib)
import import_to_resolve  # noqa: E402  (Harmonizer's own Resolve-scripting-API glue)


def _is_braw(path):
    return os.path.splitext(path)[1].lower() == ".braw"


def braw_takes(take_paths):
    """Take paths still in .braw format — export (both paths below) can't
    handle these yet (Harmonizer_App_Plan.md §7's open BRAW-export risk);
    analysis (harmonize_worker.py/align.py) already handles them fine."""
    return [p for p in take_paths if _is_braw(p)]


def export_fcpxml(report, take_paths, output_path, sequence_name=None):
    """Writes the FCPXML file directly to output_path (no reference-audio
    track — see make_fcpxml.py's own build_fcpxml docstring comment; the
    caller is responsible for disclosing that to the user). sequence_name
    is the Resolve project/event/sequence name build_fcpxml gives the
    timeline; omitted (None) falls through to its own timestamped default."""
    take_infos = [make_fcpxml.probe_media(p) for p in take_paths]
    fcpxml_el = make_fcpxml.build_fcpxml(report, take_infos, take_paths, sequence_name=sequence_name)

    import xml.etree.ElementTree as ET
    from xml.dom import minidom

    rough = ET.tostring(fcpxml_el, encoding="unicode")
    pretty = minidom.parseString(rough).toprettyxml(indent="    ")
    pretty = "\n".join(line for line in pretty.split("\n") if line.strip())
    with open(output_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')
        f.write(pretty.split("\n", 1)[1] + "\n")


def send_to_resolve(ref_path, take_paths, report, project_name=None, timeline_name=None):
    """Generates FCPXML to a temp file, imports it directly into Resolve via
    the scripting API, and auto-adds the reference audio track — the same
    flow import_to_resolve.py's own main() performs. Returns
    {"ok": True, "project": str, "timeline": str} on success, or
    {"ok": False, "error": str} for any failure (Resolve not running, no
    project open, import rejected, ...)."""
    fcpxml_path = None
    try:
        fcpxml_path, width, height = import_to_resolve.write_fcpxml(report, take_paths, timeline_name)

        resolve = import_to_resolve.connect_resolve()
        pm = resolve.GetProjectManager()

        if project_name:
            project = pm.CreateProject(project_name)
            if project is None:
                project = pm.LoadProject(project_name)
                if project is None:
                    raise SystemExit(
                        f"Could not create or load Resolve project '{project_name}'")
        else:
            project = pm.GetCurrentProject()
            if project is None:
                raise SystemExit(
                    "No project is currently open in Resolve, and no project name was given")

        import_to_resolve.set_project_resolution(project, width, height)

        media_pool = project.GetMediaPool()
        timeline = media_pool.ImportTimelineFromFile(fcpxml_path)
        if timeline is None:
            raise SystemExit(
                "Resolve rejected the generated FCPXML (ImportTimelineFromFile "
                "returned None) — check Resolve's own import log for details")

        import_to_resolve.apply_timeline_resolution(timeline, width, height)
        import_to_resolve.add_reference_audio(project, timeline, ref_path)

        return {"ok": True, "project": project.GetName(), "timeline": timeline.GetName()}
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    finally:
        if fcpxml_path and os.path.exists(fcpxml_path):
            os.remove(fcpxml_path)
