/* FnMessageBot · channels.js
   ----------------------------------------------------------------------------
   推送渠道：各渠道表单构件的序列化与构建、渠道行增删。
   支持渠道：企业微信 / 钉钉 / 飞书（通用 URL 输入）+ 通用 Webhook（结构化表单）。
   脚本与 app.js 等共享顶层作用域（无 IIFE），不得重名。
   ========================================================================== */

// 多条渠道配置以 | 拼接保存，JSON 字符串内的 | 需转义
function stringifyChannelJson(o) {
  return JSON.stringify(o).replace(/\|/g, "\\u007c");
}

function parseChannelJson(val) {
  if (!val) return {};
  try {
    const o = JSON.parse(val);
    return o && typeof o === "object" ? o : {};
  } catch (e) {
    return {};
  }
}

function fieldValue(wrap, cls) {
  const el = wrap.querySelector("." + cls);
  return el ? String(el.value || "").trim() : "";
}

function makeChannelInput(cls, placeholder, value, type) {
  const inp = document.createElement("input");
  inp.type = type || "text";
  inp.className = cls;
  inp.placeholder = placeholder;
  inp.value = value || "";
  if (type === "password") inp.autocomplete = "new-password";
  return inp;
}

function makeChannelHelper(html) {
  const tip = document.createElement("div");
  tip.className = "field-helper";
  tip.innerHTML = html;
  return tip;
}

function makeAdvancedFold(open) {
  const det = document.createElement("details");
  det.className = "channel-advanced";
  det.open = !!open;
  const sum = document.createElement("summary");
  sum.textContent = "高级选项（可选）";
  sum.style.cursor = "pointer";
  sum.style.fontSize = "12px";
  sum.style.color = "#6b7280";
  sum.style.userSelect = "none";
  det.appendChild(sum);
  const body = document.createElement("div");
  body.style.display = "flex";
  body.style.flexDirection = "column";
  body.style.gap = "8px";
  body.style.marginTop = "8px";
  det.appendChild(body);
  return { el: det, body: body };
}

function serializeWebhookFromInputs(wrap) {
  if (!wrap) return "";
  const url = fieldValue(wrap, "webhook-url");
  if (!url) return "";
  const o = { url: url };
  // 页面固定 POST + JSON；config.json 中手工配置的其它方法/格式原样保留
  if (wrap.dataset.method) o.method = wrap.dataset.method;
  if (wrap.dataset.contentType) o.content_type = wrap.dataset.contentType;
  const headersEl = wrap.querySelector(".webhook-headers");
  const bodyEl = wrap.querySelector(".webhook-body");
  const headers = headersEl ? headersEl.value.trim() : "";
  const body = bodyEl ? bodyEl.value.trim() : "";
  if (headers) o.headers = headers;
  if (body) o.body = body;
  return stringifyChannelJson(o);
}

function buildWebhookWidget(val) {
  const o = parseChannelJson(val);
  const wrap = document.createElement("div");
  wrap.className = "webhook-fields";
  wrap.style.display = "flex";
  wrap.style.flexDirection = "column";
  wrap.style.gap = "8px";
  wrap.style.width = "100%";
  // 页面固定 POST + JSON；config.json 中手工配置的其它方法/格式原样保留
  wrap.dataset.method = o.method || "POST";
  wrap.dataset.contentType = o.content_type || "json";
  const url = makeChannelInput("webhook-url", "Webhook 地址（https://...）", o.url);
  url.style.width = "100%";
  wrap.appendChild(url);
  const adv = makeAdvancedFold(!!(o.headers || o.body));
  const headers = document.createElement("textarea");
  headers.className = "webhook-headers";
  headers.rows = 3;
  headers.placeholder = '请求头（可选，JSON 对象，如 {"Authorization": "Bearer xxx"}）';
  headers.value = typeof o.headers === "string" ? o.headers : (o.headers ? JSON.stringify(o.headers, null, 2) : "");
  adv.body.appendChild(headers);
  const body = document.createElement("textarea");
  body.className = "webhook-body";
  body.rows = 4;
  body.placeholder = "请求体模板（可选；支持 {title} {content} {text} {time} 占位符）";
  body.value = typeof o.body === "string" ? o.body : "";
  adv.body.appendChild(body);
  wrap.appendChild(adv.el);
  wrap.appendChild(makeChannelHelper(
    "默认 POST + JSON（{&quot;title&quot;: ..., &quot;content&quot;: ...}）。高级选项可自定义请求头与请求体模板。"
  ));
  return wrap;
}

function createChannelRow(chType, url, enabled) {
  const tr = document.createElement("tr");
  const initiallyEnabled = enabled !== false;

  const tdType = document.createElement("td");
  // 类名钩子供窄屏布局使用：窄屏下每行改为「卡片」（类型 + 操作一行，地址占满下一行）。
  // 不靠 nth-child 定位，否则列顺序一变样式就会静默错位。
  tdType.className = "channel-type-td";
  const sel = document.createElement("select");
  for (const opt of channelOptions) {
    const o = document.createElement("option");
    o.value = opt.id;
    o.textContent = opt.name;
    if (opt.id === chType) o.selected = true;
    sel.appendChild(o);
  }
  tdType.appendChild(sel);

  const tdUrl = document.createElement("td");
  tdUrl.className = "channel-url-td";
  let rowChannelType = chType;

  function readCurrentSerializedUrl() {
    if (rowChannelType === "webhook") {
      return serializeWebhookFromInputs(tdUrl.querySelector(".webhook-fields"));
    }
    const inp = tdUrl.querySelector("input.channel-url-input");
    return inp ? inp.value.trim() : "";
  }

  function setChannelWidget(type, val) {
    rowChannelType = type;
    tdUrl.innerHTML = "";
    if (type === "webhook") {
      tdUrl.appendChild(buildWebhookWidget(val));
    } else {
      const inp = document.createElement("input");
      inp.type = "text";
      inp.className = "channel-url-input";
      inp.placeholder = "例如：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...";
      inp.value = val || "";
      tdUrl.appendChild(inp);
    }
  }
  setChannelWidget(chType, url || "");

  sel.addEventListener("change", function() {
    const prevVal = readCurrentSerializedUrl();
    setChannelWidget(sel.value, prevVal);
  });

  const tdOp = document.createElement("td");
  tdOp.className = "channel-ops-td";
  const ops = document.createElement("div");
  ops.className = "channel-ops";

  const enableLabel = document.createElement("label");
  // 开关组件统一为 .switch / .switch-slider（定义在 layout.css），全站同一套外观
  enableLabel.className = "switch";
  enableLabel.title = "关闭后保留配置但不推送该渠道";
  const enableInp = document.createElement("input");
  enableInp.type = "checkbox";
  enableInp.className = "channel-enabled-input";
  enableInp.checked = initiallyEnabled;
  enableInp.setAttribute("aria-label", "启用该渠道");
  const slider = document.createElement("span");
  slider.className = "switch-slider";
  enableLabel.appendChild(enableInp);
  enableLabel.appendChild(slider);
  function syncRowEnabledStyle() {
    if (enableInp.checked) tr.classList.remove("channel-disabled");
    else tr.classList.add("channel-disabled");
  }
  enableInp.addEventListener("change", syncRowEnabledStyle);
  syncRowEnabledStyle();

  const delBtn = document.createElement("button");
  delBtn.type = "button";
  delBtn.className = "btn-danger";
  delBtn.textContent = "删除";
  delBtn.title = "删除该渠道配置";
  delBtn.onclick = () => {
    channelsBody.removeChild(tr);
  };
  ops.appendChild(enableLabel);
  ops.appendChild(delBtn);
  tdOp.appendChild(ops);

  tr.appendChild(tdType);
  tr.appendChild(tdUrl);
  tr.appendChild(tdOp);

  channelsBody.appendChild(tr);
}
