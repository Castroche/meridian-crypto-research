const assets = {BTC:null, ETH:null, SOL:null};
const requested = (new URLSearchParams(location.search).get("asset") || new URLSearchParams(location.search).get("symbol") || location.hash.slice(1)).toUpperCase();
let selected = Object.hasOwn(assets, requested) ? requested : "BTC";
let liveReport = null;

const COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com";
const productBySymbol = { BTC: "BTC-USD", ETH: "ETH-USD", SOL: "SOL-USD" };
const symbolByProduct = Object.fromEntries(Object.entries(productBySymbol).map(([symbol, product]) => [product, symbol]));
const liveMarkets = Object.fromEntries(Object.keys(productBySymbol).map(symbol => [symbol, {
  price: null, previousPrice: null, open24h: null, bestBid: null, bestAsk: null,
  lastSize: null, side: null, exchangeTime: null, receivedAt: 0,
  ticks: 0, points: [], candleSeries: {60: [], 300: [], 900: [],3600: [],14400: [],86400: []},
}]));

let marketSocket = null;
let feedState = "connecting";
let reconnectAttempts = 0;
let reconnectTimer = null;
let staleTimer = null;
let chartFrame = null;
let priceFlashTimer = null;
let lastFeedEventAt = 0;
let selectedGranularity = 60;
let candleRequestId = 0;
let visibleCandles = [];
let chartIndicators = {};

const symbolNames = { BTC: "BITCOIN / BTC", ETH: "ETHEREUM / ETH", SOL: "SOLANA / SOL" };

function emptyResearchAsset(symbol) {
  return {
    name: symbolNames[symbol], price: null, snapshotPrice: null, change: "—", stance: "等待报告", confidence: 0,
    thesisTitle: "正在加载最新研究快照",
    thesis: "研究数据尚未就绪；实时行情与研究结论是两条独立链路。",
    target: "—", invalidation: "—", driver: "—", chart: [],
    signals: [["DATA", "研究快照暂不可用", "不会使用演示内容或过期价位替代最新报告。", 0]],
    scenarios: [["等待", 0, "最新报告加载后显示情景路径。", "—"]],
    levels: [["研究价位", "—"]], quality: 0, qualityFlags: ["最新研究报告尚未加载"],
  };
}

Object.keys(assets).forEach(symbol => { assets[symbol] = emptyResearchAsset(symbol); });

function formatPrice(value) {
  const digits = 2;
  return `$${Number(value).toLocaleString("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits})}`;
}

function adaptLiveAsset(asset) {
  const firstEvidence = key => asset.dimensions.find(d => d.key === key)?.evidence?.join("；") || "数据覆盖不足";
  return {
    symbol: asset.symbol, name: symbolNames[asset.symbol], price: formatPrice(asset.price), snapshotPrice: formatPrice(asset.price),
    change: `${asset.change_24h_pct >= 0 ? "+" : ""}${asset.change_24h_pct.toFixed(2)}%`,
    stance: asset.stance, confidence: asset.conviction,
    thesisTitle: asset.thesis.split("。", 1)[0] + "。", thesis: asset.thesis,
    target: `${formatPrice(asset.target_low)} – ${formatPrice(asset.target_high)}`,
    invalidation: `${formatPrice(asset.invalidation_price)} · ${asset.invalidation_condition}`,
    driver: asset.dominant_driver, chart: asset.chart,
    signals: asset.dimensions.map(d => [d.key.toUpperCase(), d.headline, d.evidence.join("；"), d.coverage]),
    scenarios: asset.scenarios.map(s => [s.name, s.probability, s.path, `${formatPrice(s.range_low)} – ${formatPrice(s.range_high)}`]),
    levels: asset.key_levels.map(level => [level.label, formatPrice(level.value)]),
    quality: asset.data_quality, qualityFlags: asset.quality_flags,
  };
}

function adaptTacticalAsset(asset) {
  const plans = asset.reference_setups?.length
    ? asset.reference_setups
    : [asset.primary_setup, ...(asset.conditional_setups || [])].filter(Boolean);
  const primary = asset.primary_setup || plans[0];
  const targetPrices = (primary?.target_plan || []).map(target => target.price)
    .concat(primary?.targets || []).filter(Number.isFinite);
  const target = targetPrices.length
    ? `${formatPrice(Math.min(...targetPrices))} – ${formatPrice(Math.max(...targetPrices))}`
    : "等待条件计划";
  const levels = [
    ...(asset.supports || []).slice(0, 2).map((value, index) => [`支撑 ${index + 1}`, formatPrice(value)]),
    ...(asset.resistances || []).slice(0, 2).map((value, index) => [`阻力 ${index + 1}`, formatPrice(value)]),
  ];
  if (primary?.stop != null) levels.push(["硬止损", formatPrice(primary.stop)]);
  const confidence = asset.confidence || {};
  const dataCoverage = Number(confidence.data_coverage ?? 0);
  const evidenceQuality = Number(confidence.evidence_quality ?? dataCoverage);
  const flowEvidence = [
    asset.positioning,
    asset.funding_rate == null ? null : `资金费率 ${(Number(asset.funding_rate) * 100).toFixed(4)}%`,
    asset.open_interest_change_1h_pct == null ? null : `OI 1H ${Number(asset.open_interest_change_1h_pct).toFixed(2)}%`,
  ].filter(Boolean).join("；");
  const planScenarios = plans.slice(0, 2).map(plan => [
    plan.direction === "SHORT" ? "条件下行" : "条件上行",
    null,
    plan.thesis || plan.trigger || "等待触发条件成立。",
    plan.entry_low != null && plan.entry_high != null ? `${formatPrice(plan.entry_low)} – ${formatPrice(plan.entry_high)}` : "—",
  ]);
  return {
    symbol: asset.symbol, name: symbolNames[asset.symbol], price: asset.price == null ? null : formatPrice(asset.price),
    snapshotPrice: asset.price == null ? null : formatPrice(asset.price),
    change: asset.return_24h_pct == null ? "—" : `${asset.return_24h_pct >= 0 ? "+" : ""}${Number(asset.return_24h_pct).toFixed(2)}%`,
    stance: asset.current_view, confidence: Number(confidence.view_conviction ?? 0),
    thesisTitle: asset.trend_summary || asset.state || "等待方向确认",
    thesis: asset.thesis_status_reason || asset.signal_change || "当前报告未提供详细判断。",
    target,
    invalidation: primary?.stop == null ? (primary?.invalidation || "等待触发") : `${formatPrice(primary.stop)} · 硬止损`,
    driver: asset.dominant_driver || asset.state || "—", chart: [],
    signals: [
      ["TECH", "多周期结构", `15M ${asset.trend_15m}；1H ${asset.trend_1h}；4H ${asset.trend_4h}；1D ${asset.trend_1d}`, evidenceQuality],
      ["FLOW", asset.positioning || "资金状态", flowEvidence || "资金数据覆盖不足", dataCoverage],
      ["DRIVER", asset.dominant_driver || asset.state || "主导变量", asset.thesis_status_reason || asset.signal_change, evidenceQuality],
      ["RISK", asset.counter_evidence || "反方证据", (asset.before_next_checkpoint || []).join("；") || "等待下一检查点", dataCoverage],
    ],
    scenarios: [["当前判断", null, asset.trend_summary || asset.signal_change, target], ...planScenarios],
    levels: levels.length ? levels : [["研究价位", "—"]],
    quality: dataCoverage,
    qualityFlags: asset.price_freshness_seconds > 3600 ? [`研报价格快照距生成时已 ${Math.floor(asset.price_freshness_seconds / 60)} 分钟`] : [],
  };
}

function applyLiveReport(report) {
    const reportAssets = !report.tactical_monitor && report.assets?.length
      ? report.assets.map(adaptLiveAsset)
      : (report.tactical_monitor?.dashboard || []).map(adaptTacticalAsset);
    const end = new Date(report.tactical_monitor?.next_checkpoint || Date.parse(report.as_of)+12*3600000);
    const expired = Date.now() >= end.getTime();
    const symbols = new Set(reportAssets.map(asset => asset.symbol));
    if (!["BTC", "ETH", "SOL"].every(symbol => symbols.has(symbol))) {
      throw new Error("latest tactical report does not contain the homepage asset projection");
    }
    liveReport = report;
    reportAssets.forEach(asset => { assets[asset.symbol] = asset.name ? asset : adaptLiveAsset(asset); });
    if (expired) {
      for (const symbol of Object.keys(assets)) assets[symbol] = {...emptyResearchAsset(symbol),
        stance:"已到期",thesisTitle:"等待本时段研报",thesis:"前期研究已归档。当前报价独立更新。"};
    }
    const asOf = new Date(liveReport.as_of);
    document.querySelector("#report-title").textContent = liveReport.title;
    document.querySelector("#data-cutoff").textContent = `数据截点 ${new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", hour12:false, year:"numeric",month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"}).format(asOf) + " 北京时间 · 有效至 " + end.toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"})}`;
    document.querySelector(".session-copy .eyebrow").textContent = `${liveReport.slot === "manual" ? "即时" : liveReport.slot} 研究报告`;
    document.querySelector("#report-version").textContent = liveReport.methodology_version;
    render();
}

async function loadArchive() {
  const response = await fetch("/api/reports?limit=30", {headers:{Accept:"application/json"}}).catch(()=>null);
  if (!response?.ok) { document.querySelector("#archive-list").textContent="归档暂时无法连接。"; return; }
  const reports = await response.json();
  document.querySelector("#archive-count").textContent = reports.length;
  const container = document.querySelector("#archive-list"); container.replaceChildren();
  if (!reports.length) { const empty=document.createElement("p"); empty.className="archive-empty"; empty.textContent="暂无报告"; container.append(empty); return; }
  reports.forEach(report => {
    const item=document.createElement("div"); item.className="archive-item";
    const open=document.createElement("button"); open.className="archive-open";
    const date=document.createElement("span"); date.textContent=`${new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false}).format(new Date(report.as_of))} · ${report.slot === "manual" ? "即时" : report.slot}`;
    const title=document.createElement("strong"); title.textContent=report.title;
    const retention=document.createElement("small"); retention.className=report.is_saved ? "archive-saved" : ""; retention.textContent=report.is_saved ? "◆ 已保存 · 长期保留" : `自动删除 ${new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hour12:false}).format(new Date(report.expires_at))}`;
    open.append(date,title,retention); open.addEventListener("click",()=>location.href=`/reports?id=${report.id}`);
    const actions=document.createElement("div"); actions.className="archive-row-actions";
    const save=document.createElement("button"); save.textContent=report.is_saved ? "取消保存" : "保存"; save.addEventListener("click",()=>setArchiveSaved(report.id,!report.is_saved));
    const exportLink=document.createElement("a"); exportLink.href=`/api/reports/${report.id}/export/markdown`; exportLink.textContent="导出";
    const remove=document.createElement("button"); remove.className="archive-delete"; remove.textContent="删除"; remove.addEventListener("click",()=>deleteArchiveReport(report));
    actions.append(save,exportLink,remove); item.append(open,actions); container.append(item);
  });
}

function archiveAdminHeaders(){ const token=localStorage.getItem("meridian_admin_token"); return token?{"X-Meridian-Admin-Token":token}:{}; }
async function archiveMutation(url,options={},retry=true){ const response=await fetch(url,{...options,headers:{...archiveAdminHeaders(),...(options.headers||{})}}); if(response.status===401&&retry){const token=prompt("请输入 Meridian 管理令牌");if(token){localStorage.setItem("meridian_admin_token",token);return archiveMutation(url,options,false);}} return response; }
async function setArchiveSaved(id,saved){const response=await archiveMutation(`/api/reports/${id}/saved?saved=${saved}`,{method:"PATCH"});if(response.ok)await loadArchive();else alert("保存状态更新失败");}
async function deleteArchiveReport(report){if(!confirm(`确认删除“${report.title}”？`))return;const response=await archiveMutation(`/api/reports/${report.id}`,{method:"DELETE"});if(response.ok)await loadArchive();else alert("删除失败");}

async function loadPerformance() {
  const response = await fetch("/api/performance?days=90", {headers:{Accept:"application/json"}}).catch(()=>null);
  if (!response?.ok) { document.querySelector("#performance-list").textContent="复盘统计暂时无法连接。"; return; }
  const data = await response.json();
  if (!data.total_samples) return;
  document.querySelector("#performance-list").innerHTML = data.by_asset.map(item => `<div class="performance-row">
    <strong>${item.symbol} · ${item.horizon || "24h"}</strong>
    <div class="performance-metric"><span>已结算样本</span><strong>${item.samples}</strong></div>
    <div class="performance-metric"><span>方向准确率</span><strong>${(item.direction_accuracy * 100).toFixed(1)}%</strong></div>
    <div class="performance-metric"><span>目标命中率</span><strong>${(item.target_hit_rate * 100).toFixed(1)}%</strong></div>
    <div class="performance-metric"><span>Brier 分数</span><strong>${item.avg_brier_score?.toFixed(3) ?? "—"}</strong></div>
  </div>`).join("");
}

async function loadLiveReport() {
  try {
    const response = await fetch("/api/reports/latest?report_type=standard", {cache:"no-store", headers:{Accept:"application/json"}});
    if (!response.ok) throw new Error(`latest tactical report returned ${response.status}`);
    const report = await response.json();
    applyLiveReport(report);
  } catch (error) {
    liveReport = null;
    for (const symbol of Object.keys(assets)) assets[symbol] = emptyResearchAsset(symbol);
    render();
    console.warn("Latest tactical report is unavailable", error);
    document.querySelector("#report-title").textContent = "最新研究报告暂不可用";
    document.querySelector("#data-cutoff").textContent = "实时行情仍在线 · 不展示演示结论";
  }
}

function renderTabs() {
  const tabs = document.querySelector("#asset-tabs");
  tabs.innerHTML = Object.entries(assets).map(([symbol, data]) => `
    <button class="asset-tab ${symbol === selected ? "active" : ""}" data-symbol="${symbol}" aria-pressed="${symbol === selected}">
      <span>${symbol} · USD</span><strong data-tab-price>${liveMarkets[symbol].price ? formatPrice(liveMarkets[symbol].price) : "—"}</strong><small data-tab-change class="${marketChange(symbol, data).startsWith("-") ? "negative" : ""}">${marketChange(symbol, data)}</small>
    </button>`).join("");
  tabs.querySelectorAll("button").forEach(btn => btn.addEventListener("click", () => {
    if (selected === btn.dataset.symbol) return;
    pinnedLevel=null; resetBar(); selected = btn.dataset.symbol;
    history.replaceState(null,"","/terminal?asset="+selected+location.hash);
    render();
    if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
      document.querySelector("#price-chart")?.animate([{opacity:.35}, {opacity:1}], {duration:180});
    }
    if (!liveMarkets[selected].candleSeries[selectedGranularity].length) loadCandles(selected, selectedGranularity);
  }));
}

function marketChange(symbol, fallback) {
  const market = liveMarkets[symbol];
  if (!market.price || !market.open24h) return "—";
  const change = (market.price / market.open24h - 1) * 100;
  return `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`;
}

function updateTabQuote(symbol) {
  const tab = document.querySelector(`.asset-tab[data-symbol="${symbol}"]`);
  if (!tab) return;
  const market = liveMarkets[symbol];
  const fallback = assets[symbol];
  tab.querySelector("[data-tab-price]").textContent = market.price ? formatPrice(market.price) : "—";
  const change = marketChange(symbol, fallback);
  const changeEl = tab.querySelector("[data-tab-change]");
  changeEl.textContent = change;
  changeEl.classList.toggle("negative", change.startsWith("-"));
}

function calculateEMA(candles, period) {
  const multiplier = 2 / (period + 1);
  let previous = null;
  return candles.map(candle => {
    previous = previous == null ? candle.close : candle.close * multiplier + previous * (1 - multiplier);
    return previous;
  });
}

function calculateVWAP(candles) {
  let cumulativeValue = 0, cumulativeVolume = 0;
  return candles.map(candle => {
    const typical = (candle.high + candle.low + candle.close) / 3;
    cumulativeValue += typical * candle.volume;
    cumulativeVolume += candle.volume;
    return cumulativeVolume ? cumulativeValue / cumulativeVolume : candle.close;
  });
}

function calculateRSI(candles, period = 14) {
  const values = Array(candles.length).fill(null);
  if (candles.length <= period) return values;
  let gains = 0, losses = 0;
  for (let index = 1; index <= period; index += 1) {
    const change = candles[index].close - candles[index - 1].close;
    gains += Math.max(0, change);
    losses += Math.max(0, -change);
  }
  let averageGain = gains / period, averageLoss = losses / period;
  values[period] = averageLoss === 0 ? 100 : 100 - 100 / (1 + averageGain / averageLoss);
  for (let index = period + 1; index < candles.length; index += 1) {
    const change = candles[index].close - candles[index - 1].close;
    averageGain = (averageGain * (period - 1) + Math.max(0, change)) / period;
    averageLoss = (averageLoss * (period - 1) + Math.max(0, -change)) / period;
    values[index] = averageLoss === 0 ? 100 : 100 - 100 / (1 + averageGain / averageLoss);
  }
  return values;
}

function seriesPath(values, xFor, yFor, startIndex) {
  let started = false;
  return values.map((value, index) => {
    if (!Number.isFinite(value)) return "";
    const command = started ? "L" : "M";
    started = true;
    return `${command}${xFor(index + startIndex).toFixed(1)},${yFor(value).toFixed(1)}`;
  }).join(" ");
}

function formatVolume(value) {
  if (!Number.isFinite(value)) return "—";
  if (value >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(2)}K`;
  return value.toLocaleString("en-US", {maximumFractionDigits:2});
}

function renderChartLoading(message) {
  const svg = document.querySelector("#price-chart");
  svg.innerHTML = `${[20,74.5,129,183.5,238,300,326,392].map(y => `<line class="chart-grid" x1="0" y1="${y}" x2="830" y2="${y}"/>`).join("")}
    <text class="chart-empty" x="415" y="205" text-anchor="middle">${message}</text>`;
}

function renderLiveChart(symbol = selected) {
  const svg = document.querySelector("#price-chart");
  const market = liveMarkets[symbol];
  const allCandles = market.candleSeries[selectedGranularity] || [];
  const chartStatus = document.querySelector("#chart-status");
  document.querySelector("#chart-message-count").textContent = `${market.ticks.toLocaleString("en-US")} TRADES`;
  document.querySelector("#chart-title").textContent = `${periodsText[selectedGranularity]} K 线 · Coinbase Spot`;

  if (!allCandles.length) {
    chartStatus.textContent = "加载交易所数据";
    visibleCandles = [];
    renderChartLoading("正在加载 Coinbase 历史 K 线…");
    return;
  }

  chartStatus.textContent = feedState === "live" ? "250MS 成交在线" : "显示最近成交";
  const ema9All = calculateEMA(allCandles, 9);
  const ema21All = calculateEMA(allCandles, 21);
  const rsiAll = calculateRSI(allCandles, 14);
  const startIndex = Math.max(0, allCandles.length - 72);
  const candles = allCandles.slice(startIndex);
  visibleCandles = candles;
  const ema9 = ema9All.slice(startIndex), ema21 = ema21All.slice(startIndex), vwap = calculateVWAP(candles), rsi = rsiAll.slice(startIndex);
  chartIndicators={ema9,ema21,vwap,rsi};
  const plotWidth = 830, priceTop = 20, priceBottom = 238, volumeTop = 254, volumeBottom = 300, rsiTop = 326, rsiBottom = 392;
  const slotWidth = plotWidth / Math.max(1, candles.length);
  const bodyWidth = Math.max(2, Math.min(8, slotWidth * .58));
  const xFor = index => (index - startIndex + .5) * slotWidth;
  const priceValues = candles.flatMap(candle => [candle.low, candle.high]);
  if(pinnedLevel?.symbol===symbol) priceValues.push(pinnedLevel.price);
  const rawMin = Math.min(...priceValues), rawMax = Math.max(...priceValues);
  const pricePadding = Math.max((rawMax - rawMin) * .08, rawMax * .00025);
  const min = rawMin - pricePadding, max = rawMax + pricePadding;
  const yPrice = value => priceBottom - (value - min) / (max - min) * (priceBottom - priceTop);
  const maxVolume = Math.max(...candles.map(candle => candle.volume), 1);
  const yVolume = value => volumeBottom - value / maxVolume * (volumeBottom - volumeTop);
  const yRsi = value => rsiBottom - value / 100 * (rsiBottom - rsiTop);
  const priceGrid = Array.from({length:5}, (_, index) => {
    const ratio = index / 4;
    const y = priceTop + ratio * (priceBottom - priceTop);
    const value = max - ratio * (max - min);
    return `<line class="chart-grid" x1="0" y1="${y}" x2="${plotWidth}" y2="${y}"/><text class="axis-label" x="894" y="${y + 3}" text-anchor="end">${formatPrice(value)}</text>`;
  }).join("");
  const verticalGrid = [0,.25,.5,.75,1].map(ratio => `<line class="chart-grid" x1="${(plotWidth * ratio).toFixed(1)}" y1="${priceTop}" x2="${(plotWidth * ratio).toFixed(1)}" y2="${rsiBottom}"/>`).join("");
  const candleMarkup = candles.map((candle, index) => {
    const x = (index + .5) * slotWidth;
    const rising = candle.close >= candle.open;
    const className = rising ? "candle-up" : "candle-down";
    const openY = yPrice(candle.open), closeY = yPrice(candle.close);
    const bodyY = Math.min(openY, closeY), bodyHeight = Math.max(1, Math.abs(closeY - openY));
    return `<line class="candle-wick ${className}" x1="${x.toFixed(1)}" y1="${yPrice(candle.high).toFixed(1)}" x2="${x.toFixed(1)}" y2="${yPrice(candle.low).toFixed(1)}"/>
      <rect class="candle-body ${className}" x="${(x - bodyWidth / 2).toFixed(1)}" y="${bodyY.toFixed(1)}" width="${bodyWidth.toFixed(1)}" height="${bodyHeight.toFixed(1)}"/>
      <rect class="${rising ? "volume-up" : "volume-down"}" x="${(x - bodyWidth / 2).toFixed(1)}" y="${yVolume(candle.volume).toFixed(1)}" width="${bodyWidth.toFixed(1)}" height="${Math.max(1, volumeBottom - yVolume(candle.volume)).toFixed(1)}"/>`;
  }).join("");
  const latest = candles.at(-1), latestRsi = rsi.at(-1);
  document.querySelector("#indicator-ema9").textContent = formatPrice(ema9.at(-1));
  document.querySelector("#indicator-ema21").textContent = formatPrice(ema21.at(-1));
  document.querySelector("#indicator-vwap").textContent = formatPrice(vwap.at(-1));
  document.querySelector("#indicator-rsi").textContent = Number.isFinite(latestRsi) ? latestRsi.toFixed(1) : "—";
  document.querySelector("#indicator-volume").textContent = `${formatVolume(latest.volume)} ${symbol}`;
  const currentY = yPrice(latest.close);
  svg.innerHTML = `<defs><clipPath id="price-pane-clip"><rect x="0" y="${priceTop}" width="${plotWidth}" height="${priceBottom - priceTop}"/></clipPath></defs>${verticalGrid}${priceGrid}
    <line class="chart-grid" x1="0" y1="${volumeTop}" x2="${plotWidth}" y2="${volumeTop}"/>
    <line class="chart-grid" x1="0" y1="${rsiTop}" x2="${plotWidth}" y2="${rsiTop}"/>
    <line class="rsi-band" x1="0" y1="${yRsi(70)}" x2="${plotWidth}" y2="${yRsi(70)}"/>
    <line class="rsi-band" x1="0" y1="${yRsi(30)}" x2="${plotWidth}" y2="${yRsi(30)}"/>
    <text class="pane-label" x="4" y="${volumeTop + 11}">VOLUME</text>
    <text class="pane-label" x="4" y="${rsiTop + 11}">RSI 14</text>
    <text class="axis-label" x="894" y="${yRsi(70) + 3}" text-anchor="end">70</text>
    <text class="axis-label" x="894" y="${yRsi(30) + 3}" text-anchor="end">30</text>
    ${candleMarkup}
    ${pinnedLevel?.symbol===symbol ? `<line class="price-marker" x1="0" x2="${plotWidth}" y1="${yPrice(pinnedLevel.price)}" y2="${yPrice(pinnedLevel.price)}"/><text class="marker-label" x="8" y="${yPrice(pinnedLevel.price)-3}">${formatPrice(pinnedLevel.price)}</text>` : ""}
    <g clip-path="url(#price-pane-clip)">
      <path class="indicator-ema9" d="${seriesPath(ema9, xFor, yPrice, startIndex)}"/>
      <path class="indicator-ema21" d="${seriesPath(ema21, xFor, yPrice, startIndex)}"/>
      <path class="indicator-vwap" d="${seriesPath(vwap, xFor, yPrice, startIndex)}"/>
    </g>
    <path class="rsi-line" d="${seriesPath(rsi, xFor, yRsi, startIndex)}"/>
    <line class="chart-current" x1="0" y1="${currentY.toFixed(1)}" x2="${plotWidth}" y2="${currentY.toFixed(1)}"/>
    <text class="chart-price-label" x="894" y="${Math.max(14, currentY - 7).toFixed(1)}" text-anchor="end">${formatPrice(latest.close)}</text>
    <line id="crosshair-x" class="crosshair" x1="0" y1="${priceTop}" x2="0" y2="${rsiBottom}" style="display:none"/>
    <line id="crosshair-y" class="crosshair" x1="0" y1="0" x2="${plotWidth}" y2="0" style="display:none"/>`;
  updateChartLabels(candles);
}

function updateChartLabels(candles) {
  const labels = document.querySelector("#chart-labels");
  if (!candles.length) {
    labels.innerHTML = "<span>—</span><span>—</span><span>—</span><span>—</span>";
    return;
  }
  const indices = [0, Math.floor((candles.length - 1) / 3), Math.floor((candles.length - 1) * 2 / 3), candles.length - 1];
  const formatter = new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", ...(selectedGranularity>=3600 ? {month:"2-digit",day:"2-digit"} : {}), hour12:false, hour:"2-digit", minute:"2-digit"});
  labels.innerHTML = indices.map(index => `<span>${formatter.format(new Date(candles[index].time * 1000))}</span>`).join("");
}

async function loadCandles(symbol = selected, granularity = selectedGranularity, attempt = 0) {
  const sourceInterval=granularity===14400?3600:granularity;
  const requestId = ++candleRequestId;
  const market = liveMarkets[symbol];
  document.querySelector("#chart-status").textContent = "加载交易所数据";
  if (symbol === selected && granularity === selectedGranularity) renderChartLoading("正在加载 Coinbase 历史 K 线…");
  try {
    const product = productBySymbol[symbol];
    let response;
    try {
      response = await fetch(`/api/market/candles?symbol=${symbol}&granularity=${sourceInterval}`, {
        cache:"no-store", signal:AbortSignal.timeout(10000),
      });
      if (!response.ok) throw new Error(`Coinbase candle gateway ${response.status}`);
    } catch (gatewayError) {
      if (requestId === candleRequestId && symbol === selected && granularity === selectedGranularity) {
        document.querySelector("#chart-status").textContent = "切换交易所直连";
      }
      response = await fetch(`https://api.exchange.coinbase.com/products/${product}/candles?granularity=${sourceInterval}`, {
        cache:"no-store", headers:{Accept:"application/json"}, signal:AbortSignal.timeout(6000),
      });
      if (!response.ok) throw new Error(`Coinbase Exchange candles ${response.status}`, {cause:gatewayError});
    }
    const rows = await response.json();
    market.candleSeries[granularity] = rows
      .map(([time, low, high, open, close, volume]) => ({
        time:Number(time), low:Number(low), high:Number(high), open:Number(open), close:Number(close), volume:Number(volume),
      }))
      .filter(candle => [candle.time, candle.low, candle.high, candle.open, candle.close, candle.volume].every(Number.isFinite))
      .sort((a,b) => a.time - b.time)
      .slice(-180);
    if(granularity===14400) market.candleSeries[granularity]=fourHourBars(market.candleSeries[granularity]);
    if (market.price && market.exchangeTime) updateCandleWithTrade(symbol, market.price, 0, market.exchangeTime.getTime());
    if (requestId === candleRequestId && symbol === selected && granularity === selectedGranularity) renderLiveChart(symbol);
  } catch (error) {
    console.warn("Coinbase candle history unavailable", error);
    if (requestId !== candleRequestId || symbol !== selected || granularity !== selectedGranularity) return;
    if (attempt < 3 && !market.candleSeries[granularity].length) {
      document.querySelector("#chart-status").textContent = `历史数据重试 ${attempt + 1}/3`;
      renderChartLoading("网络波动，正在恢复 Coinbase 历史 K 线…");
      setTimeout(() => {
        if (symbol === selected && granularity === selectedGranularity && !market.candleSeries[granularity].length) {
          loadCandles(symbol, granularity, attempt + 1);
        }
      }, 800 * (2 ** attempt));
    } else {
      document.querySelector("#chart-status").textContent = "历史 K 线不可用";
      renderChartLoading("Coinbase 历史 K 线暂时不可用，实时成交仍在连接");
    }
  }
}

function updateCandleWithTrade(symbol, price, size, timestamp) {
  const market = liveMarkets[symbol];
  Object.entries(market.candleSeries).forEach(([granularityKey, series]) => {
    if (!series.length) return;
    const granularity = Number(granularityKey);
    const bucket = Math.floor(timestamp / 1000 / granularity) * granularity;
    const last = series.at(-1);
    if (bucket < last.time) return;
    if (bucket === last.time) {
      last.high = Math.max(last.high, price);
      last.low = Math.min(last.low, price);
      last.close = price;
      last.volume += Number.isFinite(size) ? size : 0;
    } else {
      series.push({time:bucket, open:last.close, high:price, low:price, close:price, volume:Number.isFinite(size) ? size : 0});
      market.candleSeries[granularity] = series.slice(-180);
    }
  });
}

function setupChartInteractions() {
  document.querySelectorAll(".chart-toolbar button").forEach(button => button.addEventListener("click", () => {
    resetBar(); selectedGranularity = Number(button.dataset.granularity); showPeriod();
    document.querySelectorAll(".chart-toolbar button").forEach(item => item.classList.toggle("active", item === button));
    const series = liveMarkets[selected].candleSeries[selectedGranularity];
    if (series.length) renderLiveChart();
    else loadCandles(selected, selectedGranularity);
  }));
  const svg = document.querySelector("#price-chart");
  const tooltip = document.querySelector("#chart-tooltip");
  svg.addEventListener("pointermove", event => {
    if (!visibleCandles.length) return;
    const rect = svg.getBoundingClientRect();
    const chartX = (event.clientX - rect.left) / rect.width * 900;
    if (chartX < 0 || chartX > 830) {
      tooltip.hidden = true;
      return;
    }
    const index = Math.max(0, Math.min(visibleCandles.length - 1, Math.floor(chartX / 830 * visibleCandles.length)));
    const candle = visibleCandles[index];
    for(const [name,series] of Object.entries(chartIndicators)){
      const value=series[index];
      document.querySelector("#indicator-"+name).textContent=Number.isFinite(value)?(name==="rsi"?value.toFixed(1):formatPrice(value)):"—";
    }
    document.querySelector("#indicator-volume").textContent=formatVolume(candle.volume)+" "+selected;
    const crossX = (index + .5) * 830 / visibleCandles.length;
    const crossY = Math.max(20, Math.min(392, (event.clientY - rect.top) / rect.height * 410));
    const lineX = svg.querySelector("#crosshair-x"), lineY = svg.querySelector("#crosshair-y");
    if (lineX && lineY) {
      lineX.setAttribute("x1", crossX); lineX.setAttribute("x2", crossX); lineX.style.display = "";
      lineY.setAttribute("y1", crossY); lineY.setAttribute("y2", crossY); lineY.style.display = "";
    }
    const time = new Date(candle.time * 1000).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai", hour12:false, month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"});
    const direction = candle.close >= candle.open ? "上涨" : "下跌";
    tooltip.innerHTML = `<b>${time} · ${direction}</b><br>O ${formatPrice(candle.open)} &nbsp; H ${formatPrice(candle.high)}<br>L ${formatPrice(candle.low)} &nbsp; C ${formatPrice(candle.close)}<br>VOL ${formatVolume(candle.volume)} ${selected}`;
    tooltip.hidden = false;
    tooltip.style.left = `${Math.min(rect.width - 205, Math.max(8, event.clientX - rect.left + 14))}px`;
    tooltip.style.top = `${Math.min(rect.height - 92, Math.max(8, event.clientY - rect.top + 12))}px`;
  });
  svg.addEventListener("pointerleave", () => {
    renderLiveChart();
    tooltip.hidden = true;
    svg.querySelectorAll(".crosshair").forEach(line => { line.style.display = "none"; });
  });
}

function scheduleChartRender() {
  if (chartFrame) return;
  chartFrame = requestAnimationFrame(() => {
    chartFrame = null;
    renderLiveChart();
  });
}

function formatTradeSize(value) {
  if (!Number.isFinite(value)) return "—";
  if (value >= 1000) return value.toLocaleString("en-US", {maximumFractionDigits: 0});
  if (value >= 1) return value.toLocaleString("en-US", {maximumFractionDigits: 4});
  return value.toLocaleString("en-US", {maximumFractionDigits: 8});
}

function updateQuoteSurface(symbol, animate = false) {
  const market = liveMarkets[symbol];
  const fallback = assets[symbol];
  const price = market.price;
  const priceEl = document.querySelector("#asset-price");
  priceEl.textContent = price ? formatPrice(price) : "—";
  const changeValue = marketChange(symbol, fallback);
  const change = document.querySelector("#asset-change");
  change.textContent = changeValue;
  change.className = `change ${changeValue.startsWith("-") ? "negative" : ""}`;

  if (animate && market.previousPrice && market.price !== market.previousPrice) {
    priceEl.classList.remove("tick-up", "tick-down");
    void priceEl.offsetWidth;
    priceEl.classList.add(market.price > market.previousPrice ? "tick-up" : "tick-down");
    clearTimeout(priceFlashTimer);
    priceFlashTimer = setTimeout(() => priceEl.classList.remove("tick-up", "tick-down"), 180);
  }

  document.querySelector("#best-bid").textContent = market.bestBid ? formatPrice(market.bestBid) : "—";
  document.querySelector("#best-ask").textContent = market.bestAsk ? formatPrice(market.bestAsk) : "—";
  const mid = market.bestBid && market.bestAsk ? (market.bestBid + market.bestAsk) / 2 : null;
  const spread = mid ? (market.bestAsk - market.bestBid) / mid * 10000 : null;
  document.querySelector("#spread-bps").textContent = spread == null ? "—" : `${spread.toFixed(2)} BPS`;
  document.querySelector("#last-size").textContent = market.lastSize == null ? "—" : `${formatTradeSize(market.lastSize)} ${symbol}`;
  document.querySelector("#feed-latency").textContent = market.exchangeTime ? `${Math.max(0, market.receivedAt - market.exchangeTime.getTime())} MS` : "—";
  const side = document.querySelector("#trade-side");
  side.textContent = market.side ? `TAKER ${market.side.toUpperCase()}` : "—";
  side.className = market.side || "";
  document.querySelector("#quote-time").textContent = market.exchangeTime
    ? `${market.exchangeTime.toLocaleTimeString("zh-CN", {timeZone:"Asia/Shanghai", hour12:false, hour:"2-digit", minute:"2-digit", second:"2-digit", fractionalSecondDigits:3})} CST`
    : "等待首笔成交";
}

function render() {
  const d = assets[selected];
  renderTabs();
  document.querySelector("#asset-name").textContent = d.name;
  updateQuoteSurface(selected);
  const stance = document.querySelector("#stance"); stance.textContent = d.stance; stance.className = d.stance.includes("空") ? "bearish" : "";
  document.querySelector("#confidence-copy").textContent = `研究信号强度 ${d.confidence} / 100`;
  requestAnimationFrame(() => document.querySelector("#confidence-bar").style.width = `${d.confidence}%`);
  document.querySelector("#thesis-title").textContent = d.thesisTitle;
  document.querySelector("#thesis-copy").textContent = d.thesis;
  document.querySelector("#target-range").textContent = d.target;
  document.querySelector("#invalidation").textContent = d.invalidation;
  document.querySelector("#driver").textContent = d.driver;
  document.querySelector("#analysis-price").textContent = d.snapshotPrice || d.price || "—";
  if (document.querySelector("#quality-score")) document.querySelector("#quality-score").textContent = `${d.quality ?? 0} / 100`;
  const qualityNote = document.querySelector("#quality-note");
  if (qualityNote) {
    qualityNote.hidden = !d.qualityFlags?.length;
    qualityNote.textContent = d.qualityFlags?.length ? `数据质量提示：${d.qualityFlags.join("；")}` : "";
  }
  const make = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text != null) el.textContent = text;
    return el;
  };
  document.querySelector("#signal-list").replaceChildren(...d.signals.map(([type,title,copy,score]) => {
    const row = make("div","signal-item");
    const detail = make("div");
    detail.append(make("h3",null,title), make("p",null,copy));
    const weight = make("div","signal-score","证据强度 " + score);
    const bar = make("i");
    bar.style.setProperty("--score", Math.max(0,Math.min(100,Number(score)||0)) + "%");
    weight.append(bar);
    row.append(make("span","signal-type",type),detail,weight);
    return row;
  }));
  document.querySelector("#scenario-table").replaceChildren(...d.scenarios.map(([name,prob,copy,range]) => {
    const row = make("div","scenario-row");
    row.append(make("span",null,name),make("strong",null,prob == null ? "条件" : prob+"%"),
               make("p",null,copy),make("span","range",range));
    return row;
  }));
  document.querySelector("#level-grid").replaceChildren(...d.levels.map(([name,value]) => {
    const row = make("div","level");
    row.append(make("span",null,name),make("strong",null,value));
    return row;
  }));
  renderDeskPanel(d);
  for(const el of document.querySelectorAll("#thesis-title,#thesis-copy,#driver,#signal-list h3,#signal-list p,#scenario-table p"))linkDeskCitations(el);
  renderLiveChart(selected);
}

function setFeedState(nextState) {
  if (feedState === nextState && nextState !== "live") return;
  feedState = nextState;
  const copy = {
    connecting: "正在连接 COINBASE EXCHANGE 实时行情…",
    reconnecting: "COINBASE 实时行情重连中 · 当前显示最近成交",
    live: "COINBASE EXCHANGE · 逐笔成交在线 · 研究报告按时点更新 · 不构成投资建议",
    stale: "COINBASE 行情暂时无响应 · 正在恢复连接 · 当前显示最近成交",
    offline: "网络离线 · 实时行情暂停 · 当前显示最近成交",
  };
  const shortCopy = {
    connecting: "连接中", reconnecting: "重连中", live: "逐笔在线", stale: "行情延迟", offline: "已离线",
  };
  const ribbon = document.querySelector("#data-status");
  ribbon.textContent = copy[nextState];
  ribbon.className = `demo-ribbon ${nextState}`;
  document.querySelector("#header-live-dot").className = `live-dot ${nextState}`;
  document.querySelector("#quote-feed-status").textContent = shortCopy[nextState];
  document.querySelector("#chart-status").textContent = shortCopy[nextState];
}

function appendLivePoint(market, price, timestamp) {
  const bucket = Math.floor(timestamp / 1000) * 1000;
  const last = market.points.at(-1);
  if (last?.time === bucket) last.price = price;
  else market.points.push({time: bucket, price});
  market.points = market.points.filter(point => point.time >= bucket - 65000).slice(-66);
}

function handleTickerQuote(ticker, timestamp) {
  const symbol = symbolByProduct[ticker.product_id];
  if (!symbol) return;
  const price = Number(ticker.price);
  if (!Number.isFinite(price) || price <= 0) return;
  const market = liveMarkets[symbol];
  const percentChange = Number(ticker.price_percent_chg_24_h);
  market.bestBid = Number(ticker.best_bid) || market.bestBid;
  market.bestAsk = Number(ticker.best_ask) || market.bestAsk;
  market.open24h = Number.isFinite(percentChange) ? price / (1 + percentChange / 100) : market.open24h;
  if (!market.price) {
    market.price = price;
    market.exchangeTime = new Date(timestamp || Date.now());
    appendLivePoint(market, price, market.exchangeTime.getTime());
  }
  market.receivedAt = Date.now();
  updateTabQuote(symbol);
  if (symbol === selected) updateQuoteSurface(symbol);
  reconnectAttempts = 0;
  setFeedState("live");
}

function handleMarketTrade(trade) {
  const symbol = symbolByProduct[trade.product_id];
  if (!symbol) return;
  const price = Number(trade.price);
  if (!Number.isFinite(price) || price <= 0) return;
  const market = liveMarkets[symbol];
  const exchangeTime = new Date(trade.time || Date.now());
  market.previousPrice = market.price;
  market.price = price;
  market.lastSize = Number(trade.size) || 0;
  // Coinbase reports the maker side; the aggressor/taker is the opposite side.
  market.side = trade.side === "BUY" ? "sell" : trade.side === "SELL" ? "buy" : market.side;
  market.exchangeTime = Number.isNaN(exchangeTime.getTime()) ? new Date() : exchangeTime;
  market.receivedAt = Date.now();
  market.ticks += 1;
  appendLivePoint(market, price, market.exchangeTime.getTime());
  updateCandleWithTrade(symbol, price, market.lastSize, market.exchangeTime.getTime());
  updateTabQuote(symbol);
  if (symbol === selected) {
    updateQuoteSurface(symbol, true);
    scheduleChartRender();
  }
  reconnectAttempts = 0;
  setFeedState("live");
}

function handleMarketMessage(event) {
  lastFeedEventAt = Date.now();
  let message;
  try { message = JSON.parse(event.data); }
  catch { return; }
  if (message.channel === "ticker") {
    message.events?.forEach(item => item.tickers?.forEach(ticker => handleTickerQuote(ticker, message.timestamp)));
  }
  if (message.channel === "market_trades") {
    message.events?.forEach(item => {
      const trades = item.type === "snapshot" ? item.trades?.slice(0, 1) : item.trades;
      trades?.slice().reverse().forEach(handleMarketTrade);
    });
  }
  if (message.type === "error") {
    console.warn("Coinbase market feed error", message.message || message.reason || message);
    marketSocket?.close();
  }
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  if (!navigator.onLine) {
    setFeedState("offline");
    return;
  }
  reconnectAttempts += 1;
  const delay = Math.min(15000, 750 * 2 ** Math.min(reconnectAttempts, 5)) + Math.random() * 350;
  setFeedState("reconnecting");
  reconnectTimer = setTimeout(connectMarketFeed, delay);
}

function connectMarketFeed() {
  clearTimeout(reconnectTimer);
  if (!navigator.onLine) {
    setFeedState("offline");
    return;
  }
  if (marketSocket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(marketSocket.readyState)) return;
  setFeedState(reconnectAttempts ? "reconnecting" : "connecting");
  const socket = new WebSocket(COINBASE_WS_URL);
  marketSocket = socket;

  socket.addEventListener("open", () => {
    lastFeedEventAt = Date.now();
    const product_ids = Object.values(productBySymbol);
    socket.send(JSON.stringify({type:"subscribe", product_ids, channel:"ticker"}));
    socket.send(JSON.stringify({type:"subscribe", product_ids, channel:"market_trades"}));
    socket.send(JSON.stringify({type:"subscribe", channel:"heartbeats"}));
  });
  socket.addEventListener("message", handleMarketMessage);
  socket.addEventListener("error", () => socket.close());
  socket.addEventListener("close", () => {
    if (marketSocket !== socket) return;
    marketSocket = null;
    scheduleReconnect();
  });
}

function monitorFeed() {
  if (marketSocket?.readyState === WebSocket.OPEN && lastFeedEventAt) {
    const silence = Date.now() - lastFeedEventAt;
    if (silence > 20000) marketSocket.close();
    else if (silence > 7000) setFeedState("stale");
  }
  Object.values(liveMarkets).forEach(market => {
    if (market.price) appendLivePoint(market, market.price, Date.now());
  });
  if (liveMarkets[selected].price) scheduleChartRender();
}

function tick() {
  const now = new Date();
  document.querySelector("#beijing-time").textContent = new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", hour12:false, hour:"2-digit", minute:"2-digit", second:"2-digit"}).format(now);
  const cn = new Date(now.toLocaleString("en-US", {timeZone:"Asia/Shanghai"}));
  const hour = cn.getHours();
  const nextHour = hour < 8 ? 8 : hour < 20 ? 20 : 24;
  const next = new Date(cn); next.setHours(nextHour, 0, 0, 0);
  const diff = Math.max(0, next - cn); const h = String(Math.floor(diff/3600000)).padStart(2,"0"); const m = String(Math.floor(diff%3600000/60000)).padStart(2,"0"); const s = String(Math.floor(diff%60000/1000)).padStart(2,"0");
  document.querySelector("#countdown").textContent = `${h}:${m}:${s}`;
  document.querySelector("#next-slot").textContent = `${String(nextHour).padStart(2,"0")}:00 北京时间`;
}

const archive = document.querySelector("#archive"), scrim = document.querySelector("#scrim");
function toggleArchive(open) { archive.classList.toggle("open", open); scrim.classList.toggle("open", open); archive.setAttribute("aria-hidden", String(!open)); }
document.querySelector("#open-report").addEventListener("click", () => toggleArchive(true));
document.querySelector("#close-report").addEventListener("click", () => toggleArchive(false));
scrim.addEventListener("click", () => toggleArchive(false));
document.addEventListener("keydown", e => { if (e.key === "Escape") toggleArchive(false); });

window.addEventListener("online", () => { reconnectAttempts = 0; connectMarketFeed(); });
window.addEventListener("offline", () => { setFeedState("offline"); marketSocket?.close(); });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && (!marketSocket || Date.now() - lastFeedEventAt > 20000)) {
    marketSocket?.close();
    connectMarketFeed();
  }
});

render();
tick();
setupChartInteractions();
setFeedState("connecting");
connectMarketFeed();
loadCandles(selected, selectedGranularity);
loadLiveReport();
// Keep the archive count current. Fetch outcome statistics as the section approaches.
loadArchive();
const performanceObserver = new IntersectionObserver(entries => {
  if (entries.some(entry => entry.isIntersecting)) {
    performanceObserver.disconnect();
    loadPerformance();
  }
}, {rootMargin:"300px"});
performanceObserver.observe(document.querySelector("#performance"));
setInterval(tick, 1000);
staleTimer = setInterval(monitorFeed, 1000);
setInterval(loadLiveReport, 60000);
