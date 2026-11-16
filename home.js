import {byId, element} from "./web/ui.js";
import {marketOverview} from "./web/market-overview.js";
import {PerformanceChart} from "./web/performance.js";
import {researchFeed} from "./web/research-feed.js";

const params=new URLSearchParams(location.search);
if(params.has("asset") || params.has("symbol") || /^#(overview|signals|levels|performance|scenarios|BTC|ETH|SOL)$/i.test(location.hash)) {
  location.replace("/terminal"+location.search+location.hash);
} else {
  const chart=new PerformanceChart(byId("chart"));
  let last=null, busy=false;
  async function refresh() {
    if(busy || document.hidden)return;
    busy=true;
    const notice=byId("service-notice");
    try {
      const response=await fetch("/api/home",{cache:"no-store",signal:AbortSignal.timeout(18000)});
      if(!response.ok)throw Error();
      /** @type {import("./web/market-types").Edition} */
      const data=await response.json();
      marketOverview(data.market||[]);researchFeed(data);last=data;
      notice.hidden=!data.notice;notice.textContent=data.notice?.message||"";
    } catch {
      marketOverview(last?.market||[],true);
      if(last?.headline && Date.parse(last.headline.until)<=Date.now()) {
        researchFeed({...last,headline:null});
      }
      notice.hidden=false;notice.replaceChildren(document.createTextNode("研究数据更新失败。"+(last?"显示上次取得的记录。":"")));
      const retry=element("button","重试","retry");retry.onclick=refresh;notice.append(retry);
      if(!last){byId("market-insight").textContent="研究服务暂不可用。";byId("market-insight").setAttribute("aria-busy","false");}
    } finally {busy=false;}
  }
  function update(){if(!document.hidden){refresh();chart.load();}}
  update();setInterval(update,60000);document.addEventListener("visibilitychange",update);
}
