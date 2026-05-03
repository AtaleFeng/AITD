"""Prompt-time data compaction helpers.

This module owns conversions that shrink the bytes we send to the LLM
without changing semantics. Currently it only handles K-line series, but
future helpers (orderbook digests, position summaries, etc.) belong here
too so that engine.py can stay focused on the trading loop itself.

The functions here are deliberately:
  * pure (no IO, no global state, no logging)
  * dependency-free (stdlib only — no pandas / numpy)
  * defensive against missing/None fields, since upstream gateways can
    occasionally return partial rows during exchange API hiccups
"""

from __future__ import annotations

from typing import Any, Iterable

# CSV header for the compact K-line format. Single source of truth so that
# any consumer (parser, prompt assembler, tests) can reference it.
KLINE_CSV_HEADER = "t,o,h,l,c,v"


def _coerce_int_seconds(value: Any) -> int | None:
    """Convert a millisecond timestamp (int/float/str) to integer seconds.

    Returns None if the value is missing or cannot be parsed. We handle
    str inputs because some exchanges return timestamps as strings.
    """
    if value is None:
        return None
    try:
        ms = int(float(value))
    except (TypeError, ValueError):
        return None
    return ms // 1000


def _coerce_volume(value: Any) -> int:
    """Round a volume value to a non-negative integer.

    Missing / unparseable values become 0 — preferable to dropping the
    row, since the OHLC half is usually still useful for trend analysis.
    """
    if value is None:
        return 0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if v < 0 or v != v:  # NaN check via self-inequality
        return 0
    return int(round(v))


def klines_to_csv(klines: Iterable[dict[str, Any]] | None) -> str:
    """Serialize a list of K-line dicts to compact CSV text.

    Format (one row per bar, oldest first or newest first — preserved
    from input order; callers are responsible for ordering):

        t,o,h,l,c,v
        1777632300,77213.3,77296.0,77213.3,77262.1,34872
        ...

    Field semantics:
        t = openTime in **seconds** (10-digit integer)
        o,h,l,c = open / high / low / close
        v = volume rounded to integer

    Dropped fields (vs the legacy JSON format) and rationale:
        * closeTime    — always equal to openTime + interval, redundant
        * quoteVolume  — derivable from price * volume; strategies that
                          need precise quote volume can be added back as
                          a 7th column later if a real use case shows up

    Edge cases:
        * Empty / None input         → returns just the header
        * Bar missing openTime       → that bar is **skipped** (we cannot
                                       anchor it in time, so it would be
                                       misleading to emit a row)
        * Bar missing volume         → emitted with v=0
        * Bar missing OHLC field     → field rendered as empty string;
                                       LLM will treat it as a gap
        * NaN / negative volume      → coerced to 0
    """
    rows: list[str] = [KLINE_CSV_HEADER]
    if not klines:
        return rows[0]

    for bar in klines:
        if not isinstance(bar, dict):
            continue
        seconds = _coerce_int_seconds(bar.get("openTime"))
        if seconds is None:
            # Cannot place this bar on the timeline — skip rather than
            # invent a timestamp.
            continue
        # OHLC: render whatever the gateway gave us. We do NOT round here
        # because price precision varies wildly across symbols (BTC vs
        # SHIB) and the LLM is fine reading native-precision floats.
        def _fmt(field: str) -> str:
            value = bar.get(field)
            return "" if value is None else str(value)

        rows.append(
            f"{seconds},"
            f"{_fmt('open')},"
            f"{_fmt('high')},"
            f"{_fmt('low')},"
            f"{_fmt('close')},"
            f"{_coerce_volume(bar.get('volume'))}"
        )

    return "\n".join(rows)


def klines_by_interval_to_csv(
    klines_by_interval: dict[str, Any] | None,
) -> dict[str, str]:
    """Apply klines_to_csv to every interval in a dict.

    Used by the prompt assembler to convert
        {"15m": [bar, bar, ...], "1h": [bar, ...]}
    into
        {"15m": "t,o,h,l,c,v\\n...", "1h": "t,o,h,l,c,v\\n..."}

    Non-list values are passed through unchanged so that any odd shape
    survives instead of crashing the trading cycle.
    """
    if not isinstance(klines_by_interval, dict):
        return {}
    out: dict[str, str] = {}
    for interval, bars in klines_by_interval.items():
        if isinstance(bars, list):
            out[str(interval)] = klines_to_csv(bars)
        else:
            # Defensive: keep it round-trippable even if upstream
            # changes the shape.
            out[str(interval)] = "" if bars is None else str(bars)
    return out
