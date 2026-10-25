/* FnMessageBot · app.js
   ----------------------------------------------------------------------------
   加载配置与推送统计、初始化入口

   由 design-previews/_extract_js.py 从模板内联 <script> 提取。
   提取时**未改动任何逻辑与标识符**，仅去掉原有的 4 空格整体缩进。
   注意：这些文件之间共享顶层作用域（无 IIFE），因此**不得重名**；
   脚本按模板中 <script src> 的顺序执行。
   ========================================================================== */

async function loadConfig() {
  try {
    const res = await fetch("api/config", fetchOpts);
    const json = await res.json();
    if (res.status === 401) {
      initAuth();
      return;
    }
    if (!json.ok) {
      setStatus(false, json.message || "加载配置失败");
      return;
    }
    renderPersistentWarnings(json.warnings || []);
    const data = json.data;
    document.getElementById("app-title").textContent = data.title || "FnMessageBot";
    document.getElementById("app-version").textContent = data.version || "";

    channelOptions = data.channel_options || [];

    const selected = new Set(data.selected_events || []);
    renderEventCategories(eventsContainer, data.events_by_category || [], selected, document.getElementById("events-anchor-bar"));
    renderEventCategories(thirdPartyEventsContainer, data.third_party_events_by_category || [], selected);

    // 渲染渠道
    channelsBody.innerHTML = "";
    if (data.channels && data.channels.length) {
      for (const ch of data.channels) {
        createChannelRow(ch.type || "wechat", ch.url || "", ch.enabled !== false);
      }
    } else {
      // 默认只展示一行企业微信，需要其他渠道可点击「添加渠道」
      createChannelRow("wechat", "", true);
    }

    document.getElementById("input-log-days").value = data.log_retention_days || 7;
    document.getElementById("input-poll-interval").value = data.logger_poll_interval || 5;
    document.getElementById("input-resume-from-cursor").checked = !!data.resume_from_cursor_on_start;
    document.getElementById("input-poll-batch-summary").checked = !!data.poll_batch_summary_enabled;
    document.getElementById("input-media-batch-enabled").checked = data.media_batch_summary_enabled !== false;
    document.getElementById("input-media-batch-window").value = (data.media_batch_window ?? 90);
    document.getElementById("input-minimal-push-enabled").checked = !!data.minimal_push_enabled;
    document.getElementById("input-push-markdown-enabled").checked = data.push_markdown_enabled !== false;
    document.getElementById("input-ssh-ignore-loopback").checked = !!data.ssh_ignore_loopback;
    document.getElementById("input-title-prefix").value =
      typeof data.title_prefix === "string" ? data.title_prefix : "飞牛NAS";
    const dndEnabled = !!data.dnd_enabled;
    document.getElementById("input-dnd-enabled").checked = dndEnabled;
    document.getElementById("input-dnd-start").value = data.dnd_start_time || "22:00";
    document.getElementById("input-dnd-end").value = data.dnd_end_time || "07:00";
    document.getElementById("input-dnd-start").disabled = !dndEnabled;
    document.getElementById("input-dnd-end").disabled = !dndEnabled;
    const dndToggle = document.getElementById("input-dnd-enabled");
    dndToggle.onchange = function() {
      const en = document.getElementById("input-dnd-enabled").checked;
      document.getElementById("input-dnd-start").disabled = !en;
      document.getElementById("input-dnd-end").disabled = !en;
    };

    const nasEn = document.getElementById("input-nas-patrol-enabled");
    if (nasEn) nasEn.checked = !!data.nas_patrol_enabled;
    // 周期选择器：把服务端的 Cron 还原成「频率 + 时间」等选项，
    // 再由选项生成隐藏字段 #input-nas-patrol-cron 的值（保存时读它）。
    initPatrolSchedule(data.nas_patrol_cron || "0 12 * * *");

    // 飞牛监控接口（巡检盘温/健康数据源）：密码为打码回显，保持不变即保留
    const fnosEn = document.getElementById("input-fnos-monitor-enabled");
    if (fnosEn) fnosEn.checked = !!data.fnos_monitor_enabled;
    const fnosUrl = document.getElementById("input-fnos-monitor-url");
    if (fnosUrl) fnosUrl.value = data.fnos_monitor_url || "ws://127.0.0.1:5666";
    const fnosUser = document.getElementById("input-fnos-monitor-user");
    if (fnosUser) fnosUser.value = data.fnos_monitor_user || "";
    const fnosPwd = document.getElementById("input-fnos-monitor-password");
    if (fnosPwd) fnosPwd.value = data.fnos_monitor_password || "";

    // 配置已从服务端加载成功即可测推送，不必再次点「保存配置」。
    testBtn.disabled = false;
    setStatus(false, "");
    loadPushStats();
    // 推送记录列表：只有确认已鉴权（配置读到了）才拉取，避免未登录时的 401 重载循环
    initRecordsOnce();
  } catch (e) {
    console.error(e);
    setStatus(false, "加载配置失败，请检查服务是否正常运行。");
  }
}

async function loadPushStats() {
  try {
    const res = await fetch("api/push-stats", fetchOpts);
    const json = await res.json();
    if (!json.ok || !json.data) return;
    const t = json.data.total || {};
    const d = json.data.today || {};
    document.getElementById("stat-total-total").textContent = (t.total ?? 0);
    document.getElementById("stat-total-success").textContent = (t.success ?? 0);
    document.getElementById("stat-total-fail").textContent = (t.fail ?? 0);
    document.getElementById("stat-today-total").textContent = (d.total ?? 0);
    document.getElementById("stat-today-success").textContent = (d.success ?? 0);
    document.getElementById("stat-today-fail").textContent = (d.fail ?? 0);
  } catch (e) { /* ignore */ }
}

addChannelBtn.addEventListener("click", () => {
  const defaultType = channelOptions.length ? channelOptions[0].id : "wechat";
  createChannelRow(defaultType, "", true);
});

saveBtn.addEventListener("click", async () => {
  setStatus(false, "");
  const events = [];
  [eventsContainer, thirdPartyEventsContainer].forEach(el => {
    if (!el) return;
    // 只认 .event-input（事件项本身）。若按 input[type=checkbox] 收集，
    // 各分类的「全选」开关也会被算进来，而它没有 value、浏览器默认给 "on"，
    // 于是点过全选再保存就会多提交一个名为 "on" 的事件 id。
    el.querySelectorAll("input.event-input").forEach(cb => {
      if (cb.checked) events.push(cb.value);
    });
  });

  const channels = [];
  channelsBody.querySelectorAll("tr").forEach(tr => {
    const sel = tr.querySelector("select");
    if (!sel) return;
    const type = sel.value;
    let url = "";
    if (type === "webhook") {
      url = serializeWebhookFromInputs(tr.querySelector(".webhook-fields"));
      if (!url) return;
    } else {
      const inp = tr.querySelector("input.channel-url-input");
      if (!inp) return;
      url = inp.value.trim();
      if (!url) return;
    }
    const enableInp = tr.querySelector("input.channel-enabled-input");
    const enabled = enableInp ? !!enableInp.checked : true;
    channels.push({ type, url, enabled });
  });

  const payload = {
    events,
    channels,
    log_retention_days: document.getElementById("input-log-days").value,
    logger_poll_interval: document.getElementById("input-poll-interval").value,
    resume_from_cursor_on_start: document.getElementById("input-resume-from-cursor").checked,
    title_prefix: (document.getElementById("input-title-prefix").value || "").trim(),
    poll_batch_summary_enabled: document.getElementById("input-poll-batch-summary").checked,
    media_batch_summary_enabled: document.getElementById("input-media-batch-enabled").checked,
    media_batch_window: (function () {
      const v = parseInt(document.getElementById("input-media-batch-window").value, 10);
      return Number.isFinite(v) ? Math.max(0, Math.min(3600, v)) : 90;
    })(),
    minimal_push_enabled: document.getElementById("input-minimal-push-enabled").checked,
    push_markdown_enabled: document.getElementById("input-push-markdown-enabled").checked,
    ssh_ignore_loopback: document.getElementById("input-ssh-ignore-loopback").checked,
    dnd_enabled: document.getElementById("input-dnd-enabled").checked,
    dnd_start_time: document.getElementById("input-dnd-start").value || "22:00",
    dnd_end_time: document.getElementById("input-dnd-end").value || "07:00",
    nas_patrol_enabled: document.getElementById("input-nas-patrol-enabled").checked,
    nas_patrol_cron: (document.getElementById("input-nas-patrol-cron").value || "").trim(),
    fnos_monitor_enabled: document.getElementById("input-fnos-monitor-enabled").checked,
    fnos_monitor_url: (document.getElementById("input-fnos-monitor-url").value || "").trim(),
    fnos_monitor_user: (document.getElementById("input-fnos-monitor-user").value || "").trim(),
    fnos_monitor_password: document.getElementById("input-fnos-monitor-password").value || "",
  };

  try {
    const res = await fetch("api/save-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(payload),
    });
    const json = await res.json();
    if (res.ok && json.ok) {
      showToast(true, json.message || "配置已保存");
      clearConfigDirty();
      if (Array.isArray(json.warnings) && json.warnings.length) {
        json.warnings.forEach(w => showToast(false, "权限告警：" + w));
      }
      renderPersistentWarnings(json.warnings || []);
      testBtn.disabled = false;
      loadConfig();
    } else {
      showToast(false, json.message || "保存失败");
    }
  } catch (e) {
    console.error(e);
    showToast(false, "保存失败，请检查网络或稍后再试");
  }
});

testBtn.addEventListener("click", async () => {
  setStatus(false, "正在发送测试消息...");
  try {
    const res = await fetch("api/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({}),
    });
    const text = await res.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) { json = null; }
    if (res.ok && json.ok) {
      showToast(true, json.message || "测试消息已发送");
    } else {
      showToast(false, (json && json.message) ? json.message : ("测试发送失败：" + (text ? text.slice(0, 120) : "")));
    }
  } catch (e) {
    console.error(e);
    showToast(false, "测试发送失败，请稍后重试");
  }
  setStatus(false, "");
});

if (nasPatrolRunBtn) {
  nasPatrolRunBtn.addEventListener("click", async () => {
    nasPatrolRunBtn.disabled = true;
    setStatus(false, "正在执行巡检推送...");
    try {
      const res = await fetch("api/nas-patrol/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: "{}",
      });
      const text = await res.text();
      let json = null;
      try { json = JSON.parse(text); } catch (e) { json = null; }
      if (res.ok && json && json.ok) {
        showToast(true, json.message || "巡检已推送");
      } else {
        showToast(false, (json && json.message) ? json.message : ("巡检失败：" + (text ? text.slice(0, 160) : "")));
      }
    } catch (e) {
      console.error(e);
      showToast(false, "巡检请求失败，请稍后重试");
    } finally {
      nasPatrolRunBtn.disabled = false;
      setStatus(false, "");
    }
  });
}

window.addEventListener("load", () => {
  // 主题由 theme.js 自初始化（它会随 base.html 在所有页面加载），此处不再调用：
  // 重复调用会给切换按钮绑第二次点击监听，导致切换互相抵消。
  initTabs();  // 先确定显示哪个页签面板，再做鉴权判定与数据加载
  initAuth();
  setInterval(loadPushStats, 30000);
});
