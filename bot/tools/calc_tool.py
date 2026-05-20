"""Safe arithmetic computation via AST walking. Does not invoke Python's
built-in code evaluator at any point; the AST is parsed and traversed manually,
allowing only arithmetic ops and a small allow-list of math functions."""

from __future__ import annotations

import ast
import logging
import operator as op

from . import Tool, ToolContext, register

log = logging.getLogger(__name__)


_BIN_OPS = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv,
    ast.Mod: op.mod,
    ast.Pow: op.pow,
}
_UNARY_OPS = {ast.UAdd: op.pos, ast.USub: op.neg}
_FUNCS = {"abs": abs, "round": round, "min": min, "max": max}


def _walk(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError("only numbers allowed")
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BIN_OPS:
            raise ValueError(f"operator not allowed: {op_type.__name__}")
        return _BIN_OPS[op_type](_walk(node.left), _walk(node.right))
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARY_OPS:
            raise ValueError(f"operator not allowed: {op_type.__name__}")
        return _UNARY_OPS[op_type](_walk(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id not in _FUNCS:
            raise ValueError(f"function not allowed: {node.func.id}")
        return _FUNCS[node.func.id](*[_walk(a) for a in node.args])
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def _compute(expr: str) -> float:
    tree = ast.parse(expr)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr):
        raise ValueError("expected a single expression")
    return _walk(tree.body[0].value)


async def _calc(ctx: ToolContext, args: dict) -> dict:
    expr = args["expression"]
    if len(expr) > 200:
        return {"error": "expression too long"}
    try:
        value = _compute(expr)
    except SyntaxError as e:
        return {"error": f"syntax error: {e.msg}"}
    except ValueError as e:
        return {"error": str(e)}
    except ZeroDivisionError:
        return {"error": "division by zero"}
    except OverflowError:
        return {"error": "result too large"}
    return {"expression": expr, "result": str(value)}


register(Tool(
    name="calc",
    description="Compute the result of an arithmetic expression. Supports + - * / // ** %, parentheses, unary minus, and abs/round/min/max.",
    schema={
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "Arithmetic expression, e.g. '2 + 3 * (4 - 1)'."},
        },
        "required": ["expression"],
        "additionalProperties": False,
    },
    requires=set(),
    call=_calc,
))
