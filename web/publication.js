function libraryMatches(rows){
  const input=id=>/** @type {HTMLInputElement} */(document.getElementById(id));
  const query=input("library-search").value.trim().toLowerCase(),date=input("library-date").value;
  const topicSelect=/** @type {HTMLSelectElement} */(document.getElementById("library-topic"));
  const topicOf=r=>/EIP|以太坊|Ethereum|blob|节点|协议|比特币|验证者|Gas/i.test(r.title)?"区块链基础设施":
    /人工智能|算力|GPU|模型|\bAI\b/i.test(r.title)?"AI 与算力":/宏观|央行|利率|通胀/.test(r.title)?"宏观经济":"其他研究";
  const topics=[...new Set(rows.map(topicOf))];
  const activeTopic=topicSelect.value;
  topicSelect.replaceChildren(new Option("全部主题",""),...topics.map(t=>new Option(String(t),String(t))));
  topicSelect.value=topics.includes(activeTopic)?activeTopic:"";
  const result=rows.filter(r=>(r.title+" "+topicOf(r)).toLowerCase().includes(query)&&(!topicSelect.value||topicOf(r)===topicSelect.value)&&(!input("library-saved").checked||r.is_saved)&&
    (!date||new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Shanghai"}).format(new Date(r.published_at||r.created_at))===date));
  document.querySelector(".insight-layout").classList.toggle("small-library",rows.length<=2);
  input("insight-count").textContent=result.length;
  return result;
}
const noteDialog=/** @type {HTMLDialogElement} */(document.getElementById("note-dialog"));
noteDialog.querySelector("button").onclick=()=>noteDialog.close();
let outlineFrame=0, outlineEntries=[];
function updateOutline(){
  outlineFrame=0;
  let current=outlineEntries[0];
  for(const item of outlineEntries){if(item.section.getBoundingClientRect().top<=150)current=item;}
  for(const item of outlineEntries){if(item===current)item.link.setAttribute("aria-current","location");else item.link.removeAttribute("aria-current");}
}
addEventListener("scroll",()=>{if(!outlineFrame)outlineFrame=requestAnimationFrame(updateOutline);},{passive:true});
function buildPublication(report){
  const facts=report.facts||[],byId=new Map(facts.map((f,i)=>[f.id,{fact:f,num:i+1}]));
  const article=report.article, toc=document.getElementById("story-outline"), ledger=document.getElementById("facts");
  const types={FACT:"原始材料",INFERENCE:"研究推论",FORECAST:"预测",MERIDIAN_VIEW:"研究判断"};
  const make=(tag,text)=>Object.assign(document.createElement(tag),{textContent:text});
  function openNote(f,n){
    const source=report.insight_sources?.find(s=>s.url===f.url);
    const box=noteDialog.querySelector("div");box.replaceChildren(make("small","注释 "+n+" · "+(types[f.claim_type]||"研究材料")),
      make("h2",source?.title && !source.title.startsWith("http")?source.title:f.source),make("p",f.statement),
      make("small",[f.source,"资料时点 "+fmt(f.as_of),f.window].filter(Boolean).join(" · ")));
    if(/^https?:\/\//.test(f.url)){const a=make("a","查看原始材料 ↗");a.href=f.url;a.target="_blank";a.rel="noreferrer";box.append(a);}
    const index=make("a","定位到来源索引");index.href="#fact-"+f.id;index.onclick=()=>{const select=document.querySelector("#evidence-filter");if(select instanceof HTMLSelectElement){select.value="";select.dispatchEvent(new Event("change"));}noteDialog.close();};box.append(index);noteDialog.showModal();
  }
  const sign=(a,id)=>{
    const f=byId.get(id);if(!f)return;
    a.textContent=String(f.num);a.title="注释 "+f.num+" · "+f.fact.source;
    a.setAttribute("aria-label",a.title);a.onclick=e=>{e.preventDefault();openNote(f.fact,f.num);};
  };
  for(const a of document.querySelectorAll("#article .citation"))sign(a,a.getAttribute("href").slice(6));
  for(const para of document.querySelectorAll("#story p,#lead,#conclusion,#consensus,#meridian-view")){
    const refs=[...para.querySelectorAll("a.citation")];
    if(refs.length<2)continue;
    const first=refs[0], group=refs.map(a=>byId.get(a.getAttribute("href").slice(6))).filter(Boolean);
    if(group.length<2)continue;
    first.textContent=group.map(f=>f.num).join(",");
    first.setAttribute("aria-label","查看本段 "+group.length+" 条来源");
    first.addEventListener("click",e=>{
      e.preventDefault();e.stopImmediatePropagation();
      const box=noteDialog.querySelector("div");box.replaceChildren(make("h2","本段来源"));
      for(const f of group){const b=make("button","["+f.num+"] "+f.fact.source+" · "+f.fact.statement);b.onclick=()=>{noteDialog.close();openNote(f.fact,f.num);};box.append(b);}
      noteDialog.showModal();
    },true);
    refs.slice(1).forEach(a=>a.remove());
  }
  for(const [i,row] of [...ledger.children].entries()){
    row.querySelector("b").textContent=String(i+1);
    row.querySelector("small").textContent=(types[facts[i].claim_type]||"材料")+" · "+facts[i].source;
  }
  const classify=f=>{
    const edge=(report.deep_research?.evidence_graph||[]).find(e=>e.fact_extracted===f.statement);
    if(edge?.supports_or_contradicts==="CONTRADICTS")return "反向证据";
    if(f.claim_type==="FORECAST")return "待验证预测";
    return types[f.claim_type]||"研究材料";
  };
  const filter=/** @type {HTMLSelectElement} */(document.getElementById("evidence-filter"));
  const categories=facts.map(classify);
  filter.replaceChildren(new Option("全部来源",""),...["原始材料","研究推论","研究判断","反向证据","待验证预测"].map(name=>new Option(name,name)));
  filter.onchange=()=>{
    let count=0;
    for(const [i,row] of [...ledger.children].entries()){
      const show=!filter.value||filter.value===categories[i];
      row.toggleAttribute("hidden",!show);if(show)count++;
    }
    document.getElementById("ledger-state").textContent=count?"共 "+count+" 条":"本篇没有单独标注此类证据。";
  };
  filter.dispatchEvent(new Event("change"));
  const findings=document.querySelector("#findings ol");findings.replaceChildren();
  const sections=article.sections||[];
  const limits=sections.find(s=>/边界|风险|局限|假设/.test(s.heading));
  const highlights=[...new Set([sections[0],sections[1],limits || sections[2]].filter(Boolean))];
  for(const section of highlights){
    const para=section.paragraphs.find(p=>p.fact_ids?.length)||section.paragraphs[0];
    if(!para)continue;
    const sentence=para.text.match(/^.*?[。！？](?:[”」])?/u)?.[0]||para.text;
    const li=make("li",sentence);
    if(para.fact_ids?.length){const a=make("a","");a.className="citation";a.href="#fact-"+para.fact_ids[0];sign(a,para.fact_ids[0]);li.append(a);}
    findings.append(li);
  }
  document.getElementById("findings").hidden=!findings.children.length;
  toc.replaceChildren(make("h2","本文目录"));outlineEntries=[];
  const entries=[document.getElementById("findings"),...document.querySelectorAll("#story section"),document.getElementById("source-index")];
  entries.filter(el=>!el.hasAttribute("hidden")).forEach((section,i)=>{
    if(!section.id)section.id="chapter-"+i;
    const label=section.id==="source-index"?"来源与注释":section.querySelector("h2")?.textContent||"章节";
    const a=make("a",label);a.href="#"+section.id;toc.append(a);outlineEntries.push({section,link:a});
  });
  const jump=/** @type {HTMLSelectElement} */(document.getElementById("jump-section"));
  jump.replaceChildren(new Option("选择章节",""),...outlineEntries.map(item=>new Option(item.link.textContent,item.section.id)));
  jump.onchange=()=>{if(jump.value)location.hash=jump.value;};
  updateOutline();
}
