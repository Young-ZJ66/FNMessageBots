/* FnMessageBot · theme.js
   ----------------------------------------------------------------------------
   主题：读取/解析/应用/切换，以及切换按钮的绑定

   由 design-previews/_extract_js.py 从模板内联 <script> 提取。
   提取时**未改动任何逻辑与标识符**，仅去掉原有的 4 空格整体缩进。
   注意：这些文件之间共享顶层作用域（无 IIFE），因此**不得重名**；
   脚本按模板中 <script src> 的顺序执行。
   ========================================================================== */


const THEME_STORAGE_KEY = "fnmb_theme";
const themeToggleBtn = document.getElementById("theme-toggle-btn");

function getStoredThemeMode() {
  const mode = localStorage.getItem(THEME_STORAGE_KEY);
  return (mode === "light" || mode === "dark") ? mode : "light";
}

// 初始主题：?theme= 优先于本地保存。用途有两个——分享带主题的链接，
// 以及在无头浏览器里截图验证深色模式（无法预先写 localStorage）。
function initialThemeMode() {
  try {
    const q = new URLSearchParams(location.search).get("theme");
    if (q === "light" || q === "dark") return q;
  } catch (e) { /* 无 URLSearchParams 时退回本地保存 */ }
  return getStoredThemeMode();
}

function resolveTheme(mode) {
  return mode === "dark" ? "dark" : "light";
}

function applyTheme(mode) {
  const resolved = resolveTheme(mode);
  document.documentElement.setAttribute("data-theme", resolved);
  if (!themeToggleBtn) return;
  if (resolved === "dark") {
    themeToggleBtn.textContent = "浅色";
    themeToggleBtn.title = "切换到浅色模式";
    themeToggleBtn.setAttribute("aria-label", "切换到浅色模式");
  } else {
    themeToggleBtn.textContent = "深色";
    themeToggleBtn.title = "切换到深色模式";
    themeToggleBtn.setAttribute("aria-label", "切换到深色模式");
  }
}

function initTheme() {
  applyTheme(initialThemeMode());
  if (themeToggleBtn) {
    themeToggleBtn.addEventListener("click", function() {
      // 以「当前已应用」的主题为准来翻转，而不是本地保存值：
      // 这样 ?theme= 指定的主题也能被正确切走。
      const current = resolveTheme(document.documentElement.getAttribute("data-theme"));
      const nextMode = current === "dark" ? "light" : "dark";
      localStorage.setItem(THEME_STORAGE_KEY, nextMode);
      applyTheme(nextMode);
    });
  }
}

// 自初始化：本文件在 base.html 中于 </body> 前加载，此时按钮已存在。
// 曾经由 app.js 的 load 处理器调用，但 app.js 只在配置页加载，
// 于是历史记录页与常见问题页的 data-theme 从未被设置过——深色模式形同不存在。
// 注意不可两处都调：initTheme 每次都会新增点击监听，绑两次会让切换互相抵消。
initTheme();
