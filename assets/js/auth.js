/* FnMessageBot · auth.js
   ----------------------------------------------------------------------------
   鉴权：初始化门禁、首次设置密码、登录

   由 design-previews/_extract_js.py 从模板内联 <script> 提取。
   提取时**未改动任何逻辑与标识符**，仅去掉原有的 4 空格整体缩进。
   注意：这些文件之间共享顶层作用域（无 IIFE），因此**不得重名**；
   脚本按模板中 <script src> 的顺序执行。
   ========================================================================== */

async function initAuth() {
  const authGate = document.getElementById("auth-gate");
  const appMain = document.getElementById("app-main");
  const retryBtn = document.getElementById("auth-retry-btn");
  let data;
  try {
    const res = await fetch("api/auth/status", fetchOpts);
    data = await res.json();
  } catch (err) {
    // 应用重启/网络失败：不能停在两个区块都隐藏的空白页，给出重试入口
    authGate.style.display = "flex";
    appMain.style.display = "none";
    if (retryBtn) retryBtn.style.display = "block";
    const loginMsg = document.getElementById("auth-login-msg");
    loginMsg.textContent = "无法连接服务，请检查应用状态后重试。";
    loginMsg.className = "auth-msg error";
    return;
  }
  if (retryBtn) retryBtn.style.display = "none";
  setupRequiresCode = !!data.setup_requires_code;
  passwordMinLength = data.password_min_length || 8;
  document.getElementById("set-pw-code-wrap").style.display = setupRequiresCode ? "block" : "none";
  if (data.authenticated) {
    authGate.style.display = "none";
    appMain.style.display = "flex";
    loadConfig();
    return;
  }
  authGate.style.display = "flex";
  appMain.style.display = "none";
  document.getElementById("auth-set-password").style.display = data.need_setup ? "block" : "none";
  document.getElementById("auth-login").style.display = data.need_login ? "block" : "none";
  document.getElementById("auth-set-msg").textContent = "";
  document.getElementById("auth-login-msg").textContent = "";
}

document.getElementById("auth-retry-btn").addEventListener("click", initAuth);

document.getElementById("form-set-password").addEventListener("submit", async function(e) {
  e.preventDefault();
  const msgEl = document.getElementById("auth-set-msg");
  const p1 = document.getElementById("set-pw-password").value.trim();
  const p2 = document.getElementById("set-pw-confirm").value.trim();
  const setupCode = document.getElementById("set-pw-code").value.trim();
  msgEl.textContent = "";
  msgEl.className = "auth-msg";
  if (setupRequiresCode && !setupCode) {
    msgEl.textContent = "请输入初始化码";
    msgEl.className = "auth-msg error";
    return;
  }
  if (p1.length < passwordMinLength) {
    msgEl.textContent = "密码长度至少 " + passwordMinLength + " 位";
    msgEl.className = "auth-msg error";
    return;
  }
  if (p1 !== p2) {
    msgEl.textContent = "两次输入的密码不一致";
    msgEl.className = "auth-msg error";
    return;
  }
  const res = await fetch("api/auth/set-password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ password: p1, password_confirm: p2, setup_code: setupCode }),
  });
  const json = await res.json();
  if (json.ok) {
    msgEl.textContent = "设置成功，正在进入配置页…";
    msgEl.className = "auth-msg ok";
    initAuth();
  } else {
    msgEl.textContent = json.message || "设置失败";
    msgEl.className = "auth-msg error";
  }
});

document.getElementById("form-login").addEventListener("submit", async function(e) {
  e.preventDefault();
  const msgEl = document.getElementById("auth-login-msg");
  const password = document.getElementById("login-password").value.trim();
  msgEl.textContent = "";
  msgEl.className = "auth-msg";
  if (!password) {
    msgEl.textContent = "请输入密码";
    msgEl.className = "auth-msg error";
    return;
  }
  const res = await fetch("api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ password }),
  });
  const json = await res.json();
  if (json.ok) {
    msgEl.textContent = "登录成功，正在进入配置页…";
    msgEl.className = "auth-msg ok";
    initAuth();
  } else {
    msgEl.textContent = json.message || "登录失败";
    msgEl.className = "auth-msg error";
  }
});
