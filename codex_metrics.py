#!/usr/bin/env python3
"""Live model metrics for the usage overlay, parsed from Codex session logs.

Contract:
  session_metrics() parses the tail of the few newest rollout files under
  ~/.codex/sessions/ (shared by the Codex desktop app and CLI) and returns
  metrics from the one with the youngest model response, or None:

    {
      "model": str,                 # from the last turn_context line
      "cache_hit": float | None,    # cached_input / input, session cumulative
      "in_tok": int, "cached_tok": int, "out_tok": int,      # session totals
      "last_rate": float | None,    # output tokens / second of the last
      "last_out_tok": int, ...      #   model response (generation window)
      "last_dur_s": float, "age_s": float,   # age of the last response
    }

  Rate follows the community pattern (claude-monitor / ccmonitor): pair the
  timestamps of the boundary event that handed control to the model (user
  message, task start, tool-call output) with the token_count that closes the
  response. Generation window therefore includes reasoning, which is part of
  the output the user waits for.

All parsing is local and structural: token counts, timestamps, model ids.
Message content is never read into the returned data.
"""

import glob
import json
import os
import time
from datetime import datetime

SESSIONS_GLOB = os.path.join(os.path.expanduser("~"), ".codex", "sessions", "**", "*.jsonl")
TAIL_BYTES = 512_000          # totals live in the last token_count; tail is enough
MAX_FILE_BYTES = 8_000_000    # beyond this, only the tail is parsed
MIN_RATE_WINDOW_S = 0.5       # shorter windows are noise (parallel responses)

# events that hand control to the model: the next response starts here
_BOUNDARY_SUBTYPES = {
    "user_message", "task_started", "custom_tool_call_output",
    "function_call_output", "local_shell_call_output", "web_search_call",
    "compacted",
}


def _ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def session_metrics() -> dict | None:
    """Metrics from the rollout file with the most recent model response.

    The newest-mtime file may be an old thread merely re-opened (settings
    events today, no fresh responses), so parse the few newest files and keep
    the one whose last response is the youngest."""
    files = sorted(glob.glob(SESSIONS_GLOB, recursive=True), key=os.path.getmtime, reverse=True)
    best = None
    best_end = None
    for path in files[:3]:
        m = _parse_rollout(path)
        if m is None:
            continue
        end = time.time() - (m.get("age_s") or 1e12)
        if best is None or end > best_end:
            best, best_end = m, end
    return best


def _parse_rollout(path: str) -> dict | None:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > MAX_FILE_BYTES:
                f.seek(-TAIL_BYTES, os.SEEK_END)
                f.readline()  # drop the partial line at the window head
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None

    model = None
    total = None                  # cumulative usage from the last token_count
    boundary = None               # when the model was handed the prompt
    last = None                   # (usage, dur_s, end_dt) of the last response

    for line in lines:
        try:
            j = json.loads(line)
        except ValueError:
            continue
        p = j.get("payload") or {}
        sub = p.get("type")
        line_dt = _ts(j.get("timestamp"))

        if j.get("type") == "turn_context" and p.get("model"):
            model = p["model"]
        elif sub in _BOUNDARY_SUBTYPES or (
                j.get("type") == "response_item" and sub == "message"
                and p.get("role") == "user"):
            if line_dt:
                boundary = line_dt
        elif sub == "token_count":
            info = p.get("info") or {}
            total = info.get("total_token_usage") or total
            usage = info.get("last_token_usage") or {}
            if line_dt and boundary is not None:
                dur = (line_dt - boundary).total_seconds()
                if dur >= MIN_RATE_WINDOW_S:
                    last = (usage, dur, line_dt)
            if line_dt:
                boundary = line_dt   # next response, if any, starts after this

    if total is None:
        return None
    in_tok = total.get("input_tokens") or 0
    cached = total.get("cached_input_tokens") or 0
    out_tok = total.get("output_tokens") or 0
    usage, dur, end_dt = last or ({}, None, None)
    now = datetime.now(end_dt.tzinfo) if end_dt else None
    return {
        "model": model,
        "cache_hit": (cached / in_tok) if in_tok else None,
        "in_tok": in_tok, "cached_tok": cached, "out_tok": out_tok,
        "last_rate": (usage.get("output_tokens") / dur) if last and usage.get("output_tokens") else None,
        "last_out_tok": usage.get("output_tokens"),
        "last_dur_s": dur,
        "age_s": (now - end_dt).total_seconds() if now and end_dt else None,
        "file_age_s": time.time() - os.path.getmtime(path),
    }
