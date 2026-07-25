"""
gemini_client.py

Talks to the Gemini API's generateContent endpoint (the free-tier Gemini
Developer API -- https://ai.google.dev) over plain HTTPS. No SDKs with broad
filesystem/network permissions are used, just the `requests` library making
one POST call.

Security notes:
  * The API key is read from an environment variable / in-memory value that
    the user supplies in the app itself. It is never written to disk by
    this module and never logged.
  * We pin `responseMimeType` to application/json and pass a strict
    `responseSchema`, which makes Gemini return only structured data -- no
    free-form prose to sanitize or parse with fragile regex.
  * The model is instructed to only ever choose from the segment indices it
    is given; it does not invent its own timecodes. The backend (api.py)
    re-validates every reference against the real parsed transcript before
    any XML is built, so a malformed or out-of-range model response can
    never produce a bad edit.
"""

import json
import time
import random
import requests

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# "gemini-flash-latest" is Google's auto-updated alias for their current
# recommended Flash model, so this default doesn't go stale as Google ships
# new model generations. See https://ai.google.dev/gemini-api/docs/models
# for the full current list if you want to pin a specific version instead.
DEFAULT_MODEL = "gemini-flash-latest"

# HTTP statuses worth retrying: 503 (model overloaded/unavailable, very common
# on the free tier during peak hours) and 429 (rate limited). Anything else
# (400 bad request, 401/403 bad key, etc.) is not transient and fails fast.
RETRYABLE_STATUSES = {429, 503}
MAX_ATTEMPTS = 4
BASE_DELAY_SECONDS = 2.0

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "sequence_name": {"type": "STRING"},
        "narrative_summary": {"type": "STRING"},
        "script_segments": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "order": {"type": "INTEGER"},
                    "source_id": {"type": "STRING"},
                    "segment_index": {"type": "INTEGER"},
                    "in_offset_seconds": {"type": "NUMBER"},
                    "out_offset_seconds": {"type": "NUMBER"},
                    "editorial_note": {"type": "STRING"},
                    "on_screen_text": {"type": "STRING"},
                },
                "required": [
                    "order",
                    "source_id",
                    "segment_index",
                    "in_offset_seconds",
                    "out_offset_seconds",
                    "editorial_note",
                ],
            },
        },
    },
    "required": ["sequence_name", "narrative_summary", "script_segments"],
}

SYSTEM_INSTRUCTION = """You are an assistant video editor. You are given:
  1. A creative brief / prompt describing the video the editor wants to cut.
  2. One or more timecoded transcripts, each already split into indexed
     segments with known start/end times.

Your job is to choose which transcript segments to use, in what order, to
build the requested video, and to write a short editorial note for each cut.

Rules you must follow:
  - Only reference `source_id` values and `segment_index` values that were
    given to you. Never invent a segment that doesn't exist.
  - `in_offset_seconds` / `out_offset_seconds` trim seconds off the START and
    END of the chosen segment respectively (both default to 0, meaning use
    the full segment). Use small trims only when it clearly improves the cut
    (e.g. removing a false start or trailing silence). Never let the trimmed
    segment become shorter than 0.3 seconds.
  - Keep the narrative coherent and true to the brief. Do not fabricate
    quotes or dialogue that is not present in the transcript text.
  - `editorial_note` is a short (<20 word) human-readable instruction for the
    editor, e.g. "Cold open on Jane's line about March start date."
  - Order segments with the `order` field starting at 0.
  - If a target runtime is given, treat it as a real constraint: choose
    enough segments to approach it and trim segments as needed to avoid
    overshooting by a large margin, rather than padding with filler or
    cutting the story short to hit the number exactly. Getting close
    matters more than hitting it precisely.
  - Respond ONLY with data matching the provided JSON schema."""


class GeminiError(Exception):
    pass


def generate_script(
    api_key: str,
    prompt: str,
    sources: list,
    model: str = DEFAULT_MODEL,
    timeout: int = 90,
    on_retry=None,
    target_seconds=None,
) -> dict:
    """
    sources: list of {"source_id": str, "segments": [ {index, start_tc, end_tc,
             speaker, text}, ... ] }

    on_retry: optional callback(attempt, max_attempts, wait_seconds, reason)
              invoked before each retry, so the caller can update UI status.

    target_seconds: optional target runtime in seconds. When given, it's
              passed to the model as guidance (see SYSTEM_INSTRUCTION) —
              it's not enforced here; the caller compares the actual
              resolved runtime against it afterward and can prompt for
              another pass if it's off.

    Returns the parsed JSON dict matching RESPONSE_SCHEMA.

    Raises GeminiError for anything non-transient, or if the model is still
    overloaded/rate-limited after MAX_ATTEMPTS tries.
    """
    if not api_key or not api_key.strip():
        raise GeminiError("No Gemini API key was provided.")

    transcript_blob = _format_sources_for_prompt(sources)

    target_line = ""
    if target_seconds:
        target_line = f"TARGET RUNTIME: approximately {target_seconds:.0f} seconds total.\n\n"

    user_content = (
        f"CREATIVE BRIEF:\n{prompt.strip()}\n\n"
        f"{target_line}"
        f"AVAILABLE TRANSCRIPT SEGMENTS:\n{transcript_blob}"
    )

    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": user_content}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0.4,
        },
    }

    url = GEMINI_ENDPOINT.format(model=model)

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                data=json.dumps(body),
                timeout=timeout,
            )
        except requests.RequestException as e:
            # Network hiccups are also worth a retry. The key is sent as a
            # header (never a URL query param), but scrub defensively anyway
            # in case a future requests/urllib3 version embeds header values
            # or a proxy URL containing credentials in its exception text.
            last_error = GeminiError(f"Network error calling Gemini API: {_scrub_secret(e, api_key)}")
            if attempt < MAX_ATTEMPTS:
                _wait_before_retry(attempt, on_retry, reason="network error")
                continue
            raise last_error from e

        if resp.status_code in RETRYABLE_STATUSES:
            last_error = GeminiError(_overload_message(resp))
            if attempt < MAX_ATTEMPTS:
                _wait_before_retry(attempt, on_retry, reason=f"HTTP {resp.status_code}")
                continue
            raise last_error

        if resp.status_code != 200:
            # Not transient (bad request, bad key, etc.) — fail immediately.
            raise GeminiError(f"Gemini API returned HTTP {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        try:
            candidates = data["candidates"]
            parts = candidates[0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError) as e:
            raise GeminiError(f"Unexpected Gemini response shape: {data}") from e

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise GeminiError(f"Gemini did not return valid JSON: {e}\nRaw: {text[:500]}") from e

        return parsed

    # Should be unreachable, but keep a safety net.
    raise last_error or GeminiError("Gemini API request failed for an unknown reason.")


def _scrub_secret(err: Exception, secret: str) -> str:
    """Return str(err) with any occurrence of the raw API key removed, so a
    key can never end up in a wrapped error message, a log, or a traceback
    printed to the console."""
    text = str(err)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return text


def _overload_message(resp) -> str:
    if resp.status_code == 503:
        return (
            "Gemini's servers are temporarily overloaded (HTTP 503). "
            f"Retried {MAX_ATTEMPTS} times with backoff and it's still busy — "
            "this is on Google's end, not the app. Wait a minute and click "
            "Generate Script again, or try a different model."
        )
    return (
        "Gemini rate-limited this request (HTTP 429). "
        f"Retried {MAX_ATTEMPTS} times — if this keeps happening, you may be "
        "hitting the free tier's requests-per-minute limit. Wait a bit and retry."
    )


def _wait_before_retry(attempt: int, on_retry, reason: str):
    # Exponential backoff with jitter: ~2s, ~4s, ~8s.
    wait_seconds = BASE_DELAY_SECONDS * (2 ** (attempt - 1))
    wait_seconds += random.uniform(0, 0.5)
    if on_retry:
        try:
            on_retry(attempt, MAX_ATTEMPTS, wait_seconds, reason)
        except Exception:
            pass  # never let a UI callback break the retry loop
    time.sleep(wait_seconds)


def _format_sources_for_prompt(sources: list) -> str:
    lines = []
    for src in sources:
        lines.append(f"### source_id: {src['source_id']}")
        for seg in src["segments"]:
            speaker = f"{seg['speaker']}: " if seg.get("speaker") else ""
            lines.append(
                f"[{seg['index']}] {seg['start_tc']} - {seg['end_tc']}  {speaker}{seg['text']}"
            )
        lines.append("")
    return "\n".join(lines)
