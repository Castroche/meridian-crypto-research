"""Coherent draft -> source-aware editorial review -> bounded revision -> publish gate."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time
import re

import httpx

from .article_policy import (
    EDITORIAL_VERSION, LENGTH_GUIDANCE, WRITING_STANDARD, REVIEW_CRITERIA,
    article_blocks, article_fingerprint, packet_readiness, publication_issues, review_issues, repetition_issues,
)
from .models import EditorialAudit, InsightDraftFragment


def fragment_specs(packet):
    # Content addressing prevents reuse of a same-day packet after source changes.
    material = packet.model_dump(mode="json", exclude={"status", "error", "warnings", "updated_at"})
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return [{"fragment_key": f"article-{EDITORIAL_VERSION}-{digest}", "kind": "article",
             "position": 0, "headings": [], "target_chars": 4500}]


def _context(report, packet):
    return {
        "topic": packet.topic, "question": packet.research_question,
        "provisional_angle": packet.thesis_angle,
        "verified_news_brief": packet.news_brief,
        "facts": [f.model_dump(mode="json") for f in report.facts if not f.id.startswith("D")],
        "sources": [{**s.model_dump(mode="json"), "text": packet.source_texts.get(s.id, "")}
                    for s in packet.sources if s.access != "title"],
        "material_boundaries": packet.counter_evidence,
    }


def _schema(question):
    return {
        "narrative_plan": {"central_question": question, "opening_detail": "材料中的具体切口",
                           "progression": ["每一步给读者什么新信息、为何接在前一步后面"],
                           "ending": "开头的问题如何得到回答", "omitted_material": ["无助于主线的材料"]},
        "title": "具体、有吸引力且有证据支撑的中文标题", "research_question": question,
        "deck": "可选简短副标题，避免提前重复正文", "lead": "具体导语", "lead_fact_ids": [],
        "consensus_view": "", "consensus_fact_ids": [], "meridian_view": "", "meridian_fact_ids": [],
        "sections": [{"heading": "自然推进的章节标题", "paragraphs": [{"text": "段落", "fact_ids": []}]}],
        "conclusion": "自然收束，回应问题", "conclusion_fact_ids": [],
        "calculations": [],
    }


CALCULATION_GUIDANCE = """
事实只来自材料；直接观察的数字必须被本段 fact_ids 支持，不能借相同数字冒接别的引用。
允许透明演算，需在 calculations 中声明：
{"id":"D001","inputs":[{"name":"price","value":"0.30","unit":"美元/百万token",
"fact_ids":["I001"],"assumption":false,"description":"来源的单位价格"},
{"name":"volume","value":"10","unit":"百万token","fact_ids":[],"assumption":true,
"description":"示例输入量，非实际用量"}],"expression":"price * volume","result":"3",
"unit":"美元","explanation":"仅示例输入费用，未包含输出费用"}。
示例数字不是事实授权，实际输入需来自当前材料或明确标注的假设。变量只支持加减乘除，
公式中的每个变量必须在 inputs 声明；结果由程序重算。正文必须交代输入、口径、
计算关系及关键限制，引用 D001。含假设的演算必须在正文称为“假设”或“示例”，
不得写成真实观测。不要为了举例额外加入与问题无关的数字。
""".strip()


async def _completion(client, settings, system, payload, max_tokens):
    from .editorial import EditorialError, _stream_completion
    body = {
        "model": settings.article_editor_model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled" if "blocks" in payload else "enabled"},
        "reasoning_effort": "high",
        "max_tokens": max_tokens, "stream": True, "temperature": 0.4,
        "stream_options": {"include_usage": True},
    }
    async with asyncio.timeout(settings.editor_timeout):
        content, usage = await _stream_completion(
            client, f"{settings.deepseek_base_url}/chat/completions",
            {"Authorization": f"Bearer {settings.deepseek_api_key}", "Content-Type": "application/json"}, body,
        )
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise EditorialError("编辑返回的 JSON 不是对象")
    return payload, usage


async def review_candidate(client, settings, title, deck, context, blocks):
    from .editorial import EditorialError
    paths = list(blocks)
    async def review_batch(group):
        batch = {key:blocks[key] for key in group}
        request = {"material":context, "title":title, "deck":deck, "blocks":batch,
                   "expected":{"checked_paths":group, "factual_errors":[], "editorial_notes":[]}}
        for attempt in range(2):
            result, usage = await _completion(client, settings,
                "你是独立事实编辑。只返回 expected 中的三个 JSON 字段。逐段对照原始材料、引用、"
                "标题承诺与时间口径。核对事件日期、最新进展、增量发现和利益关系。旧事冒充新闻列为事实错误。checked_paths 原样返回本批 blocks 的全部键名。"
                "factual_errors 数组列出不受材料支持的主张、因果跳跃、错引与伪造采访。"
                "editorial_notes 数组列出可改善的文风问题。无问题对应数组为空。",
                request, 3200)
            checked = result.get("checked_paths", [])
            errors, notes = result.get("factual_errors"), result.get("editorial_notes")
            if isinstance(checked,list) and set(str(x) for x in checked)==set(group) and isinstance(errors,list) and isinstance(notes,list):
                return result, usage
            request["repair"] = "审稿 JSON 不合约，必须逐字使用 expected.checked_paths 的路径。"
        raise EditorialError(f"独立审稿未覆盖段落 {group}")
    batches = await asyncio.gather(*(review_batch(paths[i:i+4]) for i in range(0,len(paths),4)))
    review = {key:[v for r,_ in batches for v in r[key]] for key in ("checked_paths","factual_errors","editorial_notes")}
    tokens = {k:sum(int(u.get(k) or 0) for _,u in batches) for k in ("prompt_tokens","completion_tokens")}
    return review,tokens


async def write_fragment(report, packet, spec, settings, short=False, client=None):
    from .editorial import EditorialError, InsightGenerationError, _validate_insight
    if not settings.editor_enabled or not settings.deepseek_api_key:
        raise InsightGenerationError("写作模型未启用；深度文章不发布模板保底稿")
    from .article_policy import newsworthiness_issues
    problems = packet_readiness(packet) + newsworthiness_issues(packet, packet.research_date)
    if problems:
        raise InsightGenerationError("需要补充研究材料：" + "；".join(problems))
    if spec["fragment_key"] != fragment_specs(packet)[0]["fragment_key"]:
        raise InsightGenerationError("草稿与当前资料包版本不一致")
    started = time.perf_counter()
    owned = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(settings.editor_timeout, connect=20))
    context = _context(report, packet)
    previous = spec.get("previous_draft")
    feedback = spec.get("editor_feedback") or spec.get("repair_reason", "")
    input_tokens = output_tokens = 0
    try:
        # One whole-article revision, never padding fragments or template fallbacks.
        for attempt in range(1, 3):
            if attempt == 1 and spec.get("review_previous") and previous:
                payload, usage = previous, {}
            else:
                payload, usage = await _completion(
                    client, settings, WRITING_STANDARD + "\n" + (CALCULATION_GUIDANCE if "演算" in packet.research_question else "本选题只使用原文直接支持的事实。省略容量、价格的额外换算，不做示例演算。calculations 必须为空。") +
                    "\n输出严格 JSON。先在 narrative_plan 确定主线，再连贯写完整篇。"
                    "consensus_view 和 meridian_view 留空，把必要观点自然放在正文。"
                    "fact_ids 只放引用数组，不写进 prose。仅过渡/解释且不含新事实的段落允许空引用。",
                    {"task": LENGTH_GUIDANCE, "material": context, "expected_json": _schema(packet.research_question),
                     "previous_draft": previous, "editor_feedback": feedback,
                     "revision_rule": "若有前稿，根据反馈整篇修订；不要只加免责声明或换词重复。"},
                    settings.insight_max_tokens,
                )
            input_tokens += int(usage.get("prompt_tokens") or 0)
            output_tokens += int(usage.get("completion_tokens") or 0)
            previous = payload
            try:
                plan = payload.get("narrative_plan")
                if not isinstance(plan, dict) or not plan.get("central_question") or not plan.get("progression"):
                    raise EditorialError("缺少围绕中心问题的叙事推进计划")
                candidate = report.model_copy(deep=True)
                candidate.title, candidate.article, candidate.insight_status = _validate_insight(candidate, payload, False)
                blocks = {path: {"text": text, "fact_ids": ids}
                          for path, (text, ids) in article_blocks(candidate.article).items()}
                for block in blocks.values():
                    if re.search(r"（\s*）|(?<![A-Za-z_0-9])\(\s*\)", block["text"]):
                        raise EditorialError("删除空括号并核对演算")
                duplicate = repetition_issues(candidate.article)
                if duplicate:
                    raise EditorialError(duplicate[0])
                review, review_usage = await review_candidate(client,settings,candidate.title,
                    candidate.article.deck,context,blocks)
                input_tokens += int(review_usage.get("prompt_tokens") or 0)
                output_tokens += int(review_usage.get("completion_tokens") or 0)
                errors = review.get("factual_errors")
                notes = review.get("editorial_notes")
                if (not isinstance(errors, list) or not isinstance(notes, list)
                        or set(review.get("checked_paths", [])) != set(blocks)):
                    raise EditorialError("审稿没有覆盖全文")
                if errors or (notes and attempt == 1):
                    raise EditorialError(json.dumps(errors or notes, ensure_ascii=False))
                review.update(decision="publish", fingerprint=article_fingerprint(candidate))
                return {"article": payload, "model": settings.article_editor_model, "review": review, "editorial_version": EDITORIAL_VERSION}, {
                    "attempts": attempt, "latency_ms": round((time.perf_counter() - started) * 1000),
                    "input_tokens": input_tokens, "output_tokens": output_tokens,
                }
            except EditorialError as exc:
                feedback = str(exc)
        raise InsightGenerationError("整篇文章生成未完成：" + json.dumps(feedback, ensure_ascii=False)[:900], draft=previous)
    except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise InsightGenerationError(f"写作/审稿未完成：{type(exc).__name__}: {str(exc)[:700]}") from exc
    finally:
        if owned:
            await client.aclose()


def assemble(report, fragments, latency_ms=None):
    from .editorial import EditorialError, _validate_insight
    from .reporting import render_insight
    complete = [f for f in fragments if f.status == "complete" and f.kind == "article"]
    if len(complete) != 1:
        raise EditorialError("需要一篇经过审稿的完整草稿；旧版独立分段不可复用")
    fragment = complete[0]
    if fragment.payload.get("editorial_version") != EDITORIAL_VERSION:
        raise EditorialError("草稿编辑版本已过期")
    report.title, report.article, report.insight_status = _validate_insight(report, fragment.payload["article"], False)
    report.executive_judgement = report.article.deck or report.article.lead
    if report.deep_research is None:
        raise EditorialError("缺少深度文章元数据")
    report.deep_research.editorial_version = EDITORIAL_VERSION
    report.deep_research.editorial_review = fragment.payload.get("review", {})
    report.deep_research.critique = None  # No fabricated numerical quality score.
    problems = publication_issues(report)
    if problems:
        raise EditorialError("；".join(problems))
    report.editorial_audit = EditorialAudit(
        provider="deepseek", model=fragment.payload.get("model") or report.editorial_audit.model or "deepseek",
        status="model", attempts=fragment.attempts, latency_ms=latency_ms or fragment.latency_ms,
        input_tokens=fragment.input_tokens, output_tokens=fragment.output_tokens,
    )
    report.markdown = render_insight(report)
    return report


async def write_article(report, packet, settings, client=None):
    spec = fragment_specs(packet)[0]
    payload, metrics = await write_fragment(report, packet, spec, settings, client=client)
    now = datetime.now(timezone.utc)
    fragment = InsightDraftFragment(
        id=spec["fragment_key"], research_date=packet.research_date, packet_id=packet.id,
        fragment_key=spec["fragment_key"], kind="article", position=0, status="complete",
        payload=payload, created_at=now, updated_at=now, **metrics,
    )
    report.editorial_audit.model = settings.article_editor_model
    return assemble(report, [fragment])
