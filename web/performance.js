import {byId, element, percent, shortDate, stamp, svgNode, symbols} from "./ui.js";

const periods = {"24H":{step:900,bars:96}, "7D":{step:3600,bars:168}, "30D":{step:21600,bars:120}};

/** Input rows use Coinbase's time, low, high, open, close, volume order.
 * Missing bars stay null and break the line. Each return starts at the same instant.
 * @returns {import("./market-types").PerformanceData|null}
 */
export function normalize(rows, {step,bars}, now=Date.now()/1000) {
  const books=rows.map(list=>{
    const book=new Map();
    for(const raw of list) {
      const [t,,,o,c]=raw.map(Number);
      if(t%step===0 && t+step<=now && o>0 && Number.isFinite(o) && c>0 && Number.isFinite(c)) book.set(t,[o,c]);
    }
    return book;
  });
  if(books.length!==symbols.length || books.some(b=>!b.size)) return null;
  const shared=[...books[0].keys()].filter(t=>books.every(b=>b.has(t)));
  if(!shared.length) return null;
  const end=Math.max(...shared)+step, start=end-bars*step;
  if(books.some(b=>!b.has(start))) return null;
  const times=Array.from({length:bars+1},(_,i)=>start+i*step);
  const series=books.map((book,k)=>{
    const base=book.get(start)[0];
    const values=times.map((t,i)=>!i ? 0 : book.has(t-step) ? 100*(book.get(t-step)[1]/base-1) : null);
    return {symbol:symbols[k],values};
  });
  return {times,series,step};
}
function label(svg,x,y,text,anchor="start") {
  const t=svgNode("text",{x,y,"text-anchor":anchor,class:"axis-text"});t.textContent=text;svg.append(t);
}
export class PerformanceChart {
  constructor(host) {
    this.host=host; this.range="24H"; this.hidden=new Set(); this.cache=new Map(); this.data=null;
    this.guide=null; this.selected=null; this.width=0;
    this.switches=byId("periods"); this.legend=byId("legend"); this.source=byId("chart-source");
    for(const name of Object.keys(periods)) {
      const button=element("button",name);button.type="button";button.dataset.period=name;
      button.addEventListener("click",()=>this.load(name));this.switches.append(button);
    }
    this.resize=new ResizeObserver(([entry])=>{
      const w=Math.round(entry.contentRect.width);if(this.width!==w){this.width=w;this.draw();}
    });
    this.resize.observe(host);
    host.addEventListener("pointermove",e=>{
      if(!this.data || !this.guide)return;
      const x=e.clientX-host.getBoundingClientRect().left;
      this.inspect(Math.round((x-this.left)/this.span*(this.data.times.length-1)));
    });
    host.addEventListener("pointerleave",()=>this.clear());
    host.addEventListener("blur",()=>this.clear());
    host.addEventListener("keydown",e=>{
      if(!this.data)return;
      const last=this.data.times.length-1;
      const key={ArrowLeft:(this.selected??last)-1,ArrowRight:(this.selected??-1)+1,Home:0,End:last};
      if(e.key==="Escape"){this.clear();return;}
      if(e.key in key){e.preventDefault();this.inspect(key[e.key]);}
    });
  }
  clear() {
    this.selected=null;
    this.guide?.setAttribute("visibility","hidden");
    if(this.tooltip)this.tooltip.hidden=true;
  }
  status(text,retry=false) {
    this.guide=null;this.data=null;this.host.removeAttribute("tabindex");
    this.legend.replaceChildren();this.host.replaceChildren(element("p",text,"plot-message"));
    if(retry){
      const btn=element("button","重新加载","retry");btn.type="button";
      btn.addEventListener("click",()=>this.load(this.range,true));this.host.append(btn);
    }
  }
  async load(name=this.range, force=false) {
    if(!(name in periods))return;
    const cached=this.cache.get(name);
    for(const button of this.switches.querySelectorAll("button")) button.setAttribute("aria-pressed",String(button.dataset.period===name));
    if(this.pending?.name===name && !force)return;
    this.pending?.controller.abort(); this.pending=null;this.range=name;this.clear();
    const apply=data=>{this.data=data;this.source.textContent="Coinbase Spot · 截至 "+shortDate(data.times.at(-1)*1000)+" 北京时间";
      if(Date.now()/1000-data.times.at(-1)>data.step+300)this.source.textContent+=" · 延迟";
      this.draw();};
    if(cached){
      if(this.data!==cached.data) apply(cached.data);
      if(!force && Date.now()-cached.at<60000)return;
    } else {this.status("正在读取 "+name+" 历史行情…");this.source.textContent="Coinbase Spot · 北京时间";}
    const controller=new AbortController();this.pending={name,controller};
    this.host.setAttribute("aria-busy","true");
    try {
      const spec=periods[name];
      const rows=await Promise.all(symbols.map(async symbol=>{
        const response=await fetch("/api/market/candles?symbol="+symbol+"&granularity="+spec.step,{signal:controller.signal});
        if(!response.ok)throw Error("unavailable");
        return await response.json();
      }));
      if(controller.signal.aborted)return;
      const data=normalize(rows,spec);
      if(!data){if(cached){this.source.textContent+=" · 本次窗口不完整";}else this.status("三个资产缺少共同的完整比较窗口。",true);return;}
      this.cache.set(name,{data,at:Date.now()});apply(data);
    } catch {
      if(controller.signal.aborted)return;
      if(cached) this.source.textContent+=" · 更新失败，保留上次结果";
      else this.status("历史行情暂时无法连接。",true);
    } finally {
      if(this.pending?.controller===controller){this.pending=null;this.host.setAttribute("aria-busy","false");}
    }
  }
  draw() {
    if(!this.data || !this.width)return;
    const data=this.data;
    this.clear();
    const visible=data.series.filter(s=>!this.hidden.has(s.symbol));
    this.legend.replaceChildren(...data.series.map(s=>{
      const btn=element("button","","legend-item "+s.symbol);
      btn.type="button";btn.setAttribute("aria-label","显示或隐藏 "+s.symbol);
      btn.setAttribute("aria-pressed",String(!this.hidden.has(s.symbol)));
      btn.disabled=visible.length===1 && visible[0]===s;
      btn.append(element("i"),element("strong",s.symbol),element("span",percent(s.values.at(-1))));
      btn.addEventListener("click",()=>{
        if(this.hidden.has(s.symbol))this.hidden.delete(s.symbol);else this.hidden.add(s.symbol);
        this.draw();const next=this.legend.querySelector("."+s.symbol);if(next instanceof HTMLElement)next.focus();
      });return btn;
    }));
    const w=this.width,h=w<440?194:222,top=15,bottom=h-28;
    this.left=3;this.span=w-58-this.left;
    const vals=visible.flatMap(s=>s.values).filter(Number.isFinite);
    let low=Math.min(0,...vals),high=Math.max(0,...vals);
    const raw=(high-low||1)/4,base=10**Math.floor(Math.log10(raw));
    const tick=([1,2,2.5,5,10].find(n=>n*base>=raw)||10)*base;
    low=Math.floor(low/tick)*tick;high=Math.ceil(high/tick)*tick;if(high===low)high+=tick;
    const x=i=>this.left+i*this.span/(data.times.length-1);
    const y=value=>top+(high-value)/(high-low)*(bottom-top);
    const svg=svgNode("svg",{viewBox:`0 0 ${w} ${h}`,width:w,height:h,"aria-hidden":"true"});
    for(let v=low;v<=high+tick*.001;v+=tick){
      svg.append(svgNode("line",{x1:this.left,x2:this.left+this.span,y1:y(v),y2:y(v),class:Math.abs(v)<tick*.001?"zero-line":"grid-line"}));
      label(svg,w-1,y(v)+4,Number(v.toFixed(3))+"%","end");
    }
    for(let i=0;i<5;i++){
      const k=Math.round((data.times.length-1)*i/4),t=stamp(data.times[k]*1000);
      label(svg,x(k),h-4,this.range==="24H"?t.slice(11):t.slice(5,10),i===0?"start":i===4?"end":"middle");
    }
    for(const s of visible){
      let pen=false;
      const d=s.values.map((v,i)=>{
        if(v==null){pen=false;return "";}
        const cmd=pen?"L":"M";pen=true;return cmd+x(i).toFixed(1)+" "+y(v).toFixed(1);
      }).join(" ");
      svg.append(svgNode("path",{d,fill:"none",class:"series-path "+s.symbol}));
      svg.append(svgNode("circle",{cx:x(s.values.length-1),cy:y(s.values.at(-1)),r:2.8,class:"end-dot "+s.symbol}));
    }
    this.guide=svgNode("line",{y1:top,y2:bottom,class:"cursor-line",visibility:"hidden"});
    svg.append(this.guide);
    this.tooltip=element("div","","plot-tooltip");this.tooltip.hidden=true;
    this.tooltip.setAttribute("role","status");
    this.host.tabIndex=0;this.host.setAttribute("role","group");
    this.host.setAttribute("aria-label",this.range+" 相对回报。使用左右方向键逐点读取，Home 和 End 跳至首尾。");
    this.host.replaceChildren(svg,this.tooltip);
  }
  inspect(index) {
    if(!this.guide||!this.data)return;
    const last=this.data.times.length-1,i=Math.max(0,Math.min(last,index));
    if(i===this.selected)return;
    this.selected=i; const x=this.left+i*this.span/last;
    this.guide.setAttribute("x1",x);this.guide.setAttribute("x2",x);this.guide.setAttribute("visibility","visible");
    const fields=this.data.series.filter(s=>!this.hidden.has(s.symbol)).map(s=>{
      const row=element("div","","tip-row "+s.symbol);row.append(element("span",s.symbol),element("strong",percent(s.values[i])));return row;
    });
    this.tooltip.replaceChildren(element("time",shortDate(this.data.times[i]*1000)),...fields);
    this.tooltip.hidden=false;
    const width=this.tooltip.offsetWidth;
    this.tooltip.style.left=Math.max(0,Math.min(this.width-width,x>this.width/2?x-width-9:x+9))+"px";
  }
}

