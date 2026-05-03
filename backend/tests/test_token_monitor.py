"""Unit tests for backend.token_monitor.

Run with:
    cd /path/to/AITD && python3 -m unittest backend.tests.test_token_monitor -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.token_monitor import (
    context_window_for,
    estimate_tokens,
    evaluate_token_usage,
    format_log_line,
)


class TestEstimateTokens(unittest.TestCase):
    def test_empty_returns_zero(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens(None), 0)

    def test_proportional_to_length(self):
        # 30 chars / 3 ≈ 10 tokens
        self.assertEqual(estimate_tokens("a" * 30), 11)
        self.assertEqual(estimate_tokens("a" * 300), 101)

    def test_real_prompt_within_15pct_of_actual(self):
        # Simulate the production prompt size: 235388 chars / 73943 real tokens.
        # Our estimator uses 3.0 chars/token, slightly conservative.
        estimated = estimate_tokens("x" * 235388)
        actual = 73943
        ratio = estimated / actual
        self.assertGreater(ratio, 0.95, f"estimator under-counts too much: {ratio}")
        self.assertLess(ratio, 1.20, f"estimator over-counts too much: {ratio}")


class TestContextWindowFor(unittest.TestCase):
    def test_explicit_field_wins(self):
        self.assertEqual(
            context_window_for({"model": "gpt-4", "contextWindow": 50_000}),
            50_000,
        )

    def test_deepseek_v4_pro_resolves_to_128k(self):
        self.assertEqual(
            context_window_for({"model": "deepseek-v4-pro"}),
            128_000,
        )

    def test_case_insensitive_model_match(self):
        self.assertEqual(
            context_window_for({"model": "DeepSeek-V4-Pro"}),
            128_000,
        )

    def test_longest_prefix_wins(self):
        # gpt-4o (128k) should win over gpt-4 (8k) because it's longer
        self.assertEqual(context_window_for({"model": "gpt-4o-mini"}), 128_000)
        self.assertEqual(context_window_for({"model": "gpt-4-turbo"}), 128_000)
        self.assertEqual(context_window_for({"model": "gpt-4"}), 8_192)

    def test_unknown_model_returns_safe_default(self):
        self.assertEqual(
            context_window_for({"model": "made-up-llm-7b"}),
            32_000,
        )

    def test_empty_provider_returns_safe_default(self):
        self.assertEqual(context_window_for(None), 32_000)
        self.assertEqual(context_window_for({}), 32_000)
        self.assertEqual(context_window_for({"model": ""}), 32_000)


class TestEvaluateTokenUsage(unittest.TestCase):
    """Required: cover all 4 tiers (OK / WARN / DANGER / BLOCK)."""

    WIN = 100_000  # round numbers make assertions readable

    # --- Required tier 1: OK (< 60%) --------------------------------------
    def test_ok_tier_at_below_60_percent(self):
        r = evaluate_token_usage(50_000, self.WIN)
        self.assertEqual(r["level"], "OK")
        self.assertFalse(r["should_block"])
        self.assertIsNone(r["warning_text"])
        self.assertIn("[OK]", r["log_line"])

    # --- Required tier 2: WARN (60% – 85%) --------------------------------
    def test_warn_tier_at_75_percent(self):
        r = evaluate_token_usage(75_000, self.WIN)
        self.assertEqual(r["level"], "WARN")
        self.assertFalse(r["should_block"])
        self.assertIsNone(r["warning_text"], "WARN tier should NOT pollute warnings field")
        self.assertIn("[WARN]", r["log_line"])

    def test_warn_tier_lower_boundary_60_percent(self):
        r = evaluate_token_usage(60_000, self.WIN)
        self.assertEqual(r["level"], "WARN")

    # --- Required tier 3: DANGER (85% – 95%) ------------------------------
    def test_danger_tier_at_90_percent(self):
        r = evaluate_token_usage(90_000, self.WIN)
        self.assertEqual(r["level"], "DANGER")
        self.assertFalse(r["should_block"], "DANGER allows the call but warns loudly")
        self.assertIsNotNone(r["warning_text"])
        self.assertIn("approaching hard limit", r["warning_text"])
        self.assertIn("[DANGER]", r["log_line"])

    def test_danger_tier_lower_boundary_85_percent(self):
        r = evaluate_token_usage(85_000, self.WIN)
        self.assertEqual(r["level"], "DANGER")

    # --- Required tier 4: BLOCK (>= 95%) ----------------------------------
    def test_block_tier_at_95_percent(self):
        r = evaluate_token_usage(95_000, self.WIN)
        self.assertEqual(r["level"], "BLOCK")
        self.assertTrue(r["should_block"], "BLOCK must instruct caller to refuse the API call")
        self.assertIn("refusing model call", r["warning_text"])
        self.assertIn("[BLOCK]", r["log_line"])

    def test_block_tier_at_above_100_percent(self):
        # When prompt actually exceeds the window we should still BLOCK,
        # not crash on the math.
        r = evaluate_token_usage(150_000, self.WIN)
        self.assertEqual(r["level"], "BLOCK")
        self.assertTrue(r["should_block"])

    # --- Defensive ---------------------------------------------------------
    def test_zero_window_uses_safe_default(self):
        # Should not divide by zero.
        r = evaluate_token_usage(50_000, 0)
        self.assertIn(r["level"], {"OK", "WARN", "DANGER", "BLOCK"})

    def test_log_line_contains_window_in_human_form(self):
        r = evaluate_token_usage(50_000, 128_000)
        self.assertIn("128k", r["log_line"])
        r = evaluate_token_usage(50_000, 1_048_576)
        self.assertIn("1M", r["log_line"])


class TestFormatLogLine(unittest.TestCase):
    def test_stable_format(self):
        line = format_log_line(12345, 128_000, "OK", 0.10)
        self.assertEqual(
            line,
            "[tokens] prompt=12345 window=128k usage=10% [OK]",
        )

    def test_includes_cycle_id_when_provided(self):
        line = format_log_line(50_000, 128_000, "WARN", 0.39, cycle_id="abc-123")
        self.assertIn("cycle=abc-123", line)

    def test_includes_extra_when_provided(self):
        line = format_log_line(50_000, 128_000, "OK", 0.39, extra="cached=40000")
        self.assertIn("| cached=40000", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
