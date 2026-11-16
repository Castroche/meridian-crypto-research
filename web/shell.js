const routes = [["/", "研究首页"], ["/terminal", "行情终端"], ["/reports", "研报中心"], ["/insights", "深度洞察"]];
const bjClock = new Intl.DateTimeFormat("en-GB", {timeZone:"Asia/Shanghai", hour:"2-digit", minute:"2-digit"});

class MeridianHeader extends HTMLElement {
  connectedCallback() {
    this.innerHTML = `<a class="skip-link" href="#main-content">跳至主要内容</a>
      <header class="desk-nav"><div class="nav-inner">
        <a class="signature" href="/" aria-label="MERIDIAN 研究首页"><i aria-hidden="true"></i>MERIDIAN</a>
        <nav aria-label="主导航"></nav>
        <span class="shell-clock" title="北京时间"><i aria-hidden="true"></i><time></time> UTC+8</span>
      </div></header>`;
    const nav = this.querySelector("nav");
    for (const [url, title] of routes) {
      const link = document.createElement("a");
      link.textContent = title;
      link.href = url;
      if (location.pathname.replace(/\/$/, "") === url.replace(/\/$/, "")) link.setAttribute("aria-current", "page");
      nav.append(link);
    }
    const clock = this.querySelector("time");
    const tick = () => {clock.textContent = bjClock.format(Date.now());};
    tick();
    this.timer = window.setInterval(tick, 30000);
  }
  disconnectedCallback() { clearInterval(this.timer); }
}
customElements.define("meridian-header", MeridianHeader);
