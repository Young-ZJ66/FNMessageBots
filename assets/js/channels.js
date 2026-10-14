/* FnMessageBot · channels.js
   ----------------------------------------------------------------------------
   推送渠道：各渠道表单构件的序列化与构建、渠道行增删

   由 design-previews/_extract_js.py 从模板内联 <script> 提取。
   提取时**未改动任何逻辑与标识符**，仅去掉原有的 4 空格整体缩进。
   注意：这些文件之间共享顶层作用域（无 IIFE），因此**不得重名**；
   脚本按模板中 <script src> 的顺序执行。
   ========================================================================== */

function serializeSmtpFromInputs(wrap) {
  if (!wrap) return "";
  const server = (wrap.querySelector(".smtp-server") && wrap.querySelector(".smtp-server").value || "").trim();
  const portRaw = (wrap.querySelector(".smtp-port") && wrap.querySelector(".smtp-port").value || "").trim();
  const username = (wrap.querySelector(".smtp-username") && wrap.querySelector(".smtp-username").value || "").trim();
  const password = (wrap.querySelector(".smtp-password") && wrap.querySelector(".smtp-password").value || "").trim();
  const fromAddr = (wrap.querySelector(".smtp-from") && wrap.querySelector(".smtp-from").value || "").trim();
  const toAddr = (wrap.querySelector(".smtp-to") && wrap.querySelector(".smtp-to").value || "").trim();
  if (!server || !portRaw || !username || !password || !toAddr) return "";
  const parsedPort = Number.parseInt(portRaw, 10);
  if (!Number.isInteger(parsedPort) || parsedPort <= 0) return "";
  const o = {
    server: server,
    port: parsedPort,
    username: username,
    password: password,
    to: toAddr,
  };
  if (fromAddr) o.from = fromAddr;
  return JSON.stringify(o);
}

function syncBarkIconInputsFromMaster() {
  const master = document.getElementById("input-bark-icon");
  if (!master) return;
  const v = master.value;
  channelsBody.querySelectorAll(".bark-icon-input").forEach(function (el) {
    el.value = v;
  });
}

function bindBarkIconInput(iconInp) {
  const master = document.getElementById("input-bark-icon");
  if (!master || !iconInp) return;
  iconInp.addEventListener("input", function () {
    const v = this.value;
    master.value = v;
    channelsBody.querySelectorAll(".bark-icon-input").forEach(function (el) {
      if (el !== iconInp) el.value = v;
    });
  });
}

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
  const method = String(o.method || "").trim().toUpperCase();
  const contentType = String(o.content_type || "").trim().toLowerCase();
  if (method && method !== "POST") wrap.dataset.method = method;
  if (contentType && contentType !== "json") wrap.dataset.contentType = contentType;
  const url = makeChannelInput("webhook-url", "Webhook 地址（如 https://example.com/hook ）", o.url);
  url.style.width = "100%";
  wrap.appendChild(url);
  const hasHeaders = typeof o.headers === "string" ? !!o.headers.trim()
    : !!(o.headers && typeof o.headers === "object" && Object.keys(o.headers).length);
  const adv = makeAdvancedFold(hasHeaders || !!o.body);
  const headers = document.createElement("textarea");
  headers.className = "webhook-headers";
  headers.rows = 2;
  headers.placeholder = "请求头，每行一个，如 Authorization: Bearer xxx";
  headers.value = typeof o.headers === "string" ? o.headers
    : (o.headers && typeof o.headers === "object"
      ? Object.keys(o.headers).map(function (k) { return k + ": " + o.headers[k]; }).join("\n") : "");
  adv.body.appendChild(headers);
  const body = document.createElement("textarea");
  body.className = "webhook-body";
  body.rows = 3;
  body.placeholder = '请求体模板，留空默认 {"title": "{title}", "content": "{content}"}';
  body.value = o.body || "";
  adv.body.appendChild(body);
  adv.body.appendChild(makeChannelHelper(
    "占位符：<code>{title}</code> 标题、<code>{content}</code> 正文、<code>{text}</code> 标题+正文、<code>{time}</code> 发送时间。"
  ));
  wrap.appendChild(adv.el);
  const tip = makeChannelHelper(
    "以 POST + JSON 发送 <code>{\"title\": \"标题\", \"content\": \"正文\"}</code>，返回 HTTP 2xx 视为成功。"
  );
  if (wrap.dataset.method || wrap.dataset.contentType) {
    tip.textContent = "按配置文件中的 " + (wrap.dataset.method || "POST") + " + "
      + (wrap.dataset.contentType || "json") + " 方式发送，返回 HTTP 2xx 视为成功。";
  }
  wrap.appendChild(tip);
  return wrap;
}

function serializeMeowFromInputs(wrap) {
  if (!wrap) return "";
  const nickname = fieldValue(wrap, "meow-nickname");
  if (!nickname) return "";
  const o = { nickname: nickname };
  const link = fieldValue(wrap, "meow-url");
  if (link) o.url = link;
  if (wrap.dataset.apiBase) o.api_base = wrap.dataset.apiBase;
  return stringifyChannelJson(o);
}

function buildMeowWidget(val) {
  const o = parseChannelJson(val);
  const wrap = document.createElement("div");
  wrap.className = "meow-fields";
  wrap.style.display = "flex";
  wrap.style.flexDirection = "column";
  wrap.style.gap = "8px";
  wrap.style.width = "100%";
  if (o.api_base) wrap.dataset.apiBase = String(o.api_base);
  const nick = makeChannelInput("meow-nickname", "MeoW 昵称（App 中注册的昵称）", o.nickname);
  nick.style.width = "100%";
  wrap.appendChild(nick);
  const adv = makeAdvancedFold(!!o.url);
  const link = makeChannelInput("meow-url", "点击通知跳转的链接，如 NAS 访问地址", o.url);
  link.style.width = "100%";
  adv.body.appendChild(link);
  wrap.appendChild(adv.el);
  wrap.appendChild(makeChannelHelper(
    "在 MeoW App 中注册昵称后填入即可，请仔细核对：同一 IP 一天内累计 3 次推送到未注册昵称会被 MeoW 封禁。非会员限 3 秒 1 条、每分钟 15 条、每小时 60 条，事件较多时建议开启「轮询汇总模式」。"
  ));
  return wrap;
}

function createChannelRow(chType, url, enabled) {
  const tr = document.createElement("tr");
  const initiallyEnabled = enabled !== false;

  const tdType = document.createElement("td");
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
  let rowChannelType = chType;

  function readCurrentSerializedUrl() {
    if (rowChannelType === "pushplus") {
      const ta = tdUrl.querySelector("textarea");
      return ta ? ta.value.trim() : "";
    }
    if (rowChannelType === "smtp") {
      const wrap = tdUrl.querySelector(".smtp-fields");
      return serializeSmtpFromInputs(wrap);
    }
    if (rowChannelType === "webhook") {
      return serializeWebhookFromInputs(tdUrl.querySelector(".webhook-fields"));
    }
    if (rowChannelType === "meow") {
      return serializeMeowFromInputs(tdUrl.querySelector(".meow-fields"));
    }
    if (rowChannelType === "bark") {
      const inp = tdUrl.querySelector(".bark-channel-fields input.channel-url-input");
      return inp ? inp.value.trim() : "";
    }
    const inp = tdUrl.querySelector("input.channel-url-input");
    return inp ? inp.value.trim() : "";
  }

  function setChannelWidget(type, val) {
    rowChannelType = type;
    tdUrl.innerHTML = "";
    if (type === "pushplus") {
      const ta = document.createElement("textarea");
      ta.rows = 3;
      ta.placeholder = PUSHPLUS_PLACEHOLDER;
      ta.value = val || "";
      ta.style.minHeight = "60px";
      tdUrl.appendChild(ta);
    } else if (type === "smtp") {
      let server = "", port = "", username = "", password = "", fromAddr = "", toAddr = "";
      if (val) {
        try {
          const o = JSON.parse(val);
          if (o && typeof o === "object") {
            server = (o.server || "").trim();
            port = String(o.port || "").trim();
            username = (o.username || "").trim();
            password = (o.password || "").trim();
            fromAddr = (o.from || "").trim();
            toAddr = (o.to || "").trim();
          }
        } catch (e) { /* ignore */ }
      }
      const wrap = document.createElement("div");
      wrap.className = "smtp-fields";
      wrap.style.display = "grid";
      wrap.style.gridTemplateColumns = "repeat(3, minmax(160px, 1fr))";
      wrap.style.gap = "8px";
      const inServer = document.createElement("input");
      inServer.type = "text";
      inServer.className = "smtp-server";
      inServer.placeholder = "SMTP服务器（如 smtp.qq.com）";
      inServer.value = server;
      const inPort = document.createElement("input");
      inPort.type = "number";
      inPort.className = "smtp-port";
      inPort.placeholder = "端口（465/587）";
      inPort.value = port;
      const inUser = document.createElement("input");
      inUser.type = "text";
      inUser.className = "smtp-username";
      inUser.placeholder = "用户名（邮箱）";
      inUser.value = username;
      const inPass = document.createElement("input");
      inPass.type = "password";
      inPass.className = "smtp-password";
      inPass.placeholder = "密码/授权码";
      inPass.autocomplete = "new-password";
      inPass.value = password;
      const inFrom = document.createElement("input");
      inFrom.type = "text";
      inFrom.className = "smtp-from";
      inFrom.placeholder = "发件人（可选，默认同用户名）";
      inFrom.value = fromAddr;
      const inTo = document.createElement("input");
      inTo.type = "text";
      inTo.className = "smtp-to";
      inTo.placeholder = "收件人（多个用英文逗号）";
      inTo.value = toAddr;
      wrap.appendChild(inServer);
      wrap.appendChild(inPort);
      wrap.appendChild(inUser);
      wrap.appendChild(inPass);
      wrap.appendChild(inFrom);
      wrap.appendChild(inTo);
      tdUrl.appendChild(wrap);
    } else if (type === "webhook") {
      tdUrl.appendChild(buildWebhookWidget(val));
    } else if (type === "meow") {
      tdUrl.appendChild(buildMeowWidget(val));
    } else if (type === "bark") {
      const wrap = document.createElement("div");
      wrap.className = "bark-channel-fields";
      wrap.style.display = "flex";
      wrap.style.flexDirection = "column";
      wrap.style.gap = "8px";
      wrap.style.width = "100%";
      const inp = document.createElement("input");
      inp.type = "text";
      inp.className = "channel-url-input";
      inp.placeholder = "Bark 推送 URL（如 https://api.day.app/YourKey ）";
      inp.value = val || "";
      const iconWrap = document.createElement("div");
      iconWrap.style.marginTop = "2px";
      const iconLabel = document.createElement("div");
      iconLabel.className = "field-helper";
      iconLabel.style.marginBottom = "2px";
      iconLabel.textContent = "Bark 自定义图标 URL（留空则默认；须公网 https 图片）";
      const iconInp = document.createElement("input");
      iconInp.type = "text";
      iconInp.className = "bark-icon-input";
      iconInp.placeholder = "留空则使用 Bark 默认图标";
      const master = document.getElementById("input-bark-icon");
      iconInp.value = master ? master.value : "";
      bindBarkIconInput(iconInp);
      iconWrap.appendChild(iconLabel);
      iconWrap.appendChild(iconInp);
      wrap.appendChild(inp);
      wrap.appendChild(iconWrap);
      tdUrl.appendChild(wrap);
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
  enableLabel.className = "channel-switch";
  enableLabel.title = "关闭后保留配置但不推送该渠道";
  const enableInp = document.createElement("input");
  enableInp.type = "checkbox";
  enableInp.className = "channel-enabled-input";
  enableInp.checked = initiallyEnabled;
  enableInp.setAttribute("aria-label", "启用该渠道");
  const slider = document.createElement("span");
  slider.className = "channel-switch-slider";
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
