/* FnMessageBot · history.js
   ----------------------------------------------------------------------------
   推送记录页：列表、筛选、加载更多、详情面板

   由 design-previews/_extract_js.py 从模板内联 <script> 提取，随后删除了其中
   自带的主题初始化（原本读 localStorage 后设置 data-theme）。
   现在主题统一由 theme.js 负责——它随 base.html 在所有页面加载，
   并且支持 ?theme= 与按钮文案更新。

   删除的原因不只是重复：该段声明了 var THEME_STORAGE_KEY，而 theme.js 声明了
   同名的 const。经典脚本共享全局词法环境，已存在 const 绑定时再 var 同名会抛
   SyntaxError，**整个 history.js 都不会执行**——列表与筛选会静默失效。

   本文件与其它脚本共享顶层作用域（无 IIFE），不得重名。
   ========================================================================== */


  var fetchOpts = { credentials: "include" };
  var offset = 0;
  var pageSize = 30;
  var currentFilter = "";
  function stringifyDetailValue(value) {
if (value === null || value === undefined || value === "") return "";
if (typeof value === "string") return value;
try { return JSON.stringify(value); } catch (e) { return String(value); }
  }
  function shortenText(text, maxLen) {
text = text || "";
return text.length > maxLen ? text.slice(0, Math.max(0, maxLen - 1)) + "…" : text;
  }
  function buildChannelExtra(c) {
if (!c || c.success) return "";
var parts = [];
var err = stringifyDetailValue(c.error);
var resp = stringifyDetailValue(c.response);
if (err) parts.push("错误: " + err);
if (resp) parts.push("返回: " + resp);
return parts.join("；") || "无返回详情";
  }
  var detailPanel = document.getElementById("detail-panel");
  function closeDetail() {
if (!detailPanel) return;
detailPanel.style.display = "none";
detailPanel.innerHTML = "";
  }
  function escHtml(s) {
return String(s === null || s === undefined ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function openDetail(id) {
if (!detailPanel || !id) return;
fetch("api/push-history/" + encodeURIComponent(id), fetchOpts).then(function(r){
  if (r.status === 401) { window.location.href = "./"; return null; }
  return r.json();
}).then(function(json){
  if (!json || !json.ok || !json.data) return;
  var rec = json.data;
  var detail = rec.detail;
  if (typeof detail === "string") {
    try { detail = JSON.parse(detail); } catch (e) { detail = null; }
  }
  var html = '<div class="detail-head">';
  html += '<span class="detail-title">' + escHtml(rec.event_type_label || rec.event_type || "") + '</span>';
  html += '<span class="detail-meta">' + escHtml(rec.created_at || "") + '</span>';
  html += '<span class="' + (rec.success ? "result-ok" : "result-fail") + '">' + (rec.success ? "成功" : "失败") + '</span>';
  html += '<button type="button" class="detail-close" id="detail-close">关闭</button>';
  html += '</div>';
  if (rec.summary) html += '<div class="detail-summary">' + escHtml(rec.summary) + '</div>';
  if (detail && Array.isArray(detail.channel_results) && detail.channel_results.length) {
    html += '<h2>渠道返回结果</h2>';
    detail.channel_results.forEach(function(c){
      c = c || {};
      var extra = buildChannelExtra(c);
      html += '<div class="detail-channel-item">' + escHtml(c.channel || "未知渠道") + ': ' + (c.success ? "成功" : "失败")
        + (extra ? '<span class="detail-channel-extra">' + escHtml(extra) + '</span>' : '') + '</div>';
    });
  } else if (detail && detail.failure_summary) {
    html += '<h2>失败原因</h2><div class="detail-summary">' + escHtml(stringifyDetailValue(detail.failure_summary)) + '</div>';
  }
  if (detail && Array.isArray(detail.batch_items) && detail.batch_items.length) {
    var total = Number(detail.batch_total || detail.batch_items.length) || detail.batch_items.length;
    var capped = detail.batch_items.length < total;
    html += '<h2>批次明细（共 ' + total + ' 条' + (capped ? '，仅记录前 ' + detail.batch_items.length + ' 条' : '') + '）</h2>';
    html += '<table class="detail-batch"><thead><tr><th>#</th><th>时间</th><th>事件类型</th><th>内容</th></tr></thead><tbody>';
    detail.batch_items.forEach(function(it, idx){
      it = it || {};
      html += '<tr><td>' + (idx + 1) + '</td>'
        + '<td class="detail-batch-time">' + escHtml(it.timestamp || "") + '</td>'
        + '<td>' + escHtml(it.event_type || "") + '</td>'
        + '<td>' + escHtml(it.brief || "") + '</td></tr>';
    });
    html += '</tbody></table>';
  }
  detailPanel.innerHTML = html;
  detailPanel.style.display = "block";
  document.getElementById("detail-close").onclick = closeDetail;
  detailPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}).catch(function(){});
  }
  function loadList(reset) {
if (reset) { offset = 0; document.getElementById("tbody").innerHTML = ""; }
var params = "limit=" + pageSize + "&offset=" + offset;
if (currentFilter !== "") params += "&success=" + currentFilter;
fetch("api/push-history?" + params, fetchOpts).then(function(r){
  if (r.status === 401) { window.location.href = "./"; return; }
  return r.json();
}).then(function(json){
  if (!json || !json.ok || !Array.isArray(json.data)) return;
  var rows = json.data;
  var tbody = document.getElementById("tbody");
  var emptyEl = document.getElementById("empty");
  if (rows.length === 0 && offset === 0) { emptyEl.style.display = "block"; } else { emptyEl.style.display = "none"; }
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    var tr = document.createElement("tr");
    var td1 = document.createElement("td"); td1.textContent = r.created_at || ""; tr.appendChild(td1);
    var td2 = document.createElement("td"); td2.textContent = r.event_type_label || r.event_type || ""; tr.appendChild(td2);
    var td3 = document.createElement("td"); td3.textContent = r.success ? "成功" : "失败"; td3.className = r.success ? "result-ok" : "result-fail"; tr.appendChild(td3);
    var td4 = document.createElement("td"); td4.className = "summary"; td4.textContent = r.summary || "-"; td4.title = r.summary || ""; tr.appendChild(td4);
    var td5 = document.createElement("td"); td5.className = "channel-results";
    var channelText = "-";
    var channelTitle = "";
    if (r.detail) {
      try {
        var detail = typeof r.detail === "string" ? JSON.parse(r.detail) : r.detail;
        if (detail && Array.isArray(detail.channel_results) && detail.channel_results.length > 0) {
          var parts = [];
          var fullParts = [];
          detail.channel_results.forEach(function(c){
            c = c || {};
            var status = c.success ? "成功" : "失败";
            var extra = buildChannelExtra(c);
            var extraShort = extra ? shortenText(extra, 72) : "";
            var name = c.channel || "未知渠道";
            var short = name + ": " + status + (extraShort ? " (" + extraShort + ")" : "");
            parts.push(short);
            var fullExtra = extra ? " — " + extra : "";
            fullParts.push(name + ": " + status + fullExtra);
          });
          channelText = parts.join("; ");
          channelTitle = fullParts.join(String.fromCharCode(10));
        } else if (detail && detail.failure_summary) {
          channelText = stringifyDetailValue(detail.failure_summary);
          channelTitle = channelText;
        }
      } catch (e) {
        channelText = "详情解析失败";
      }
    }
    td5.textContent = channelText;
    td5.title = channelTitle || channelText;
    tr.appendChild(td5);
    // 点击行展开详情（摘要 / 渠道返回 / 批次明细）
    tr.className = "clickable";
    tr.onclick = function(){ openDetail(r.id); };
    tbody.appendChild(tr);
  }
  offset += rows.length;
  document.getElementById("btn-more").style.display = rows.length >= pageSize ? "inline-block" : "none";
}).catch(function(){});
  }
  document.querySelectorAll(".filter-btn").forEach(function(btn){
btn.onclick = function(){
  document.querySelectorAll(".filter-btn").forEach(function(b){
    b.classList.remove("btn-primary");
    b.classList.add("btn-ghost");
  });
  this.classList.remove("btn-ghost");
  this.classList.add("btn-primary");
  currentFilter = this.getAttribute("data-filter") || "";
  loadList(true);
};
  });
  document.getElementById("btn-more").onclick = function(){ loadList(false); };
  loadList(true);
