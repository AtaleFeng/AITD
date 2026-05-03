"""Unit tests for backend.prompt_format.

Run with:
    cd /path/to/AITD && python3 -m unittest backend.tests.test_prompt_format -v
or simply:
    python3 backend/tests/test_prompt_format.py
"""

from __future__ import annotations

import csv
import io
import json
import unittest
from pathlib import Path
import sys

# Allow running this file directly: `python3 backend/tests/test_prompt_format.py`
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.prompt_format import (
    KLINE_CSV_HEADER,
    klines_by_interval_to_csv,
    klines_to_csv,
)


def _sample_bar(open_time_ms: int, *, close: float = 78225.7, vol: float = 13301.2) -> dict:
    """Helper: build a typical kline dict matching what gateways return."""
    return {
        "openTime": open_time_ms,
        "open": 78223.9,
        "high": 78331.5,
        "low": 78205.7,
        "close": close,
        "volume": vol,
        "closeTime": open_time_ms,         # gateways set these even though
        "quoteVolume": 10409872.34487,     # we drop them in CSV form
    }


class TestKlinesToCsv(unittest.TestCase):
    """Required edge cases from the task spec, plus a few defensive ones."""

    # --- Required case 1: normal 64-bar series ----------------------------
    def test_normal_64_bars_produces_correct_csv(self):
        bars = [_sample_bar(1777689000000 + i * 900_000) for i in range(64)]
        out = klines_to_csv(bars)
        lines = out.splitlines()
        self.assertEqual(lines[0], KLINE_CSV_HEADER)
        self.assertEqual(len(lines), 65, "header + 64 data rows")
        # First data row: timestamp converted ms→s
        first = lines[1].split(",")
        self.assertEqual(first[0], "1777689000")
        self.assertEqual(len(first), 6, "6 columns: t,o,h,l,c,v")

    # --- Required case 2: empty list returns header only ------------------
    def test_empty_list_returns_only_header(self):
        self.assertEqual(klines_to_csv([]), KLINE_CSV_HEADER)

    def test_none_input_returns_only_header(self):
        self.assertEqual(klines_to_csv(None), KLINE_CSV_HEADER)

    # --- Required case 3: missing volume → 0 (not crash) ------------------
    def test_missing_volume_becomes_zero(self):
        bar = _sample_bar(1777689000000)
        del bar["volume"]
        out = klines_to_csv([bar])
        self.assertEqual(out.splitlines()[1].split(",")[5], "0")

    def test_none_volume_becomes_zero(self):
        bar = _sample_bar(1777689000000)
        bar["volume"] = None
        self.assertEqual(klines_to_csv([bar]).splitlines()[1].split(",")[5], "0")

    def test_negative_volume_becomes_zero(self):
        bar = _sample_bar(1777689000000)
        bar["volume"] = -5.2  # spurious gateway value
        self.assertEqual(klines_to_csv([bar]).splitlines()[1].split(",")[5], "0")

    def test_nan_volume_becomes_zero(self):
        bar = _sample_bar(1777689000000)
        bar["volume"] = float("nan")
        self.assertEqual(klines_to_csv([bar]).splitlines()[1].split(",")[5], "0")

    def test_string_volume_is_parsed(self):
        # Some exchanges return numbers as strings; we should accept that.
        bar = _sample_bar(1777689000000)
        bar["volume"] = "13301.2"
        self.assertEqual(klines_to_csv([bar]).splitlines()[1].split(",")[5], "13301")

    # --- Required case 4: missing/None timestamp → row skipped ------------
    def test_none_timestamp_row_is_skipped(self):
        bars = [
            _sample_bar(1777689000000),
            _sample_bar(0),  # placeholder, will be replaced
            _sample_bar(1777690800000),
        ]
        bars[1]["openTime"] = None
        lines = klines_to_csv(bars).splitlines()
        # header + 2 valid rows (the None one is skipped)
        self.assertEqual(len(lines), 3)

    def test_missing_timestamp_row_is_skipped(self):
        bars = [_sample_bar(1777689000000), _sample_bar(0)]
        del bars[1]["openTime"]
        self.assertEqual(len(klines_to_csv(bars).splitlines()), 2)

    def test_unparseable_timestamp_row_is_skipped(self):
        bars = [_sample_bar(1777689000000), _sample_bar(0)]
        bars[1]["openTime"] = "not-a-number"
        self.assertEqual(len(klines_to_csv(bars).splitlines()), 2)

    # --- Required case 5: output is csv.reader-parseable (round-trip) -----
    def test_output_round_trips_through_csv_reader(self):
        bars = [_sample_bar(1777689000000 + i * 900_000) for i in range(10)]
        out = klines_to_csv(bars)
        reader = csv.reader(io.StringIO(out))
        rows = list(reader)
        self.assertEqual(rows[0], KLINE_CSV_HEADER.split(","))
        self.assertEqual(len(rows), 11)
        # Round-trip the timestamps and confirm millisecond → second conv.
        for original, row in zip(bars, rows[1:]):
            self.assertEqual(int(row[0]), original["openTime"] // 1000)

    # --- Defensive: non-dict items are silently skipped -------------------
    def test_non_dict_items_are_skipped(self):
        bars = [_sample_bar(1777689000000), "junk", None, 42, _sample_bar(1777690800000)]
        lines = klines_to_csv(bars).splitlines()
        self.assertEqual(len(lines), 3, "header + 2 valid rows")

    # --- Defensive: missing OHLC field renders empty, not crash -----------
    def test_missing_ohlc_field_renders_empty_string(self):
        bar = _sample_bar(1777689000000)
        del bar["high"]
        row = klines_to_csv([bar]).splitlines()[1].split(",")
        # Layout: t,o,h,l,c,v  → high is at index 2
        self.assertEqual(row[2], "")

    # --- Token-economy sanity: CSV is meaningfully smaller than JSON ------
    def test_csv_is_at_least_60_percent_smaller_than_json(self):
        bars = [_sample_bar(1777689000000 + i * 900_000) for i in range(64)]
        csv_size = len(klines_to_csv(bars))
        json_size = len(json.dumps(bars, ensure_ascii=False, indent=2))
        ratio = csv_size / json_size
        self.assertLess(
            ratio, 0.40,
            f"CSV should be <40% the size of JSON; got {ratio:.0%}",
        )


class TestKlinesByIntervalToCsv(unittest.TestCase):
    """Wrapper that converts the {interval: [bars]} dict shape."""

    def test_multiple_intervals_each_become_csv_string(self):
        data = {
            "15m": [_sample_bar(1777689000000 + i * 900_000) for i in range(3)],
            "1h":  [_sample_bar(1777689000000 + i * 3_600_000) for i in range(2)],
        }
        out = klines_by_interval_to_csv(data)
        self.assertSetEqual(set(out.keys()), {"15m", "1h"})
        self.assertTrue(out["15m"].startswith(KLINE_CSV_HEADER))
        self.assertEqual(len(out["15m"].splitlines()), 4, "header + 3 bars")
        self.assertEqual(len(out["1h"].splitlines()), 3, "header + 2 bars")

    def test_none_input_returns_empty_dict(self):
        self.assertEqual(klines_by_interval_to_csv(None), {})

    def test_non_dict_input_returns_empty_dict(self):
        self.assertEqual(klines_by_interval_to_csv("not a dict"), {})
        self.assertEqual(klines_by_interval_to_csv([]), {})

    def test_empty_interval_list_yields_header_only_csv(self):
        out = klines_by_interval_to_csv({"5m": []})
        self.assertEqual(out["5m"], KLINE_CSV_HEADER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
