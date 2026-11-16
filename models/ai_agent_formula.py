"""Safe evaluation of the small formulas the AI writes for the calculate tool.

The AI never sees the values: it writes a formula over named inputs ("won / (won + lost) * 100") and Odoo computes
it here. Nothing is passed to Python's eval: the formula is parsed and only a whitelist of node types is interpreted
(numbers, text, arithmetic, comparisons, and/or/not, if-else and a few functions).
"""

import ast
import datetime
import math

MAX_FORMULA_LENGTH = 300
MAX_POWER = 10


class FormulaError(ValueError):
    """The formula is not allowed or cannot be read."""


def _date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return datetime.date.fromisoformat(value[:10])
        except ValueError:
            pass
    raise FormulaError(f"{value!r} is not a date")


def _days(later, earlier):
    """Whole days from earlier to later; dates or 'YYYY-MM-DD...' text."""
    return (_date(later) - _date(earlier)).days


def _numbers(values):
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _avg(values):
    numbers = _numbers(values if isinstance(values, (list, tuple)) else [values])
    return sum(numbers) / len(numbers) if numbers else None


FUNCTIONS = {
    "min": lambda *a: min(_numbers(a[0] if len(a) == 1 and isinstance(a[0], list) else a)),
    "max": lambda *a: max(_numbers(a[0] if len(a) == 1 and isinstance(a[0], list) else a)),
    "sum": lambda values: sum(_numbers(values if isinstance(values, list) else [values])),
    "avg": _avg,
    "len": lambda values: len(values) if isinstance(values, list) else 1,
    "abs": abs,
    "round": lambda value, digits=0: round(value, int(digits)),
    "days": _days,
}

_BINARY = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}
_COMPARE = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def parse(formula, names):
    """Check the formula once: only known names, functions and operators. Returns the parsed tree."""
    if not isinstance(formula, str) or not formula.strip():
        raise FormulaError("The formula is empty.")
    if len(formula) > MAX_FORMULA_LENGTH:
        raise FormulaError(f"The formula is longer than {MAX_FORMULA_LENGTH} characters.")
    try:
        tree = ast.parse(formula.strip(), mode="eval")
    except SyntaxError as err:
        raise FormulaError(f"The formula is not valid: {err.msg}.") from err
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Name,
               ast.Constant, ast.Load, ast.List, ast.Tuple, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
               *_BINARY, *_COMPARE)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise FormulaError(f"'{type(node).__name__}' is not allowed in a formula.")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                raise FormulaError("Only these functions are allowed: " + ", ".join(sorted(FUNCTIONS)) + ".")
        elif isinstance(node, ast.Name) and node.id not in names and node.id not in FUNCTIONS and node.id != "today":
            raise FormulaError(f"Unknown name '{node.id}'. Use only the inputs: " + ", ".join(sorted(names)) + ".")
        elif isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, str, bool, type(None))):
            raise FormulaError("Only numbers, text, True, False and None are allowed as constants.")
    return tree


def evaluate(tree, values, today=None):
    """Evaluate a parsed formula. Empty inputs, division by zero and type errors give None (an empty cell)."""
    scope = dict(values, today=today or datetime.date.today())

    def run(node):
        if isinstance(node, ast.Expression):
            return run(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return scope.get(node.id)
        if isinstance(node, (ast.List, ast.Tuple)):
            return [run(item) for item in node.elts]
        if isinstance(node, ast.UnaryOp):
            operand = run(node.operand)
            if isinstance(node.op, ast.Not):
                return not operand
            if operand is None:
                return None
            return -operand if isinstance(node.op, ast.USub) else +operand
        if isinstance(node, ast.BoolOp):
            result = None
            for item in node.values:
                result = run(item)
                if isinstance(node.op, ast.And) and not result:
                    return result
                if isinstance(node.op, ast.Or) and result:
                    return result
            return result
        if isinstance(node, ast.BinOp):
            left, right = run(node.left), run(node.right)
            if left is None or right is None:
                return None
            numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (left, right))
            texts = isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str) \
                and len(left) + len(right) <= 1000
            if not (numbers or texts):
                return None  # arithmetic only on numbers (no 'a' * 10**9)
            if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > MAX_POWER:
                raise FormulaError(f"Powers above {MAX_POWER} are not allowed.")
            return _BINARY[type(node.op)](left, right)
        if isinstance(node, ast.Compare):
            left = run(node.left)
            for operator, comparator in zip(node.ops, node.comparators):
                right = run(comparator)
                if left is None or right is None:
                    return None
                if not _COMPARE[type(operator)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return run(node.body) if run(node.test) else run(node.orelse)
        if isinstance(node, ast.Call):
            arguments = [run(argument) for argument in node.args]
            if any(argument is None for argument in arguments):
                return None
            try:
                return FUNCTIONS[node.func.id](*arguments)
            except FormulaError:
                return None  # e.g. days() on a row whose date is empty
        raise FormulaError(f"'{type(node).__name__}' is not allowed in a formula.")

    try:
        result = run(tree)
    except FormulaError:
        raise
    except (ZeroDivisionError, TypeError, ValueError, OverflowError, AttributeError):
        return None
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        return None
    return result
