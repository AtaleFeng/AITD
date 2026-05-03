"""Tool registry for LLM function calling.

This module owns:
  * the data shapes (`ToolDefinition`, `ToolContext`, `ToolResult`)
  * the global registry (`default_registry`) and decorator (`register_tool`)
  * the safe execution entry point (`execute_tool`) — the LLM loop's only
    way to invoke a tool. It NEVER raises.
  * the first concrete tool: `get_funding_rate`

Design rationale and decisions are documented in
    docs/design/阶段2-tool-registry-design.md
Read that file before changing this one.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable


# --------------------------------------------------------------------------- #
# Data shapes                                                                 #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ToolDefinition:
    """Static metadata + executor for a single tool.

    Fields:
      name              unique identifier exposed to the model (e.g. "get_funding_rate")
      description       English human-readable description for the model
      parameters_schema JSON Schema "properties" dict — keys are param names,
                        values are JSON Schema fragments
      executor          callable: (args: dict, ctx: ToolContext) -> ToolResult
      required_params   tuple of param names that are mandatory
    """
    name: str
    description: str
    parameters_schema: dict
    executor: Callable[[dict, "ToolContext"], "ToolResult"]
    required_params: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolContext:
    """Runtime info passed to every tool. Tools NEVER reach for globals."""
    instance_id: str | None = None
    cycle_id: str | None = None
    network_settings: dict | None = None
    settings: dict | None = None
    exchange_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    """Uniform return shape for tools.

    `data` carries the success payload (any JSON-serializable dict).
    `error` and `hint` are surfaced to the model on failure.
    `meta` is for the decision audit trail (durations, source endpoints, etc.)
    and is intentionally excluded from `to_model_text()` to save tokens.
    """
    ok: bool
    data: dict | None = None
    error: str | None = None
    hint: str | None = None
    meta: dict | None = None

    @classmethod
    def success(cls, data: dict, *, hint: str | None = None, meta: dict | None = None) -> "ToolResult":
        return cls(ok=True, data=data, hint=hint, meta=meta)

    @classmethod
    def failure(cls, error: str, *, hint: str | None = None, meta: dict | None = None) -> "ToolResult":
        return cls(ok=False, error=error, hint=hint, meta=meta)

    def to_model_text(self) -> str:
        """Compact JSON suitable for the LLM tool-result message.

        Always excludes `meta` — that field is for trace logging, never for
        the model. Uses ensure_ascii=False so Chinese chars are not escaped
        and use 1 token instead of 4-6.
        """
        payload: dict[str, Any] = {"ok": self.ok}
        if self.data is not None:
            payload["data"] = self.data
        if self.error is not None:
            payload["error"] = self.error
        if self.hint is not None:
            payload["hint"] = self.hint
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# Registry                                                                    #
# --------------------------------------------------------------------------- #

class ToolRegistry:
    """Stores ToolDefinition objects and exposes them to the LLM transport.

    The global `default_registry` instance is what the @register_tool
    decorator writes into; tests can construct fresh ToolRegistry()
    instances for isolation.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(
                f"Tool {tool.name!r} already registered. "
                f"Tool names must be unique across the registry."
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def openai_tools_schema(self) -> list[dict]:
        """Render the registry to the OpenAI tools=[...] payload format.

        This is the exact shape DeepSeek / OpenAI / Qwen consume; Anthropic
        and Gemini will need a thin adapter layer in T12.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": {
                        "type": "object",
                        "properties": t.parameters_schema,
                        "required": list(t.required_params),
                    },
                },
            }
            for t in self._tools.values()
        ]


default_registry = ToolRegistry()


# --------------------------------------------------------------------------- #
# Registration decorator                                                      #
# --------------------------------------------------------------------------- #

def register_tool(
    *,
    name: str,
    description: str,
    parameters: dict,
    required: list[str] | None = None,
    registry: ToolRegistry | None = None,
):
    """Decorator: register a function as a callable tool.

    The decorated function receives `(args: dict, ctx: ToolContext)` and
    must return a `ToolResult`. The original function is returned unchanged
    so it can still be called directly (handy for unit tests).
    """
    target = registry or default_registry

    def decorator(func: Callable[[dict, ToolContext], ToolResult]):
        tool = ToolDefinition(
            name=name,
            description=description,
            parameters_schema=parameters,
            executor=func,
            required_params=tuple(required or []),
        )
        target.register(tool)
        return func

    return decorator


# --------------------------------------------------------------------------- #
# Safe execution entry point                                                  #
# --------------------------------------------------------------------------- #

def execute_tool(
    name: str,
    args: dict | None,
    context: ToolContext,
    *,
    registry: ToolRegistry | None = None,
) -> ToolResult:
    """Run a registered tool. **Never raises.**

    Always returns a ToolResult, even if:
      * the tool name is unknown
      * required parameters are missing
      * the tool's executor raises an exception
      * the tool's executor returns a wrong type

    This invariant lets the LLM loop treat every result uniformly without
    try/except scaffolding.
    """
    target = registry or default_registry
    safe_args: dict = args if isinstance(args, dict) else {}
    started = time.monotonic()

    tool = target.get(name)
    if tool is None:
        return ToolResult.failure(
            error=f"unknown tool: {name!r}",
            hint=f"available tools: {', '.join(target.names()) or '(none)'}",
            meta={"durationSeconds": time.monotonic() - started},
        )

    missing = [p for p in tool.required_params if p not in safe_args]
    if missing:
        return ToolResult.failure(
            error=f"missing required parameters: {', '.join(missing)}",
            hint=f"tool {tool.name!r} requires {list(tool.required_params)}",
            meta={"durationSeconds": time.monotonic() - started},
        )

    try:
        result = tool.executor(safe_args, context)
    except Exception as exc:  # noqa: BLE001 — deliberate broad catch for safety
        return ToolResult.failure(
            error=f"{type(exc).__name__}: {exc}",
            meta={"durationSeconds": time.monotonic() - started},
        )

    if not isinstance(result, ToolResult):
        return ToolResult.failure(
            error=f"tool {tool.name!r} returned {type(result).__name__}, expected ToolResult",
            meta={"durationSeconds": time.monotonic() - started},
        )

    # Inject duration into meta if the tool didn't supply it. We do not
    # overwrite an explicit meta from the tool (it might have richer info),
    # but we ensure durationSeconds is always recorded.
    if result.meta is None:
        return ToolResult(
            ok=result.ok,
            data=result.data,
            error=result.error,
            hint=result.hint,
            meta={"durationSeconds": time.monotonic() - started},
        )
    if "durationSeconds" not in result.meta:
        merged_meta = dict(result.meta)
        merged_meta["durationSeconds"] = time.monotonic() - started
        return ToolResult(
            ok=result.ok,
            data=result.data,
            error=result.error,
            hint=result.hint,
            meta=merged_meta,
        )
    return result


# --------------------------------------------------------------------------- #
# Concrete tools                                                              #
# --------------------------------------------------------------------------- #

@register_tool(
    name="get_funding_rate",
    description=(
        "Query the current funding rate for a perpetual futures symbol. "
        "Returns the most recent funding rate (as a fraction, e.g. 0.0054 = 0.54%) "
        "and the next funding time. Use this when you need fresher funding data "
        "than what's already in the candidates context, or to look up symbols "
        "outside the candidate universe."
    ),
    parameters={
        "symbol": {
            "type": "string",
            "description": (
                "Symbol in the active exchange's native format. "
                "For Binance: BTCUSDT, ETHUSDT. "
                "For OKX: BTC-USDT-SWAP, ETH-USDT-SWAP. "
                "The tool will normalize common variants automatically."
            ),
        },
    },
    required=["symbol"],
)
def get_funding_rate(args: dict, ctx: ToolContext) -> ToolResult:
    # Lazy import: backend.market imports backend.exchanges which (transitively)
    # imports backend.config. Importing at module-load time would create a
    # cycle in some startup orderings.
    from .market import fetch_premium

    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return ToolResult.failure("symbol must be a non-empty string")
    try:
        premium = fetch_premium(symbol, ctx.exchange_id)
    except Exception as exc:  # noqa: BLE001
        return ToolResult.failure(
            error=f"fetch_premium failed: {exc}",
            hint="check that the symbol is valid for the active exchange",
            meta={"exchange": ctx.exchange_id},
        )
    return ToolResult.success(
        {
            "symbol": premium.get("symbol") or symbol,
            "fundingRate": premium.get("fundingRate"),
            "fundingPct": premium.get("fundingPct"),
            "nextFundingTime": premium.get("nextFundingTime"),
            "markPrice": premium.get("markPrice"),
        },
        meta={"exchange": ctx.exchange_id},
    )
