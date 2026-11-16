let pinnedLevel=null, barSelection=0;
const deskEl=id=>document.getElementById(id);
const deskText=(tag,text)=>Object.assign(document.createElement(tag),{textContent:text});
const bj=value=>new Date(value).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false});
const periodsText={60:"1M",300:"5M",900:"15M",3600:"1H",14400:"4H",86400:"1D"};
function linkDeskCitations(el){
  const text=el.textContent, refs=/[\[(（](F\d+(?:[、,， ]+F?\d+)*)[\])）]/g;
  let start=0;const parts=[];
  for(const match of text.matchAll(refs)){
    parts.push(text.slice(start,match.index));start=match.index+match[0].length;
    for(const id of match[1].match(/F\d+/g)||[]){
      const target=document.getElementById("desk-fact-"+id);if(!target)continue;
      const a=deskText("a",String(Number(id.slice(1))));a.href="#"+target.id;a.className="desk-cite";
      a.setAttribute("aria-label","查看依据 "+a.textContent);
      a.onclick=e=>{e.preventDefault();let parent=target.parentElement;while(parent){if(parent instanceof HTMLDetailsElement)parent.open=true;parent=parent.parentElement;}target.focus();};
      parts.push(a);
    }
  }
  if(parts.length)el.replaceChildren(...parts,text.slice(start));
}
function fourHourBars(rows){
  const groups=new Map();
  for(const row of rows){
    const start=Math.floor(row.time/14400)*14400;
    if(!groups.has(start))groups.set(start,[]);
    groups.get(start).push(row);
  }
  return [...groups].filter(([time,items])=>items[0].time===time &&
    items.every((r,i)=>r.time===time+3600*i) && (items.length===4 || time+14400>Date.now()/1000))
    .map(([time,items])=>({time,open:items[0].open,close:items.at(-1).close,
      low:Math.min(...items.map(r=>r.low)),high:Math.max(...items.map(r=>r.high)),volume:items.reduce((v,r)=>v+r.volume,0)}));
}
function currentRow(){
  if(!liveReport || ["已到期","等待报告"].includes(assets[selected].stance))return null;
  return liveReport.tactical_monitor?.dashboard?.find(row=>row.symbol===selected);
}
function showPeriod(){
  const trend=currentRow()?.["trend_"+periodsText[selectedGranularity].toLowerCase()];
  deskEl("period-evidence").textContent=periodsText[selectedGranularity]+" 周期 · "+
    (trend && trend!=="UNAVAILABLE" ? "出稿结构 "+trend : "该周期未发布独立研判");
}
function resetBar(){barSelection++;deskEl("bar-readout").textContent="";}
function renderDeskPanel(d){
  const row=currentRow();
  if(!row)pinnedLevel=null;
  deskEl("target-label").textContent=row?"条件计划目标区间":"研究目标区间";
  deskEl("mobile-stance").textContent="出稿观点 · "+d.stance+"　查看依据 ↓";
  const decision={WAIT:"等待确认",NO_ACTION:"维持观察",LONG:"观察多头条件",SHORT:"观察空头条件"};
  deskEl("desk-action").textContent=row?(decision[row.action]||row.current_view):d.stance;
  const metrics=[["出稿时滚动 24H",d.change],["资金费率",row?.funding_rate==null?"—":(row.funding_rate*100).toFixed(5)+"%"],
    ["上一检查点",row?.previous_view||"暂无可比观点"]];
  const dl=deskEl("desk-metrics");dl.replaceChildren();
  for(const [k,v] of metrics)dl.append(deskText("dt",k),deskText("dd",v));
  deskEl("confidence-copy").textContent=row?"模型方向评分 "+d.confidence+" / 100":"方向评分暂无";
  const levels=deskEl("price-actions");levels.replaceChildren();
  for(const [label,value] of d.levels){
    const price=Number(String(value).replace(/[$,]/g,""));if(!Number.isFinite(price)||price<=0)continue;
    const b=deskText("button",label+"  "+value);b.setAttribute("aria-pressed",String(pinnedLevel?.price===price));
    b.onclick=()=>{pinnedLevel=pinnedLevel?.price===price?null:{symbol:selected,price};renderDeskPanel(d);renderLiveChart();};
    levels.append(b);
  }
  if(!levels.children.length)levels.textContent="暂无有效价位";
  const facts=deskEl("terminal-facts");facts.replaceChildren();
  const groups=new Map();
  for(const f of (row?liveReport.facts:[])||[]){
    if(!f.statement.startsWith(selected+" "))continue;
    const group=({market:"价格与趋势",technical:"价格与趋势",flow:"资金与衍生品",fundamental:"基本面与链上"})[f.category]||"其他观测";
    if(!groups.has(group)){groups.set(group,deskText("details",""));groups.get(group).append(deskText("summary",group));facts.append(groups.get(group));}
    const box=deskText("p",f.statement+" ");box.append(deskText("small",[f.source,bj(f.as_of),f.window].filter(Boolean).join(" · ")));
    if(/^https?:\/\//.test(f.url)){const a=deskText("a"," 原始来源 ↗");a.href=f.url;a.target="_blank";a.rel="noreferrer";box.append(a);}
    box.id="desk-fact-"+f.id;box.tabIndex=-1;
    groups.get(group).append(box);
  }
  if(!facts.children.length)facts.textContent="本期暂无对应来源";
  deskEl("open-full").toggleAttribute("disabled",!liveReport);showPeriod();
}
const full=/** @type {HTMLDialogElement} */(deskEl("full-reader"));
deskEl("open-full").onclick=()=>{if(!liveReport)return;full.querySelector("iframe").src="/reports?id="+encodeURIComponent(liveReport.id);full.showModal();};
full.querySelector("button").onclick=()=>full.close();
deskEl("chart-full").onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.querySelector(".chart-wrap").requestFullscreen();}catch{deskEl("chart-status").textContent="当前浏览器不支持全屏";}};
deskEl("rail-toggle").onclick=e=>{
  const compact=document.querySelector(".workspace").classList.toggle("compact");
  deskEl("rail-toggle").textContent=compact?"展开":"收起侧栏";deskEl("rail-toggle").setAttribute("aria-expanded",String(!compact));
};
async function inspectBar(candle){
  if(!candle)return;
  const request=++barSelection;
  const at=candle.time*1000, until=Math.min(at+selectedGranularity*1000,Date.now());
  const out=deskEl("bar-readout");
  const delta=(candle.close/candle.open-1)*100;
  const within=liveReport && Date.parse(liveReport.published_at||liveReport.created_at)<=until &&
    Date.parse(liveReport.tactical_monitor?.next_checkpoint)>at;
  out.textContent=bj(at)+" · "+periodsText[selectedGranularity]+" 区间涨跌 "+delta.toFixed(2)+"% · 成交 "+Number(candle.volume.toFixed(3))+" "+selected+"。"+
    (within?"区间内已发布研判："+(liveReport.tactical_monitor?.dashboard.find(r=>r.symbol===selected)?.current_view||"未记录"):"当前研报未覆盖该区间。");
  if(within)return;
  try {
    const response=await fetch("/api/reports?limit=100");
    if(!response.ok || request!==barSelection)return;
    const reports=await response.json();
    const prior=reports.find(r=>r.report_type==="standard"&&Date.parse(r.published_at||r.created_at)<=until);
    if(!prior)return;
    const detail=await fetch("/api/reports/"+encodeURIComponent(prior.id));
    if(!detail.ok)return;
    const research=await detail.json(), monitor=research.tactical_monitor;
    const row=monitor?.dashboard.find(item=>item.symbol===selected);
    if(request!==barSelection||!row||Date.parse(monitor.next_checkpoint)<=at)return;
    const a=deskText("a"," 区间内有效研判 · "+row.current_view+" ↗");
    a.href="/reports?id="+encodeURIComponent(prior.id);out.append(a);
  } catch { /* The selected candle remains readable offline. */ }

}
const pricePlot=deskEl("price-chart");
pricePlot.tabIndex=0;
pricePlot.addEventListener("click",e=>{const r=pricePlot.getBoundingClientRect(), pos=(e.clientX-r.left)/r.width*900/830;if(pos>=0 && pos<1)inspectBar(visibleCandles[Math.floor(pos*visibleCandles.length)]);});
pricePlot.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();inspectBar(visibleCandles.at(-1));}});
