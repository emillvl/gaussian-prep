"""Small mathematical expression language. Never eval/sympify model-supplied text."""
from __future__ import annotations

import ast
from itertools import product
from math import isfinite
import re

import sympy as sp


class MathUnsupported(ValueError):
    pass


_LATEX_COMMANDS = {"\\cdot": "*", "\\times": "*", "\\div": "/", "\\pi": "pi", "\\left": "", "\\right": "", "\\displaystyle": "", "\\,": "", "\\!": "", "\\;": "", "\\:": "", "\\ ": " "}
_UNICODE_MATH = {"\u2212": "-", "\u00d7": "*", "\u00f7": "/", "\u03c0": "pi", "\u221a": "sqrt", "\u00b2": "**2", "\u00b3": "**3", "\u00b9": "**1", "\u00b7": "*", "\u2044": "/", "\u2215": "/", "\u00a0": " ", "\u2009": " ", "\u202f": " "}


def to_engine_expression(text: str) -> str:
    """Convert model output (LaTeX or unicode math) into the bounded expression language."""
    if not text:
        return text
    value = re.sub(r"\\\(|\\\)|\\\[|\\\]", "", text.strip()).replace("$", "")
    for _ in range(4):
        value = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", value)
        value = re.sub(r"\\sqrt\[([^\]]*)\]\{([^{}]*)\}", r"((\2)**(1/(\1)))", value)
        value = re.sub(r"\\sqrt\{([^{}]*)\}", r"sqrt(\1)", value)
    value = re.sub(r"\^\{([^{}]*)\}", r"**(\1)", value)
    value = re.sub(r"\^([0-9A-Za-z.]+)", r"**\1", value)
    value = re.sub(r"\\(?:mathrm|text|operatorname|mbox)\{([^{}]*)\}", r"\1", value)
    for command, replacement in _LATEX_COMMANDS.items():
        value = value.replace(command, replacement)
    for character, replacement in _UNICODE_MATH.items():
        value = value.replace(character, replacement)
    value = value.replace("\\%", "/100").replace("%", "/100")
    if "[" not in value:
        value = re.sub(r"(?<=\d),(?=\d)", "", value)
    return value


class ExpressionReader:
    def __init__(self, variables: list[str]):
        if len(variables) != len(set(variables)) or any(not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]{0,15}", v) for v in variables):
            raise MathUnsupported("Variables must be distinct simple names")
        reserved = {"pi", "sqrt", "abs", "sin", "cos", "tan", "log", "exp", "mean", "median", "stdev", "sum"}
        if reserved.intersection(variables):
            raise MathUnsupported("A variable shadows a mathematical function or constant")
        self.symbols = {name: sp.Symbol(name, real=True) for name in variables}
        self.nonzero: list[sp.Expr] = []
        self.conditions: list = []

    def read(self, expression: str) -> sp.Expr:
        if not expression or len(expression) > 2000:
            raise MathUnsupported("Expression is empty or too long")
        expression = to_engine_expression(expression)
        try:
            tree = ast.parse(expression.replace("^", "**"), mode="eval")
        except (SyntaxError, RecursionError) as exc:
            raise MathUnsupported("Use explicit operators, e.g. 2*x and x**2") from exc
        if sum(1 for _ in ast.walk(tree)) > 250:
            raise MathUnsupported("Expression is too complex")
        value = self._node(tree.body)
        if isinstance(value, list):
            raise MathUnsupported("Expected one expression, not a list")
        if value.has(sp.zoo, sp.nan, sp.oo, -sp.oo):
            raise MathUnsupported("Undefined or infinite expression")
        return value

    def _node(self, node):
        value = self._build(node)
        if isinstance(value, sp.Rational) and (int(value.p).bit_length() > 4096 or int(value.q).bit_length() > 4096):
            raise MathUnsupported("Intermediate numeric value exceeds the computation limit")
        return value

    def _build(self, node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if abs(node.value) > 10**15:
                raise MathUnsupported("Numeric literal exceeds the computation limit")
            return sp.Rational(str(node.value))
        if isinstance(node, ast.Name):
            if node.id == "pi":
                return sp.pi
            if node.id in self.symbols:
                return self.symbols[node.id]
            raise MathUnsupported(f"Unknown mathematical name: {node.id}")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self._node(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = self._node(node.left), self._node(node.right)
            if isinstance(left, list) or isinstance(right, list):
                raise MathUnsupported("Lists require a statistical function")
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                self.nonzero.append(right)
                self.conditions.append(sp.Ne(right, 0))
                return left / right
            if isinstance(node.op, ast.Pow):
                if right.is_number and (right.is_real is not True or abs(right) > 20):
                    raise MathUnsupported("Exponent exceeds the computation limit")
                if right.is_negative:
                    self.nonzero.append(left)
                    self.conditions.append(sp.Ne(left, 0))
                if isinstance(right, sp.Rational) and right.q % 2 == 0:
                    self.conditions.append(sp.Ge(left, 0))
                return left**right
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) <= 100:
            values = [self._node(n) for n in node.elts]
            if not values or any(isinstance(v, list) for v in values):
                raise MathUnsupported("Expected a nonempty flat list")
            return values
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            args = [self._node(arg) for arg in node.args]
            functions = {"sqrt": sp.sqrt, "abs": sp.Abs, "sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "log": sp.log, "exp": sp.exp}
            if name in functions and len(args) == 1 and not isinstance(args[0], list):
                if name == "sqrt":
                    self.conditions.append(sp.Ge(args[0], 0))
                elif name == "log":
                    self.conditions.append(sp.Gt(args[0], 0))
                elif name == "tan":
                    self.conditions.append(sp.Ne(sp.cos(args[0]), 0))
                return functions[name](args[0])
            if name in {"mean", "median", "stdev", "sum"} and len(args) == 1 and isinstance(args[0], list):
                values = args[0]
                mean = sum(values) / len(values)
                if name == "mean":
                    return mean
                if name == "sum":
                    return sum(values)
                if name == "stdev":
                    return sp.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
                if any(v.is_number is not True or v.is_real is not True for v in values):
                    raise MathUnsupported("Median requires real numeric values")
                values = sorted(values, key=lambda v: float(v))
                middle = len(values) // 2
                return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
        raise MathUnsupported("Unsupported syntax; only mathematical expressions are allowed")


def equal(left: sp.Expr, right: sp.Expr) -> bool:
    difference = sp.simplify(left - right)
    if difference == 0:
        return True
    if difference.is_zero is False:
        return False
    if difference.free_symbols and difference.is_polynomial(*difference.free_symbols):
        return False
    raise MathUnsupported("Symbolic equality was inconclusive")


def _numeric_equivalent(first: sp.Expr, second: sp.Expr, symbols: list[sp.Symbol], conditions=()) -> bool | None:
    # Samples can DISPROVE an identity, never prove one. Vary variables independently.
    values = (sp.Integer(1), sp.Integer(2), sp.Rational(1, 2), sp.Integer(3), sp.Integer(-1), sp.Integer(0), sp.Integer(-2))
    for sample in product(values, repeat=len(symbols)):
        substitution = dict(zip(symbols, sample))
        try:
            if any(c.subs(substitution) != sp.true for c in conditions):
                continue
            left, right = complex(first.subs(substitution)), complex(second.subs(substitution))
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if not all(isfinite(part) for part in (left.real, left.imag, right.real, right.imag)):
            continue
        if abs(left.imag) > 1e-10 or abs(right.imag) > 1e-10:
            continue
        if abs(left - right) > 1e-9 * max(1.0, abs(left), abs(right)):
            return False
    return None


def equivalent(first: sp.Expr, second: sp.Expr, symbols: list[sp.Symbol], conditions=()) -> bool | None:
    """True/False when two expressions agree, None when the comparison is inconclusive."""
    difference = sp.simplify(first - second)
    if difference == 0:
        return True
    if not difference.free_symbols:
        return difference.is_zero
    return _numeric_equivalent(first, second, symbols, conditions)
