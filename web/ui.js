export const symbols = /** @type {const} */ (["BTC","ETH","SOL"]);
export const names = {BTC:"Bitcoin", ETH:"Ethereum", SOL:"Solana"};
export const productNames = {daily:"市场日报", standard:"交易简报", insight:"深度洞察"};
export const byId = (id) => document.getElementById(id);
export function element(tag, text="", cls="") {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  el.textContent = text;
  return el;
}
export function link(href, text, cls="") {
  const a = element("a",text,cls);
  a.href = href;
  return a;
}
export function svgNode(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg",tag);
  for(const [key,value] of Object.entries(attrs)) node.setAttribute(key,String(value));
  return node;
}
const dateFmt = new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
export function stamp(value) { return value && Number.isFinite(+new Date(value)) ? dateFmt.format(new Date(value)) : "时间未知"; }
export const shortDate = (v) => stamp(v).slice(5);
const priceFmt = new Intl.NumberFormat("en-US",{minimumFractionDigits:2,maximumFractionDigits:2});
export const price = (v) => Number.isFinite(v) ? priceFmt.format(v) : "—";
export const percent = (v, digits=2) => Number.isFinite(v) ? (v>0?"+":"")+v.toFixed(digits)+"%" : "—";
export const direction = (v) => v>0 ? "up" : v<0 ? "down" : "flat";
export function amount(v) {
  if (!Number.isFinite(v)) return "—";
  if (v >= 1e9) return (v/1e9).toFixed(2)+"B";
  if (v >= 1e6) return (v/1e6).toFixed(2)+"M";
  return price(v);
}
export function dataStatus(el, label, state) {
  el.dataset.state = state;
  el.textContent = label;
}
