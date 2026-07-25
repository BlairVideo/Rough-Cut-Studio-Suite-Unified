"""
llama_client.py

Talks to a locally-running Ollama server (https://ollama.com) as an
alternative to the Gemini API — for editors who'd rather not send transcript
content to a cloud service at all, or who don't have a Gemini key handy.
Ollama itself downloads and runs the model (e.g. Llama 3.1) entirely on the
local machine; this module just makes plain HTTP calls to Ollama's REST API
on 127.0.0.1 (or another host the person explicitly configures), the same
`requests` library already used for the Gemini call.

Security notes:
  * No API key, no cloud call — Ollama's default install only listens on
    localhost, so unless the person has deliberately reconfigured Ollama to
    listen elsewhere and pointed this app at that address, nothing leaves
    the machine.
  * `format` is set to a real JSON Schema, built per-request by
    `_build_response_schema` (below), not just the string `"json"`. Since
    Ollama 0.5, passing a schema object there constrains decoding
    token-by-token to match it (via a grammar derived from the schema),
    the same category of guarantee as Gemini's `responseSchema` — not
    just "please return JSON" advice in the prompt. A first attempt at
    this app only used `"format": "json"` (valid JSON, but no shape
    guarantee) and a local model happily returned valid JSON that was
    shaped nothing like what was asked for. The schema also enums
    `source_id` to the real source IDs for the current call, since a
    local model has separately been observed emitting a plausible-looking
    but wrong value there (a speaker's name, a quoted line) even once the
    shape itself was correct. This module still
    validates the parsed result defensively on top of that, and api.py's
    `_resolve_segments` re-validation (real transcript segments only, no
    invented timecodes) applies identically regardless of which provider
    produced the response — schema-constrained decoding narrows how a
    local model can go wrong, it doesn't replace re-validation.
"""

import json
import time
import requests

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1"

# Local inference on ordinary hardware (especially CPU-only or a first-run
# model load) can be far slower than a cloud call — a generous timeout and
# a couple of retries avoid failing a request that was simply still thinking.
RETRYABLE_EXCEPTIONS = (requests.exceptions.Timeout, requests.exceptions.ConnectionError)
MAX_ATTEMPTS = 2
BASE_DELAY_SECONDS = 2.0

# Standard JSON Schema (not Gemini's OBJECT/STRING-enum dialect) — this is
# what Ollama's structured-outputs feature expects in the `format` field.
# `additionalProperties: false` at every object level rules out a model
# wrapping or renaming the response (e.g. a stray top-level "transcript"
# key) rather than just discouraging it. `_build_response_schema` below
# goes further and enums `source_id` to the real source IDs for this call —
# a local model has been observed putting a speaker's name or a quoted
# line into `source_id` instead (plausible-looking JSON, wrong content);
# an enum makes that specific value structurally impossible to emit rather
# than something we can only catch after the fact. `minItems: 1` on
# script_segments similarly rules out a technically-valid but useless
# empty response.
def _build_response_schema(source_ids: list) -> dict:
    source_id_schema = {"type": "string", "enum": list(source_ids)} if source_ids else {"type": "string"}
    return {
        "type": "object",
        "properties": {
            "sequence_name": {"type": "string"},
            "narrative_summary": {"type": "string"},
            "script_segments": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "order": {"type": "integer"},
                        "source_id": source_id_schema,
                        "segment_index": {"type": "integer"},
                        "in_offset_seconds": {"type": "number"},
                        "out_offset_seconds": {"type": "number"},
                        "editorial_note": {"type": "string"},
                        "on_screen_text": {"type": "string"},
                    },
                    "required": [
                        "order",
                        "source_id",
                        "segment_index",
                        "in_offset_seconds",
                        "out_offset_seconds",
                        "editorial_note",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["sequence_name", "narrative_summary", "script_segments"],
        "additionalProperties": False,
    }


# Ollama defaults to a fairly small context window (often 2048-4096 tokens,
# depending on the model's own Modelfile) unless a request explicitly asks
# for more via `options.num_ctx` — and it truncates silently rather than
# erroring, which is exactly the shape of bug this was: a multi-transcript
# prompt got cut off partway through, so the model only ever saw (and could
# only ever reference) the first transcript. This estimates how much
# context the actual prompt needs and asks for that, rather than trusting
# whatever the model's default happens to be.
NUM_CTX_STEPS = (4096, 8192, 16384, 32768, 65536, 131072)
CHARS_PER_TOKEN_ESTIMATE = 3.3  # conservative for English prose; real tokenizers vary by model
OUTPUT_HEADROOM_TOKENS = 2048  # room for the model's own script_segments response

# A context window this large isn't just slow on typical consumer hardware —
# the KV cache it requires (which scales with context length, independent
# of the model's own weight size) can easily exceed what a laptop's unified
# memory can hold at all, on top of the model weights themselves. Rather
# than silently requesting something that will thrash into swap or hang
# for many minutes only to fail anyway, generate_script fails fast with a
# clear explanation once the *actual* need exceeds this ceiling. This is a
# deliberately conservative default for a broad range of hardware, not a
# hard technical limit — someone running this on a high-memory Mac Studio
# who knows what they're doing can raise it here.
MAX_PRACTICAL_NUM_CTX = 32768


def _estimate_num_ctx(system_text: str, user_text: str) -> int:
    """Returns (chosen_num_ctx, actually_needed_tokens). chosen_num_ctx is
    the smallest standard step that fits the estimated need; the caller is
    responsible for deciding what to do if that need exceeds what's
    practical to run (see MAX_PRACTICAL_NUM_CTX)."""
    est_input_tokens = (len(system_text) + len(user_text)) / CHARS_PER_TOKEN_ESTIMATE
    needed = est_input_tokens + OUTPUT_HEADROOM_TOKENS
    for step in NUM_CTX_STEPS:
        if needed <= step:
            return step, needed
    return NUM_CTX_STEPS[-1], needed


SYSTEM_INSTRUCTION = """You are an assistant video editor. You are given:
  1. A creative brief / prompt describing the video the editor wants to cut.
  2. One or more timecoded transcripts, each already split into indexed
     segments with known start/end times.

Your job is to choose which transcript segments to use, in what order, to
build the requested video, and to write a short editorial note for each cut.
Respond with your choices as script_segments — do not repeat or restate the
transcript itself anywhere in your response.

Rules you must follow:
  - `source_id` identifies which TRANSCRIPT a segment came from, not who is
    speaking and not a quote from the segment. Every segment line you are
    given is already labeled with its exact source_id — copy that value
    character-for-character. Never put a speaker's name, a line of
    dialogue, or anything else into `source_id`.
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
  - You are usually given more than one transcript (one per source_id).
    Use segments from every transcript that's actually relevant to the
    brief — do not limit yourself to only the first transcript you see
    unless the brief specifically calls for only one source.
  - You must choose at least one segment. Never return an empty
    script_segments list, even if the brief is vague — pick the segments
    that best fit your own best interpretation of it.
  - Order segments with the `order` field starting at 0.
  - If a target runtime is given, treat it as a real constraint: choose
    enough segments to approach it and trim segments as needed to avoid
    overshooting by a large margin, rather than padding with filler or
    cutting the story short to hit the number exactly. Getting close
    matters more than hitting it precisely."""


class LlamaError(Exception):
    pass


# Ollama's own default is to unload a model ~5 minutes after its last use.
# During an editing session it's completely normal to spend longer than
# that reading a generated script or reviewing the Cuts tab before clicking
# Revise — without an explicit keep_alive, that next call pays the full
# cost of reloading the model from disk before it can even start on the
# actual prompt, on top of whatever the context size costs. 30 minutes
# comfortably covers a real editing gap without pinning the model in
# memory indefinitely if the person switches away entirely.
KEEP_ALIVE = "30m"


def generate_script(
    prompt: str,
    sources: list,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    timeout: int = 300,
    on_retry=None,
    on_start=None,
    target_seconds=None,
) -> dict:
    """
    Same contract as gemini_client.generate_script (minus the API key):
    sources is a list of {"source_id": str, "segments": [ {index, start_tc,
    end_tc, speaker, text}, ... ] }. Returns a parsed dict matching the
    schema built by _build_response_schema, constrained to this call's
    actual source_id values.

    on_start: optional callback(info: dict) invoked once, right before the
    request is sent, with {"model": ..., "num_ctx": ...} — lets the caller
    surface what context size was actually chosen, since that's otherwise
    invisible and is the main lever behind how slow a call will be.

    Raises LlamaError if Ollama can't be reached, the named model isn't
    available, or the response isn't parseable JSON.
    """
    host = (host or DEFAULT_HOST).rstrip("/")
    model = (model or DEFAULT_MODEL).strip()
    if not model:
        raise LlamaError("No Llama model was specified.")

    transcript_blob = _format_sources_for_prompt(sources)
    source_ids = [src["source_id"] for src in sources if src.get("source_id")]

    target_line = ""
    if target_seconds:
        target_line = f"TARGET RUNTIME: approximately {target_seconds:.0f} seconds total.\n\n"

    user_content = (
        f"CREATIVE BRIEF:\n{prompt.strip()}\n\n"
        f"{target_line}"
        f"AVAILABLE TRANSCRIPT SEGMENTS:\n{transcript_blob}"
    )

    num_ctx, needed_tokens = _estimate_num_ctx(SYSTEM_INSTRUCTION, user_content)
    if needed_tokens > MAX_PRACTICAL_NUM_CTX:
        raise LlamaError(
            f"This transcript set needs roughly {int(needed_tokens):,} tokens of context to "
            f"process without truncating — well beyond the {MAX_PRACTICAL_NUM_CTX:,}-token limit "
            f"this app uses for local models, because a context window that large can easily "
            f"exceed a laptop's available memory on its own, before the model's own weights are "
            f"even counted, and can hang for a very long time rather than simply running slowly. "
            f"For a transcript this size, the practical options are: use the Gemini provider for "
            f"this generation (it has no such local memory ceiling), or reduce how much transcript "
            f"is loaded at once (fewer or shorter sources). If you're running this on a "
            f"high-memory machine and know it can handle more, raise MAX_PRACTICAL_NUM_CTX in "
            f"backend/llama_client.py."
        )
    if on_start:
        try:
            on_start({"model": model, "num_ctx": num_ctx})
        except Exception:
            pass  # a UI push failing should never block the actual request

    # A bigger context window costs proportionally more compute per call —
    # scale the timeout so a large-but-legitimate transcript set doesn't
    # just fail on a clock instead of failing (or succeeding) correctly.
    # Capped so a stuck/hung local server still fails eventually rather
    # than blocking the app indefinitely.
    effective_timeout = min(1800, max(timeout, int(timeout * (num_ctx / NUM_CTX_STEPS[0]))))

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": user_content},
        ],
        "format": _build_response_schema(source_ids),
        "stream": False,
        "options": {"temperature": 0.4, "num_ctx": num_ctx},
        "keep_alive": KEEP_ALIVE,
    }

    url = f"{host}/api/chat"

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(url, headers={"Content-Type": "application/json"},
                                  data=json.dumps(body), timeout=effective_timeout)
        except RETRYABLE_EXCEPTIONS as e:
            last_error = LlamaError(_connection_error_message(host, e))
            if attempt < MAX_ATTEMPTS:
                _wait_before_retry(attempt, on_retry, reason="connection issue")
                continue
            raise last_error from e
        except requests.RequestException as e:
            raise LlamaError(f"Network error calling Ollama at {host}: {e}") from e

        if resp.status_code == 404:
            raise LlamaError(
                f"Ollama returned 404 for model '{model}'. Pull it first from a terminal "
                f"with `ollama pull {model}`, or pick a model you've already pulled."
            )
        if resp.status_code == 400 and "format" in resp.text.lower():
            # Older Ollama versions (pre-0.5) don't understand a schema object
            # in `format`, only the string "json" — fall back for compatibility
            # rather than hard-failing on an otherwise-working install. `body`
            # is mutated in place so later retry attempts (if any) skip this
            # branch entirely and go straight to json-mode.
            body["format"] = "json"
            try:
                resp = requests.post(url, headers={"Content-Type": "application/json"},
                                      data=json.dumps(body), timeout=effective_timeout)
            except RETRYABLE_EXCEPTIONS as e:
                # This fallback call is just as susceptible to a transient
                # timeout/connection hiccup as the initial request — give it
                # the same retry-with-backoff treatment via the outer loop
                # instead of failing immediately.
                last_error = LlamaError(_connection_error_message(host, e))
                if attempt < MAX_ATTEMPTS:
                    _wait_before_retry(attempt, on_retry, reason="connection issue (format fallback)")
                    continue
                raise last_error from e
            except requests.RequestException as e:
                raise LlamaError(f"Network error calling Ollama at {host}: {e}") from e
        if resp.status_code != 200:
            raise LlamaError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:500]}")

        try:
            data = resp.json()
            text = data["message"]["content"]
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            raise LlamaError(f"Unexpected response shape from Ollama: {resp.text[:500]}") from e

        try:
            parsed = _parse_json_response(text)
        except json.JSONDecodeError as e:
            raise LlamaError(f"Llama did not return valid JSON: {e}\nRaw: {text[:500]}") from e

        return parsed

    raise last_error or LlamaError("Request to Ollama failed for an unknown reason.")


def list_models(host: str = DEFAULT_HOST) -> list:
    """Returns the names of models already pulled into this Ollama install
    (via `ollama pull`), so the UI can offer a real, working list instead of
    a guessed one. Raises LlamaError if Ollama can't be reached."""
    host = (host or DEFAULT_HOST).rstrip("/")
    try:
        resp = requests.get(f"{host}/api/tags", timeout=10)
    except RETRYABLE_EXCEPTIONS as e:
        raise LlamaError(_connection_error_message(host, e)) from e
    except requests.RequestException as e:
        raise LlamaError(f"Network error calling Ollama at {host}: {e}") from e

    if resp.status_code != 200:
        raise LlamaError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:500]}")

    try:
        data = resp.json()
        return [m["name"] for m in data.get("models", []) if m.get("name")]
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        raise LlamaError(f"Unexpected response shape from Ollama: {resp.text[:500]}") from e


def _parse_json_response(text: str):
    # Ollama's JSON mode is generally clean, but strip markdown code fences
    # defensively in case a model wraps its output anyway.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Smaller local models occasionally add a stray sentence before or
        # after the JSON object even when asked for JSON only. Fall back to
        # extracting the outermost {...} span rather than giving up outright.
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def _connection_error_message(host: str, e: Exception) -> str:
    return (
        f"Couldn't reach Ollama at {host}. Make sure Ollama is installed and running "
        f"(run `ollama serve` in a terminal, or open the Ollama app), and that the model "
        f"you selected has been pulled (`ollama pull llama3.1`). ({e.__class__.__name__})"
    )


def _wait_before_retry(attempt: int, on_retry, reason: str):
    wait_seconds = BASE_DELAY_SECONDS * attempt
    if on_retry:
        try:
            on_retry(attempt, MAX_ATTEMPTS, wait_seconds, reason)
        except Exception:
            pass
    time.sleep(wait_seconds)


def _format_sources_for_prompt(sources: list) -> str:
    source_ids = [src["source_id"] for src in sources if src.get("source_id")]
    lines = [f"Valid source_id values, exactly as spelled here: {', '.join(source_ids)}", ""]
    for src in sources:
        source_id = src["source_id"]
        lines.append(f"### source_id: {source_id}")
        for seg in src["segments"]:
            speaker = f"{seg['speaker']}: " if seg.get("speaker") else ""
            lines.append(
                f"[{seg['index']}] (source_id: {source_id}) {seg['start_tc']} - {seg['end_tc']}  "
                f"{speaker}{seg['text']}"
            )
        lines.append("")
    return "\n".join(lines)
