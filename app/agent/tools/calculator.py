"""安全计算器工具：用 AST 白名单求值，不使用 eval。"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

from langchain_core.tools import tool

_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}
_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "floor": math.floor,
    "ceil": math.ceil,
    "factorial": math.factorial,
}


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"不支持的常量：{node.value!r}")
        return node.value
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符：{type(node.op).__name__}")
        return op(_evaluate(node.left), _evaluate(node.right))
    if isinstance(node, ast.UnaryOp):
        unary = _UNARY_OPS.get(type(node.op))
        if unary is None:
            raise ValueError(f"不支持的一元运算符：{type(node.op).__name__}")
        return unary(_evaluate(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ValueError(f"未知标识符：{node.id}")
    if isinstance(node, ast.Call):
        func_name = getattr(node.func, "id", None)
        func = _FUNCTIONS.get(func_name or "")
        if func is None:
            raise ValueError(f"不支持的函数：{func_name}")
        if node.keywords:
            raise ValueError("不支持关键字参数")
        return func(*[_evaluate(arg) for arg in node.args])
    raise ValueError(f"不支持的语法节点：{type(node).__name__}")


def _format_number(value: float) -> str:
    if isinstance(value, int):
        return str(value)
    if float(value).is_integer() and abs(value) < 1e16:
        return str(int(value))
    return f"{value:.12g}"


@tool
def calculator(expression: str) -> str:
    """执行精确的数学计算并返回结果。

    支持 + - * / // % **、括号、以及 sqrt/log/exp/sin/cos/tan/floor/ceil/factorial 等函数，
    常量支持 pi、e。不要用它做单位换算之外的自然语言推理。

    Args:
        expression: 需要计算的数学表达式，例如 "(3+4)*2.5" 或 "sqrt(144)+log(100, 10)"。
    """
    normalized = expression.strip().replace("×", "*").replace("÷", "/").replace("^", "**")
    if not normalized:
        return "计算失败：表达式为空"
    if len(normalized) > 500:
        return "计算失败：表达式过长"
    try:
        tree = ast.parse(normalized, mode="eval")
        result = _evaluate(tree)
    except ZeroDivisionError:
        return "计算失败：除数不能为 0"
    except ValueError as exc:
        return f"计算失败：{exc}"
    except SyntaxError:
        return f"计算失败：表达式语法错误 -> {expression}"
    except OverflowError:
        return "计算失败：结果超出可表示范围"
    return f"{normalized} = {_format_number(result)}"
