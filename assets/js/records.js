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

/* 单条渠道的结果说明（用于**失败的**渠道）：给出错误与返回内容。
   成功时返回空串——成功渠道没有额外信息要说，界面也不显示「成功」二字，
   直接把渠道返回的内容摊开就够了。
   记录里的 success 指的是「推送是否送达」（至少一个渠道成功即算成功），
   与事件本身是好消息还是坏消息无关，所以文案一律写「推送成功/推送失败」，
   不要只写「成功/失败」以免与事件性质混淆。 */
function recordsChannelExtra(c) {
  if (!c || c.success) return "";
  const parts = [];
  const err = recordsStringify(c.error);
  const resp = recordsStringify(c.response);
  if (err) parts.push("错误: " + err);
  if (resp) parts.push("返回: " + resp);
  return parts.join("；") || "无返回详情";
}

/* 渠道返回内容：成功时是接口返回的原文（没有就留空），失败时是错误与返回。
   详情里只呈现这个文本与颜色，不再写「成功/失败」字样。 */
function recordsChannelOutput(c) {
  if (!c) return "";
  if (!c.success) return recordsChannelExtra(c);
  return recordsStringify(c.response);
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

/* 把详情内容渲染成 HTML：只呈现**推送内容**与**渠道返回结果**。
   刻意不在这里写「成功/失败」——
   记录里的 success 表示「推送是否送达」，与事件本身的好坏无关，
   放在详情里既多余又容易与事件性质混淆；某条渠道有没有送达，
   看它后面那段返回内容（或它的颜色）就知道了。
   卡片头部已经显示了时间与事件类型，这里不重复。 */
function recordsBuildDetailHtml(rec, shownSummary) {
  let detail = rec.detail;
  if (typeof detail === "string") {
    try { detail = JSON.parse(detail); } catch (e) { detail = null; }
  }
  const results = (detail && Array.isArray(detail.channel_results)) ? detail.channel_results : [];
  const hasBatch = !!(detail && Array.isArray(detail.batch_items) && detail.batch_items.length);
  const hasFailure = !!(detail && detail.failure_summary);

  let html = "";

  // 卡片那一行已经显示了同样的摘要，详情里再写一遍就是同一句话挨着出现两次。
  // 所以只在「详情里的摘要与卡片不同」或「不写它就只剩空详情」时才展示。
  const redundant = rec.summary && shownSummary && rec.summary === shownSummary;
  if (rec.summary && (!redundant || (!results.length && !hasBatch && !hasFailure))) {
    html += "<h2>推送内容</h2><div class=\"detail-summary\">"
      + recordsEscHtml(rec.summary) + "</div>";
  }

  if (results.length) {
    html += "<h2>渠道返回结果</h2>";
    results.forEach(function (c) {
      c = c || {};
      const out = recordsChannelOutput(c);
      html += '<div class="detail-channel-item' + (c.success ? "" : " is-fail") + '">'
        + '<span class="detail-channel-dot ' + (c.success ? "is-ok" : "is-fail")
        + '" aria-hidden="true"></span>'
        + '<span class="detail-channel-name">' + recordsEscHtml(c.channel || "未知渠道") + "</span>"
        + '<span class="detail-channel-extra">'
        + (out ? recordsEscHtml(out) : "无返回内容") + "</span>"
        + "</div>";
    });
  } else if (hasFailure) {
    // 没有逐渠道明细时，只给出错误原文
    html += "<h2>错误信息</h2><div class=\"detail-summary\">"
      + recordsEscHtml(recordsStringify(detail.failure_summary)) + "</div>";
  }

  if (hasBatch) {
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

  // 都没有（例如只有事件名、没有摘要与渠道明细）时给一句说明，而不是留一片空白
  if (!html) {
    html = '<div class="record-detail-loading">该记录没有更多详情</div>';
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
    const shownSummaryEl = card.querySelector(".record-summary");
    box.innerHTML = recordsBuildDetailHtml(json.data, shownSummaryEl ? shownSummaryEl.textContent : "");
    box.dataset.loaded = "1";
    // 详情里的内容可能比卡片高，展开后再校正一次位置
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }).catch(function () {
    box.innerHTML = '<div class="record-detail-loading">详情加载失败</div>';
  });
}


/* ── 卡片 ── */
/* 一条记录一张卡：首行是时间 / 事件类型 / 推送结果，随后是摘要与各渠道，
   末尾预留一个隐藏的详情容器，展开时就地填进去。
   整卡可点（也支持 Enter/空格）切换详情展开。
   徽标写「推送成功/推送失败」而不是「成功/失败」：记录里的 success 是
   「推送是否送达」的结果，与事件本身是好消息还是坏消息无关
   （例如「任务计划执行失败」这条事件推送成功，只写「成功」会读起来矛盾）。 */
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
  badge.textContent = record.success ? "推送成功" : "推送失败";
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
      const out = recordsChannelOutput(c);
      chip.className = "record-channel " + (c.success ? "is-ok" : "is-fail");
      // 只写渠道名；有返回内容（成功时的接口原文、失败时的错误与返回）就附上。
      // 不写「成功/失败」：卡片右上角已有推送结果徽标，逐渠道再写一遍既重复，
      // 又会与事件性质混淆；失败与否看这段返回内容最直接。
      chip.textContent = out
        ? (c.channel || "未知渠道") + " · " + recordsShorten(out, 48)
        : (c.channel || "未知渠道");
      if (out) chip.title = out;
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
