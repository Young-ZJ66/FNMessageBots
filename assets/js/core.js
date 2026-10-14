/* FnMessageBot · core.js
   ----------------------------------------------------------------------------
   DOM 引用与共享状态、未保存更改跟踪、状态条/Toast/持久警告

   由 design-previews/_extract_js.py 从模板内联 <script> 提取。
   提取时**未改动任何逻辑与标识符**，仅去掉原有的 4 空格整体缩进。
   注意：这些文件之间共享顶层作用域（无 IIFE），因此**不得重名**；
   脚本按模板中 <script src> 的顺序执行。
   ========================================================================== */

const eventsContainer = document.getElementById("events-container");
const thirdPartyEventsContainer = document.getElementById("third-party-events-container");
const channelsBody = document.getElementById("channels-body");
const addChannelBtn = document.getElementById("add-channel-btn");
const saveBtn = document.getElementById("save-btn");
const testBtn = document.getElementById("test-btn");
const nasPatrolRunBtn = document.getElementById("nas-patrol-run-btn");
const statusBar = document.getElementById("status-bar");
const warningPanel = document.getElementById("warning-panel");
const appMainEl = document.getElementById("app-main");
if (appMainEl) {
  appMainEl.addEventListener("change", markConfigDirty, true);
  appMainEl.addEventListener("input", markConfigDirty, true);
}

let channelOptions = [];
const fetchOpts = { credentials: "include" };

// 未保存更改跟踪：任意表单交互即标记（测试推送区除外）；保存成功后清除
let configDirty = false;
function markConfigDirty(e) {
  if (e && e.target && e.target.closest && e.target.closest(".test-section")) return;
  if (configDirty) return;
  configDirty = true;
  const bar = document.querySelector(".save-bar");
  if (bar) bar.classList.add("is-dirty");
}
function clearConfigDirty() {
  configDirty = false;
  const bar = document.querySelector(".save-bar");
  if (bar) bar.classList.remove("is-dirty");
}
window.addEventListener("beforeunload", (e) => {
  if (!configDirty) return;
  e.preventDefault();
  e.returnValue = "";
});

let setupRequiresCode = false;
let passwordMinLength = 8;

function setStatus(ok, message) {
  statusBar.className = "status-bar " + (ok ? "status-ok" : "status-error");
  statusBar.innerHTML = message ? "<span>" + message + "</span>" : "";
}

function showToast(ok, message) {
  const container = document.getElementById("toast-container");
  const el = document.createElement("div");
  el.className = "toast " + (ok ? "toast-ok" : "toast-error");
  el.textContent = message || (ok ? "操作成功" : "操作失败");
  container.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateY(-8px)";
    el.style.transition = "opacity 0.2s, transform 0.2s";
    setTimeout(() => el.remove(), 200);
  }, 3200);
}

function renderPersistentWarnings(warnings) {
  if (!warningPanel) return;
  const list = Array.isArray(warnings) ? warnings.filter(Boolean) : [];
  if (!list.length) {
    warningPanel.classList.remove("show");
    warningPanel.innerHTML = "";
    return;
  }
  function isMediaOrPhotoDbLine(s) {
    return /^(影视库|相册库):/.test(String(s));
  }
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function formatWarningItemHtml(item) {
    var s = String(item);
    var marker = "常见问题 · 第13条";
    if (s.indexOf("Docker：") === 0 && s.indexOf(marker) >= 0) {
      var i = s.indexOf(marker);
      var before = escapeHtml(s.slice(0, i));
      var after = escapeHtml(s.slice(i + marker.length));
      return (
        before +
        '<a href="faq#faq-docker-sock" target="_blank" rel="noopener noreferrer">' +
        marker +
        "</a>" +
        after
      );
    }
    return escapeHtml(s);
  }
  const mediaPhotoLines = list.filter(isMediaOrPhotoDbLine);
  const otherLines = list.filter(function(s) { return !isMediaOrPhotoDbLine(s); });
  const ordered = otherLines.concat(mediaPhotoLines);
  const lines = ordered.map(function(item) {
    return '<div class="warning-panel-item">- ' + formatWarningItemHtml(item) + "</div>";
  }).join("");
  var faq10 = "";
  if (mediaPhotoLines.length) {
    faq10 =
      '<p class="warning-panel-faq-link">' +
      '影视 / 相册数据库路径、挂载与权限说明请查看 ' +
      '<a href="faq#faq-external-db" target="_blank" rel="noopener noreferrer">常见问题 · 第12条</a>。' +
      "</p>";
  }
  warningPanel.classList.add("show");
  warningPanel.innerHTML =
    '<div class="warning-panel-title">数据库路径权限告警（保存后检测）</div>' + lines + faq10;
}

const PUSHPLUS_PLACEHOLDER = '{"token":"你的token","title":"{title}","content":"消息内容","template":"html","channel":"wechat"}';
