"""Live tool-schema discovery and dynamic argument binding.

The whole point of this module: **never hardcode an upstream tool's parameter
names**.  On start-up the bot calls ``tools/list``, and every subsequent call is
assembled against the schema the server actually returned.  Field names differ
between server builds (``symbolId`` vs ``symbol``, ``volume`` vs ``volumeInLots``,
``M_1`` vs ``M1``), and an order silently missing its stop-loss because a name
changed is the single most expensive bug this bot could have.

Two guarantees:

1. If a required parameter cannot be filled, we raise :class:`SchemaBindError`
   and send nothing - a loud failure instead of a malformed order.
2. Values are coerced to the declared JSON type and, where the schema declares an
   ``enum``, snapped onto a real enum member.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from mcp_client.errors import SchemaBindError, ToolUnavailable
from utils.logging import get_logger

log = get_logger("mcp.schema")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(name: str) -> str:
    """``"Stop Loss_Price"`` -> ``"stoplossprice"`` for tolerant comparisons."""
    return _NON_ALNUM.sub("", str(name).lower())


# ---------------------------------------------------------------------------
# Tool catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def properties(self) -> dict[str, Any]:
        props = self.input_schema.get("properties")
        return props if isinstance(props, dict) else {}

    @property
    def required(self) -> list[str]:
        req = self.input_schema.get("required")
        return [str(r) for r in req] if isinstance(req, list) else []


class ToolCatalog:
    """Case/punctuation-insensitive lookup over the live tool list."""

    def __init__(self, tools: Sequence[ToolSpec]) -> None:
        self._tools = list(tools)
        self._by_norm = {normalize(t.name): t for t in tools}

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> list[str]:
        return [t.name for t in self._tools]

    def find(self, *candidates: str) -> Optional[ToolSpec]:
        for candidate in candidates:
            tool = self._by_norm.get(normalize(candidate))
            if tool is not None:
                return tool
        return None

    def require(self, *candidates: str) -> ToolSpec:
        tool = self.find(*candidates)
        if tool is None:
            raise ToolUnavailable(
                f"None of {list(candidates)} is exposed by the MCP server. "
                f"Available tools: {self.names}"
            )
        return tool

    def has(self, *candidates: str) -> bool:
        return self.find(*candidates) is not None


# ---------------------------------------------------------------------------
# Canonical parameter vocabulary
# ---------------------------------------------------------------------------

#: canonical name -> parameter-name aliases seen across cTrader/MCP server builds.
#: Ordered most-specific-first: ``stop_loss`` must get a shot at ``stopLossPrice``
#: before the generic ``price`` canonical can claim it.
CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    # The relative variants must be listed before the absolute ones: a MARKET
    # order cannot carry an absolute stopLoss, so this bot needs to address
    # `relativeStopLoss` directly rather than have it swallowed by `stop_loss`.
    "relative_stop_loss": ("relativestoploss", "relativesl", "stoplossinpoints",
                           "stoplossdistance"),
    "relative_take_profit": ("relativetakeprofit", "relativetp",
                             "takeprofitinpoints", "takeprofitdistance"),
    "stop_loss": ("stoploss", "sl", "stoplossprice", "stoplossinprice", "slprice",
                  "stoplosspips", "stoplossinpips"),
    "take_profit": ("takeprofit", "tp", "takeprofitprice", "takeprofitinprice",
                    "tpprice", "takeprofitpips", "takeprofitinpips"),
    "order_type": ("ordertype", "type", "kind", "ordertypename"),
    "trade_side": ("tradeside", "side", "direction", "orderside", "buysell",
                   "tradedirection", "operation"),
    "symbol_id": ("symbolid", "symbolids", "instrumentid", "id"),
    "symbol_name": ("symbolname", "symbol", "instrument", "ticker", "pair"),
    "volume": ("volume", "volumeinlots", "lots", "lotsize", "quantity", "qty",
               "size", "amount", "volumeinunits", "units", "tradevolume"),
    "limit_price": ("limitprice", "entryprice", "orderprice", "price",
                    "priceinprice", "openprice"),
    "stop_price": ("stopprice", "stoporderprice", "triggerprice"),
    "period": ("period", "timeframe", "periodtype", "interval", "resolution",
               "granularity", "bartype"),
    "count": ("count", "limit", "bars", "barcount", "maxbars", "numberofbars", "maxrows"),
    "from_ts": ("fromtimestamp", "from", "starttime", "start", "fromtime",
                "fromdate", "since", "begintime"),
    "to_ts": ("totimestamp", "to", "endtime", "end", "totime", "todate", "until"),
    "position_id": ("positionid", "posid"),
    "order_id": ("orderid", "pendingorderid"),
    "deal_id": ("dealid",),
    "label": ("label", "ordertag", "tag"),
    "comment": ("comment", "note", "description"),
    "client_order_id": ("clientorderid", "clientmsgid", "clientid", "customid"),
    "expiry": ("expiry", "expirytimestamp", "expirationtime", "expiretime",
               "goodtilldate", "expiration"),
    "time_in_force": ("timeinforce", "tif"),
    "trailing": ("trailingstoploss", "trailing", "istrailing"),
    "guaranteed_stop": ("guaranteedstoploss", "guaranteedstop"),
}

#: Order in which the substring fallback pass is attempted (most specific first).
_FALLBACK_ORDER: tuple[str, ...] = (
    "relative_stop_loss", "relative_take_profit", "stop_loss", "take_profit", "order_type", "trade_side", "symbol_id",
    "symbol_name", "limit_price", "stop_price", "period", "from_ts", "to_ts",
    "count", "position_id", "order_id", "deal_id", "volume", "label", "comment",
    "client_order_id", "expiry", "time_in_force",
)

#: Aliases shorter than this are exact-match only (see :func:`map_properties`).
MIN_SUBSTRING_ALIAS = 4

_NORM_ALIASES: dict[str, tuple[str, ...]] = {
    canonical: tuple(normalize(a) for a in aliases)
    for canonical, aliases in CANONICAL_ALIASES.items()
}


@dataclass(frozen=True)
class Candidates:
    """A value with preference-ordered alternatives.

    Used where the wire representation is server-dependent, e.g. a BUY side may
    be ``"BUY"``, ``"buy"``, ``1`` or ``"LONG"``.  The binder picks the first
    alternative the live schema will actually accept.
    """

    values: tuple[Any, ...]

    @staticmethod
    def of(*values: Any) -> "Candidates":
        return Candidates(tuple(values))


def map_properties(tool: ToolSpec) -> dict[str, str]:
    """Map each of the tool's real property names onto a canonical name."""
    mapping: dict[str, str] = {}
    taken: set[str] = set()

    # Pass 1 - exact alias match (highest confidence).
    for prop in tool.properties:
        norm = normalize(prop)
        for canonical, aliases in _NORM_ALIASES.items():
            if norm in aliases:
                mapping[prop] = canonical
                taken.add(canonical)
                break

    # Pass 2 - substring fallback for names we have not seen before
    # (e.g. "stopLossInPoints"), most-specific canonical first.
    #
    # Only aliases of at least MIN_SUBSTRING_ALIAS characters take part. Short
    # ones are far too greedy as substrings: "n" (an alias for `count`) matches
    # the n in "expiratio[n]Timestamp", which silently routed a bar count into
    # an order's expiry field. Short aliases still work in pass 1, where the
    # match has to be exact.
    for prop in tool.properties:
        if prop in mapping:
            continue
        norm = normalize(prop)
        for canonical in _FALLBACK_ORDER:
            if canonical in taken:
                continue
            if any(len(alias) >= MIN_SUBSTRING_ALIAS and alias in norm
                   for alias in _NORM_ALIASES[canonical]):
                mapping[prop] = canonical
                taken.add(canonical)
                break
    return mapping


# ---------------------------------------------------------------------------
# Type coercion
# ---------------------------------------------------------------------------

def _declared_types(prop_schema: Mapping[str, Any]) -> list[str]:
    declared = prop_schema.get("type")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list):
        return [t for t in declared if isinstance(t, str)]
    # anyOf/oneOf unions
    for key in ("anyOf", "oneOf"):
        variants = prop_schema.get(key)
        if isinstance(variants, list):
            types: list[str] = []
            for variant in variants:
                if isinstance(variant, Mapping):
                    types.extend(_declared_types(variant))
            return types
    return []


def _enum_values(prop_schema: Mapping[str, Any]) -> list[Any]:
    enum = prop_schema.get("enum")
    if isinstance(enum, list):
        return enum
    for key in ("anyOf", "oneOf"):
        variants = prop_schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, Mapping) and isinstance(variant.get("enum"), list):
                    return list(variant["enum"])
    return []


#: Returned when no declared type can represent a value. Distinct from None,
#: which is a legitimate "field not supplied".
UNCOERCIBLE = object()


def _coerce(value: Any, types: Sequence[str]) -> Any:
    """Coerce ``value`` to the first declared JSON type that accepts it.

    Returns :data:`UNCOERCIBLE` when none of them can. Sending a value the
    schema cannot represent - an ISO timestamp into an ``integer`` field, say -
    gets the whole order rejected, so the caller tries the next candidate (or
    drops an optional field) instead.
    """
    if not types:
        return value
    for declared in types:
        try:
            if declared == "integer":
                return int(round(float(value)))
            if declared == "number":
                return float(value)
            if declared == "boolean":
                if isinstance(value, str):
                    return value.strip().lower() in {"1", "true", "yes", "on"}
                return bool(value)
            if declared == "string":
                if isinstance(value, bool):
                    return "true" if value else "false"
                return str(value)
            if declared == "array":
                return list(value) if isinstance(value, (list, tuple, set)) else [value]
            if declared in ("object", "null"):
                return value
        except (TypeError, ValueError):
            continue
    return UNCOERCIBLE


def _match_enum(value: Any, enum: Sequence[Any]) -> Optional[Any]:
    """Snap ``value`` onto an enum member, comparing loosely (M1 ~ M_1)."""
    for member in enum:
        if member == value:
            return member
    target = normalize(value)
    for member in enum:
        if normalize(member) == target:
            return member
    return None


def bind_value(prop_schema: Mapping[str, Any], value: Any) -> Optional[Any]:
    """Fit one value to one property schema, or return None if impossible."""
    candidates = value.values if isinstance(value, Candidates) else (value,)
    types = _declared_types(prop_schema)
    enum = _enum_values(prop_schema)

    # Array-typed properties (e.g. get_spot_prices' symbolId: integer[]).
    item_schema = prop_schema.get("items") if isinstance(prop_schema.get("items"), Mapping) else {}
    is_array = "array" in types

    for candidate in candidates:
        if candidate is None:
            continue
        if enum:
            matched = _match_enum(candidate, enum)
            if matched is not None:
                return matched
            continue
        if is_array:
            raw_items = list(candidate) if isinstance(candidate, (list, tuple, set)) else [candidate]
            item_types = _declared_types(item_schema) if item_schema else []
            coerced_items = [
                _coerce(item, item_types) if item_types else item for item in raw_items
            ]
            if any(item is UNCOERCIBLE for item in coerced_items):
                continue
            return coerced_items
        coerced = _coerce(candidate, types)
        if coerced is UNCOERCIBLE:
            continue        # this representation does not fit; try the next one
        return coerced
    return None


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------

@dataclass
class BindResult:
    arguments: dict[str, Any]
    used_canonicals: dict[str, str] = field(default_factory=dict)  # canonical -> property
    unmapped_required: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)


def bind_arguments(
    tool: ToolSpec,
    values: Mapping[str, Any],
    *,
    extras: Optional[Mapping[str, Any]] = None,
    strict: bool = True,
) -> dict[str, Any]:
    """Build a ``tools/call`` argument dict for ``tool`` from canonical values.

    ``values`` is keyed by canonical name (see :data:`CANONICAL_ALIASES`);
    ``extras`` is keyed by *literal* upstream property name and always wins, so an
    operator can satisfy an exotic required field via configuration without a
    code change.
    """
    mapping = map_properties(tool)
    properties = tool.properties
    result = BindResult(arguments={})

    for prop, canonical in mapping.items():
        if canonical not in values:
            continue
        value = values[canonical]
        if value is None:
            continue
        bound = bind_value(properties.get(prop, {}), value)
        if bound is None:
            result.ignored.append(prop)
            continue
        result.arguments[prop] = bound
        result.used_canonicals[canonical] = prop

    # Literal overrides / additions (coerced against the schema when known).
    for prop, value in (extras or {}).items():
        if value is None:
            continue
        schema = properties.get(prop)
        result.arguments[prop] = bind_value(schema, value) if schema else value

    missing = [r for r in tool.required if r not in result.arguments]
    if missing and strict:
        detail = {name: properties.get(name, {}) for name in missing}
        raise SchemaBindError(
            f"Tool {tool.name!r} requires {missing} which could not be filled from "
            f"{sorted(values)}. Live schema for the missing fields: {detail}. "
            f"Set the matching value, or supply it literally via extra-fields config."
        )
    result.unmapped_required = missing

    unknown = [c for c in values if c not in result.used_canonicals and values[c] is not None]
    if unknown:
        log.debug("Tool %s has no property for canonicals %s (dropped)", tool.name, unknown)
    return result.arguments


def declared_types(prop_schema: Mapping[str, Any]) -> list[str]:
    """Public accessor for a property's declared JSON types."""
    return _declared_types(prop_schema)


def wants_units(prop_name: str, prop_schema: Mapping[str, Any]) -> Optional[bool]:
    """Does this volume property want *units* (True) or *lots* (False)?

    ``None`` means "cannot tell" - the caller then falls back to configuration.
    Getting this wrong is a 100x position-size error on gold, so the check reads
    both the property name and its description text.
    """
    haystack = f"{prop_name} {prop_schema.get('description', '')} {prop_schema.get('title', '')}".lower()
    if "unit" in haystack or "in units" in haystack:
        return True
    if "lot" in haystack:
        return False
    return None


def describe_tool(tool: ToolSpec) -> str:
    """One-line human summary used in the start-up discovery log."""
    props = []
    required = set(tool.required)
    for name, schema in tool.properties.items():
        types = "|".join(_declared_types(schema)) or "any"
        enum = _enum_values(schema)
        suffix = f"={{{','.join(str(e) for e in enum[:6])}}}" if enum else ""
        props.append(f"{name}:{types}{suffix}{'*' if name in required else ''}")
    return f"{tool.name}({', '.join(props)})"
