"""Unit conversion via pint. Handles SI units, imperial, time, data sizes,
and temperature (which needs pint's quantity syntax to handle offset units
correctly — 0°C is not 0K, so a bare unit multiplication would be wrong)."""

from __future__ import annotations

import logging

from . import Tool, ToolContext, register

log = logging.getLogger(__name__)


# Pint's UnitRegistry loads ~thousands of unit definitions on construction.
# Build it once at module-import time and reuse across calls; the bot's
# process lifetime amortises the cost.
try:
    import pint
    _UREG = pint.UnitRegistry()
    # Suppress pint's deprecation warnings about default formatting — they
    # clutter the bot's log on every conversion call.
    _UREG.default_format = "~P"  # short, pretty: "10 km", "3.14 °C"
    _PINT_AVAILABLE = True
    _PINT_ERROR: str | None = None
except Exception as e:  # pragma: no cover — only fires if pint missing
    _UREG = None
    _PINT_AVAILABLE = False
    _PINT_ERROR = str(e)
    log.warning("pint not available; unit_convert will be disabled: %s", e)


def _convert(value: float, from_unit: str, to_unit: str) -> tuple[float, str]:
    """Run a single conversion. Returns (result_value, pretty_string).
    Uses pint's Quantity API so offset units (Celsius, Fahrenheit) convert
    correctly — `(value * unit).to(other)` would silently mis-handle them
    because '20 °C' is not literally '20 * °C' in absolute terms."""
    assert _UREG is not None  # gate at call site
    qty = _UREG.Quantity(value, from_unit)
    converted = qty.to(to_unit)
    # `.magnitude` gives the bare number; the unit-aware __str__ formats with the unit.
    return float(converted.magnitude), f"{qty:~P} = {converted:~P}"


async def _unit_convert(ctx: ToolContext, args: dict) -> dict:
    if not _PINT_AVAILABLE:
        return {"error": f"unit conversion unavailable: {_PINT_ERROR}"}

    try:
        value = float(args.get("value"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {"error": "value must be a number"}
    from_unit = (args.get("from_unit") or "").strip()
    to_unit = (args.get("to_unit") or "").strip()
    if not from_unit or not to_unit:
        return {"error": "from_unit and to_unit are both required"}

    try:
        result, formatted = _convert(value, from_unit, to_unit)
    except pint.UndefinedUnitError as e:  # type: ignore[union-attr]
        # Most common error path: the model picks a unit name pint doesn't
        # know ('mph' vs 'mile/hour'). The model can re-try with a different
        # spelling rather than giving up.
        return {"error": f"unknown unit: {e}. Try 'mile/hour', 'meter', 'celsius', etc."}
    except pint.DimensionalityError as e:  # type: ignore[union-attr]
        # Categorical error: trying to convert length to mass, etc. Worth
        # surfacing so the model can tell the user instead of guessing.
        return {"error": f"incompatible units: {e}"}
    except Exception as e:
        log.exception("unit_convert internal error")
        return {"error": f"conversion failed: {e}"}

    return {
        "value": value,
        "from_unit": from_unit,
        "to_unit": to_unit,
        "result": result,
        "formatted": formatted,
    }


register(Tool(
    name="unit_convert",
    description=(
        "Convert a numeric value between units of the same dimension. Handles "
        "length (meter, mile, foot), mass (kg, lb), volume (liter, gallon), "
        "time (second, day, year), data (byte, gigabyte), temperature "
        "(celsius, fahrenheit, kelvin), and many more. Use unit names spelled "
        "out (e.g. 'meter', 'mile/hour'), or common abbreviations ('m', 'mph'). "
        "Returns an error for incompatible dimensions (you can't convert "
        "length to mass)."
    ),
    schema={
        "type": "object",
        "properties": {
            "value": {
                "type": "number",
                "description": "Numeric value to convert.",
            },
            "from_unit": {
                "type": "string",
                "description": "Source unit (e.g. 'mile', 'celsius', 'kg/m^3').",
            },
            "to_unit": {
                "type": "string",
                "description": "Target unit (e.g. 'km', 'fahrenheit', 'lb/ft^3').",
            },
        },
        "required": ["value", "from_unit", "to_unit"],
        "additionalProperties": False,
    },
    requires=set(),
    call=_unit_convert,
))
