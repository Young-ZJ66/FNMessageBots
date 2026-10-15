/* FnMessageBot · schedule.js
   ----------------------------------------------------------------------------
   定时巡检的周期选择器：把「频率 + 时间」翻译成标准 5 段 Cron

   为什么做这层转换：原来直接让用户填 Cron 表达式，等于要求他会写 crontab。
   现在给出类似闹钟的选项（每天 / 每周 / 每小时 / 每隔 N 小时 / 每隔 N 分钟），
   由本文件生成 Cron 写进隐藏字段 #input-nas-patrol-cron。
   该 id 保持不变，因此 app.js 的保存载荷与后端校验都不需要改。

   保留「自定义 Cron」这一项并在认不出表达式时自动切过去，是刻意的：
   用户手写的 `0 3 * * 1,3,5` 之类不能被选项悄悄改写掉。

   本文件与其它脚本共享顶层作用域（无 IIFE），顶层名字统一 patrol 前缀。
   ========================================================================== */

const PATROL_WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

/* 认得出的模式才转成选项；认不出就交给自定义，绝不猜。 */
function patrolParseCron(cron) {
  const s = String(cron || "").trim().replace(/\s+/g, " ");
  if (!s) return { mode: "daily", hour: 12, minute: 0 };
  const parts = s.split(" ");
  if (parts.length !== 5) return { mode: "custom", raw: s };
  const [mi, ho, dom, mon, dow] = parts;
  const isNum = v => /^\d+$/.test(v);
  const ok = (v, a, b) => isNum(v) && Number(v) >= a && Number(v) <= b;

  // 每天 MM HH * * *
  if (ok(mi, 0, 59) && ok(ho, 0, 23) && dom === "*" && mon === "*" && dow === "*") {
    return { mode: "daily", minute: Number(mi), hour: Number(ho) };
  }
  // 每周 MM HH * * D
  if (ok(mi, 0, 59) && ok(ho, 0, 23) && dom === "*" && mon === "*" && ok(dow, 0, 6)) {
    return { mode: "weekly", minute: Number(mi), hour: Number(ho), weekday: Number(dow) };
  }
  // 每小时 MM * * * *
  if (ok(mi, 0, 59) && ho === "*" && dom === "*" && mon === "*" && dow === "*") {
    return { mode: "hourly", minute: Number(mi) };
  }
  // 每隔 N 小时：0 */N * * *
  let m = /^0 \*\/(\d{1,2})$/.exec(mi + " " + ho);
  if (m && dom === "*" && mon === "*" && dow === "*") {
    const n = Number(m[1]);
    if (n >= 1 && n <= 23) return { mode: "every_hours", intervalHours: n };
  }
  // 每隔 N 分钟：*/N * * * *
  m = /^\*\/(\d{1,2})$/.exec(mi);
  if (m && ho === "*" && dom === "*" && mon === "*" && dow === "*") {
    const n = Number(m[1]);
    if (n >= 1 && n <= 59) return { mode: "every_minutes", intervalMinutes: n };
  }
  return { mode: "custom", raw: s };
}

function patrolBuildCron(state) {
  switch (state.mode) {
    case "daily": return state.minute + " " + state.hour + " * * *";
    case "weekly": return state.minute + " " + state.hour + " * * " + state.weekday;
    case "hourly": return state.minute + " * * * *";
    case "every_hours": return "0 */" + state.intervalHours + " * * *";
    case "every_minutes": return "*/" + state.intervalMinutes + " * * * *";
    default: return (state.raw || "").trim();
  }
}

function patrolPad(n) {
  return (n < 10 ? "0" : "") + n;
}

/* 人类可读的下次执行时间。只对本文件能生成的模式估算；
   自定义表达式不猜（后端 croniter 才是权威）。 */
function patrolNextRunText(state, now) {
  now = now || new Date();
  const dayNames = ["今天", "明天"];
  function dayLabel(d) {
    const diff = Math.floor((new Date(d.getFullYear(), d.getMonth(), d.getDate())
      - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000);
    if (diff === 0) return "今天";
    if (diff === 1) return "明天";
    return PATROL_WEEKDAYS[d.getDay()];
  }
  function at(d, h, m) {
    const t = new Date(d.getFullYear(), d.getMonth(), d.getDate(), h, m, 0, 0);
    return t;
  }
  let target = null;
  if (state.mode === "daily") {
    target = at(now, state.hour, state.minute);
    if (target <= now) target = new Date(target.getTime() + 86400000);
  } else if (state.mode === "weekly") {
    target = at(now, state.hour, state.minute);
    let guard = 0;
    while ((target <= now || target.getDay() !== state.weekday) && guard < 14) {
      target = new Date(target.getTime() + 86400000);
      guard++;
    }
    if (guard >= 14) return "";
  } else if (state.mode === "hourly") {
    target = at(now, now.getHours(), state.minute);
    if (target <= now) target = new Date(target.getTime() + 3600000);
  } else if (state.mode === "every_hours") {
    const n = state.intervalHours;
    for (let step = 0; step <= 24 * 60; step += 60) {
      const t = new Date(now.getTime() + step * 60000);
      t.setMinutes(0, 0, 0);
      if (t > now && t.getHours() % n === 0) { target = t; break; }
    }
    if (!target) return "";
  } else if (state.mode === "every_minutes") {
    const n = state.intervalMinutes;
    for (let step = 0; step <= 60; step++) {
      const t = new Date(now.getTime() + step * 60000);
      t.setSeconds(0, 0);
      if (t > now && t.getMinutes() % n === 0) { target = t; break; }
    }
    if (!target) return "";
  } else {
    return "";
  }
  if (!target) return "";
  return dayLabel(target) + " " + patrolPad(target.getHours()) + ":" + patrolPad(target.getMinutes());
}

/* ── 与 DOM 的对接 ── */
function patrolEls() {
  return {
    mode: document.getElementById("input-patrol-mode"),
    time: document.getElementById("input-patrol-time"),
    weekday: document.getElementById("input-patrol-weekday"),
    minute: document.getElementById("input-patrol-minute"),
    hours: document.getElementById("input-patrol-interval-hours"),
    minutes: document.getElementById("input-patrol-interval-minutes"),
    raw: document.getElementById("input-patrol-cron-raw"),
    hidden: document.getElementById("input-nas-patrol-cron"),
    summary: document.getElementById("patrol-summary"),
    enabled: document.getElementById("input-nas-patrol-enabled"),
    rows: {
      time: document.getElementById("patrol-time-row"),
      weekday: document.getElementById("patrol-weekday-row"),
      minute: document.getElementById("patrol-minute-row"),
      hours: document.getElementById("patrol-hours-row"),
      minutes: document.getElementById("patrol-minutes-row"),
      cron: document.getElementById("patrol-cron-row")
    }
  };
}

function patrolFillSelect(sel, values, format) {
  if (!sel || sel.options.length) return;
  values.forEach(function (v) {
    const o = document.createElement("option");
    o.value = String(v);
    o.textContent = format(v);
    sel.appendChild(o);
  });
}

function patrolReadState() {
  const el = patrolEls();
  const mode = el.mode ? el.mode.value : "daily";
  const timeVal = (el.time && el.time.value) || "12:00";
  const hm = timeVal.split(":");
  return {
    mode: mode,
    hour: Number(hm[0] || 0),
    minute: Number(hm[1] || 0),
    weekday: Number((el.weekday && el.weekday.value) || 1),
    intervalHours: Number((el.hours && el.hours.value) || 6),
    intervalMinutes: Number((el.minutes && el.minutes.value) || 30),
    raw: (el.raw && el.raw.value) || ""
  };
}

function patrolSyncRows(state) {
  const rows = patrolEls().rows;
  const show = {
    time: state.mode === "daily" || state.mode === "weekly",
    weekday: state.mode === "weekly",
    minute: state.mode === "hourly",
    hours: state.mode === "every_hours",
    minutes: state.mode === "every_minutes",
    cron: state.mode === "custom"
  };
  Object.keys(rows).forEach(function (k) {
    if (rows[k]) rows[k].hidden = !show[k];
  });
}

function patrolDescribe(state) {
  switch (state.mode) {
    case "daily":
      return "每天 " + patrolPad(state.hour) + ":" + patrolPad(state.minute);
    case "weekly":
      return "每周" + PATROL_WEEKDAYS[state.weekday] + " " + patrolPad(state.hour) + ":" + patrolPad(state.minute);
    case "hourly":
      return "每小时第 " + state.minute + " 分";
    case "every_hours":
      return "每隔 " + state.intervalHours + " 小时";
    case "every_minutes":
      return "每隔 " + state.intervalMinutes + " 分钟";
    default:
      return "自定义表达式";
  }
}

/* 把当前选择写进隐藏字段并刷新提示。用户交互（select/time 的 change）本身会
   冒泡到 #app-main，由 core.js 的 markConfigDirty 标记未保存，无需在此处理。 */
function patrolRefresh() {
  const el = patrolEls();
  if (!el.mode) return;
  const state = patrolReadState();
  patrolSyncRows(state);
  const cron = patrolBuildCron(state);
  if (el.hidden) el.hidden.value = cron;
  if (el.summary) {
    const next = patrolNextRunText(state);
    let text = "当前设置：" + patrolDescribe(state);
    if (cron) text += "（Cron: " + cron + "）";
    if (next) text += "　下次执行约 " + next;
    if (!cron) text += "　请填写 Cron 表达式";
    el.summary.textContent = text;
  }
}

/* 巡检关闭时把整组周期控件置灰：它们不生效，留着可编辑容易让人误会。 */
function patrolSyncEnabled() {
  const el = patrolEls();
  if (!el.enabled) return;
  const off = !el.enabled.checked;
  ["mode", "time", "weekday", "minute", "hours", "minutes", "raw"].forEach(function (k) {
    if (el[k]) el[k].disabled = off;
  });
}

/* 事件与选项只准备一次：loadConfig 每次保存后都会重新调用，
   若每次都 addEventListener，刷新会被执行多次、选项也会重复。 */
let patrolBound = false;

function patrolBindOnce() {
  if (patrolBound) return;
  const el = patrolEls();
  if (!el.mode) return;
  patrolBound = true;

  patrolFillSelect(el.hours, [1, 2, 3, 4, 6, 8, 12, 24].filter(v => v <= 23),
    v => "每隔 " + v + " 小时");
  patrolFillSelect(el.minutes, [1, 2, 5, 10, 15, 20, 30, 45], v => "每隔 " + v + " 分钟");

  el.mode.addEventListener("change", patrolRefresh);
  if (el.time) el.time.addEventListener("change", patrolRefresh);
  if (el.weekday) el.weekday.addEventListener("change", patrolRefresh);
  if (el.minute) el.minute.addEventListener("change", patrolRefresh);
  if (el.hours) el.hours.addEventListener("change", patrolRefresh);
  if (el.minutes) el.minutes.addEventListener("change", patrolRefresh);
  if (el.raw) el.raw.addEventListener("input", patrolRefresh);
  if (el.enabled) el.enabled.addEventListener("change", patrolSyncEnabled);
}

/* 确保某个值在下拉里有对应选项（手写 Cron 的分钟数可能不在预设步长里）。 */
function patrolEnsureOption(sel, value, text) {
  if (!sel) return;
  let opt = Array.from(sel.options).find(o => o.value === String(value));
  if (!opt) {
    opt = document.createElement("option");
    opt.value = String(value);
    opt.textContent = text;
    sel.appendChild(opt);
  }
  sel.value = String(value);
}

function initPatrolSchedule(cron) {
  const el = patrolEls();
  if (!el.mode) return;
  patrolBindOnce();

  const state = patrolParseCron(cron);
  el.mode.value = state.mode;
  if (state.mode === "custom") {
    if (el.raw) el.raw.value = state.raw || "";
  } else {
    if (el.time && state.hour !== undefined) {
      el.time.value = patrolPad(state.hour) + ":" + patrolPad(state.minute);
    }
    if (el.weekday && state.weekday !== undefined) el.weekday.value = String(state.weekday);
    if (el.minute && state.minute !== undefined) {
      patrolEnsureOption(el.minute, state.minute, "第 " + state.minute + " 分");
    }
    if (el.hours && state.intervalHours) {
      patrolEnsureOption(el.hours, state.intervalHours, "每隔 " + state.intervalHours + " 小时");
    }
    if (el.minutes && state.intervalMinutes) {
      patrolEnsureOption(el.minutes, state.intervalMinutes, "每隔 " + state.intervalMinutes + " 分钟");
    }
  }

  patrolSyncEnabled();
  patrolRefresh();
}
