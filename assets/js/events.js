/* FnMessageBot · events.js
   ----------------------------------------------------------------------------
   监控事件：每个分类一张卡片，卡片内一行一个事件项，开关控制启用

   本次重写（原来是「一张大卡片 + 网格排布 + 勾选框」）：
   - 每个分类独立成卡片，卡片之间自然堆叠，不再套一个大滚动区
   - 一行一个事件项：左侧名称与说明，右侧开关
   - 勾选框全部换成开关（底层仍是 input[type=checkbox]，故 .checked 读写不变）

   同时修掉一个原有 bug：全选控件也是 input[type=checkbox] 且没有 value，
   浏览器会把它的 value 默认为 "on"；而 app.js 保存时收集的是容器内**所有**
   checkbox 的 value。于是点过全选再保存，"on" 就会被当成事件 id 提交上去。
   现在事件项统一带 .event-input，收集与计数都只认它。

   本文件与其它脚本共享顶层作用域（无 IIFE），不得重名。
   ========================================================================== */

/* 与 index.html 里的 switch 宏保持同一套结构：label.switch + input + span.switch-slider */
function buildEventSwitch(inputClass, ariaLabel, checked) {
  const label = document.createElement("label");
  label.className = "switch";
  label.title = ariaLabel;
  const inp = document.createElement("input");
  inp.type = "checkbox";
  inp.className = inputClass;
  inp.checked = !!checked;
  inp.setAttribute("aria-label", ariaLabel);
  const slider = document.createElement("span");
  slider.className = "switch-slider";
  label.appendChild(inp);
  label.appendChild(slider);
  return { label: label, input: inp };
}

function renderEventCategories(container, categories, selected, anchorBar) {
  if (!container) return;
  container.innerHTML = "";
  if (anchorBar) anchorBar.innerHTML = "";
  const anchorChips = [];

  for (const cat of categories || []) {
    const card = document.createElement("section");
    card.className = "section event-category";

    // ── 卡片头：标题可折叠 + 已选计数 + 全选开关 ──
    const header = document.createElement("div");
    header.className = "event-category-header";

    const titleBtn = document.createElement("button");
    titleBtn.type = "button";
    titleBtn.className = "event-category-toggle";
    titleBtn.setAttribute("aria-expanded", "true");
    const nameSpan = document.createElement("span");
    nameSpan.textContent = cat.name || "";
    titleBtn.appendChild(nameSpan);
    const chevron = document.createElement("span");
    chevron.className = "chevron";
    chevron.textContent = "▾";
    titleBtn.appendChild(chevron);
    titleBtn.addEventListener("click", () => {
      const collapsed = card.classList.toggle("collapsed");
      titleBtn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    });
    header.appendChild(titleBtn);

    const ops = document.createElement("div");
    ops.className = "event-category-ops";
    const count = document.createElement("span");
    count.className = "event-cat-count";
    ops.appendChild(count);

    const all = buildEventSwitch("event-cat-all", "全选「" + (cat.name || "") + "」", false);
    const allWrap = document.createElement("span");
    allWrap.className = "event-cat-all-wrap";
    const allText = document.createElement("span");
    allText.textContent = "全选";
    allWrap.appendChild(allText);
    allWrap.appendChild(all.label);
    ops.appendChild(allWrap);
    header.appendChild(ops);
    card.appendChild(header);

    // ── 事件行：一行一个 ──
    const rows = document.createElement("div");
    rows.className = "event-rows";

    function inputs() {
      return Array.from(rows.querySelectorAll("input.event-input"));
    }
    function updateCatToggle() {
      const boxes = inputs();
      const checkedCount = boxes.filter(b => b.checked).length;
      all.input.checked = boxes.length > 0 && checkedCount === boxes.length;
      count.textContent = `已选 ${checkedCount}/${boxes.length}`;
    }

    for (const ev of cat.events || []) {
      const row = document.createElement("div");
      row.className = "event-row";

      const label = document.createElement("div");
      label.className = "event-row-label";
      const title = document.createElement("span");
      title.className = "event-name";
      title.textContent = ev.title || ev.id;
      label.appendChild(title);
      if (ev.note) {
        const helper = document.createElement("div");
        helper.className = "field-helper";
        helper.textContent = ev.note;
        label.appendChild(helper);
      }

      // value 必须是事件 id：app.js 保存时按它收集
      const sw = buildEventSwitch("event-input", ev.title || ev.id, selected.has(ev.id));
      sw.input.value = ev.id;
      sw.input.addEventListener("change", updateCatToggle);

      row.appendChild(label);
      row.appendChild(sw.label);
      rows.appendChild(row);
    }

    all.input.addEventListener("change", () => {
      const target = all.input.checked;
      inputs().forEach(b => { b.checked = target; });
      updateCatToggle();
    });

    updateCatToggle();
    card.appendChild(rows);
    container.appendChild(card);

    // 锚点芯片：滚动到该分类卡片（折叠状态先展开，否则滚过去只看到标题）
    if (anchorBar) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "event-anchor-chip";
      chip.textContent = cat.name || "";
      chip.addEventListener("click", () => {
        if (card.classList.contains("collapsed")) {
          card.classList.remove("collapsed");
          titleBtn.setAttribute("aria-expanded", "true");
        }
        card.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      anchorBar.appendChild(chip);
      anchorChips.push(chip);
    }
  }
  return anchorChips;
}
