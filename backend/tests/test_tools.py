"""Unit tests for backend.tools.

Run with:
    cd /path/to/AITD && python3 -m unittest backend.tests.test_tools -v

Coverage targets per design doc (docs/design/阶段2-tool-registry-design.md):
  * 5 ToolResult tests
  * 5 ToolRegistry tests
  * 3 register_tool decorator tests
  * 10 execute_tool tests
  * 5 get_funding_rate tests
  Total: 28
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.tools import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    default_registry,
    execute_tool,
    register_tool,
)


def _ctx() -> ToolContext:
    """Convenience builder — most tests don't care about ctx contents."""
    return ToolContext(instance_id="test", cycle_id="cycle-1", exchange_id="binance")


# ============================================================================ #
# 1. ToolResult                                                                 #
# ============================================================================ #

class TestToolResult(unittest.TestCase):

    def test_success_factory_sets_ok_true_and_carries_data(self):
        r = ToolResult.success({"x": 1})
        self.assertTrue(r.ok)
        self.assertEqual(r.data, {"x": 1})
        self.assertIsNone(r.error)

    def test_failure_factory_sets_ok_false_with_error_and_hint(self):
        r = ToolResult.failure("boom", hint="try again")
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "boom")
        self.assertEqual(r.hint, "try again")
        self.assertIsNone(r.data)

    def test_to_model_text_is_compact_json(self):
        r = ToolResult.success({"a": 1, "b": 2})
        text = r.to_model_text()
        self.assertEqual(text, '{"ok":true,"data":{"a":1,"b":2}}')
        # Must be valid JSON
        self.assertEqual(json.loads(text), {"ok": True, "data": {"a": 1, "b": 2}})

    def test_to_model_text_excludes_meta_field(self):
        r = ToolResult.success({"x": 1}, meta={"durationSeconds": 0.5, "exchange": "binance"})
        text = r.to_model_text()
        self.assertNotIn("meta", text)
        self.assertNotIn("durationSeconds", text)
        self.assertNotIn("exchange", text)

    def test_to_model_text_preserves_chinese_chars(self):
        # ensure_ascii=False so 中文 doesn't blow up to \uXXXX (4-6 tokens each)
        r = ToolResult.success({"note": "市场震荡"})
        text = r.to_model_text()
        self.assertIn("市场震荡", text)
        self.assertNotIn("\\u", text)

    def test_toolresult_is_frozen(self):
        r = ToolResult.success({"x": 1})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            r.ok = False  # type: ignore[misc]


# ============================================================================ #
# 2. ToolRegistry                                                               #
# ============================================================================ #

class TestToolRegistry(unittest.TestCase):

    def _dummy_tool(self, name: str = "dummy") -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description="dummy tool",
            parameters_schema={"x": {"type": "string"}},
            executor=lambda args, ctx: ToolResult.success({"echo": args.get("x")}),
            required_params=("x",),
        )

    def test_register_then_get_round_trips(self):
        reg = ToolRegistry()
        tool = self._dummy_tool()
        reg.register(tool)
        self.assertIs(reg.get("dummy"), tool)

    def test_duplicate_register_raises_value_error(self):
        reg = ToolRegistry()
        reg.register(self._dummy_tool())
        with self.assertRaises(ValueError) as ctx:
            reg.register(self._dummy_tool())
        self.assertIn("already registered", str(ctx.exception))

    def test_names_returns_sorted_list(self):
        reg = ToolRegistry()
        reg.register(self._dummy_tool("zebra"))
        reg.register(self._dummy_tool("apple"))
        reg.register(self._dummy_tool("mango"))
        self.assertEqual(reg.names(), ["apple", "mango", "zebra"])

    def test_openai_tools_schema_well_formed(self):
        reg = ToolRegistry()
        reg.register(self._dummy_tool("foo"))
        schema = reg.openai_tools_schema()
        self.assertEqual(len(schema), 1)
        entry = schema[0]
        self.assertEqual(entry["type"], "function")
        self.assertEqual(entry["function"]["name"], "foo")
        self.assertEqual(entry["function"]["description"], "dummy tool")
        self.assertEqual(entry["function"]["parameters"]["type"], "object")
        self.assertEqual(entry["function"]["parameters"]["properties"], {"x": {"type": "string"}})
        self.assertEqual(entry["function"]["parameters"]["required"], ["x"])

    def test_empty_registry_yields_empty_lists(self):
        reg = ToolRegistry()
        self.assertEqual(reg.names(), [])
        self.assertEqual(reg.openai_tools_schema(), [])
        self.assertIsNone(reg.get("nonexistent"))


# ============================================================================ #
# 3. register_tool decorator                                                    #
# ============================================================================ #

class TestRegisterToolDecorator(unittest.TestCase):

    def test_decorated_function_remains_callable_directly(self):
        reg = ToolRegistry()

        @register_tool(
            name="callable_check",
            description="check direct call",
            parameters={"value": {"type": "integer"}},
            required=["value"],
            registry=reg,
        )
        def my_tool(args, ctx):
            return ToolResult.success({"got": args["value"]})

        # Direct call (bypassing the registry) still works
        result = my_tool({"value": 42}, _ctx())
        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"got": 42})

    def test_decorator_registers_into_default_registry_by_default(self):
        # Ensure no leftover from a previous test
        unique_name = "test_default_reg_target_dec"
        self.addCleanup(default_registry._tools.pop, unique_name, None)

        @register_tool(
            name=unique_name,
            description="goes to default",
            parameters={},
        )
        def my_tool(args, ctx):
            return ToolResult.success({})

        self.assertIn(unique_name, default_registry.names())

    def test_decorator_uses_custom_registry_when_provided(self):
        custom = ToolRegistry()

        @register_tool(
            name="custom_only",
            description="goes to custom",
            parameters={},
            registry=custom,
        )
        def my_tool(args, ctx):
            return ToolResult.success({})

        self.assertIn("custom_only", custom.names())
        self.assertNotIn("custom_only", default_registry.names())


# ============================================================================ #
# 4. execute_tool                                                               #
# ============================================================================ #

class TestExecuteTool(unittest.TestCase):

    def setUp(self):
        # Each test gets a fresh registry to avoid cross-pollution
        self.reg = ToolRegistry()

        @register_tool(
            name="echo",
            description="echo back the value",
            parameters={"value": {"type": "string"}},
            required=["value"],
            registry=self.reg,
        )
        def _echo(args, ctx):
            return ToolResult.success({"value": args["value"], "ctx_exchange": ctx.exchange_id})

        @register_tool(
            name="boom",
            description="raises an exception",
            parameters={},
            registry=self.reg,
        )
        def _boom(args, ctx):
            raise RuntimeError("intentional crash")

        @register_tool(
            name="returns_string",
            description="returns wrong type",
            parameters={},
            registry=self.reg,
        )
        def _bad(args, ctx):
            return "not a ToolResult"  # type: ignore[return-value]

        @register_tool(
            name="custom_meta",
            description="provides own meta",
            parameters={},
            registry=self.reg,
        )
        def _cm(args, ctx):
            return ToolResult.success({"k": "v"}, meta={"source": "manual"})

    def test_normal_execution_returns_success(self):
        result = execute_tool("echo", {"value": "hi"}, _ctx(), registry=self.reg)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["value"], "hi")

    def test_unknown_tool_returns_failure_listing_available(self):
        result = execute_tool("nonexistent", {}, _ctx(), registry=self.reg)
        self.assertFalse(result.ok)
        self.assertIn("unknown tool", result.error)
        # hint should list available tools
        self.assertIn("echo", result.hint)
        self.assertIn("boom", result.hint)

    def test_missing_required_param_returns_failure(self):
        result = execute_tool("echo", {}, _ctx(), registry=self.reg)
        self.assertFalse(result.ok)
        self.assertIn("missing required parameters", result.error)
        self.assertIn("value", result.error)
        self.assertIn("value", result.hint)

    def test_executor_exception_wrapped_as_failure(self):
        result = execute_tool("boom", {}, _ctx(), registry=self.reg)
        self.assertFalse(result.ok)
        # Error must include the exception type for debugging
        self.assertIn("RuntimeError", result.error)
        self.assertIn("intentional crash", result.error)

    def test_executor_returning_non_toolresult_is_caught(self):
        result = execute_tool("returns_string", {}, _ctx(), registry=self.reg)
        self.assertFalse(result.ok)
        self.assertIn("expected ToolResult", result.error)

    def test_meta_duration_seconds_always_set(self):
        # Successful path
        r1 = execute_tool("echo", {"value": "x"}, _ctx(), registry=self.reg)
        self.assertIsNotNone(r1.meta)
        self.assertIn("durationSeconds", r1.meta)
        self.assertGreaterEqual(r1.meta["durationSeconds"], 0)
        # Failure path
        r2 = execute_tool("boom", {}, _ctx(), registry=self.reg)
        self.assertIsNotNone(r2.meta)
        self.assertIn("durationSeconds", r2.meta)
        # Unknown tool path
        r3 = execute_tool("???", {}, _ctx(), registry=self.reg)
        self.assertIn("durationSeconds", r3.meta)

    def test_custom_registry_isolation(self):
        # Tools in self.reg are NOT visible to default_registry, and vice versa
        result = execute_tool("echo", {"value": "x"}, _ctx(), registry=None)  # default
        self.assertFalse(result.ok)  # default doesn't have "echo"
        self.assertIn("unknown tool", result.error)

    def test_context_passed_to_executor(self):
        ctx = ToolContext(instance_id="iX", cycle_id="cY", exchange_id="okx")
        result = execute_tool("echo", {"value": "ping"}, ctx, registry=self.reg)
        self.assertEqual(result.data["ctx_exchange"], "okx")

    def test_extra_args_are_passed_through_silently(self):
        # Tools may evolve to accept new optional params; old args should not break
        result = execute_tool("echo", {"value": "x", "future_param": 1}, _ctx(), registry=self.reg)
        self.assertTrue(result.ok)

    def test_args_none_treated_as_empty_dict(self):
        # When the model calls with no args (rare but possible), we should not
        # crash on dict access
        result = execute_tool("echo", None, _ctx(), registry=self.reg)
        self.assertFalse(result.ok)  # missing required param "value"
        self.assertIn("missing required", result.error)

    def test_tool_supplied_meta_is_preserved_with_duration_added(self):
        result = execute_tool("custom_meta", {}, _ctx(), registry=self.reg)
        self.assertEqual(result.meta["source"], "manual")
        self.assertIn("durationSeconds", result.meta)


# ============================================================================ #
# 5. get_funding_rate (integration-light)                                       #
# ============================================================================ #

class TestGetFundingRate(unittest.TestCase):
    """These tests mock backend.market.fetch_premium to avoid hitting network."""

    def test_normal_path_returns_success(self):
        fake_premium = {
            "symbol": "BTCUSDT",
            "fundingRate": 0.00012,
            "fundingPct": 0.012,
            "nextFundingTime": 1777689000000,
            "markPrice": 78225.0,
        }
        with patch("backend.market.fetch_premium", return_value=fake_premium):
            result = execute_tool(
                "get_funding_rate",
                {"symbol": "BTCUSDT"},
                ToolContext(exchange_id="binance"),
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["symbol"], "BTCUSDT")
        self.assertEqual(result.data["fundingRate"], 0.00012)
        self.assertEqual(result.data["fundingPct"], 0.012)
        self.assertEqual(result.data["markPrice"], 78225.0)
        self.assertEqual(result.data["nextFundingTime"], 1777689000000)
        # meta should record which exchange we called
        self.assertEqual(result.meta.get("exchange"), "binance")

    def test_empty_symbol_returns_failure_without_calling_fetch(self):
        with patch("backend.market.fetch_premium") as mock_fetch:
            result = execute_tool(
                "get_funding_rate",
                {"symbol": "   "},
                ToolContext(exchange_id="binance"),
            )
        self.assertFalse(result.ok)
        self.assertIn("non-empty string", result.error)
        mock_fetch.assert_not_called()

    def test_missing_symbol_arg_caught_at_execute_layer(self):
        # execute_tool itself should reject missing required param before
        # the executor body even runs
        with patch("backend.market.fetch_premium") as mock_fetch:
            result = execute_tool(
                "get_funding_rate",
                {},
                ToolContext(exchange_id="binance"),
            )
        self.assertFalse(result.ok)
        self.assertIn("missing required parameters", result.error)
        self.assertIn("symbol", result.error)
        mock_fetch.assert_not_called()

    def test_fetch_premium_exception_wrapped_safely(self):
        with patch("backend.market.fetch_premium", side_effect=ConnectionError("network down")):
            result = execute_tool(
                "get_funding_rate",
                {"symbol": "BTCUSDT"},
                ToolContext(exchange_id="binance"),
            )
        self.assertFalse(result.ok)
        self.assertIn("fetch_premium failed", result.error)
        self.assertIn("network down", result.error)
        self.assertIsNotNone(result.hint)

    def test_all_expected_fields_present(self):
        fake = {
            "symbol": "ETH-USDT-SWAP",
            "fundingRate": 0.0001,
            "fundingPct": 0.01,
            "nextFundingTime": 1777689000000,
            "markPrice": 2294.5,
        }
        with patch("backend.market.fetch_premium", return_value=fake):
            result = execute_tool(
                "get_funding_rate",
                {"symbol": "eth-usdt-swap"},  # lowercase, will be uppercased
                ToolContext(exchange_id="okx"),
            )
        for field in ("symbol", "fundingRate", "fundingPct", "nextFundingTime", "markPrice"):
            self.assertIn(field, result.data, f"missing field: {field}")


# ============================================================================ #
# 6. End-to-end model-text round trip                                           #
# ============================================================================ #

class TestEndToEndModelTextSize(unittest.TestCase):
    """Sanity check that to_model_text() output is reasonably small."""

    def test_typical_funding_result_under_200_chars(self):
        # The model-facing payload for a funding result should be tiny;
        # if this balloons we've regressed on the token-friendly principle.
        r = ToolResult.success({
            "symbol": "BTCUSDT",
            "fundingRate": 0.00012,
            "fundingPct": 0.012,
            "nextFundingTime": 1777689000000,
            "markPrice": 78225.0,
        }, meta={"exchange": "binance", "durationSeconds": 0.5})
        text = r.to_model_text()
        self.assertLess(len(text), 200, f"funding result too verbose: {text}")
        self.assertIn("BTCUSDT", text)
        self.assertNotIn("durationSeconds", text)  # meta excluded


if __name__ == "__main__":
    unittest.main(verbosity=2)
