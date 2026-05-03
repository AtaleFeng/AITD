"""Token-budget monitoring for the trading decision LLM call.

Two responsibilities:
  1. **Pre-call estimate** — before we send a prompt to the model, look at
     its character length to project token usage. If we are about to blow
     the model's context window, refuse the call (saves money + avoids
     finish_reason=length silent-truncation bugs).
  2. **Post-call report** — after the model responds, pull the real
     prompt_tokens from the API usage block and emit a clearly-formatted
     log line at one of four severity levels.

Both functions are pure: they take inputs, return outputs (and optionally
print a structured log line), no IO or global state.

Usage budget tiers (% of model context window):
    <  60%   OK       — quiet log, just a one-liner with the number
    60–85%   WARN     — yellow log, "approaching limit"
    85–95%   DANGER   — red log + a string is appended to warnings list
    >= 95%   BLOCK    — refuse to call; return a sentinel so caller can
                        produce a fallback decision instead of paying for
                        a guaranteed truncated response

Estimation choices:
    * chars/token ≈ 3.0  (conservative — slight over-estimate makes us err
                          toward warning early; real DeepSeek v4-pro is 3.18)
    * If the provider has an explicit contextWindow field, use that.
      Otherwise look up by preset; fall back to 32k as a safe default.
"""

from __future__ import annotations

from typing import Any

# Conservative chars-per-token. 3.0 over-estimates tokens slightly which
# means we trigger warnings a bit early — preferred direction for a budget
# guard. Real measurements from DeepSeek v4-pro on production prompts:
# 3.18 chars/token. cl100k (GPT/Claude) on dense JSON: ~3.5.
_CHARS_PER_TOKEN_FALLBACK = 3.0

# Default context window when we cannot identify the model. 32k is a safe
# floor — most modern API models exceed it but a few legacy ones don't.
_DEFAULT_CONTEXT_WINDOW = 32_000

# Known model context windows. Keys are matched case-insensitively against
# the resolved model identifier (so we accept "DeepSeek-V4-Pro",
# "deepseek-v4-pro", etc.). When in doubt we pick the smaller of vendor's
# claims to avoid surprising people.
_CONTEXT_WINDOW_BY_MODEL_PREFIX: dict[str, int] = {
    # DeepSeek
    "deepseek-v4": 128_000,
    "deepseek-v3": 64_000,
    "deepseek-r1": 64_000,
    "deepseek-reasoner": 64_000,
    "deepseek-chat": 64_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5": 16_385,
    "o1": 200_000,
    "o3": 200_000,
    # Anthropic Claude
    "claude-3": 200_000,
    "claude-sonnet": 200_000,
    "claude-opus": 200_000,
    "claude-haiku": 200_000,
    # Qwen
    "qwen-max": 30_720,
    "qwen-plus": 131_072,
    "qwen-turbo": 1_008_192,
    # Google
    "gemini-2.5": 1_048_576,
    "gemini-1.5": 1_048_576,
}

# Tier thresholds expressed as fractions of the context window.
_TIER_WARN = 0.60
_TIER_DANGER = 0.85
_TIER_BLOCK = 0.95


def estimate_tokens(text: str | None) -> int:
    """Approximate the token count of *text* without invoking a tokenizer.

    Uses a fixed chars-per-token ratio. Real cost can deviate by ±15%
    depending on language / symbol density, but for budget alarms an
    approximation is sufficient — the post-call log overrides this with
    the exact number from API usage.
    """
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN_FALLBACK) + 1


def context_window_for(provider: dict[str, Any] | None) -> int:
    """Resolve the context window (in tokens) for the configured model.

    Priority:
      1. Explicit `contextWindow` field on the provider config.
      2. Longest matching prefix in _CONTEXT_WINDOW_BY_MODEL_PREFIX.
      3. _DEFAULT_CONTEXT_WINDOW.
    """
    if not provider:
        return _DEFAULT_CONTEXT_WINDOW
    explicit = provider.get("contextWindow")
    if isinstance(explicit, (int, float)) and explicit > 0:
        return int(explicit)
    model = str(provider.get("model") or "").strip().lower()
    if not model:
        return _DEFAULT_CONTEXT_WINDOW
    # Pick the longest matching prefix so "deepseek-v4-pro" matches
    # "deepseek-v4" (128k) and not "deepseek" alone.
    best_prefix = ""
    best_window = _DEFAULT_CONTEXT_WINDOW
    for prefix, window in _CONTEXT_WINDOW_BY_MODEL_PREFIX.items():
        if model.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best_window = window
    return best_window


def evaluate_token_usage(prompt_tokens: int, context_window: int) -> dict[str, Any]:
    """Classify a prompt-token count against its model's context window.

    Returns a dict with:
      level:   "OK" | "WARN" | "DANGER" | "BLOCK"
      pct:     fraction used, 0.0–1.0+
      should_block:    True iff caller must skip the API call
      warning_text:    string appropriate for cycle.warnings, or None
      log_line:        single-line string ready to print

    Designed so callers don't need to know the threshold numbers.
    """
    if context_window <= 0:
        context_window = _DEFAULT_CONTEXT_WINDOW
    pct = prompt_tokens / context_window
    if pct >= _TIER_BLOCK:
        level = "BLOCK"
        should_block = True
        warning_text = (
            f"context window usage {pct:.0%} >= {_TIER_BLOCK:.0%}, "
            f"refusing model call to avoid silent truncation; "
            f"reduce candidate universe or kline lookback"
        )
    elif pct >= _TIER_DANGER:
        level = "DANGER"
        should_block = False
        warning_text = (
            f"context window usage {pct:.0%} >= {_TIER_DANGER:.0%}, "
            f"approaching hard limit; consider reducing candidate universe "
            f"or kline lookback before next cycle"
        )
    elif pct >= _TIER_WARN:
        level = "WARN"
        should_block = False
        warning_text = None
    else:
        level = "OK"
        should_block = False
        warning_text = None
    log_line = format_log_line(prompt_tokens, context_window, level, pct)
    return {
        "level": level,
        "pct": pct,
        "should_block": should_block,
        "warning_text": warning_text,
        "log_line": log_line,
    }


def format_log_line(
    prompt_tokens: int,
    context_window: int,
    level: str,
    pct: float,
    *,
    cycle_id: str | None = None,
    extra: str | None = None,
) -> str:
    """Single-line log string. Stable format for grep / log viewers."""
    cycle_part = f" cycle={cycle_id}" if cycle_id else ""
    extra_part = f" | {extra}" if extra else ""
    window_h = _human_window(context_window)
    return (
        f"[tokens]{cycle_part} prompt={prompt_tokens} "
        f"window={window_h} usage={pct:.0%} [{level}]{extra_part}"
    )


def _human_window(window: int) -> str:
    """Render context window in compact form: 128000 → '128k'."""
    if window >= 1_000_000:
        return f"{window // 1_000_000}M"
    if window >= 1_000:
        return f"{window // 1_000}k"
    return str(window)
