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
  const mode = getStoredThemeMode();
  applyTheme(mode);
  if (themeToggleBtn) {
    themeToggleBtn.addEventListener("click", function() {
      const current = resolveTheme(getStoredThemeMode());
      const nextMode = current === "dark" ? "light" : "dark";
      localStorage.setItem(THEME_STORAGE_KEY, nextMode);
      applyTheme(nextMode);
    });
  }
}
