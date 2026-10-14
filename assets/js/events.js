/* FnMessageBot · events.js
   ----------------------------------------------------------------------------
   监控事件：按分类渲染勾选卡片与全选/折叠

   由 design-previews/_extract_js.py 从模板内联 <script> 提取。
   提取时**未改动任何逻辑与标识符**，仅去掉原有的 4 空格整体缩进。
   注意：这些文件之间共享顶层作用域（无 IIFE），因此**不得重名**；
   脚本按模板中 <script src> 的顺序执行。
   ========================================================================== */

function renderEventCategories(container, categories, selected, anchorBar) {
  if (!container) return;
  container.innerHTML = "";
  if (anchorBar) anchorBar.innerHTML = "";
  const anchorChips = [];
  for (const cat of categories || []) {
    const catBlock = document.createElement("div");
    catBlock.className = "event-category";

    const catHeader = document.createElement("div");
    catHeader.className = "event-category-title";

    // 标题 + 折叠箭头：点击折叠/展开该分组
    const catCollapse = document.createElement("span");
    catCollapse.className = "event-cat-collapse";
    const catTitleSpan = document.createElement("span");
    catTitleSpan.textContent = cat.name || "";
    catCollapse.appendChild(catTitleSpan);
    const chevron = document.createElement("span");
    chevron.className = "chevron";
    chevron.textContent = "▾";
    catCollapse.appendChild(chevron);
    const toggleCollapse = () => catBlock.classList.toggle("collapsed");
    catCollapse.addEventListener("click", toggleCollapse);
    catHeader.appendChild(catCollapse);

    const catOps = document.createElement("span");
    catOps.style.display = "inline-flex";
    catOps.style.alignItems = "center";
    // 已选计数：随勾选实时更新
    const catCount = document.createElement("span");
    catCount.className = "event-cat-count";
    catOps.appendChild(catCount);

    const catToggleLabel = document.createElement("label");
    catToggleLabel.style.fontSize = "13px";
    catToggleLabel.style.cursor = "pointer";
    catToggleLabel.style.flexShrink = "0";
    const catToggle = document.createElement("input");
    catToggle.type = "checkbox";
    catToggle.style.marginRight = "4px";
    catToggleLabel.appendChild(catToggle);
    const catToggleText = document.createElement("span");
    catToggleText.textContent = "全选";
    catToggleLabel.appendChild(catToggleText);
    catOps.appendChild(catToggleLabel);
    catHeader.appendChild(catOps);

    catBlock.appendChild(catHeader);

    const grid = document.createElement("div");
    grid.className = "events-grid";

    function updateCatToggle() {
      const boxes = grid.querySelectorAll("input[type=checkbox]");
      const checkedCount = Array.from(boxes).filter(b => b.checked).length;
      if (!boxes.length) {
        catToggle.checked = false;
      } else {
        catToggle.checked = checkedCount === boxes.length;
      }
      catCount.textContent = `已选 ${checkedCount}/${boxes.length}`;
    }

    for (const ev of cat.events || []) {
      const div = document.createElement("div");
      div.className = "event-item";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = ev.id;
      if (selected.has(ev.id)) cb.checked = true;
      cb.addEventListener("change", () => updateCatToggle());

      const label = document.createElement("div");
      const title = document.createElement("span");
      title.textContent = ev.title || ev.id;
      label.appendChild(title);
      if (ev.note) {
        const helper = document.createElement("div");
        helper.className = "field-helper";
        helper.textContent = ev.note;
        label.appendChild(helper);
      }
      div.appendChild(cb);
      div.appendChild(label);
      grid.appendChild(div);
    }

    catToggle.addEventListener("change", () => {
      const boxes = grid.querySelectorAll("input[type=checkbox]");
      const target = catToggle.checked;
      boxes.forEach(b => { b.checked = target; });
    });

    updateCatToggle();
    catBlock.appendChild(grid);
    container.appendChild(catBlock);

    // 锚点：点击滚动容器到该分组（折叠状态先展开）
    if (anchorBar) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "event-anchor-chip";
      chip.textContent = cat.name || "";
      chip.addEventListener("click", () => {
        if (catBlock.classList.contains("collapsed")) {
          catBlock.classList.remove("collapsed");
        }
        const top = catBlock.getBoundingClientRect().top
          - container.getBoundingClientRect().top
          + container.scrollTop - 4;
        container.scrollTo({ top, behavior: "smooth" });
      });
      anchorBar.appendChild(chip);
      anchorChips.push(chip);
    }
  }
  return anchorChips;
}
