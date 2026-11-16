import {amount, byId, dataStatus, direction, element, link, names, percent, price, shortDate, stamp, svgNode, symbols} from "./ui.js";

/** @param {import("./market-types").Quote} quote */
function drawSpark(quote) {
  const rows = quote.spark;
  if (rows.length < 2) return null;
  const values = rows.map(r=>r[1]), times = rows.map(r=>Date.parse(r[0]));
  const low = Math.min(...values), range = Math.max(...values)-low || 1;
  const span = times.at(-1)-times[0];
  if (!span) return null;
  const plot = svgNode("svg",{viewBox:"0 0 126 43","aria-hidden":"true",class:"spark "+direction(values.at(-1)-values[0])});
  const path = rows.map((r,i)=>{
    const x = 2+(times[i]-times[0])/span*122, y = 38-(r[1]-low)/range*32;
    return (i && times[i]-times[i-1] <= 3600000 ? "L":"M")+x.toFixed(1)+" "+y.toFixed(1);
  }).join(" ");
  plot.append(svgNode("path",{d:path,fill:"none","stroke-width":1.6,stroke:"currentColor"}));
  return plot;
}
function metric(label, value, title="") {
  const cell = element("div");
  cell.append(element("dt",label),element("dd",value));
  if(title) cell.title=title;
  return cell;
}
/** @param {import("./market-types").SymbolName} symbol @param {import("./market-types").Quote|undefined} quote */
function assetCard(symbol, quote) {
  const card = link("/terminal?asset="+symbol,"","asset-card");
  card.setAttribute("aria-label",symbol+" "+names[symbol]+" 行情终端");
  const head = element("div","","asset-heading"), icon=element("i",symbol==="BTC"?"₿":"","coin coin-"+symbol);
  icon.setAttribute("aria-hidden","true");
  const pair=element("strong",symbol);
  head.append(icon,pair,element("span",names[symbol],"asset-name"),element("span","↗","asset-open"));
  const body=element("div","","asset-body"), quoteText=element("div"), quotePrice=element("div","","quote-price");
  quotePrice.append(element("strong",price(quote?.price)),element("small",quote?.unit||""));
  const move=element("span",percent(quote?.change),"return "+direction(quote?.change));
  quoteText.append(quotePrice,move,element("span","24H","window-label"));body.append(quoteText);
  const spark=quote && drawSpark(quote); if(spark) body.append(spark);
  const foot=element("dl","","asset-details");
  foot.append(metric("24H 成交额",amount(quote?.volume),quote?.source?.includes("Coinbase")?"USD，成交量乘以快照价估算":"按现货交易对报价币计"),
    metric("资金费率",percent(quote?.funding==null?null:quote.funding*100,4),quote?.derivatives_as_of?"衍生品快照 "+stamp(quote.derivatives_as_of):"简报记录或来源未提供独立时间"));
  foot.append(metric("未平仓量",amount(quote?.interest),"永续合约未平仓名义金额，USD"));
  const origin=quote ? (quote.source||"来源未注明")+" · "+shortDate(quote.as_of)+(quote.origin==="report"?" · 出稿快照":"") : "行情尚未取得";
  card.append(head,body,foot,element("small",origin,"quote-origin"));return card;
}
/** @param {import("./market-types").Quote[]} quotes */
export function marketOverview(quotes, failed=false) {
  const grid=byId("asset-rows");
  grid.replaceChildren(...symbols.map(s=>assetCard(s,quotes.find(q=>q.symbol===s))));
  grid.setAttribute("aria-busy","false");
  const observed=quotes.filter(q=>Number.isFinite(q.change));
  const old=quotes.some(q=> Date.now()-Date.parse(q.as_of)>15*60000);
  const up=observed.filter(q=>q.change>0).length, down=observed.filter(q=>q.change<0).length;
  byId("market-summary").textContent= observed.length ?
    `跟踪 ${observed.length} 种主要资产 · 滚动 24 小时内 ${up} 种上涨，${down} 种下跌` :
    "主要资产的现货、成交额与永续合约资金费率";
  if(down) {
    const weakest=observed.reduce((a,b)=>a.change<b.change?a:b);
    byId("market-summary").append(" · "+weakest.symbol+" 跌幅最大");
  }
  const status=failed ? "更新失败" : !quotes.length ? "暂无快照" : old ? "延迟快照" : "行情快照";
  dataStatus(byId("quote-status"),status,failed||old?"stale":quotes.length?"ready":"empty");
  const stamps=quotes.map(q=>Date.parse(q.as_of)).filter(Number.isFinite);
  byId("monitor-time").textContent=stamps.length?"最近观测 "+stamp(Math.max(...stamps))+" 北京时间":"等待行情采集";
}
