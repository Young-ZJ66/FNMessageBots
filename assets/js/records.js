/* FnMessageBot · records.js
   ----------------------------------------------------------------------------
   推送记录：卡片列表、筛选、加载更多、详情面板

   记录原本是一个独立页面（history.html + history.js，表格形态）。
   现已并入首页「概览」页签，改为卡片列表，故整体重写。

   命名：本文件与其它脚本共享顶层作用域（无 IIFE），所有顶层名字都加
   records 前缀以避开 core.js / app.js / channels.js 等。这不是洁癖——
   history.js 曾因顶层 var THEME_STORAGE_KEY 与 theme.js 的 const 同名，
   导致整个文件抛 SyntaxError 而静默失效；测试 ScriptScopeCase 也守这一点。
   特别是 fetchOpts：core.js 已用 const 声明，这里**绝不能再声明**，直接复用。
   ========================================================================== */

let recordsOffset = 0;
const recordsPageSize = 30;
let recordsFilter = "";

function recordsEscHtml(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function recordsStringify(value) {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value); } catch (e) { return String(value); }
}

/* 单条渠道的结果说明：成功时没有附加信息，失败时给出错误与返回内容。 */
function recordsChannelExtra(c) {
  if (!c || c.success) return "";
  const parts = [];
  const err = recordsStringify(c.error);
  const resp = recordsStringify(c.response);
  if (err) parts.push("错误: " + err);
  if (resp) parts.push("返回: " + resp);
  return parts.join("；") || "无返回详情";
}

function recordsShorten(text, maxLen) {
  text = text || "";
  return text.length > maxLen ? text.slice(0, Math.max(0, maxLen - 1)) + "…" : text;
}

/* 从记录的 detail 字段里取出渠道结果，解析失败时返回 null。 */
function recordsChannelResults(record) {
  if (!record || !record.detail) return null;
  try {
    const detail = typeof record.detail === "string" ? JSON.parse(record.detail) : record.detail;
    if (!detail) return null;
    if (Array.isArray(detail.channel_results) && detail.channel_results.length) {
      return detail.channel_results;
    }
    return null;
  } catch (e) {
    return null;
  }
}

/* 401 时不能 window.location.href = "./"：首页是公开路径，未登录时每次加载都会
   401，跳回自身就成了无限重载。改为与 loadConfig 一致，交给 initAuth() 显示登录门。 */
function recordsHandleUnauthorized() {
  initAuth();
}

/* ── 详情：在**被点击的那张卡片内**就地展开 ──
   早先是渲染到列表下方的独立面板（#detail-panel），记录一多就要滚到底部才能看，
   而且看不出是哪一条的详情。现在展开在卡片里，点一下开、再点一下收。
   详情 HTML 缓存在卡片元素上，重复展开不重复请求。 */

/* 把详情内容渲染成 HTML。失败时返回空串，由调用方决定是否提示。 */
function recordsBuildDetailHtml(rec) {
  let detail = rec.detail;
  if (typeof detail === "string") {
    try { detail = JSON.parse(detail); } catch (e) { detail = null; }
  }
  let html = '<div class="detail-head">';
  html += '<span class="detail-title">'
    + recordsEscHtml(rec.event_type_label || rec.event_type || "") + "</span>";
  html += '<span class="detail-meta">' + recordsEscHtml(rec.created_at || "") + "</span>";
  html += '<span class="' + (rec.success ? "result-ok" : "result-fail") + '">'
    + (rec.success ? "成功" : "失败") + "</span>";
  html += "</div>";
  if (rec.summary) html += '<div class="detail-summary">' + recordsEscHtml(rec.summary) + "</div>";
  if (detail && Array.isArray(detail.channel_results) && detail.channel_results.length) {
    html += "<h2>渠道返回结果</h2>";
    detail.channel_results.forEach(function (c) {
      c = c || {};
      const extra = recordsChannelExtra(c);
      html += '<div class="detail-channel-item">' + recordsEscHtml(c.channel || "未知渠道")
        + ": " + (c.success ? "成功" : "失败")
        + (extra ? '<span class="detail-channel-extra">' + recordsEscHtml(extra) + "</span>" : "")
        + "</div>";
    });
  } else if (detail && detail.failure_summary) {
    html += "<h2>失败原因</h2><div class=\"detail-summary\">"
      + recordsEscHtml(recordsStringify(detail.failure_summary)) + "</div>";
  }
  if (detail && Array.isArray(detail.batch_items) && detail.batch_items.length) {
    const total = Number(detail.batch_total || detail.batch_items.length) || detail.batch_items.length;
    const capped = detail.batch_items.length < total;
    html += "<h2>批次明细（共 " + total + " 条"
      + (capped ? "，仅记录前 " + detail.batch_items.length + " 条" : "") + "）</h2>";
    html += '<table class="detail-batch"><thead><tr><th>#</th><th>时间</th>'
      + "<th>事件类型</th><th>内容</th></tr></thead><tbody>";
    detail.batch_items.forEach(function (it, idx) {
      it = it || {};
      html += "<tr><td>" + (idx + 1) + "</td>"
        + '<td class="detail-batch-time">' + recordsEscHtml(it.timestamp || "") + "</td>"
        + "<td>" + recordsEscHtml(it.event_type || "") + "</td>"
        + "<td>" + recordsEscHtml(it.brief || "") + "</td></tr>";
    });
    html += "</tbody></table>";
  }
  return html;
}

function recordsSetCardExpanded(card, expanded) {
  const box = card.querySelector(".record-detail");
  if (!box) return;
  box.hidden = !expanded;
  card.setAttribute("aria-expanded", expanded ? "true" : "false");
  if (expanded) {
    card.classList.add("is-open");
    // 展开后把卡片滚进可视区：记录模块是限高内部滚动的，
    // 不滚的话展开的部分可能整段落在可视区外。
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } else {
    card.classList.remove("is-open");
  }
}

function recordsToggleDetail(card) {
  const id = card.getAttribute("data-record-id");
  if (!id) return;
  const box = card.querySelector(".record-detail");
  if (!box) return;

  // 已展开则收起（内容已缓存在 box 里，无需重新请求）
  if (!box.hidden) {
    recordsSetCardExpanded(card, false);
    return;
  }
  if (box.dataset.loaded === "1") {
    recordsSetCardExpanded(card, true);
    return;
  }

  box.hidden = false;
  box.innerHTML = '<div class="record-detail-loading">加载中…</div>';
  card.setAttribute("aria-expanded", "true");
  card.classList.add("is-open");

  fetch("api/push-history/" + encodeURIComponent(id), fetchOpts).then(function (r) {
    if (r.status === 401) { recordsHandleUnauthorized(); return null; }
    return r.json();
  }).then(function (json) {
    if (!json || !json.ok || !json.data) {
      box.innerHTML = '<div class="record-detail-loading">详情加载失败</div>';
      return;
    }
    box.innerHTML = recordsBuildDetailHtml(json.data);
    box.dataset.loaded = "1";
    // 详情里的内容可能比卡片高，展开后再校正一次位置
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }).catch(function () {
    box.innerHTML = '<div class="record-detail-loading">详情加载失败</div>';
  });
}


/* ── 卡片 ── */
/* 一条记录一张卡：首行是时间 / 事件类型 / 结果，随后是摘要与各渠道结果，
   末尾预留一个隐藏的详情容器，展开时就地填进去。
   整卡可点（也支持 Enter/空格）切换详情展开。 */
function recordsBuildCard(record) {
  const card = document.createElement("article");
  card.className = "record-card" + (record.success ? "" : " record-card-fail");
  card.tabIndex = 0;
  card.setAttribute("role", "button");
  card.setAttribute("aria-expanded", "false");
  card.title = "点击展开详情";
  if (record.id !== undefined && record.id !== null) {
    card.setAttribute("data-record-id", String(record.id));
  }

  const head = document.createElement("div");
  head.className = "record-head";

  const time = document.createElement("span");
  time.className = "record-time";
  time.textContent = record.created_at || "";
  head.appendChild(time);

  const type = document.createElement("span");
  type.className = "record-type";
  type.textContent = record.event_type_label || record.event_type || "";
  head.appendChild(type);

  const badge = document.createElement("span");
  badge.className = "record-badge " + (record.success ? "record-badge-ok" : "record-badge-fail");
  badge.textContent = record.success ? "成功" : "失败";
  head.appendChild(badge);

  card.appendChild(head);

  const summary = document.createElement("div");
  summary.className = "record-summary";
  summary.textContent = record.summary || "（无摘要）";
  card.appendChild(summary);

  const results = recordsChannelResults(record);
  if (results) {
    const list = document.createElement("div");
    list.className = "record-channels";
    results.forEach(function (c) {
      c = c || {};
      const chip = document.createElement("span");
      const extra = recordsChannelExtra(c);
      chip.className = "record-channel " + (c.success ? "is-ok" : "is-fail");
      chip.textContent = (c.channel || "未知渠道") + " · " + (c.success ? "成功" : "失败");
      if (extra) chip.title = extra;
      list.appendChild(chip);
    });
    card.appendChild(list);
  }

  // 详情容器：默认隐藏，展开时就地填充
  const detail = document.createElement("div");
  detail.className = "detail-wrap record-detail";
  detail.hidden = true;
  card.appendChild(detail);

  card.addEventListener("click", function () { recordsToggleDetail(card); });
  card.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      recordsToggleDetail(card);
    }
  });
  return card;
}

function loadRecords(reset) {
  const list = document.getElementById("records-list");
  const emptyEl = document.getElementById("records-empty");
  const moreBtn = document.getElementById("records-more");
  if (!list) return;
  if (reset) {
    recordsOffset = 0;
    list.innerHTML = "";
  }
  let params = "limit=" + recordsPageSize + "&offset=" + recordsOffset;
  if (recordsFilter !== "") params += "&success=" + recordsFilter;
  fetch("api/push-history?" + params, fetchOpts).then(function (r) {
    if (r.status === 401) { recordsHandleUnauthorized(); return null; }
    return r.json();
  }).then(function (json) {
    if (!json || !json.ok || !Array.isArray(json.data)) return;
    const rows = json.data;
    if (emptyEl) emptyEl.style.display = (rows.length === 0 && recordsOffset === 0) ? "block" : "none";
    rows.forEach(function (r) { list.appendChild(recordsBuildCard(r)); });
    recordsOffset += rows.length;
    if (moreBtn) moreBtn.style.display = rows.length >= recordsPageSize ? "inline-flex" : "none";
  }).catch(function () { /* 列表失败保留已有内容 */ });
}

function initRecords() {
  document.querySelectorAll(".filter-btn").forEach(function (btn) {
    btn.onclick = function () {
      document.querySelectorAll(".filter-btn").forEach(function (b) {
        b.classList.remove("btn-primary");
        b.classList.add("btn-ghost");
      });
      this.classList.remove("btn-ghost");
      this.classList.add("btn-primary");
      recordsFilter = this.getAttribute("data-filter") || "";
      loadRecords(true);
    };
  });
  const moreBtn = document.getElementById("records-more");
  if (moreBtn) moreBtn.onclick = function () { loadRecords(false); };
  loadRecords(true);
}

/* 只初始化一次（loadConfig 每次保存后都会再调一次），但每次都要重新拉取列表，
   否则刚保存完看到的还是旧记录。 */
let recordsInitialized = false;
function initRecordsOnce() {
  if (recordsInitialized) {
    loadRecords(true);
    return;
  }
  recordsInitialized = true;
  initRecords();
}
