/* FnMessageBot · faq.js
   ----------------------------------------------------------------------------
   常见问题页：目录与折叠交互

   由 design-previews/_extract_js.py 从模板内联 <script> 提取。
   提取时**未改动任何逻辑与标识符**，仅去掉原有的 4 空格整体缩进。
   注意：这些文件之间共享顶层作用域（无 IIFE），因此**不得重名**；
   脚本按模板中 <script src> 的顺序执行。
   ========================================================================== */


(function () {
  var key = "fnmb_theme";
  var mode = localStorage.getItem(key);
  var resolved = (mode === "dark") ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", resolved);
})();
