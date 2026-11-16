let reports = [];
let selectedId = null;
let activeFilter = history.state?.filter || "all";

const listEl = document.querySelector("#report-list");
const toastEl = document.querySelector("#toast");

function fmtDate(value, withTime = true) {
  return new Intl.DateTimeFormat("zh-CN", {timeZone:"Asia/Shanghai", year:"numeric", month:"2-digit", day:"2-digit", ...(withTime ? {hour:"2-digit",minute:"2-digit",hour12:false} : {})}).format(new Date(value));
}

function money(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "N/A";
  const digits = 2;
  return `$${Number(value).toLocaleString("en-US", {minimumFractionDigits:digits,maximumFractionDigits:digits})}`;
}

function showToast(message) {
  toastEl.textContent = message; toastEl.classList.add("show");
  clearTimeout(showToast.timer); showToast.timer = setTimeout(() => toastEl.classList.remove("show"), 2400);
}

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function fetchWithRetry(url, options = {}, attempts = 3) {
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const response = await fetch(url, options);
      if (response.ok || ![502, 503, 504].includes(response.status) || attempt === attempts - 1) return response;
      const detail = await response.clone().json().catch(() => ({}));
      if (String(detail.message || "").toLowerCase().includes("blocked")) return response;
    } catch (error) {
      lastError = error;
      if (attempt === attempts - 1) throw error;
    }
    await wait(300 * (2 ** attempt));
  }
  throw lastError || new Error("网络请求失败");
}

async function apiError(response, fallback) {
  const detail = await response.clone().json().catch(() => ({}));
  return String(detail.message || "").toLowerCase().includes("blocked")
    ? "研报存储额度已暂停，请在 Vercel 恢复 Blob 服务"
    : detail.detail === "Admin token is not configured" ? "服务端尚未配置管理令牌" : typeof detail.detail === "string" ? detail.detail : fallback;
}

function adminHeaders() {
  const token = localStorage.getItem("meridian_admin_token");
  return token ? {"X-Meridian-Admin-Token":token} : {};
}

async function mutate(url, options = {}, retry = true) {
  const response = await fetch(url, {...options, headers:{...adminHeaders(), ...(options.headers || {})}});
  if (response.status === 401 && retry) {
    const token = window.prompt("请输入 Meridian 管理令牌");
    if (token) { localStorage.setItem("meridian_admin_token", token); return mutate(url, options, false); }
  }
  return response;
}

async function loadReports(preferredId) {
  const response = await fetchWithRetry("/api/reports?limit=100");
  if (!response.ok) throw new Error(await apiError(response, "报告列表加载失败"));
  reports = await response.json();
  document.querySelector("#report-total").textContent = reports.length;
  selectedId = preferredId || new URLSearchParams(location.search).get("id") || selectedId || reports[0]?.id || null;
  renderList();
  if (selectedId) await selectReport(selectedId, false);
  else showEmpty();
}

function renderList() {
  document.querySelectorAll(".filter-row button").forEach(button => button.classList.toggle("active", button.dataset.filter === activeFilter));
  const filtered = activeFilter === "saved" ? reports.filter(item => item.is_saved)
    : activeFilter === "all" ? reports : reports.filter(item => item.report_type === activeFilter);
  listEl.replaceChildren();
  if (!filtered.length) {
    const empty = document.createElement("div"); empty.className = "empty-list"; empty.textContent = activeFilter === "saved" ? "还没有保存报告" : "暂无报告"; listEl.append(empty); return;
  }
  filtered.forEach(item => {
    const button = document.createElement("button");
    button.className = `report-entry ${item.id === selectedId ? "active" : ""}`;
    button.dataset.id = item.id;
    const meta = document.createElement("div"); meta.className = "entry-meta";
    const typeLabel = item.report_type === "daily" ? "Daily Strategy" : "Tactical Monitor";
    const date = document.createElement("span"); date.textContent = `${fmtDate(item.as_of)} · ${item.slot === "manual" ? "即时" : item.slot} · ${typeLabel}`;
    meta.append(date);
    if (item.is_saved) { const saved = document.createElement("span"); saved.className = "saved-mark"; saved.textContent = "◆ 已保存"; meta.append(saved); }
    const title = document.createElement("strong"); title.textContent = item.title;
    const retention = document.createElement("small"); retention.textContent = item.is_saved ? "长期保留" : `自动删除：${fmtDate(item.expires_at)}`;
    button.append(meta,title,retention);
    button.addEventListener("click", () => selectReport(item.id));
    listEl.append(button);
  });
}

function showEmpty() {
  document.querySelector("#reader-empty").hidden = false;
  document.querySelector("#reader-content").hidden = true;
}

async function selectReport(id, updateUrl = true) {
  const response = await fetchWithRetry(`/api/reports/${id}`);
  if (!response.ok) { showToast(await apiError(response, "报告不存在或已被清理")); if (response.status === 404) showEmpty(); return; }
  const report = await response.json();
  selectedId = id;
  if (updateUrl && new URLSearchParams(location.search).get("id") !== id) history.pushState({filter:activeFilter},"",`/reports?id=${id}`);
  renderList(); renderReport(report);
}

function renderReport(report) {
  const meta = reports.find(item => item.id === report.id);
  document.querySelector("#reader-empty").hidden = true;
  document.querySelector("#reader-content").hidden = false;
  const isArticle = Boolean(report.article);
  const reportLabel = report.report_type === "daily" ? "Daily Market Strategy" : report.report_type === "midday" ? "历史午报" : "Tactical Monitor";
  document.querySelector("#detail-slot").textContent = `${report.slot === "manual" ? "即时" : report.slot} ${reportLabel}`;
  document.querySelector("#detail-title").textContent = report.title;
  document.querySelector("#detail-meta").textContent = `数据时点 ${fmtDate(report.as_of)} · ${report.methodology_version} · 报告 ID ${report.id}`;
  document.querySelector("#detail-regime").textContent = report.market_regime + " · " + (report.editorial_audit?.status==="model"?"DeepSeek 编辑":"数据编发");
  document.querySelector("#detail-judgement").textContent = report.executive_judgement;
  renderBulletList("#detail-takeaways", report.key_takeaways || [report.executive_judgement]);
  renderBulletList("#detail-changes", report.what_changed || ["该历史报告未记录相对上期变化。"]);
  const saveButton = document.querySelector("#save-report");
  saveButton.classList.toggle("saved", Boolean(meta?.is_saved));
  saveButton.textContent = meta?.is_saved ? "已保存 · 取消" : "保存报告";
  document.querySelector("#export-md").href = `/api/reports/${report.id}/export/markdown`;
  document.querySelector("#export-json").href = `/api/reports/${report.id}/export/json`;
  const standardReport=document.querySelector("#standard-report");
  standardReport.hidden = isArticle;
  standardReport.className=report.daily_strategy?"daily-product":report.tactical_monitor?"tactical-product":"legacy-product";
  document.querySelector(".executive-block").hidden=Boolean(report.daily_strategy||report.tactical_monitor);
  document.querySelector("#article-report").hidden = !isArticle;
  if (isArticle) renderArticle(report);
  else {
    const productNodes = report.daily_strategy ? renderDailyStrategy(report.daily_strategy)
      : report.tactical_monitor ? renderTacticalMonitor(report.tactical_monitor)
      : (report.assets || []).map(renderAsset);
    const evidence=productSection("SOURCES","数据与来源");evidence.id="evidence";
    for(const chart of report.charts||[]) evidence.append(renderChart(chart));
    for (const fact of report.facts||[]) {
      const row=document.createElement("div");row.className="source-record";row.id="fact-"+fact.id;
      const label=document.createElement(fact.url ? "a" : "span");label.textContent=fact.id+"  "+fact.statement;
      if(fact.url){label.href=fact.url;label.target="_blank";label.rel="noreferrer";}
      const desc=document.createElement("small");desc.textContent=[fact.source,fmtDate(fact.as_of),fact.window,fact.method].filter(Boolean).join(" · ");
      row.append(label,desc);evidence.append(row);
    }
    const summary=productSection("RESEARCH SUMMARY","编辑摘要");addList(summary,report.key_takeaways);
    const host=document.querySelector("#asset-reports"); host.replaceChildren(...productNodes,summary,evidence);
    for(const card of host.querySelectorAll(".tactical-card,.daily-asset-card")){
      const sym=card.textContent.match(/BTC|ETH|SOL/)?.[0];
      if(sym){card.id="asset-"+sym;const a=document.createElement("a");a.className="asset-jump";a.href="/terminal?asset="+sym;a.textContent="查看 "+sym+" 行情 ↗";card.append(a);}
    }
    for (const el of host.querySelectorAll("p,li")) {
      if (!/\[F\d+\]/.test(el.textContent) || el.children.length) continue;
      const parts=el.textContent.split(/(\[F\d+\])/);el.replaceChildren(...parts.map(t=>{
        if(!/^\[F\d+\]$/.test(t))return t;
        const a=document.createElement("a");a.className="citation";a.href="#fact-"+t.slice(1,-1);a.textContent=t;return a;
      }));
    }
    requestAnimationFrame(highlightReportEvidence);

    renderNews(report.news);
  }
  document.querySelector("#detail-disclaimer").textContent = `${report.disclaimer} 未保存报告在生成三天后自动删除；保存报告长期保留。`;
}

function productSection(code,title){const section=document.createElement("section");section.className="asset-report product-v2";section.append(sectionHeading(code,title));return section}
function addList(section,items,empty="N/A"){const list=document.createElement("ul");(items?.length?items:[empty]).forEach(text=>{const li=document.createElement("li");li.textContent=text;list.append(li)});section.append(list)}
function observationText(item){if(!item.is_available)return `${item.label}: N/A`;const value=typeof item.value==="number"?Number(item.value).toLocaleString("en-US",{maximumFractionDigits:4}):item.value;return `${item.label}: ${value}${item.unit||""}`}
function appendMetric(parent,label,value,className=""){
  const cell=document.createElement("div");if(className)cell.className=className;
  const name=document.createElement("span");name.textContent=label;
  const result=document.createElement("strong");result.textContent=value??"N/A";
  cell.append(name,result);parent.append(cell);return cell;
}
function appendLabeledText(parent,label,text){
  const row=document.createElement("p");const strong=document.createElement("b");strong.textContent=label;
  row.append(strong,document.createTextNode(text||"N/A"));parent.append(row);return row;
}
function safeExternalUrl(value){try{const url=new URL(value,location.origin);return ["http:","https:"].includes(url.protocol)?url.href:"#"}catch{return "#"}}
function renderReferencePlan(plan){
  const item=document.createElement("div");item.className=`trade-setup trade-setup-${(plan.direction||"wait").toLowerCase()}`;
  const title=document.createElement("h4");title.textContent=`${plan.symbol}｜${plan.direction==="LONG"?"做多":plan.direction==="SHORT"?"做空":"等待"}｜${plan.state}`;item.append(title);
  const thesis=document.createElement("p");thesis.className="trade-advice";thesis.textContent=plan.thesis;item.append(thesis);
  const grid=document.createElement("div");grid.className="trade-levels";
  const targets=plan.target_plan?.length?plan.target_plan:(plan.targets||[]).map((price,index)=>({label:`TP${index+1}`,price,risk_reward:[1,1.8,2.8][index],reduce_pct:[40,30,30][index]}));
  [["观察入场区",`${money(plan.entry_low)} – ${money(plan.entry_high)}`],["硬止损",money(plan.stop)],...targets.map(target=>[`${target.label} · ${target.risk_reward}R`,`${money(target.price)} · 减仓 ${target.reduce_pct}%`]),["计划盈亏比",plan.expected_r==null?"N/A":`${plan.expected_r}R`],["有效期",plan.expiration?new Date(plan.expiration).toLocaleString("zh-CN"):plan.time_horizon],["数据状态",plan.data_status||"FRESH"]].forEach(([label,value])=>{const metric=document.createElement("div");const l=document.createElement("span");l.textContent=label;const v=document.createElement("strong");v.textContent=value;metric.append(l,v);grid.append(metric)});item.append(grid);
  const rules=document.createElement("div");rules.className="trade-rules";[["触发",plan.trigger],["执行",plan.entry_method],["取消",plan.cancel_condition||plan.invalidation]].forEach(([label,text])=>{const row=document.createElement("p");const strong=document.createElement("strong");strong.textContent=label;row.append(strong,document.createTextNode(text||"N/A"));rules.append(row)});item.append(rules);return item;
}

function renderDailyStrategy(daily){
  const nodes=[];
  const market=daily.market_day||{};
  const overview=productSection("DAILY RECAP",daily.session_window?`${daily.session_window.session_date}｜全天市场概览`:daily.market_regime);overview.classList.add("daily-overview");
  const lead=document.createElement("p");lead.className="product-lead";lead.textContent=market.one_liner||daily.key_takeaways?.[0]||daily.market_regime;overview.append(lead);
  const strip=document.createElement("div");strip.className="metric-strip overview-metrics";
  [["全市场总市值",money(market.total_market_cap_usd)],["总市值 24H",market.total_market_cap_change_24h_pct==null?"N/A":`${market.total_market_cap_change_24h_pct>=0?"+":""}${market.total_market_cap_change_24h_pct.toFixed(2)}%`],["全市场成交量",money(market.total_volume_24h_usd)],["BTC 市占率",market.btc_dominance_pct==null?"N/A":`${market.btc_dominance_pct.toFixed(2)}%`]].forEach(([label,value])=>appendMetric(strip,label,value,"metric"));overview.append(strip);
  const context=document.createElement("div");context.className="context-grid";
  const fear=daily.sentiment;const liq=daily.liquidations;
  [["市场宽度",market.breadth||"N/A"],["恐慌贪婪",fear?.value==null?"N/A":`${fear.value} · ${fear.classification}${fear.previous_value==null?"":` · 前日 ${fear.previous_value}`}`],["24H 爆仓",liq?.total_usd_24h==null?"N/A":`${money(liq.total_usd_24h)} · 多 ${money(liq.long_usd_24h)} · 空 ${money(liq.short_usd_24h)}`],["爆仓覆盖",liq?.coverage?`${liq.coverage} · ${liq.window||"24H"} · ${liq.source||"未标注"}${liq.note?` · ${liq.note}`:""}`:"UNAVAILABLE"]].forEach(([label,text])=>{const cell=document.createElement("div");const strong=document.createElement("strong");strong.textContent=label;const p=document.createElement("p");p.textContent=text;cell.append(strong,p);context.append(cell)});overview.append(context);nodes.push(overview);

  const assets=productSection("ASSETS","BTC / ETH / SOL 全天走势");const assetGrid=document.createElement("div");assetGrid.className="daily-asset-grid";
  (daily.executive_dashboard||[]).forEach(asset=>{
    const card=document.createElement("article");card.className="daily-asset-card";
    const head=document.createElement("div");head.className="asset-card-head";
    [asset.strength_rank?`#${asset.strength_rank}`:"—",asset.symbol,asset.view].forEach((value,index)=>{const node=document.createElement(index===1?"h4":index===2?"b":"span");node.textContent=value;head.append(node)});card.append(head);
    const day=asset.return_day_pct;const tape=document.createElement("div");tape.className="asset-tape";
    [["开",asset.session_open],["高",asset.session_high],["低",asset.session_low],["收",asset.session_close]].forEach(([label,value])=>appendMetric(tape,label,money(value)));card.append(tape);
    const move=document.createElement("p");move.className=`day-move ${(day||0)>=0?"positive":"negative"}`;move.textContent=day==null?`当日 N/A · 成交量 ${asset.session_volume==null?"N/A":Number(asset.session_volume).toLocaleString()+" "+(asset.session_volume_unit||"")}`:`当日 ${day>=0?"+":""}${Number(day).toFixed(2)}% · 最大回撤 ${asset.max_drawdown_pct==null?"N/A":Number(asset.max_drawdown_pct).toFixed(2)+"%"} · 成交量 ${asset.session_volume==null?"N/A":Number(asset.session_volume).toLocaleString()+" "+(asset.session_volume_unit||"")}`;card.append(move);
    const summary=document.createElement("p");summary.textContent=asset.trend_summary||asset.what_changed;card.append(summary);
    if (asset.comparison_notes?.length) addList(card, asset.comparison_notes);
    const levels=document.createElement("div");levels.className="level-row";appendMetric(levels,"支撑",(asset.supports||[]).map(money).join(" / ")||"N/A");appendMetric(levels,"压力",(asset.resistances||[]).map(money).join(" / ")||"N/A");card.append(levels);
    const details=document.createElement("details");const detailSummary=document.createElement("summary");detailSummary.textContent="证据与未来判断";details.append(detailSummary);appendLabeledText(details,"主导：",asset.key_driver);appendLabeledText(details,"反方：",(asset.contradicting_evidence||[]).join("；"));appendLabeledText(details,"24–72H：",asset.thesis);appendLabeledText(details,"失效：",asset.invalidation);card.append(details);assetGrid.append(card);
  });assets.append(assetGrid);nodes.push(assets);

  const chain=productSection("ON-CHAIN","链上积极度");const chainGrid=document.createElement("div");chainGrid.className="chain-grid";(daily.chain_activity||[]).forEach(item=>{const card=document.createElement("article");card.className="chain-card";const head=document.createElement("div");[item.symbol,item.activity,`覆盖 ${item.coverage_pct}%`].forEach((value,index)=>{const node=document.createElement(index===0?"span":index===1?"strong":"small");node.textContent=value;head.append(node)});const conclusion=document.createElement("p");conclusion.textContent=item.conclusion;const metrics=document.createElement("dl");[["费用",money(item.fees_24h_usd)],["TVL",money(item.tvl_usd)],["稳定币 7D",item.stablecoin_supply_change_7d_pct==null?"N/A":`${Number(item.stablecoin_supply_change_7d_pct).toFixed(2)}%`],["活跃地址",item.active_addresses_24h==null?"N/A":Number(item.active_addresses_24h).toLocaleString()],["交易数",item.transactions_24h==null?"N/A":Number(item.transactions_24h).toLocaleString()]].forEach(([label,value])=>{const row=document.createElement("div");const dt=document.createElement("dt");dt.textContent=label;const dd=document.createElement("dd");dd.textContent=value;row.append(dt,dd);metrics.append(row)});card.append(head,conclusion,metrics);chainGrid.append(card)});chain.append(chainGrid);nodes.push(chain);

  const events=productSection("EVENT IMPACT","新闻、宏观与链上情绪");if(daily.news_impacts?.length){daily.news_impacts.forEach(item=>{const row=document.createElement("article");row.className="impact-row";const title=document.createElement("a");title.href=safeExternalUrl(item.url);title.target="_blank";title.rel="noreferrer";title.textContent=item.headline;const meta=document.createElement("small");meta.textContent=`${fmtDate(item.occurred_at)} · ${item.source} · ${item.category} · ${item.confidence}`;const observed=document.createElement("p");observed.textContent=`已观察：${item.observed_market_reaction}`;const path=document.createElement("p");path.textContent=`传导：${item.transmission_path}`;const sentiment=document.createElement("p");sentiment.textContent=`链上情绪：${item.onchain_sentiment_impact}`;row.append(title,meta,observed,path,sentiment);events.append(row)})}else addList(events,[],"自然日内没有达到来源与相关性门槛的重大事件。");nodes.push(events);

  const outlook=productSection("24–72H","未来大盘情景");const outlookGrid=document.createElement("div");outlookGrid.className="outlook-grid";(daily.outlook||[]).forEach(item=>{const card=document.createElement("article");const badge=document.createElement("span");badge.textContent=`${item.name} · ${item.likelihood}`;const title=document.createElement("h4");title.textContent=item.market_path;card.append(badge,title);appendLabeledText(card,"触发：",item.condition);appendLabeledText(card,"确认：",item.confirmation);appendLabeledText(card,"失效：",item.invalidation);outlookGrid.append(card)});outlook.append(outlookGrid);nodes.push(outlook);

  const health=productSection("DATA","数据质量");const details=document.createElement("details");details.className="data-health-groups";const summary=document.createElement("summary");summary.textContent="按数据源查看可用与缺失指标";details.append(summary);const groups=daily.data_health_groups?.length?daily.data_health_groups:(daily.data_health||[]).map(item=>({source:item.source,availability:item.availability,quality:item.quality,freshness:item.freshness,available_features:[item.feature],missing_features:[]}));details.open=groups.some(item=>item.availability!=="AVAILABLE");groups.forEach(item=>{const row=document.createElement("div");row.className="health-row";const source=document.createElement("strong");source.textContent=item.source;const status=document.createElement("span");status.textContent=`${item.availability} · ${item.quality} · ${item.freshness}`;const coverage=document.createElement("p");coverage.append(document.createTextNode(`可用：${(item.available_features||[]).join("、")||"无"}`),document.createElement("br"),document.createTextNode(`缺失：${(item.missing_features||[]).join("、")||"无"}`));row.append(source,status,coverage);details.append(row)});health.append(details);nodes.push(health);return nodes;
}

function renderTacticalMonitor(tactical){
  const nodes=[];const overview=productSection("MARKET NOW","一句话大盘");overview.classList.add("tactical-overview");const lead=document.createElement("p");lead.className="product-lead";lead.textContent=tactical.market_brief||tactical.market_overview?.[0]||"数据不足";overview.append(lead);const metrics=tactical.market_metrics;if(metrics){const strip=document.createElement("div");strip.className="metric-strip overview-metrics";[["总市值",money(metrics.total_market_cap_usd)],["总市值 24H",metrics.market_cap_change_24h_pct==null?"N/A":`${metrics.market_cap_change_24h_pct>=0?"+":""}${metrics.market_cap_change_24h_pct.toFixed(2)}%`],["全市场成交量",money(metrics.total_volume_24h_usd)],["BTC 市占率",metrics.btc_dominance_pct==null?"N/A":`${metrics.btc_dominance_pct.toFixed(2)}%`],["市场宽度",tactical.market_breadth||"N/A"],["强弱排序",tactical.leadership||"N/A"]].forEach(([label,value])=>appendMetric(strip,label,value,"metric"));overview.append(strip)}nodes.push(overview);

  const actions=productSection("TRADE PLANS","BTC / ETH / SOL 实时决策");const grid=document.createElement("div");grid.className="tactical-action-grid";(tactical.dashboard||[]).forEach(asset=>{const card=document.createElement("article");card.className=`tactical-card decision-${(asset.decision||"unclear").toLowerCase()}`;const head=document.createElement("div");head.className="tactical-card-head";const identity=document.createElement("div");const symbol=document.createElement("span");symbol.textContent=asset.symbol;const price=document.createElement("h4");price.textContent=money(asset.price);identity.append(symbol,price);const badge=document.createElement("b");badge.textContent=asset.decision==="LONG"?"明确偏多":asset.decision==="SHORT"?"明确偏空":"走势不明 · 观望";head.append(identity,badge);card.append(head);const marketTape=document.createElement("div");marketTape.className="asset-market-tape";[["24H",asset.return_24h_pct==null?"N/A":`${asset.return_24h_pct>=0?"+":""}${Number(asset.return_24h_pct).toFixed(2)}%`],["成交额",money(asset.volume_24h_usd)],["资金费率",asset.funding_rate==null?"N/A":`${(Number(asset.funding_rate)*100).toFixed(4)}%`],["OI 1H",asset.open_interest_change_1h_pct==null?"N/A":`${asset.open_interest_change_1h_pct>=0?"+":""}${Number(asset.open_interest_change_1h_pct).toFixed(2)}%`]].forEach(([label,value])=>appendMetric(marketTape,label,value));card.append(marketTape);const trends=document.createElement("div");trends.className="trend-strip";[["15M",asset.trend_15m||"N/A"],["1H",asset.trend_1h],["4H",asset.trend_4h],["1D",asset.trend_1d]].forEach(([label,value])=>appendMetric(trends,label,value));card.append(trends);const judgement=document.createElement("p");judgement.className="tactical-judgement";judgement.textContent=asset.trend_summary||asset.thesis_status_reason;card.append(judgement);const levels=document.createElement("div");levels.className="level-row";appendMetric(levels,"支撑",(asset.supports||[]).map(money).join(" / ")||"N/A");appendMetric(levels,"压力",(asset.resistances||[]).map(money).join(" / ")||"N/A");card.append(levels);const plans=asset.primary_setup?[asset.primary_setup]:(asset.conditional_setups?.length?asset.conditional_setups:asset.reference_setups||[]);plans.forEach(plan=>card.append(renderReferencePlan(plan)));const details=document.createElement("details");details.className="tactical-details";const detailSummary=document.createElement("summary");detailSummary.textContent="查看分析证据与数据时效";details.append(detailSummary);appendLabeledText(details,"主导：",asset.dominant_driver);appendLabeledText(details,"反方：",asset.counter_evidence);appendLabeledText(details,"资金：",asset.positioning);appendLabeledText(details,"未平仓：",money(asset.open_interest_usd));appendLabeledText(details,"数据时效：",`价格 ${asset.price_freshness_seconds==null?"N/A":Math.floor(asset.price_freshness_seconds/60)+" 分钟"} · 衍生品 ${asset.derivatives_freshness_seconds==null?"N/A":Math.floor(asset.derivatives_freshness_seconds/60)+" 分钟"}`);card.append(details);grid.append(card)});actions.append(grid);nodes.push(actions);

  const analysis=productSection("ANALYSIS","大盘分析与检查点变化");const details=document.createElement("details");details.className="market-analysis-details";details.open=true;const summary=document.createElement("summary");summary.textContent="查看大盘分析";details.append(summary);addList(details,[...(tactical.market_overview||[]),...(tactical.since_last_checkpoint||[])]);analysis.append(details);nodes.push(analysis);const next=productSection("NEXT","下一检查点前观察");addList(next,tactical.before_next_checkpoint);nodes.push(next);return nodes;
}

function renderBulletList(selector, items) {
  const list = document.querySelector(selector); list.replaceChildren();
  items.forEach(text => { const item=document.createElement("li"); item.textContent=text; list.append(item); });
}

function appendInline(target, text) {
  const pattern = /(\*\*([^*]+)\*\*|\[([^\]]+)\]\((https?:\/\/[^)]+)\)|\*([^*]+)\*)/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    target.append(document.createTextNode(text.slice(cursor, match.index)));
    if (match[2]) { const strong=document.createElement("strong"); strong.textContent=match[2]; target.append(strong); }
    else if (match[3]) { const link=document.createElement("a"); link.textContent=match[3]; link.href=match[4]; link.target="_blank"; link.rel="noreferrer"; target.append(link); }
    else { const em=document.createElement("em"); em.textContent=match[5]; target.append(em); }
    cursor = match.index + match[0].length;
  }
  target.append(document.createTextNode(text.slice(cursor)));
}

function renderLegacyArticle(markdown) {
  const container = document.querySelector("#article-report"); container.replaceChildren();
  let list = null;
  markdown.split(/\r?\n/).forEach(raw => {
    const line = raw.trim();
    if (!line || line.startsWith("# ")) return;
    if (line.startsWith("- ")) {
      if (!list) { list=document.createElement("ul"); container.append(list); }
      const item=document.createElement("li"); appendInline(item,line.slice(2)); list.append(item); return;
    }
    list = null;
    let element, lineText;
    if (line.startsWith("## ")) { element=document.createElement("h2"); lineText=line.slice(3); }
    else if (line.startsWith("### ")) { element=document.createElement("h3"); lineText=line.slice(4); }
    else if (line.startsWith("> ")) { element=document.createElement("blockquote"); lineText=line.slice(2); }
    else { element=document.createElement("p"); lineText=line; }
    appendInline(element, lineText); container.append(element);
  });
}

function articleHeading(text, level = 2) {
  const heading=document.createElement(`h${level}`); heading.textContent=text; return heading;
}

function renderCitations(target, factIds) {
  if (!factIds?.length) return;
  const wrap=document.createElement("span"); wrap.className="fact-citations";
  factIds.forEach(id=>{const mark=document.createElement("a");mark.href=`#fact-${id}`;mark.textContent=id;wrap.append(mark);});
  target.append(wrap);
}

function renderChart(chart) {
  const figure=document.createElement("figure"); figure.className="research-chart";
  const caption=document.createElement("figcaption");
  const title=document.createElement("strong"); title.textContent=chart.title;
  const subtitle=document.createElement("span"); subtitle.textContent=chart.subtitle || chart.unit;
  caption.append(title,subtitle); figure.append(caption);
  const width=760,height=300,pad={left:52,right:18,top:20,bottom:36};
  const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");
  svg.setAttribute("viewBox",`0 0 ${width} ${height}`); svg.setAttribute("role","img"); svg.setAttribute("aria-label",chart.title);
  const all=(chart.series||[]).flatMap(item=>item.values).filter(Number.isFinite);
  let min=Math.min(...all),max=Math.max(...all); if(!Number.isFinite(min)||!Number.isFinite(max)){min=0;max=1;} if(min===max){min-=1;max+=1;}
  const range=max-min; min-=range*.08; max+=range*.08;
  const x=i=>pad.left+i*(width-pad.left-pad.right)/Math.max(1,(chart.labels?.length||1)-1);
  const y=v=>pad.top+(max-v)*(height-pad.top-pad.bottom)/(max-min);
  [0,.25,.5,.75,1].forEach(step=>{const gy=pad.top+step*(height-pad.top-pad.bottom);const line=document.createElementNS(svg.namespaceURI,"line");line.setAttribute("x1",pad.left);line.setAttribute("x2",width-pad.right);line.setAttribute("y1",gy);line.setAttribute("y2",gy);line.setAttribute("class","chart-grid");svg.append(line);const label=document.createElementNS(svg.namespaceURI,"text");label.setAttribute("x",pad.left-8);label.setAttribute("y",gy+4);label.setAttribute("text-anchor","end");label.setAttribute("class","chart-axis");label.textContent=(max-step*(max-min)).toLocaleString(undefined,{maximumFractionDigits:2});svg.append(label);});
  const colors=["#55ded7","#77aaff","#b3a9ed","#ef9b85"];
  if(chart.chart_type==="bar"){
    const groups=chart.labels.length,seriesCount=chart.series.length,groupWidth=(width-pad.left-pad.right)/Math.max(1,groups),barWidth=Math.min(42,groupWidth*.72/seriesCount);
    chart.series.forEach((series,si)=>series.values.forEach((value,i)=>{const rect=document.createElementNS(svg.namespaceURI,"rect");rect.setAttribute("x",pad.left+i*groupWidth+groupWidth*.14+si*barWidth);rect.setAttribute("y",Math.min(y(0),y(value)));rect.setAttribute("width",Math.max(2,barWidth-3));rect.setAttribute("height",Math.max(1,Math.abs(y(0)-y(value))));rect.setAttribute("fill",colors[si%colors.length]);svg.append(rect);}));
  }else{
    chart.series.forEach((series,si)=>{const path=document.createElementNS(svg.namespaceURI,"polyline");path.setAttribute("points",series.values.map((value,i)=>`${x(i)},${y(value)}`).join(" "));path.setAttribute("fill","none");path.setAttribute("stroke",colors[si%colors.length]);path.setAttribute("stroke-width","2");path.setAttribute("vector-effect","non-scaling-stroke");svg.append(path);});
  }
  figure.append(svg);
  const legend=document.createElement("div");legend.className="chart-legend";chart.series.forEach((series,index)=>{const item=document.createElement("span");item.style.setProperty("--series",colors[index%colors.length]);item.textContent=series.name;legend.append(item);});figure.append(legend);
  const insight=document.createElement("p");insight.textContent=chart.insight;const source=document.createElement("small");source.textContent=`数据源：${chart.source}`;figure.append(insight,source);return figure;
}

function renderArticle(report) {
  if (!report.article) { renderLegacyArticle(report.markdown); return; }
  const container=document.querySelector("#article-report");container.replaceChildren();
  const article=report.article;
  const deck=document.createElement("p");deck.className="article-deck";deck.textContent=article.deck;
  const question=document.createElement("div");question.className="research-question";const qLabel=document.createElement("span");qLabel.textContent="本期问题";const qText=document.createElement("strong");qText.textContent=article.research_question;question.append(qLabel,qText);
  const lead=document.createElement("p");lead.className="article-lead";lead.textContent=article.lead;container.append(deck,question,lead,articleHeading("市场共识与 Meridian 判断"));
  [["市场共识",article.consensus_view],["Meridian 判断",article.meridian_view]].forEach(([label,text])=>{const p=document.createElement("p");const strong=document.createElement("strong");strong.textContent=`${label}。`;p.append(strong,document.createTextNode(text));container.append(p);});
  article.sections.forEach(section=>{container.append(articleHeading(section.heading));section.paragraphs.forEach(paragraph=>{const p=document.createElement("p");p.textContent=paragraph.text;renderCitations(p,paragraph.fact_ids);container.append(p);});});
  container.append(articleHeading("图表证据"));
  if(report.charts?.length) report.charts.forEach(chart=>container.append(renderChart(chart))); else {const p=document.createElement("p");p.textContent="本期没有满足完整性要求的历史序列。";container.append(p);}
  container.append(articleHeading("投资含义与执行边界"));
  (report.assets||[]).sort((a,b)=>b.conviction-a.conviction).forEach(asset=>{const block=document.createElement("div");block.className="article-asset-implication";const h=articleHeading(`${asset.symbol}｜${asset.stance}`,3);const p=document.createElement("p");p.textContent=`${asset.thesis} 首选方案：${asset.trade_setups?.[0]?.name || "等待触发"}。`;block.append(h,p);container.append(block);});
  container.append(articleHeading("结论"));const conclusion=document.createElement("p");conclusion.className="article-conclusion";conclusion.textContent=article.conclusion;container.append(conclusion);
  const details=document.createElement("details");details.className="fact-ledger";const summary=document.createElement("summary");summary.textContent=`事实与来源 · ${report.facts?.length||0} 条`;const list=document.createElement("ol");(report.facts||[]).forEach(fact=>{const li=document.createElement("li");li.id=`fact-${fact.id}`;const strong=document.createElement("strong");strong.textContent=`${fact.id} `;const source=document.createElement("small");source.textContent=`${fact.source} · ${fmtDate(fact.as_of)}`;li.append(strong,document.createTextNode(fact.statement),source);list.append(li);});details.append(summary,list);container.append(details);
}

function renderAsset(asset) {
  const section = document.createElement("section"); section.className = "asset-report";
  const call = document.createElement("div"); call.className = "asset-call";
  const symbol = document.createElement("span"); symbol.className = "asset-symbol"; symbol.textContent = asset.symbol;
  const copy = document.createElement("div");
  const title = document.createElement("h3"); title.textContent = `${asset.stance} · ${money(asset.price)}`;
  const thesis = document.createElement("p"); thesis.textContent = asset.thesis; copy.append(title,thesis);
  const conviction = document.createElement("div"); conviction.className = "conviction";
  const convictionValue = document.createElement("strong"); convictionValue.textContent = `${asset.conviction}/100`;
  const convictionLabel = document.createElement("span"); convictionLabel.textContent = "观点置信度"; conviction.append(convictionValue,convictionLabel);
  call.append(symbol,copy,conviction); section.append(call);
  const strip = document.createElement("div"); strip.className = "metric-strip";
  [["24H 涨跌",`${asset.change_24h_pct >= 0 ? "+" : ""}${asset.change_24h_pct.toFixed(2)}%`],["目标区间",`${money(asset.target_low)} – ${money(asset.target_high)}`],["结构失效",`${money(asset.invalidation_price)} · ${asset.invalidation_condition}`],["证据质量",`${asset.data_quality}/100`]].forEach(([label,value]) => {
    const metric = document.createElement("div"); metric.className = "metric";
    const l = document.createElement("span"); l.textContent = label; const v = document.createElement("strong"); v.textContent = value; metric.append(l,v); strip.append(metric);
  }); section.append(strip,renderDecisionFrame(asset));
  const details=document.createElement("details");details.className="tactical-details";const summary=document.createElement("summary");summary.textContent="展开完整执行计划、证据与情景";details.append(summary,renderTradeSetups(asset.trade_setups || []),renderDimensions(asset.dimensions),renderScenarios(asset.scenarios));section.append(details);
  return section;
}

function renderDecisionFrame(asset) {
  const block=document.createElement("div"); block.className="subsection decision-frame"; block.append(sectionHeading("THESIS","判断关键与反方观点"));
  const grid=document.createElement("div"); grid.className="decision-grid";
  const items=[
    ["判断关键",asset.decision_hinge || asset.thesis],
    ["反方观点",asset.counter_case || `价格触及 ${money(asset.invalidation_price)} 后重新评估。`],
  ];
  items.forEach(([label,text])=>{const item=document.createElement("div");const title=document.createElement("strong");title.textContent=label;const copy=document.createElement("p");copy.textContent=text;item.append(title,copy);grid.append(item);});
  block.append(grid);
  const watch=document.createElement("div"); watch.className="monitoring-list"; const label=document.createElement("strong"); label.textContent="下一步监测"; const list=document.createElement("ul");
  (asset.monitoring_points || []).forEach(text=>{const item=document.createElement("li");item.textContent=text;list.append(item);}); watch.append(label,list); block.append(watch); return block;
}

function sectionHeading(code, title) {
  const head = document.createElement("div"); head.className = "section-title";
  const c = document.createElement("span"); c.textContent = code; const h = document.createElement("h3"); h.textContent = title; head.append(c,h); return head;
}

function renderTradeSetups(setups) {
  const block = document.createElement("div"); block.className = "subsection trade-plans"; block.append(sectionHeading("EXECUTION","做单计划与风控"));
  if (!setups.length) { const empty=document.createElement("p"); empty.className="empty-list"; empty.textContent="该历史报告生成于交易计划升级前。"; block.append(empty); return block; }
  setups.forEach(setup => {
    const item=document.createElement("div"); item.className="trade-setup";
    const head=document.createElement("div"); head.className="trade-setup-head";
    const badges=document.createElement("div"); badges.className="trade-badges";
    [setup.priority,setup.direction,setup.status].forEach((text,index)=>{const badge=document.createElement("span");badge.textContent=text;badge.className=index===1?(setup.direction==="做多"?"long":"short"):"";badges.append(badge);});
    const name=document.createElement("h4"); name.textContent=setup.name; head.append(badges,name);
    const advice=document.createElement("p"); advice.className="trade-advice"; advice.textContent=setup.advice; head.append(advice); item.append(head);
    const grid=document.createElement("div"); grid.className="execution-grid";
    const metrics=[
      ["入场区",`${money(setup.entry_low)} – ${money(setup.entry_high)}`],
      ["硬止损",money(setup.stop_loss)],
      ...setup.targets.map(target=>[`${target.label} · ${target.risk_reward.toFixed(1)}R`,`${money(target.price)} · 减仓 ${target.reduce_pct}%`]),
      ["单笔风险",`账户权益 ${setup.risk_per_trade_pct.toFixed(2)}%`],
      ["杠杆上限",`${setup.max_leverage}x`],
    ];
    metrics.forEach(([label,value])=>{const metric=document.createElement("div");metric.className="execution-metric";const l=document.createElement("span");l.textContent=label;const v=document.createElement("strong");v.textContent=value;metric.append(l,v);grid.append(metric);}); item.append(grid);
    const rules=document.createElement("div"); rules.className="execution-rules";
    [["触发",setup.trigger],["执行",setup.entry_method],["取消",setup.cancel_condition],["仓位",setup.position_size_formula]].forEach(([label,text])=>{const row=document.createElement("p");const strong=document.createElement("strong");strong.textContent=label;row.append(strong,document.createTextNode(text));rules.append(row);}); item.append(rules);
    const basis=document.createElement("div"); basis.className="trade-basis"; const label=document.createElement("span");label.textContent="建议依据";const list=document.createElement("ul");setup.rationale.forEach(text=>{const li=document.createElement("li");li.textContent=text;list.append(li);});basis.append(label,list);item.append(basis); block.append(item);
  }); return block;
}

function renderDimensions(dimensions) {
  const block = document.createElement("div"); block.className = "subsection"; block.append(sectionHeading("EVIDENCE","证据账本"));
  dimensions.forEach(item => {
    const row = document.createElement("div"); row.className = "dimension-row";
    const label = document.createElement("span"); label.className = "dimension-label"; label.textContent = item.label;
    const copy = document.createElement("div"); copy.className = "dimension-copy"; const headline = document.createElement("strong"); headline.textContent = item.headline;
    const list = document.createElement("ul"); item.evidence.forEach(text => { const li=document.createElement("li"); li.textContent=text; list.append(li); }); copy.append(headline,list);
    const score = document.createElement("span"); score.className = "dimension-score"; score.textContent = `方向 ${item.score >= 0 ? "+" : ""}${item.score}\n覆盖 ${item.coverage}%`;
    row.append(label,copy,score); block.append(row);
  }); return block;
}

function renderScenarios(scenarios) {
  const block = document.createElement("div"); block.className = "subsection"; block.append(sectionHeading("SCENARIOS","未来 24 小时路径"));
  const table = document.createElement("table"); table.className = "scenario-table";
  table.innerHTML = "<thead><tr><th>情景</th><th>概率</th><th>价格区间</th><th>路径</th></tr></thead>";
  const body = document.createElement("tbody"); scenarios.forEach(item => { const row=document.createElement("tr"); [item.name,`${item.probability}%`,`${money(item.range_low)} – ${money(item.range_high)}`,item.path].forEach(value => { const cell=document.createElement("td"); cell.textContent=value; row.append(cell); }); body.append(row); }); table.append(body); block.append(table); return block;
}

function renderNews(news) {
  const container = document.querySelector("#detail-news"); container.replaceChildren();
  const relevant = news.filter(item => item.source_score >= 72 && item.category !== "NOISE" && item.price_relevance >= .45 && item.causal_path).slice(0,12);
  if (!relevant.length) { const empty=document.createElement("p"); empty.className="empty-list"; empty.textContent="本批次没有达到引用门槛的高相关事件。"; container.append(empty); return; }
  relevant.forEach(item => { const row=document.createElement("div"); row.className="news-item"; const meta=document.createElement("span"); meta.textContent=`${fmtDate(item.published_at)} · ${item.assets.join("/")}`; const link=document.createElement("a"); link.href=item.url; link.target="_blank"; link.rel="noreferrer"; link.textContent=item.title; const source=document.createElement("small"); source.textContent=`${item.source} · 来源分 ${item.source_score}`; row.append(meta,link,source); container.append(row); });
}

document.querySelectorAll(".filter-row button").forEach(button => button.addEventListener("click", () => {
  activeFilter = button.dataset.filter; history.replaceState({filter:activeFilter},"",location.href); document.querySelectorAll(".filter-row button").forEach(item => item.classList.toggle("active", item === button)); renderList();
}));

document.querySelector("#save-report").addEventListener("click", async () => {
  const current = reports.find(item => item.id === selectedId); if (!current) return;
  const response = await mutate(`/api/reports/${selectedId}/saved?saved=${!current.is_saved}`, {method:"PATCH"});
  if (!response.ok) { showToast(response.status === 401 ? "管理令牌无效" : await apiError(response,"保存状态更新失败")); return; }
  showToast(current.is_saved ? "已取消保存，将按三日规则清理" : "报告已保存，不会自动删除"); await loadReports(selectedId);
});

document.querySelector("#delete-report").addEventListener("click", async () => {
  const current = reports.find(item => item.id === selectedId); if (!current) return;
  if (!confirm(`确认删除“${current.title}”？此操作不可恢复。`)) return;
  const response = await mutate(`/api/reports/${selectedId}`, {method:"DELETE"});
  if (!response.ok) { showToast(response.status === 401 ? "管理令牌无效" : await apiError(response,"删除失败")); return; }
  showToast("报告已删除"); selectedId = null; history.replaceState(null,"","/reports"); await loadReports();
});

loadReports().catch(error => { showEmpty(); showToast(error.message); });

function highlightReportEvidence() {
  document.querySelector(".evidence-active")?.classList.remove("evidence-active");
  const target = document.getElementById(location.hash.slice(1));
  if (target) { target.classList.add("evidence-active"); target.scrollIntoView(); }
}
addEventListener("hashchange", highlightReportEvidence);
addEventListener("popstate", () => {
  activeFilter = history.state?.filter || "all";
  const id = new URLSearchParams(location.search).get("id") || reports[0]?.id;
  if (id !== selectedId) loadReports(id);
  else renderList();
});

const archiveToggle = document.querySelector("#archive-toggle");
archiveToggle.addEventListener("click", () => {
  archiveToggle.setAttribute("aria-expanded", String(archiveToggle.getAttribute("aria-expanded") !== "true"));
});
document.querySelector("#edition-archive").addEventListener("click", event => {
  if (event.target.closest(".report-entry,.insight-item")) archiveToggle.setAttribute("aria-expanded", "false");
});
