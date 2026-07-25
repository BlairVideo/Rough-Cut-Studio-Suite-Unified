"""
script_writer.py

Turns the resolved (validated) segment list into a human-readable script
document — the kind an editor or producer can read top to bottom — as
Markdown text. This is generated locally from data already validated
against the real transcripts; no network calls happen here.
"""

from datetime import datetime


def build_script_markdown(
    sequence_name: str,
    narrative_summary: str,
    resolved_segments: list,
    fps: float,
    broll_segments: list = None,
    target_seconds=None,
) -> str:
    resolved_segments = sorted(resolved_segments, key=lambda s: s["order"])
    broll_segments = broll_segments or []
    lines = []
    lines.append(f"# {sequence_name}")
    lines.append("")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} \u00b7 {fps} fps_")
    lines.append("")
    if narrative_summary:
        lines.append("## Narrative Summary")
        lines.append("")
        lines.append(narrative_summary.strip())
        lines.append("")
    lines.append("## Cut List (V1, Main)")
    lines.append("")
    lines.append("| # | Source | In | Out | Editorial Note | On-Screen Text |")
    lines.append("|---|--------|----|----|-----------------|----------------|")
    for seg in resolved_segments:
        onscreen = seg.get("on_screen_text") or ""
        lines.append(
            f"| {seg['order'] + 1} | {seg['source_name']} | {seg['in_tc']} | {seg['out_tc']} "
            f"| {seg.get('note', '')} | {onscreen} |"
        )
    lines.append("")

    if broll_segments:
        lines.append("## B-Roll Overlays (V2, silent)")
        lines.append("")
        lines.append("| Timeline Start | Source | In | Out | Note |")
        lines.append("|----------------|--------|----|----|------|")
        for seg in sorted(broll_segments, key=lambda s: s.get("timeline_start_seconds", 0)):
            lines.append(
                f"| {seg.get('timeline_start_tc', '00:00:00:00')} | {seg['source_name']} "
                f"| {seg['in_tc']} | {seg['out_tc']} | {seg.get('note', '')} |"
            )
        lines.append("")

    lines.append("## Shot-by-Shot Notes")
    lines.append("")
    for seg in resolved_segments:
        lines.append(f"**{seg['order'] + 1}. {seg['source_name']}** ({seg['in_tc']} \u2192 {seg['out_tc']})")
        lines.append("")
        if seg.get("note"):
            lines.append(f"- {seg['note']}")
        if seg.get("on_screen_text"):
            lines.append(f"- On-screen text: \u201c{seg['on_screen_text']}\u201d")
        if seg.get("source_text"):
            quoted = seg["source_text"].strip()
            lines.append(f"- Transcript: \u201c{quoted}\u201d")
        lines.append("")

    total_seconds = sum(s["out_seconds"] - s["in_seconds"] for s in resolved_segments)
    m, s = divmod(int(total_seconds), 60)
    runtime_line = f"**Estimated runtime:** {m}m {s:02d}s across {len(resolved_segments)} cuts"
    if target_seconds:
        tm, ts = divmod(int(target_seconds), 60)
        diff = total_seconds - target_seconds
        diff_label = f"{'+' if diff >= 0 else ''}{int(diff)}s vs target"
        runtime_line += f" (target {tm}m {ts:02d}s, {diff_label})"
    runtime_line += "."
    lines.append(runtime_line)
    lines.append("")
    return "\n".join(lines)
