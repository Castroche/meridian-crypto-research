"""Comparable observations only. Historical checks use completed hourly candles."""
from datetime import timedelta
from statistics import mean
from zoneinfo import ZoneInfo

HOUR = timedelta(hours=1)


def venue(snapshot):
    return " / ".join(snapshot.sources[:3])


def closed_hours(snapshot):
    return {bar.opened_at: bar for bar in snapshot.candles if bar.opened_at + HOUR <= snapshot.as_of}


def checkpoint_notes(snapshot, prior, since, trends):
    if not prior or since is None:
        return ["首次可用检查点，等待同源上期样本。"]
    if since >= snapshot.as_of or not prior.comparison_source or prior.comparison_source != venue(snapshot):
        return ["上期时点或行情来源不可比，暂缓跨期计算。"]
    notes = []
    for label, old, current in (("滚动 24H 成交额", prior.volume_24h_usd, snapshot.volume_24h_usd),
                                ("未平仓名义金额", prior.open_interest_usd, snapshot.open_interest_usd)):
        if old and current is not None:
            notes.append(f"{label}较上期 {(current / old - 1) * 100:+.2f}%。")
    if prior.open_interest_usd and snapshot.open_interest_usd is not None and prior.price > 0:
        oi_change = snapshot.open_interest_usd / prior.open_interest_usd - 1
        price_change = snapshot.price / prior.price - 1
        if price_change * oi_change < 0 and abs(price_change) >= .0035 and abs(oi_change) >= .01:
            notes.append("现货与美元计价未平仓金额反向变化；名义金额同时受价格影响，无法单凭此项判断开仓方向。")
    if prior.funding_rate is not None and snapshot.funding_rate is not None and prior.funding_rate != snapshot.funding_rate:
        notes.append(f"资金费率 {prior.funding_rate:.4%} → {snapshot.funding_rate:.4%}。")
    for key, current in trends.items():
        old = getattr(prior, key)
        if old != current and "UNAVAILABLE" not in (old, current):
            notes.append(f"{key[6:].upper()} 结构 {old} → {current}。")
    bars = closed_hours(snapshot)
    touched_reported = False
    for t, bar in sorted(bars.items()):
        if t < since:
            continue
        if not touched_reported:
            touched = [level for level in (prior.supports[:1] + prior.resistances[:1]) if bar.low <= level <= bar.high]
            if touched:
                touched_reported = True
                notes.append("期间已收盘小时线触及上期结构位 " + " / ".join(f"{level:g}" for level in touched) + "。")
        for plan in prior.reference_setups:
            if plan.trigger_price is None or t + HOUR > plan.expiration:
                continue
            crossed = bar.close > plan.trigger_price if plan.direction == "LONG" else bar.close < plan.trigger_price
            if not crossed:
                continue
            lookback = [bars[t - HOUR * i].volume for i in range(1, 25) if t - HOUR * i in bars]
            confirmed = len(lookback) == 24 and bar.volume >= mean(lookback)
            verdict = "收盘与成交量条件均满足" if confirmed else "量能未确认"
            notes.append(f"{(t + HOUR).astimezone(ZoneInfo('Asia/Shanghai')):%m-%d %H:%M} 北京时间，上期 {plan.direction} 触发价 {plan.trigger_price:g}，{verdict}。")
            break
    return notes


def daily_notes(snapshot, stats, history, prior, prior_as_of):
    notes = []
    yesterday = history[0]["return_pct"]
    if yesterday is not None and stats["return_pct"] is not None:
        notes.append(f"较前一自然日，涨跌幅变化 {stats['return_pct'] - yesterday:+.2f} 个百分点。")
    if stats["close"] is not None and history[-1]["close"] and all(s["close"] is not None for s in history):
        notes.append(f"同一现货市场七日前收盘至本日收盘 {100 * (stats['close'] / history[-1]['close'] - 1):+.2f}%。")
    volumes = [s["volume"] for s in history if s["volume"] is not None]
    if len(volumes) == 7 and mean(volumes) > 0 and stats["volume"] is not None:
        ratio = stats["volume"] / mean(volumes)
        notes.append(f"自然日成交量为此前七日均量的 {ratio:.2f} 倍。")
    else:
        notes.append(f"此前七日有 {len(volumes)}/7 个可比成交量样本，暂缺完整周基准。")
    if not prior or not prior_as_of:
        notes.append("前一期日报缺失，暂无观点复盘样本。")
        return notes
    notes.append(f"上期观点为{prior.view}，复核条件为{prior.invalidation}。")
    if not prior.comparison_source or prior.comparison_source != venue(snapshot):
        notes.append("上期来源未确认或已切换，无法判定失效条件。")
        return notes
    down = "跌破" in prior.invalidation
    levels = prior.supports if down else prior.resistances
    if levels:
        bars = closed_hours(snapshot)
        ends = [b for t,b in bars.items() if t - 3 * HOUR >= prior_as_of and t + HOUR <= prior_as_of + 72 * HOUR and
                (t + HOUR).astimezone(ZoneInfo("Asia/Shanghai")).hour % 4 == 0 and
                all(t - i * HOUR in bars for i in range(1,4))]
        triggered = any(b.close < levels[0] if down else b.close > levels[0] for b in ends)
        notes.append(f"可核验的 {len(ends)} 根已收盘 4H 线中，" + ("已触及失效条件。" if triggered else "未观察到失效。") if ends else "尚无可复核的完整 4H 线。")
    return notes


def chain_percentile(fundamental):
    if not fundamental:
        return ""
    rows = sorted({p.at:p.value for p in fundamental.tvl_history if p.at <= fundamental.as_of}.items())[-31:]
    if len(rows) < 8:
        return ""
    t, latest = rows[-1]
    prior = [value for _, value in rows[:-1]]
    rank = 100 * sum(value <= latest for value in prior) / len(prior)
    return f" 截至 {t:%Y-%m-%d}，最近 TVL 样本高于或等于此前 {len(prior)} 个观测的 {rank:.0f}%。"

