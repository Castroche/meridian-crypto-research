"""Small, auditable arithmetic examples; never execute model-provided code."""
from __future__ import annotations

import ast
from decimal import Decimal, InvalidOperation
import re

from .models import ResearchFact, ResearchReport


def register_calculations(report: ResearchReport, calculations: list[dict]) -> None:
    from .editorial import EditorialError, NUMBER, _number_key, _number_variants, _allowed_numbers_for_facts

    # Revisions replace, rather than inherit, the previous draft's computed facts.
    base = [f for f in report.facts if not f.id.startswith("D")]
    report.facts = base
    valid_ids = {f.id for f in base}
    derived = []
    try:
        for index, item in enumerate(calculations, 1):
            if item.get("id") != f"D{index:03d}":
                raise ValueError("计算 ID 必须从 D001 连续编号")
            expression = str(item["expression"])
            if len(expression) > 400:
                raise ValueError("计算表达式过长")
            values, supporting, descriptions = {}, [], []
            hypothetical = False
            for operand in item["inputs"]:
                name, value = str(operand["name"]), str(operand["value"])
                if not re.fullmatch(r"[a-z][a-z_]*", name) or name in values:
                    raise ValueError("计算变量名非法或重复")
                if not re.fullmatch(r"-?\d+(?:\.\d+)?", value) or len(value) > 40:
                    raise ValueError("计算输入必须是有限十进制数")
                values[name] = Decimal(value)
                ids = operand.get("fact_ids", [])
                assumption = operand.get("assumption") is True
                description = str(operand.get("description", "")).strip()
                unit = str(operand.get("unit", "")).strip()
                if not description or not unit:
                    raise ValueError("计算输入缺少口径说明或单位")
                if assumption:
                    if ids:
                        raise ValueError("假设输入不能伪装成有事实引用的观测值")
                    hypothetical = True
                elif not ids or not set(ids).issubset(valid_ids) or _number_key(value) not in _allowed_numbers_for_facts(report, ids):
                    raise ValueError("计算输入没有被其引用事实授权")
                supporting.extend(ids)
                descriptions.append(f"{name}={value} {unit}（{'假设' if assumption else '来源观测'}：{description}）")
            tree = ast.parse(expression, mode="eval")
            if len(list(ast.walk(tree))) > 60:
                raise ValueError("计算表达式过于复杂")

            def evaluate(node):
                if isinstance(node, ast.Expression):
                    return evaluate(node.body)
                if isinstance(node, ast.Name) and node.id in values:
                    return values[node.id]
                if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                    return Decimal(str(node.value))
                if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                    left, right = evaluate(node.left), evaluate(node.right)
                    if isinstance(node.op, ast.Add): return left + right
                    if isinstance(node.op, ast.Sub): return left - right
                    if isinstance(node.op, ast.Mult): return left * right
                    return left / right
                raise ValueError("仅允许已声明变量、括号及加减乘除")

            result = evaluate(tree)
            supplied = str(item["result"])
            if not re.fullmatch(r"-?\d+(?:\.\d+)?", supplied) or _number_key(supplied) not in _number_variants(format(result, "f")):
                raise ValueError("计算结果错误（允许常规显示舍入）")
            explanation = str(item.get("explanation", "")).strip()
            unit = str(item.get("unit", "")).strip()
            if not explanation or not unit:
                raise ValueError("计算缺少解释或结果单位")
            # Explanations may not smuggle additional numeric observations into the ledger.
            allowed = {_number_key(str(v)) for v in values.values()} | _number_variants(supplied)
            if any(_number_key(t) not in allowed for t in NUMBER.findall(explanation)):
                raise ValueError("计算解释包含额外未授权数字")
            derived.append(ResearchFact(
                id=item["id"], category="background", claim_type="INFERENCE",
                statement=f"{'假设演算，非实际观测' if hypothetical else '根据来源计算'}："
                          + "；".join(descriptions) + f"；{expression} = {supplied} {unit}。{explanation}",
                source="Meridian 透明计算", as_of=max((f.as_of for f in base), default=report.as_of),
                supporting_fact_ids=list(dict.fromkeys(supporting)),
            ))
    except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError, SyntaxError) as exc:
        raise EditorialError(f"计算核验失败：{exc}") from exc
    report.facts = base + derived
