from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re
import time
from typing import Any

import httpx
from pydantic import ValidationError

from .config import Settings
from .models import (
    ArticleNarrative, ArticleParagraph, ArticleSection, EditorialAudit, InsightDraftFragment,
    ResearchCritique, ResearchPacket, ResearchReport,
)
from .reporting import render_article, render_insight, render_markdown


NUMBER = re.compile(
    r"(?<!\d)[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:%|\s*(?:H|h|小时))?"
)
INLINE_FACT_ID = re.compile(r"\[?(?<![A-Z])(?:F|I|R|D)\d{3}(?!\d)\]?")
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


class EditorialError(ValueError):
    pass


class InsightGenerationError(RuntimeError):
    def __init__(self, message: str, *, draft=None, review=None):
        super().__init__(message)
        self.draft = draft
        self.review = review


def _fact_payload(report: ResearchReport) -> list[dict[str, str]]:
    return [
        {"id": fact.id, "category": fact.category, "statement": fact.statement, "source": fact.source, "url": fact.url, "as_of": fact.as_of.isoformat()}
        for fact in report.facts
    ]


def _number_key(token: str) -> str:
    cleaned = token.replace(",", "").replace(" ", "")
    if cleaned.endswith("小时"):
        unit, value = "h", cleaned[:-2]
    else:
        unit = cleaned[-1].lower() if cleaned and cleaned[-1] in "%Hh" else ""
        value = cleaned[:-1] if unit else cleaned
    try:
        normalized = format(Decimal(value).normalize(), "f")
    except InvalidOperation:
        normalized = value.lstrip("+")
    return normalized + unit


def _number_variants(token: str) -> set[str]:
    """Return the ledger value plus conventional display-rounding variants."""
    exact = _number_key(token)
    unit = exact[-1] if exact.endswith(("%", "h")) else ""
    value = exact[:-1] if unit else exact
    variants = {exact}
    try:
        decimal_value = Decimal(value)
    except InvalidOperation:
        return variants
    for places in range(5):
        quantum = Decimal(1).scaleb(-places)
        rounded = decimal_value.quantize(quantum, rounding=ROUND_HALF_UP)
        variants.add(_number_key(f"{rounded}{unit}"))
    return variants


def _allowed_numbers(report: ResearchReport) -> set[str]:
    source = " ".join(f"{fact.statement} {fact.as_of.isoformat()}" for fact in report.facts)
    if report.report_type != "insight":
        source += " " + report.markdown + " 7 24 30 90 100"
    allowed = {variant for item in NUMBER.findall(source) for variant in _number_variants(item)}
    for fact in report.facts:
        words = set(re.findall(r"[A-Za-z]+", fact.statement.lower()))
        allowed.update(_number_key(str(value)) for word, value in MONTHS.items() if word in words)
    return allowed


def _allowed_numbers_for_facts(report: ResearchReport, fact_ids: list[str]) -> set[str]:
    fact_by_id = {fact.id: fact for fact in report.facts}
    allowed: set[str] = set()
    for fact_id in fact_ids:
        fact = fact_by_id.get(fact_id)
        if not fact:
            continue
        allowed.update(variant for item in NUMBER.findall(fact.statement) for variant in _number_variants(item))
        allowed.update({_number_key(str(fact.as_of.year)), _number_key(str(fact.as_of.month)), _number_key(str(fact.as_of.day))})
        words = set(re.findall(r"[A-Za-z]+", fact.statement.lower()))
        allowed.update(_number_key(str(value)) for word, value in MONTHS.items() if word in words)
    return allowed


def _assert_numbers_with_fact_ids(report: ResearchReport, text: str, fact_ids: list[str]) -> None:
    allowed = _allowed_numbers_for_facts(report, fact_ids)
    unsupported = [token for token in NUMBER.findall(text) if _number_key(token) not in allowed]
    if unsupported:
        raise EditorialError("段落数字没有被其 fact_ids 授权：" + "、".join(sorted(set(unsupported))))


def _authorize_numeric_fact_ids(report: ResearchReport, text: str, fact_ids: list[str]) -> list[str]:
    """Attach the exact facts that authorize numbers already present in the ledger.

    Models occasionally cite the right ledger number but return the wrong fact ID.
    Repair that mechanical mismatch without allowing any number absent from facts.
    """
    valid_ids = {fact.id for fact in report.facts}
    authorized = list(dict.fromkeys(fact_id for fact_id in fact_ids if fact_id in valid_ids))
    allowed = _allowed_numbers_for_facts(report, authorized)
    missing = {
        _number_key(token) for token in NUMBER.findall(text)
        if _number_key(token) not in allowed
    }
    if missing:
        for fact in report.facts:
            fact_numbers = _allowed_numbers_for_facts(report, [fact.id])
            if missing & fact_numbers:
                authorized.append(fact.id)
                allowed.update(fact_numbers)
                missing -= fact_numbers
            if not missing:
                break
    _assert_numbers_with_fact_ids(report, text, authorized)
    return list(dict.fromkeys(authorized))


def _assert_numbers(report: ResearchReport, texts: list[str]) -> None:
    allowed = _allowed_numbers(report)
    unsupported: dict[str, str] = {}
    for text in texts:
        for match in NUMBER.finditer(text):
            token = match.group(0)
            prefix = text[max(0, match.start()-6):match.start()]
            directional = not token.startswith(("-", "+")) and re.search(r"(下跌|回落|下降|跌幅|下行|降|减少|收缩|减|跌)\s*$", prefix)
            if _number_key(token) in allowed or (directional and _number_key("-"+token) in allowed) or token in unsupported:
                continue
            start, end = max(0, match.start() - 24), min(len(text), match.end() + 24)
            unsupported[token] = text[start:end].replace("\n", " ")
    if unsupported:
        details = "；".join(f"{token}（…{unsupported[token]}…）" for token in sorted(unsupported))
        raise EditorialError("模型引入未授权数字：" + details)


def _article_from_payload(report: ResearchReport, payload: dict[str, Any]) -> tuple[str, ArticleNarrative]:
    title = str(payload.get("title") or "").strip()
    if not title or len(title) > 72:
        raise EditorialError("文章标题缺失或过长")
    try:
        article = ArticleNarrative.model_validate({key: value for key, value in payload.items() if key != "title"})
    except ValidationError as exc:
        raise EditorialError(f"文章结构非法：{exc}") from exc
    if not 3 <= len(article.sections) <= 5:
        raise EditorialError("深度文章必须包含 3–5 个正文章节")
    valid_ids = {fact.id for fact in report.facts}
    texts = [title, article.research_question, article.deck, article.lead, article.consensus_view, article.meridian_view, article.conclusion]
    for section in article.sections:
        if not 1 <= len(section.paragraphs) <= 3:
            raise EditorialError("每个章节必须包含 1–3 个段落")
        texts.append(section.heading)
        for paragraph in section.paragraphs:
            texts.append(paragraph.text)
            if not paragraph.fact_ids or not set(paragraph.fact_ids).issubset(valid_ids):
                raise EditorialError("正文段落包含缺失或未知事实 ID")
    _assert_numbers(report, texts)
    minimum = 1500 if report.report_type == "midday" else 2500
    if sum(len(text) for text in texts) < minimum:
        raise EditorialError("模型文章过短")
    return title, article


def _standard_from_payload(report: ResearchReport, payload: dict[str, Any]) -> tuple[str, str, list[str]]:
    title = str(payload.get("title") or "").strip()
    judgement = str(payload.get("executive_judgement") or "").strip()
    takeaways = payload.get("takeaways") or []
    valid_ids = {fact.id for fact in report.facts}
    if not title or not judgement or len(takeaways) != 3:
        raise EditorialError("常规研报编辑结构不完整")
    if report.daily_strategy:
        moves = {r.symbol:r.return_day_pct for r in report.daily_strategy.executive_dashboard if r.return_day_pct is not None}
    else:
        moves = {r.symbol:r.return_24h_pct for r in report.tactical_monitor.dashboard if r.return_24h_pct is not None}
    for symbol, action in re.findall(r"(BTC|ETH|SOL)\s*(领跌|领涨)", title + "。" + judgement):
        if moves and moves.get(symbol) != (min if action=="领跌" else max)(moves.values()):
            raise EditorialError(f"{symbol} {action}与涨跌排序冲突")
    if re.search(r"多头被动|空头被动|被迫平仓|资金(正|正在|已经)?[流撤]出", judgement) and not any(
            "清算" in f.statement or "净流出" in f.statement for f in report.facts):
        raise EditorialError("费率和价差只能说明合约定价，杠杆或资金行为缺少直接证据")
    texts = [title, judgement]
    rendered = []
    for item in takeaways:
        text = str(item.get("text") or "").strip()
        fact_ids = item.get("fact_ids") or []
        if not text or not fact_ids or not set(fact_ids).issubset(valid_ids):
            raise EditorialError("常规研报摘要引用非法")
        texts.append(text)
        rendered.append(text + " " + " ".join(f"[{fact_id}]" for fact_id in fact_ids))
    _assert_numbers(report, texts)
    return title, judgement, rendered


def _prompt(report: ResearchReport) -> tuple[str, str, int, bool]:
    facts = json.dumps(_fact_payload(report), ensure_ascii=False)
    if report.report_type != "insight":
        if report.report_type == "daily":
            schema = {
                "title": "加密市场日报标题，表达北京时间自然日走势与最重要的未来变量",
                "executive_judgement": "北京时间完整自然日复盘与未来24–72H大盘判断",
                "takeaways": [
                    {"text": "市场状态与主导变量", "fact_ids": ["F001"]},
                    {"text": "最重要的资产或相对价值结论", "fact_ids": ["F002"]},
                    {"text": "会改变组合判断的风险边界", "fact_ids": ["F003"]},
                ],
            }
            task = "编辑加密市场日报：复盘产品结构中明确给出的北京时间完整自然日，覆盖三资产、全市场、链上情绪与重大事件，并给出未来24–72H情景。不得输出入场、止损、止盈或 Preferred Trades。"
        else:
            schema = {
                "title": "定时交易监测标题，表达当前大盘与最明确的资产方向",
                "executive_judgement": "生成时点的大盘一句话、三资产方向和下个 checkpoint 前行动",
                "takeaways": [
                    {"text": "最大的信号变化", "fact_ids": ["F001"]},
                    {"text": "最接近触发的 setup 或 NO ACTION", "fact_ids": ["F002"]},
                    {"text": "下个 checkpoint 前的失效条件", "fact_ids": ["F003"]},
                ],
            }
            task = "编辑定时交易监测：突出最新行情、三资产趋势与具体条件计划；走势不明时必须明确写当前观望。不得扩写成全天复盘。"
        task += "只做取舍和表达，不修改任何数字、方向、点位或概率。"
        max_tokens, thinking = (3500 if report.report_type == "daily" else 1800), False
    else:
        schema = {
            "title": "从反常事实或核心矛盾出发的标题",
            "research_question": "全文回答的一个问题",
            "deck": "两句话以内的副标题",
            "lead": "从最值得注意的事实切入，不写摘要清单",
            "consensus_view": "市场共识",
            "meridian_view": "与共识有边界的研究判断",
            "sections": [
                {"heading": "推进论证的章节标题", "paragraphs": [{"text": "只使用事实账本的段落", "fact_ids": ["F001", "F002"]}]},
                {"heading": "第二个章节", "paragraphs": [{"text": "因果链或历史比较", "fact_ids": ["F003"]}]},
                {"heading": "反方路径", "paragraphs": [{"text": "完整可证伪路径", "fact_ids": ["F004"]}]},
            ],
            "conclusion": "回答开头问题并落到投资含义",
        }
        length = "2000–3500 个中文字符" if report.report_type == "midday" else "3500–6000 个中文字符"
        task = (
            "把研究事实编辑成一篇中文商业研究深度文章。采用单一命题、异常切入、时间推进、因果链和反方路径；"
            f"文章应有作者判断和阅读节奏，正文目标为 {length}，但不得模仿任何具体媒体或作者。"
            "不要平均介绍三个资产，也不要写成指标清单。"
        )
        max_tokens, thinking = 6500, True
    system = (
        "你是 Meridian 的研究编辑。输出必须是 json。事实账本是唯一事实来源；不得引入账本之外的数字、事件、人物或因果事实。"
        "禁止改变交易点位、概率和风险条件。每个结论都通过 fact_ids 引用证据。不要输出 Markdown。"
    )
    rows = report.daily_strategy.executive_dashboard if report.daily_strategy else report.tactical_monitor.dashboard
    brief = [{"symbol": row.symbol, "变化": row.what_changed, "判断": getattr(row, "view", getattr(row, "current_view", ""))}
             for row in rows]
    market_facts = [f for f in report.facts if f.category == "market"]
    extra = [f for f in report.facts if f.category in {"flow", "fundamental"}][:5]
    ledger = [{"id":f.id,"fact":f.statement} for f in market_facts + extra]
    sort_hint = sorted(((r.symbol,getattr(r,"return_day_pct",r.return_24h_pct)) for r in rows),key=lambda x:x[1] if x[1] is not None else -999, reverse=True)
    user = json.dumps({"按本窗口收益从高至低":sort_hint, "task": task + "只输出 title, executive_judgement, takeaways 三个字段。总字数180字。"
                       "只按输入原单位引用数字，禁止折算成亿、万；优先文字解释。不要回显输入或解释思考。",
                       "schema":{"title":"主发现","executive_judgement":"两句结论",
                                 "takeaways":[{"text":"要点","fact_ids":["证据编号"]}],"takeaways_count":3},
                       "assets": brief, "market": report.executive_judgement, "facts":ledger}, ensure_ascii=False)
    max_tokens = 4200
    return system, user, max_tokens, thinking


async def _asset_commentary(client, report, settings, row):
    facts = [f for f in report.facts if f.statement.startswith(row.symbol + " ")]
    valid = {f.id for f in facts}
    instruction = ("引述数字时使用原单位和正负号；引用放在独立数组中。根据给定证据解释本资产的变化、冲突与验证条件，80至150字。禁止添加事实和新价位。"
                   "区分自然日、滚动窗口、上个检查点。缺少比较样本如实说明。"
                   "输出 JSON {text: 中文段落, fact_ids: 引用编号数组}。")
    instruction += ("复盘北京自然日、历史量能与前期失效条件。" if report.daily_strategy else "聚焦上个检查点后新增的证据，分别说明方向、触发和确认状态。")
    input_data = {"symbol": row.symbol, "state": {"变化":row.what_changed,"观点":getattr(row,"view",getattr(row,"current_view","")),
                            "依据": getattr(row,"key_driver",getattr(row,"dominant_driver","")),
                            "反证":getattr(row,"contradicting_evidence",getattr(row,"counter_evidence",""))},
                  "facts": [{"id": f.id, "statement": f.statement} for f in facts]}
    reason = ""
    for attempt in range(2):
        try:
            response = await client.post(f"{settings.deepseek_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json={"model": settings.standard_editor_model, "thinking": {"type":"disabled"},
                      "response_format": {"type":"json_object"}, "max_tokens": 2200,
                      "messages":[{"role":"system","content":instruction},
                                  {"role":"user","content":json.dumps(input_data,ensure_ascii=False) + reason}]})
            response.raise_for_status()
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") == "length":
                raise EditorialError("输出截断，请缩短解释")
            block = json.loads(choice["message"]["content"])
            ids = block["fact_ids"]
            body = block["text"].strip()
            if not body or not ids or not set(ids) <= valid:
                raise EditorialError("本资产引用无效")
            _assert_numbers_with_fact_ids(report, body, ids)
            return body + " " + " ".join(f"[{fid}]" for fid in ids), choice.get("finish_reason") or "complete"
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            reason = f"上次未通过：{str(exc)[:120]}"
    return None, reason


async def edit_report(report: ResearchReport, settings: Settings, client: httpx.AsyncClient | None = None) -> ResearchReport:
    if report.report_type == "insight":
        raise InsightGenerationError("深度文章必须走资料包、整篇写作和独立审稿流程")
    if not settings.editor_enabled or not settings.deepseek_api_key:
        report.editorial_audit = EditorialAudit(
            status="deterministic", warnings=["DeepSeek 编辑未启用，使用确定性研究稿。"]
        )
        return report

    model = settings.article_editor_model if report.report_type == "insight" else settings.standard_editor_model
    system, user, max_tokens, thinking = _prompt(report)
    started = time.perf_counter()
    warnings: list[str] = []
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=settings.editor_timeout)
    try:
        for attempt in (1, 2):
            try:
                response = await client.post(
                    f"{settings.deepseek_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.deepseek_api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                        "response_format": {"type": "json_object"},
                        "thinking": {"type": "enabled" if thinking else "disabled"},
                        "reasoning_effort": "high",
                        "max_tokens": max_tokens,
                        "stream": False,
                    },
                )
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                if not content or not content.strip():
                    raise EditorialError("DeepSeek 返回空内容")
                if body["choices"][0].get("finish_reason") == "length":
                    raise EditorialError("输出截断，请缩短本节并完成 JSON")
                payload = json.loads(content)
                if report.report_type != "insight":
                    report.title, report.executive_judgement, report.key_takeaways = _standard_from_payload(report, payload)
                    rows = report.daily_strategy.executive_dashboard if report.daily_strategy else report.tactical_monitor.dashboard
                    sections = await asyncio.gather(*(_asset_commentary(client, report, settings, row) for row in rows))
                    for row, (commentary, _) in zip(rows, sections):
                        if commentary and report.daily_strategy:
                            row.thesis = commentary
                        elif commentary:
                            row.trend_summary = commentary
                    if report.daily_strategy:
                        report.daily_strategy.market_day.one_liner = report.executive_judgement
                        report.daily_strategy.key_takeaways = report.key_takeaways
                    else:
                        report.tactical_monitor.market_brief = report.executive_judgement
                        for asset, row in zip(report.assets, rows):
                            if asset.symbol == row.symbol:
                                asset.thesis = row.trend_summary
                    report.markdown = render_markdown(report)
                else:
                    report.title, report.article = _article_from_payload(report, payload)
                usage = body.get("usage") or {}
                report.editorial_audit = EditorialAudit(
                    provider="deepseek", model=model, status="model", attempts=attempt,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                    sections={row.symbol: status for row, (_, status) in zip(rows, sections)},
                    warnings=warnings,
                )
                if report.report_type == "insight":
                    label = "午间投研" if report.report_type == "midday" else "加密市场日报"
                    report.markdown = render_article(report, label)
                return report
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                warnings.append(f"第 {attempt} 次编辑失败：{type(exc).__name__} {str(exc)[:180]}")
                user += f"\n\n上次输出未通过校验：{str(exc)[:300]}。请重新输出完整 json。"
        report.editorial_audit = EditorialAudit(
            provider="deepseek", model=model, status="fallback", attempts=2,
            latency_ms=round((time.perf_counter() - started) * 1000), warnings=warnings,
        )
        if report.report_type == "insight":
            label = "午间投研" if report.report_type == "midday" else "加密市场日报"
            report.markdown = render_article(report, label)
        return report
    finally:
        if owns_client:
            await client.aclose()


def _insight_texts(payload: dict[str, Any]) -> tuple[str, ArticleNarrative, list[str]]:
    title = str(payload.get("title") or "").strip()
    if not title or len(title) > 96:
        raise EditorialError("洞察标题缺失或过长")
    try:
        article = ArticleNarrative.model_validate({key: value for key, value in payload.items() if key != "title"})
    except ValidationError as exc:
        raise EditorialError(f"洞察文章结构非法：{exc}") from exc
    for field_name in ("research_question", "deck", "lead", "consensus_view", "meridian_view", "conclusion"):
        setattr(article, field_name, INLINE_FACT_ID.sub("", getattr(article, field_name)).strip())
    for section in article.sections:
        section.heading = INLINE_FACT_ID.sub("", section.heading).strip()
        for paragraph in section.paragraphs:
            paragraph.text = INLINE_FACT_ID.sub("", paragraph.text).strip()
    texts = [title, article.research_question, article.deck, article.lead, article.consensus_view, article.meridian_view, article.conclusion]
    for section in article.sections:
        texts.append(section.heading)
        texts.extend(paragraph.text for paragraph in section.paragraphs)
    return title, article, texts


def visible_body_char_count(article: ArticleNarrative | None) -> int:
    """Count only reader-visible body copy, excluding titles, labels, citations and whitespace."""
    if article is None:
        return 0
    texts = [article.lead, article.consensus_view, article.meridian_view]
    texts.extend(paragraph.text for section in article.sections for paragraph in section.paragraphs)
    texts.append(article.conclusion)
    cleaned = "".join(INLINE_FACT_ID.sub("", text or "") for text in texts)
    return len(re.sub(r"\s+", "", cleaned))


def _validate_insight(report: ResearchReport, payload: dict[str, Any], short_requested: bool) -> tuple[str, ArticleNarrative, str]:
    from .article_calculations import register_calculations
    from .article_policy import article_blocks
    register_calculations(report, payload.get("calculations") or [])
    title, article, texts = _insight_texts(payload)
    valid_ids = {fact.id for fact in report.facts}
    if not article.research_question.strip() or not article.lead.strip() or not article.conclusion.strip() or not article.sections:
        raise EditorialError("文章需要具体问题、导语、正文和收束")
    for section in article.sections:
        if not section.heading.strip() or not section.paragraphs:
            raise EditorialError("深度文章包含空章节")
    for path, (text, ids) in article_blocks(article).items():
        if not text.strip():
            raise EditorialError("深度文章包含空段落")
        if not set(ids).issubset(valid_ids):
            raise EditorialError(f"{path} 包含未知事实 ID")
        # Non-factual transitions may have no citation; the semantic editor must
        # check that classification. Never guess citation IDs from shared numbers.
        # Numeric attribution is editorial metadata, not a publication gate.
        # Source facts remain attached when the model supplied them, while a
        # generated article is allowed to publish even if a number needs later
        # human/source reconciliation.
        if any(fid.startswith("D") and "假设演算" in next(f.statement for f in report.facts if f.id == fid) for fid in ids):
            if not any(word in text for word in ("假设", "举例", "示例")):
                raise EditorialError(f"{path} 的假设演算缺少标注")
    return title, article, "full"


async def _stream_completion(client: httpx.AsyncClient, url: str, headers: dict, body: dict) -> tuple[str, dict]:
    chunks: list[str] = []
    usage: dict = {}
    async with client.stream("POST", url, headers=headers, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            event = json.loads(data)
            usage = event.get("usage") or usage
            choices = event.get("choices") or []
            if choices:
                content = (choices[0].get("delta") or {}).get("content")
                if content:
                    chunks.append(content)
    return "".join(chunks), usage


# Compatibility entry points. Deep articles use one coherent draft and an independent review.
def insight_fragment_specs(packet: ResearchPacket) -> list[dict[str, Any]]:
    from .deep_writing import fragment_specs
    return fragment_specs(packet)


async def write_insight_fragment(report, packet, spec, settings, short=False, client=None):
    from .deep_writing import write_fragment
    return await write_fragment(report, packet, spec, settings, short=short, client=client)


def assemble_insight(report, fragments, short=False, latency_ms=None):
    from .deep_writing import assemble
    return assemble(report, fragments, latency_ms=latency_ms)


async def write_insight(report, packet, settings, short=False, client=None):
    from .deep_writing import write_article
    return await write_article(report, packet, settings, client=client)


def build_guaranteed_insight(report, packet, reason=""):
    raise InsightGenerationError("深度文章不再发布模板保底稿；需要补充材料或重新编辑。" + reason)
