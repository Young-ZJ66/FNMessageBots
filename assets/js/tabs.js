/* FnMessageBot · tabs.js
   ----------------------------------------------------------------------------
   页签切换与深链（?tab= / #tab）

   设计说明
   - 页签面板始终留在 DOM 里，只切换 hidden 属性。原因：channels/events 的填充
     由 loadConfig 在页面加载时一次性完成，若面板被移除就会取不到元素。
   - 深链优先读 ?tab=（便于分享与截图验证），其次 #tab；切换时用 replaceState
     更新地址栏而不新增历史记录——否则每切一次页签，浏览器返回键就要按一次。
   - 本文件在全局作用域，与其它脚本共享顶层命名，不得重名。
   ========================================================================== */

const TAB_IDS = ["overview", "channels", "events", "policy", "patrol"];
const TAB_TITLES = {
  overview: "概览",
  channels: "推送渠道",
  events: "监控事件",
  policy: "通知策略",
  patrol: "定时巡检"
};

function tabFromUrl() {
  try {
    const q = new URLSearchParams(location.search).get("tab");
    if (q && TAB_IDS.indexOf(q) !== -1) return q;
  } catch (e) { /* 极老浏览器无 URLSearchParams：退回 hash */ }
  const h = (location.hash || "").replace(/^#/, "").replace(/^tab=/, "");
  return TAB_IDS.indexOf(h) !== -1 ? h : "";
}

function switchTab(name, opts) {
  const target = TAB_IDS.indexOf(name) !== -1 ? name : TAB_IDS[0];
  // 按 data-tab-panel 属性选择，而不是用 getElementById("tab-" + id) 拼 id：
  // 拼出来的 id 无法被静态检查，模板与 JS 之间的契约就断了。
  document.querySelectorAll("[data-tab-panel]").forEach(function (panel) {
    panel.hidden = panel.getAttribute("data-tab-panel") !== target;
  });
  document.querySelectorAll("#main-nav .nav-item[data-tab]").forEach(function (btn) {
    if (btn.getAttribute("data-tab") === target) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });
  const title = document.getElementById("tab-title");
  const label = TAB_TITLES[target] || "";
  if (title) title.textContent = label;
  if (label) document.title = label + " - FnMessageBot";
  if (!opts || opts.updateUrl !== false) {
    try {
      const url = new URL(location.href);
      url.searchParams.set("tab", target);
      url.hash = "";
      history.replaceState(null, "", url);
    } catch (e) { /* 地址栏更新失败不影响切换 */ }
  }
}

function initTabs() {
  const nav = document.getElementById("main-nav");
  if (nav) {
    nav.addEventListener("click", function (e) {
      const btn = e.target && e.target.closest ? e.target.closest(".nav-item[data-tab]") : null;
      if (!btn) return;
      e.preventDefault();
      switchTab(btn.getAttribute("data-tab"));
    });
  }
  window.addEventListener("hashchange", function () {
    const t = tabFromUrl();
    if (t) switchTab(t, { updateUrl: false });
  });
  switchTab(tabFromUrl() || TAB_IDS[0]);
}
