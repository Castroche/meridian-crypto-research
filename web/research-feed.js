import {byId, element, link, productNames, stamp} from "./ui.js";

/** @param {import("./market-types").Edition} data */
export function researchFeed(data) {
  const host=byId("market-insight"), lead=data.headline;
  host.replaceChildren();host.setAttribute("aria-busy","false");
  if(lead) {
    host.append(element("small",productNames[lead.kind]+"  /  刊发 "+stamp(lead.published_at),"product-label"),
      link(lead.url,lead.title,"lead-title"),element("p",lead.summary,"lead-copy"));
    for(const fact of data.evidence) host.append(link(lead.url+"#fact-"+fact.id,fact.statement+" ↗","evidence"));
    host.append(element("small","资料截至 "+stamp(lead.as_of)+" · 有效至 "+stamp(lead.until),"issue-time"),link(lead.url,"阅读完整研究 →","research-link"));
  } else host.append(element("p","本期有效研究待发布。历史稿件可在研报中心按日期查阅。"),link("/reports","查阅报告 →","research-link"));
  const feature=byId("feature"), f=data.products.insight;
  feature.replaceChildren(element("small","深度洞察","product-label"));
  if(f) feature.append(link(f.url,f.title,"feature-title"),element("p",f.summary,"lead-copy"),element("small","刊发 "+stamp(f.published_at),"issue-time"));
  else feature.append(element("p","尚无已发布特稿。"));
  const feed=byId("recent");feed.replaceChildren();
  const mobileLead=byId("compact-lead");
  mobileLead.replaceChildren();
  if(lead) mobileLead.append(link(lead.url,"研究焦点 · "+lead.title+" →"));
  const rows=data.recent.filter(r=>r.report_type!=="insight").slice(0,3);
  for(const r of rows){
    const a=link("/reports?id="+encodeURIComponent(r.id),"","feed-item");
    a.append(element("small",stamp(r.published_at||r.created_at)+"  ·  "+(productNames[r.report_type]||"研究")),element("strong",r.title));if(r.id===data.products.daily?.id){
      a.append(element("p",data.products.daily.summary,"lead-copy"));
    }
    feed.append(a);
  }
  if(!rows.length)feed.append(element("p","暂无历史研报。"));
}

