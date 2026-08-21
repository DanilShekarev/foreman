// Канбан задач команды: одна общая доска, колонки фиксированы на сервере.
// Оверлей поверх чата; keyed-рендер без полной перерисовки (скролл колонок
// живёт), pointer-based drag&drop с placeholder-тенью и автоскроллом,
// детальная боковая панель, фильтры, метки/приоритет/срок/чеклист/комментарии.
// Живое обновление — по ревизии в поллинге (KANBAN.onRev).
const KANBAN = (() => {
  let view = null;            // корневой оверлей
  let data = null;            // {columns, rev}
  let cards = new Map();      // id -> карточка (мини-вид из /api/kanban)
  let labels = [];            // справочник меток
  let nodes = new Map();      // id -> DOM мини-карточки
  let openCardId = null;      // карточка, открытая в панели
  let panelCard = null;       // полные данные панели
  let drag = null;            // состояние перетаскивания
  let pendingRefresh = false; // onRev пришёл во время драга

  const PRIO_RU = {low: "низкий", normal: "обычный",
                   high: "высокий", urgent: "срочный"};

  let filters = {q: "", assignee: 0, labels: [], priority: "", mine: false};
  try {
    filters = {...filters,
               ...JSON.parse(localStorage.getItem("gd_kb_filters") || "{}")};
  } catch (e) { /* битый localStorage — дефолты */ }

  function saveFilters(){
    localStorage.setItem("gd_kb_filters", JSON.stringify(filters));
  }

  function isOpen(){
    return !!view && view.style.display !== "none";
  }

  // ------------------------------------------------------------- каркас

  function ensureView(){
    if (view) return view;
    view = document.createElement("div");
    view.id = "kanban-view";
    view.style.display = "none";
    view.innerHTML = `
      <div id="kb-header">
        <span class="kb-title">${ICON("board")} Задачи</span>
        <div id="kb-filter">
          <input type="text" id="kb-q" placeholder="Поиск задач…">
          <button id="kb-f-mine" title="Только мои задачи">Мои</button>
          <select id="kb-f-assignee" title="Исполнитель">
            <option value="0">Все люди</option>
          </select>
          <select id="kb-f-prio" title="Приоритет">
            <option value="">Любой приоритет</option>
            <option value="urgent">Срочный</option>
            <option value="high">Высокий</option>
            <option value="normal">Обычный</option>
            <option value="low">Низкий</option>
          </select>
          <span id="kb-f-labels"></span>
          <button id="kb-f-reset" style="display:none" title="Сбросить фильтры">${ICON("x", "xs")} сброс</button>
        </div>
        <button id="kb-labels-btn" title="Справочник меток">${ICON("tag", "sm")} Метки</button>
        <button id="kb-close" title="Закрыть">${ICON("x")}</button>
      </div>
      <div id="kb-main">
        <div id="kb-columns"></div>
        <div id="kb-panel" style="display:none"></div>
      </div>
      <div id="kb-drag-layer"></div>`;
    document.body.appendChild(view);
    view.querySelector("#kb-close").onclick = close;
    view.querySelector("#kb-labels-btn").onclick = labelsModal;

    const q = view.querySelector("#kb-q");
    q.value = filters.q;
    let qTimer = null;
    q.oninput = () => {
      clearTimeout(qTimer);
      qTimer = setTimeout(() => {
        filters.q = q.value.trim();
        saveFilters(); sync();
      }, 150);
    };
    const mine = view.querySelector("#kb-f-mine");
    mine.onclick = () => {
      filters.mine = !filters.mine;
      saveFilters(); sync();
    };
    view.querySelector("#kb-f-assignee").onchange = e => {
      filters.assignee = +e.target.value;
      saveFilters(); sync();
    };
    view.querySelector("#kb-f-prio").onchange = e => {
      filters.priority = e.target.value;
      saveFilters(); sync();
    };
    view.querySelector("#kb-f-reset").onclick = () => {
      filters = {q: "", assignee: 0, labels: [], priority: "", mine: false};
      q.value = "";
      view.querySelector("#kb-f-assignee").value = "0";
      view.querySelector("#kb-f-prio").value = "";
      saveFilters(); sync();
    };

    document.addEventListener("keydown", e => {
      if (e.key !== "Escape" || !isOpen()) return;
      if (drag && drag.active){ cancelDrag(); return; }
      if (document.getElementById("modal-back").style.display !== "none") return;
      if (openCardId != null){ closePanel(); return; }
      close();
    });
    return view;
  }

  function buildColumns(){
    const cols = view.querySelector("#kb-columns");
    if (cols.childElementCount) return;
    for (const col of data.columns){
      const el = document.createElement("div");
      el.className = "kb-col";
      el.dataset.key = col.key;
      el.innerHTML = `
        <div class="kb-col-head">
          <span class="kb-col-name">${MD.esc(col.title)}</span>
          <span class="kb-count"></span>
        </div>
        <div class="kb-cards"></div>
        <div class="kb-empty" style="display:none">Пока пусто</div>
        <button class="kb-quick-btn">${ICON("plus", "sm")} Задача</button>
        <div class="kb-quick-add" style="display:none">
          <textarea rows="2" placeholder="Заголовок задачи…"></textarea>
          <div class="kb-quick-row">
            <button class="kb-quick-ok">Добавить</button>
            <button class="kb-quick-x">${ICON("x", "sm")}</button>
          </div>
        </div>`;
      bindQuickAdd(el, col.key);
      cols.appendChild(el);
    }
  }

  function bindQuickAdd(colEl, colKey){
    const btn = colEl.querySelector(".kb-quick-btn");
    const box = colEl.querySelector(".kb-quick-add");
    const ta = box.querySelector("textarea");
    const show = on => {
      btn.style.display = on ? "none" : "";
      box.style.display = on ? "" : "none";
      if (on){ ta.value = ""; ta.focus(); }
    };
    btn.onclick = () => show(true);
    box.querySelector(".kb-quick-x").onclick = () => show(false);
    const submit = async () => {
      const title = ta.value.trim();
      if (!title) { show(false); return; }
      show(false);
      const r = await fetch("api/kanban", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({title, column: colKey}),
      });
      const card = await r.json().catch(() => ({}));
      if (card.error){ alert(card.error); return; }
      cards.set(card.id, card);
      sync();
    };
    box.querySelector(".kb-quick-ok").onclick = submit;
    ta.onkeydown = e => {
      if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); submit(); }
      if (e.key === "Escape"){ e.stopPropagation(); show(false); }
    };
  }

  // Подсветка в сайдбаре: доска открыта — активен пункт «Задачи», чат гаснет
  // (renderList в app.js смотрит на isOpen()). Флаг переживает перезагрузку.
  function syncNav(on){
    const nav = document.getElementById("nav-kanban");
    if (nav) nav.classList.toggle("active", on);
    // const APP не попадает в window — проверять только через typeof.
    if (typeof APP !== "undefined" && APP.rerenderLists) APP.rerenderLists();
  }

  async function open(){
    ensureView().style.display = "flex";
    localStorage.setItem("gd_kb_open", "1");
    syncNav(true);
    await refresh();
  }

  function close(){
    if (!view || view.style.display === "none") return;
    view.style.display = "none";
    localStorage.removeItem("gd_kb_open");
    syncNav(false);
    // Чат под доской снова виден — теперь его можно пометить прочитанным.
    if (typeof CHAT !== "undefined" && CHAT.markVisible) CHAT.markVisible();
  }

  async function refresh(){
    const r = await fetch("api/kanban");
    if (!r.ok) return;
    data = await r.json();
    cards = new Map(data.cards.map(c => [c.id, c]));
    labels = data.labels || [];
    buildColumns();
    syncFilterBar();
    sync();
    if (openCardId != null && !panelBusy()) reloadPanel();
  }

  // Поллинг сообщает ревизию доски; во время драга обновление откладываем.
  function onRev(rev){
    if (!isOpen() || !data || rev === data.rev) return;
    if (drag && drag.active){ pendingRefresh = true; return; }
    refresh();
  }

  // ------------------------------------------------------------- фильтры

  function syncFilterBar(){
    const sel = view.querySelector("#kb-f-assignee");
    const cur = String(filters.assignee || 0);
    sel.innerHTML = `<option value="0">Все люди</option>` +
      (window.USERS || []).map(u =>
        `<option value="${u.id}">${MD.esc(u.display_name)}</option>`).join("");
    sel.value = [...sel.options].some(o => o.value === cur) ? cur : "0";
    view.querySelector("#kb-f-prio").value = filters.priority;

    const box = view.querySelector("#kb-f-labels");
    box.innerHTML = "";
    for (const l of labels){
      const chip = document.createElement("span");
      chip.className = "kb-chip kb-chip-pick lab-" + l.color +
        (filters.labels.includes(l.id) ? " on" : "");
      chip.textContent = l.name;
      chip.onclick = () => {
        const i = filters.labels.indexOf(l.id);
        if (i >= 0) filters.labels.splice(i, 1);
        else filters.labels.push(l.id);
        saveFilters(); syncFilterBar(); sync();
      };
      box.appendChild(chip);
    }
  }

  function filtersActive(){
    return !!(filters.q || filters.assignee || filters.labels.length ||
              filters.priority || filters.mine);
  }

  function matches(c){
    if (filters.q &&
        !(c.title + " " + (c.body || "")).toLowerCase()
          .includes(filters.q.toLowerCase())) return false;
    const myId = window.ME && window.ME.id;
    if (filters.mine && !(c.assignees || []).some(a => a.user_id === myId))
      return false;
    if (filters.assignee &&
        !(c.assignees || []).some(a => a.user_id === filters.assignee))
      return false;
    if (filters.priority && c.priority !== filters.priority) return false;
    if (filters.labels.length &&
        !(c.labels || []).some(l => filters.labels.includes(l.id)))
      return false;
    return true;
  }

  // ------------------------------------------------------------- рендер

  const AVA_COLORS = ["var(--av-1)", "var(--av-2)", "var(--av-3)",
                      "var(--av-4)", "var(--av-5)", "var(--av-6)"];

  function avatarColor(name){
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.codePointAt(0)) % 360;
    return AVA_COLORS[h % 6];
  }

  function initials(name){
    const parts = (name || "?").trim().split(/\s+/);
    return ((parts[0][0] || "?") + (parts[1] ? parts[1][0] : "")).toUpperCase();
  }

  function fmtDue(iso){
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString("ru-RU", {day: "numeric", month: "short"})
            .replace(".", "");
  }

  function isOverdue(c){
    if (!c.due_date || c.column_key === "done") return false;
    return c.due_date < new Date().toISOString().slice(0, 10);
  }

  function fmtWhen(iso){
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleDateString("ru-RU") + " " +
           d.toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"});
  }

  function stamp(c){
    return [c.title, c.updated_at, c.priority, c.due_date, c.column_key,
            c.checklist_done, c.checklist_total, c.comments_count,
            (c.labels || []).map(l => l.id + l.color + l.name).join(","),
            (c.assignees || []).map(a => a.user_id).join(","),
            openCardId === c.id ? "open" : ""].join("|");
  }

  function fillCard(div, c){
    div.dataset.stamp = stamp(c);
    div.classList.toggle("open", openCardId === c.id);
    const chips = (c.labels || []).map(l =>
      `<span class="kb-chip lab-${l.color}" title="${MD.esc(l.name)}">${MD.esc(l.name)}</span>`).join("");
    const badges = [];
    if (c.priority && c.priority !== "normal")
      badges.push(`<span class="kb-badge kb-prio-${c.priority}"
        title="Приоритет: ${PRIO_RU[c.priority]}">${PRIO_RU[c.priority]}</span>`);
    if (c.due_date)
      badges.push(`<span class="kb-badge kb-due${isOverdue(c) ? " overdue" : ""}"
        title="Срок">${ICON("calendar", "xs")} ${fmtDue(c.due_date)}</span>`);
    if (c.checklist_total)
      badges.push(`<span class="kb-badge${c.checklist_done === c.checklist_total ? " done" : ""}"
        title="Чеклист">${ICON("check", "xs")} ${c.checklist_done}/${c.checklist_total}</span>`);
    if (c.comments_count)
      badges.push(`<span class="kb-badge" title="Комментарии">${ICON("thread", "xs")} ${c.comments_count}</span>`);
    const ppl = (c.assignees || []).map(a =>
      `<span class="kb-ava" title="${MD.esc(a.name)}"
         style="background:${avatarColor(a.name)}">${MD.esc(initials(a.name))}</span>`).join("");
    div.innerHTML = `
      ${chips ? `<div class="kb-chips">${chips}</div>` : ""}
      <div class="kb-card-title">${MD.esc(c.title)}</div>
      <div class="kb-card-foot">
        <span class="kb-id">#${c.id}</span>
        ${badges.join("")}
        <span class="kb-people">${ppl}</span>
      </div>`;
  }

  function buildCard(c){
    const div = document.createElement("div");
    div.className = "kb-card";
    div.dataset.id = c.id;
    fillCard(div, c);
    div.addEventListener("pointerdown", e => onPointerDown(e, div, c.id));
    // contextmenu — событие правой кнопки, drag тут не участвует
    // (onPointerDown уже игнорирует всё, кроме e.button === 0).
    div.addEventListener("contextmenu", e => {
      e.preventDefault();
      openCardCtxMenu(e.clientX, e.clientY, c.id);
    });
    return div;
  }

  // Правый клик по мини-карточке: id — стабильный ключ, свежие данные карточки
  // и колонок берём из cards/data в момент клика (см. стиль onPointerUp —
  // сама карточка node может быть отрисована по устаревшему снапшоту).
  function openCardCtxMenu(x, y, id){
    const cur = cards.get(id);
    if (!cur) return;
    const items = [{icon: ICON("chevron-right", "sm"), label: "Открыть", act: () => openPanel(id)}];
    for (const col of data.columns){
      if (col.key === cur.column_key) continue;
      items.push({icon: ICON("board", "sm"), label: `В «${MD.esc(col.title)}»`,
        act: () => patchCard(id, {column: col.key}, {column_key: col.key})});
    }
    items.push({icon: ICON("trash", "sm"), label: "Удалить", danger: true,
      act: () => { if (confirm("Убрать задачу с доски?")) patchCard(id, {archived: true}); }});
    APP.openContextMenu(x, y, items);
  }

  // Keyed-рендер: узлы переиспользуются, патчатся по штампу и переставляются
  // insertBefore — скролл и фокус не сбрасываются.
  function sync(){
    if (!view || !data) return;
    const seen = new Set();
    for (const colEl of view.querySelectorAll(".kb-col")){
      const key = colEl.dataset.key;
      const list = colEl.querySelector(".kb-cards");
      const colCards = [...cards.values()]
        .filter(c => c.column_key === key)
        .sort((a, b) => a.position - b.position || a.id - b.id);
      const shown = colCards.filter(matches);
      let i = 0;
      for (const c of shown){
        seen.add(c.id);
        let node = nodes.get(c.id);
        if (!node){
          node = buildCard(c);
          nodes.set(c.id, node);
        } else if (node.dataset.stamp !== stamp(c)){
          fillCard(node, c);
        }
        const ref = list.children[i] || null;
        if (node !== ref) list.insertBefore(node, ref);
        i++;
      }
      while (list.children.length > i){
        const extra = list.lastElementChild;
        list.removeChild(extra);
      }
      colEl.querySelector(".kb-count").textContent =
        shown.length < colCards.length
          ? `${shown.length} из ${colCards.length}` : (colCards.length || "");
      colEl.querySelector(".kb-empty").style.display =
        shown.length ? "none" : "";
    }
    for (const [id, node] of nodes){
      if (!seen.has(id)){ node.remove(); nodes.delete(id); }
    }
    view.querySelector("#kb-f-reset").style.display =
      filtersActive() ? "" : "none";
    view.querySelector("#kb-f-mine").classList.toggle("on", filters.mine);
  }

  // ------------------------------------------------------- запросы + мердж

  async function patchCard(id, body, optimistic){
    if (optimistic){
      const c = cards.get(id);
      if (c) Object.assign(c, optimistic);
      sync();
    }
    const r = await fetch(`api/kanban/${id}`, {
      method: "PATCH", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const resp = await r.json().catch(() => ({}));
    if (resp.error){
      alert(resp.error);
      await refresh();
      return null;
    }
    if (resp.archived_at) cards.delete(id);
    else cards.set(id, resp);
    sync();
    return resp;
  }

  // ------------------------------------------------------------- drag&drop
  // Свой pointer-based DnD: HTML5 DnD не даёт контроля над ghost-снапшотом.
  // Оригинал карточки становится placeholder-тенью (.kb-ghost) и ездит по
  // DOM за курсором, за курсором летит клон в #kb-drag-layer.

  // ВАЖНО: слушатели — на window, а capture — на неподвижном #kanban-view.
  // Вешать capture на карточку нельзя: placeGhost переставляет её через
  // insertBefore (remove+insert), и браузер молча сбрасывает capture —
  // события перестают приходить, драг «застревает» в своей колонке.
  function onPointerDown(e, div, id){
    if (e.button !== 0) return;
    if (e.target.closest("a, button, input, textarea")) return;
    drag = {id, div, pointerId: e.pointerId,
            startX: e.clientX, startY: e.clientY,
            x: e.clientX, y: e.clientY, active: false, clone: null, raf: 0};
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerCancel);
  }

  function startDrag(){
    const {div} = drag;
    const rect = div.getBoundingClientRect();
    drag.dx = drag.startX - rect.left;
    drag.dy = drag.startY - rect.top;
    const clone = div.cloneNode(true);
    clone.className = "kb-card kb-drag-clone";
    clone.style.width = rect.width + "px";
    view.querySelector("#kb-drag-layer").appendChild(clone);
    drag.clone = clone;
    div.classList.add("kb-ghost");
    drag.active = true;
    document.body.classList.add("kb-dragging");
    try { view.setPointerCapture(drag.pointerId); } catch (e) { /* мышь уже отпущена */ }
    moveClone();
    drag.raf = requestAnimationFrame(autoScroll);
  }

  function moveClone(){
    drag.clone.style.left = (drag.x - drag.dx) + "px";
    drag.clone.style.top = (drag.y - drag.dy) + "px";
  }

  function onPointerMove(e){
    if (!drag || e.pointerId !== drag.pointerId) return;
    drag.x = e.clientX; drag.y = e.clientY;
    if (!drag.active){
      if (Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY) < 5)
        return;
      startDrag();
    }
    moveClone();
    placeGhost();
  }

  // Колонка под курсором: сперва честный hit-test, затем по X-диапазону —
  // колонки прижаты кверху, и под короткой колонкой «пусто».
  function columnAt(x, y){
    const el = document.elementFromPoint(x, y);
    const hit = el && el.closest(".kb-col");
    if (hit) return hit;
    for (const colEl of view.querySelectorAll(".kb-col")){
      const r = colEl.getBoundingClientRect();
      if (x >= r.left && x <= r.right) return colEl;
    }
    return null;
  }

  function placeGhost(){
    const col = columnAt(drag.x, drag.y);
    if (!col) return;
    const list = col.querySelector(".kb-cards");
    const others = [...list.querySelectorAll(".kb-card:not(.kb-ghost)")];
    const after = others.find(n =>
      drag.y < n.getBoundingClientRect().top + n.offsetHeight / 2) || null;
    if (after) list.insertBefore(drag.div, after);
    else list.appendChild(drag.div);
  }

  function autoScroll(){
    if (!drag || !drag.active) return;
    const M = 48;
    const colEl = columnAt(drag.x, drag.y);
    const list = colEl && colEl.querySelector(".kb-cards");
    if (list){
      const r = list.getBoundingClientRect();
      if (drag.y < r.top + M) list.scrollTop -= (r.top + M - drag.y) / 3;
      else if (drag.y > r.bottom - M)
        list.scrollTop += (drag.y - (r.bottom - M)) / 3;
    }
    const cols = view.querySelector("#kb-columns");
    const cr = cols.getBoundingClientRect();
    if (drag.x < cr.left + M) cols.scrollLeft -= (cr.left + M - drag.x) / 3;
    else if (drag.x > cr.right - M)
      cols.scrollLeft += (drag.x - (cr.right - M)) / 3;
    drag.raf = requestAnimationFrame(autoScroll);
  }

  function teardownDrag(){
    const {div, clone, raf, pointerId} = drag;
    cancelAnimationFrame(raf);
    if (clone) clone.remove();
    div.classList.remove("kb-ghost");
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
    window.removeEventListener("pointercancel", onPointerCancel);
    try { view.releasePointerCapture(pointerId); } catch (e) { /* уже отпущен */ }
    document.body.classList.remove("kb-dragging");
    drag = null;
  }

  function cancelDrag(){
    if (!drag) return;
    teardownDrag();
    sync();  // вернёт карточку на место из состояния
    afterDragRefresh();
  }

  function onPointerCancel(){
    cancelDrag();
  }

  function afterDragRefresh(){
    if (pendingRefresh){ pendingRefresh = false; refresh(); }
  }

  function onPointerUp(e){
    if (!drag || e.pointerId !== drag.pointerId) return;
    const {id, div, active} = drag;
    teardownDrag();
    if (!active){ openPanel(id); return; }
    const list = div.parentElement;
    const colKey = list.closest(".kb-col").dataset.key;
    const prev = cards.get(+(div.previousElementSibling?.dataset.id || 0));
    const next = cards.get(+(div.nextElementSibling?.dataset.id || 0));
    let pos;
    if (prev && next) pos = (prev.position + next.position) / 2;
    else if (prev) pos = prev.position + 1;
    else if (next) pos = next.position - 1;
    else pos = 1;
    patchCard(id, {column: colKey, position: pos},
              {column_key: colKey, position: pos})
      .then(afterDragRefresh);
  }

  // ------------------------------------------------------------- панель

  function panelBusy(){
    const p = view.querySelector("#kb-panel");
    if (p.querySelector(".kb-p-body-toast")) return true;  // описание правится
    const a = document.activeElement;
    return p.contains(a) && (a.tagName === "TEXTAREA" ||
                             a.tagName === "INPUT" || a.isContentEditable);
  }

  // WYSIWYG для описания карточки: тот же Toast UI, что в волте, но компактный
  // и без картинок (у карточек нет своего хранилища файлов). null — нет vendor.
  function makeBodyEditor(holder, text){
    if (!window.toastui || !toastui.Editor) return null;
    const ed = new toastui.Editor({
      el: holder,
      height: "280px",
      initialEditType: "wysiwyg",
      previewStyle: "tab",
      initialValue: text,
      language: "ru",
      usageStatistics: false,
      autofocus: true,
      toolbarItems: [
        ["heading", "bold", "italic", "strike"],
        ["ul", "ol", "task"],
        ["table", "link"],
        ["code", "codeblock"],
      ],
      widgetRules: [
        {rule: /\[\[[^\[\]\n]+\]\]/,
         toDOM(t){
           const s = document.createElement("span");
           s.className = "wikilink";
           s.textContent = t.slice(2, -2);
           return s;
         }},
        {rule: /@([a-zA-Z0-9_.-]+|агент)/,
         toDOM(t){
           const s = document.createElement("span");
           s.className = "mention";
           s.textContent = t;
           return s;
         }},
      ],
    });
    holder.classList.toggle("toastui-editor-dark",
      document.documentElement.dataset.theme !== "light");
    return ed;
  }

  function closePanel(){
    openCardId = null;
    panelCard = null;
    view.querySelector("#kb-panel").style.display = "none";
    view.classList.remove("panel-open");
    sync();
  }

  async function openPanel(id){
    openCardId = id;
    view.classList.add("panel-open");
    const p = view.querySelector("#kb-panel");
    p.style.display = "";
    p.innerHTML = `<div class="kb-p-loading">…</div>`;
    sync();
    await reloadPanel();
  }

  async function reloadPanel(){
    if (openCardId == null) return;
    const r = await fetch(`api/kanban/${openCardId}`);
    const card = await r.json().catch(() => ({}));
    if (card.error){ closePanel(); return; }
    panelCard = card;
    renderPanel();
  }

  // Ответы ручек мини-вида вливаются и в доску, и в панель.
  function mergeCard(resp){
    if (!resp || resp.error) return;
    if (resp.archived_at) cards.delete(resp.id);
    else cards.set(resp.id, resp);
    if (panelCard && panelCard.id === resp.id)
      Object.assign(panelCard, resp);
    sync();
  }

  function colTitle(key){
    const c = (data.columns || []).find(x => x.key === key);
    return c ? c.title : key;
  }

  function actLine(a){
    const who = MD.esc(a.actor_name || "агент");
    const d = a.data || {};
    switch (a.kind){
      case "create": return `${who} — создание карточки`;
      case "move":
        return `${who}: «${MD.esc(colTitle(d.from))}» → «${MD.esc(colTitle(d.to))}»`;
      case "title": return `${who} — заголовок: «${MD.esc(d.to || "")}»`;
      case "body": return `${who} — описание изменено`;
      case "assignees": return `${who} — исполнители изменены`;
      case "labels": return `${who} — метки изменены`;
      case "priority":
        return `${who} — приоритет: ${PRIO_RU[d.from] || d.from} → ${PRIO_RU[d.to] || d.to}`;
      case "due":
        return d.to ? `${who} — срок: ${fmtDue(d.to)}` : `${who} — срок снят`;
      case "checklist":
        return d.add ? `${who} — пункт «${MD.esc(d.add)}»`
                     : `${who} — убран пункт «${MD.esc(d.del || "")}»`;
      case "archive": return `${who} — в архив`;
      case "restore": return `${who} — из архива`;
    }
    return `${who} — ${MD.esc(a.kind)}`;
  }

  function popover(anchor, buildContent){
    const old = view.querySelector(".kb-pop");
    if (old){ old.remove(); return null; }
    const pop = document.createElement("div");
    pop.className = "kb-pop";
    buildContent(pop);
    anchor.parentElement.appendChild(pop);
    const away = e => {
      if (!pop.contains(e.target) && e.target !== anchor){
        pop.remove();
        document.removeEventListener("pointerdown", away, true);
      }
    };
    setTimeout(() =>
      document.addEventListener("pointerdown", away, true), 0);
    return pop;
  }

  function renderPanel(){
    const p = view.querySelector("#kb-panel");
    const c = panelCard;
    const done = c.checklist.filter(i => i.done).length;
    const total = c.checklist.length;
    const pct = total ? Math.round(done / total * 100) : 0;
    const colOpts = data.columns.map(x =>
      `<option value="${x.key}"${x.key === c.column_key ? " selected" : ""}>${MD.esc(x.title)}</option>`).join("");
    const prioOpts = Object.entries(PRIO_RU).map(([k, v]) =>
      `<option value="${k}"${k === c.priority ? " selected" : ""}>${v}</option>`).join("");
    const chips = (c.labels || []).map(l =>
      `<span class="kb-chip lab-${l.color}">${MD.esc(l.name)}</span>`).join("");
    const ppl = (c.assignees || []).map(a =>
      `<span class="kb-ava" title="${MD.esc(a.name)}"
         style="background:${avatarColor(a.name)}">${MD.esc(initials(a.name))}</span>`).join("");
    p.innerHTML = `
      <div class="kb-p-head">
        <span class="kb-id">#${c.id}</span>
        <span style="flex:1"></span>
        <button id="kb-p-arch" title="Убрать с доски (в архив)">${ICON("archive")}</button>
        <button id="kb-p-close" title="Закрыть (Esc)">${ICON("x")}</button>
      </div>
      <div class="kb-p-scroll">
        <div class="kb-p-title" id="kb-p-title" title="Нажмите, чтобы переименовать">${MD.esc(c.title)}</div>
        <div class="kb-p-props">
          <label>Колонка
            <select id="kb-p-col">${colOpts}</select></label>
          <label>Приоритет
            <select id="kb-p-prio">${prioOpts}</select></label>
          <label>Срок
            <span class="kb-p-due">
              <input type="date" id="kb-p-due" value="${c.due_date || ""}">
              ${c.due_date ? `<button id="kb-p-due-x" title="Снять срок">${ICON("x", "xs")}</button>` : ""}
            </span></label>
          <label>Исполнители
            <span class="kb-p-row">
              <span class="kb-people">${ppl || "<i>никого</i>"}</span>
              <button id="kb-p-assign">${ICON("plus", "sm")}</button>
            </span></label>
          <label>Метки
            <span class="kb-p-row">
              <span>${chips || "<i>нет</i>"}</span>
              <button id="kb-p-labels">${ICON("plus", "sm")}</button>
            </span></label>
        </div>
        <div class="kb-p-sec">
          <div class="kb-p-cap">Описание
            <button id="kb-p-body-edit" title="Редактировать">${ICON("pencil", "sm")}</button></div>
          <div class="kb-p-body md" id="kb-p-body">${
            c.body ? MD.render(c.body) : '<i class="kb-p-none">нет описания</i>'}</div>
        </div>
        <div class="kb-p-sec">
          <div class="kb-p-cap">Чеклист${total ? ` <small>${done}/${total}</small>` : ""}</div>
          ${total ? `<div class="kb-progress"><i style="width:${pct}%"></i></div>` : ""}
          <div id="kb-p-checks"></div>
          <div class="kb-p-addline">
            <input type="text" id="kb-p-check-new" placeholder="Новый пункт…">
          </div>
        </div>
        <div class="kb-p-sec">
          <div class="kb-p-cap">Комментарии</div>
          <div id="kb-p-comments"></div>
          <div class="kb-p-addline">
            <textarea id="kb-p-comment-new" rows="2"
              placeholder="Комментарий… (Ctrl+Enter — отправить)"></textarea>
            <button id="kb-p-comment-send">${ICON("send", "sm")}</button>
          </div>
        </div>
        <div class="kb-p-sec">
          <div class="kb-p-cap kb-p-acts-toggle" id="kb-p-acts-cap">
            История <small>(${c.activity.length})</small>
            <span class="kb-p-acts-chev">${ICON("chevron-right", "xs")}</span></div>
          <div id="kb-p-acts" style="display:none"></div>
        </div>
        <div class="kb-p-meta">создал(а) ${MD.esc(c.creator_name || "агент")}
          · ${fmtWhen(c.created_at)}</div>
      </div>`;

    p.querySelector("#kb-p-close").onclick = closePanel;
    p.querySelector("#kb-p-arch").onclick = async () => {
      if (!confirm("Убрать задачу с доски?")) return;
      await patchCard(c.id, {archived: true});
      closePanel();
    };

    const titleEl = p.querySelector("#kb-p-title");
    titleEl.onclick = () => {
      const input = document.createElement("input");
      input.type = "text"; input.maxLength = 200; input.value = c.title;
      input.className = "kb-p-title-input";
      titleEl.replaceWith(input);
      input.focus(); input.select();
      const commit = async () => {
        const t = input.value.trim();
        if (t && t !== c.title) mergeCard(await patchCard(c.id, {title: t}));
        renderPanel();
      };
      input.onblur = commit;
      input.onkeydown = e => {
        if (e.key === "Enter") input.blur();
        if (e.key === "Escape"){ e.stopPropagation(); input.value = c.title; input.blur(); }
      };
    };

    p.querySelector("#kb-p-col").onchange = async e =>
      mergeCard(await patchCard(c.id, {column: e.target.value},
                                {column_key: e.target.value}));
    p.querySelector("#kb-p-prio").onchange = async e =>
      mergeCard(await patchCard(c.id, {priority: e.target.value},
                                {priority: e.target.value}));
    p.querySelector("#kb-p-due").onchange = async e => {
      mergeCard(await patchCard(c.id, {due: e.target.value || null}));
      renderPanel();
    };
    const dueX = p.querySelector("#kb-p-due-x");
    if (dueX) dueX.onclick = async () => {
      mergeCard(await patchCard(c.id, {due: null}));
      renderPanel();
    };

    p.querySelector("#kb-p-assign").onclick = e => popover(e.target, pop => {
      const assigned = new Set((c.assignees || []).map(a => a.user_id));
      pop.innerHTML = (window.USERS || []).map(u =>
        `<label class="kb-pick"><input type="checkbox" value="${u.id}"${assigned.has(u.id) ? " checked" : ""}>
           ${MD.esc(u.display_name)} <small>@${u.login}</small></label>`).join("")
        || "<i>пока никого</i>";
      pop.addEventListener("change", async () => {
        const ids = [...pop.querySelectorAll("input:checked")].map(i => +i.value);
        mergeCard(await patchCard(c.id, {assignees: ids}));
        renderPanel();
      });
    });

    p.querySelector("#kb-p-labels").onclick = e => popover(e.target, pop => {
      const on = new Set((c.labels || []).map(l => l.id));
      pop.innerHTML = labels.map(l =>
        `<label class="kb-pick"><input type="checkbox" value="${l.id}"${on.has(l.id) ? " checked" : ""}>
           <span class="kb-chip lab-${l.color}">${MD.esc(l.name)}</span></label>`).join("")
        + `<div class="kb-pop-foot"><button id="kb-pop-manage">${ICON("tag", "xs")} управлять метками</button></div>`;
      pop.addEventListener("change", async () => {
        const ids = [...pop.querySelectorAll("input:checked")].map(i => +i.value);
        mergeCard(await patchCard(c.id, {labels: ids}));
        renderPanel();
      });
      pop.querySelector("#kb-pop-manage").onclick = () => {
        pop.remove(); labelsModal();
      };
    });

    p.querySelector("#kb-p-body-edit").onclick = () => {
      const box = p.querySelector("#kb-p-body");
      const holder = document.createElement("div");
      holder.className = "gd-toast kb-p-body-toast";
      box.replaceWith(holder);
      const ed = makeBodyEditor(holder, c.body || "");
      let ta = null;
      if (!ed){  // vendor не загрузился — обычная textarea
        ta = document.createElement("textarea");
        ta.rows = 8; ta.value = c.body || "";
        ta.className = "kb-p-body-edit";
        holder.replaceWith(ta);
      }
      const row = document.createElement("div");
      row.className = "kb-p-addline";
      row.innerHTML = `<button id="kb-p-body-save">Сохранить</button>
                       <button id="kb-p-body-cancel">Отмена</button>`;
      (ta || holder).after(row);
      if (ta) ta.focus();
      const finish = () => { if (ed) ed.destroy(); };
      row.querySelector("#kb-p-body-cancel").onclick = () => {
        finish(); renderPanel();
      };
      row.querySelector("#kb-p-body-save").onclick = async () => {
        const text = ed ? ed.getMarkdown() : ta.value;
        finish();
        mergeCard(await patchCard(c.id, {body: text}));
        panelCard.body = text;
        renderPanel();
      };
    };

    const checksBox = p.querySelector("#kb-p-checks");
    for (const it of c.checklist){
      const row = document.createElement("div");
      row.className = "kb-check" + (it.done ? " done" : "");
      row.innerHTML = `
        <input type="checkbox"${it.done ? " checked" : ""}>
        <span class="kb-check-text">${MD.esc(it.text)}</span>
        <button class="kb-check-del" title="Удалить пункт">${ICON("x", "xs")}</button>`;
      row.querySelector("input").onchange = async e => {
        it.done = e.target.checked ? 1 : 0;
        row.classList.toggle("done", !!it.done);
        const resp = await fetch(`api/kanban/checklist/${it.id}`, {
          method: "PATCH", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({done: !!it.done}),
        }).then(x => x.json()).catch(() => ({}));
        if (resp.error){ alert(resp.error); reloadPanel(); return; }
        const card = cards.get(c.id);
        if (card){
          card.checklist_done = c.checklist.filter(i => i.done).length;
          sync();
        }
        renderPanel();
      };
      row.querySelector(".kb-check-del").onclick = async () => {
        const resp = await fetch(`api/kanban/checklist/${it.id}`,
                                 {method: "DELETE"})
          .then(x => x.json()).catch(() => ({}));
        if (resp.error) alert(resp.error);
        await refresh();
        reloadPanel();
      };
      checksBox.appendChild(row);
    }
    const checkNew = p.querySelector("#kb-p-check-new");
    checkNew.onkeydown = async e => {
      if (e.key !== "Enter") return;
      const text = checkNew.value.trim();
      if (!text) return;
      checkNew.value = "";
      const resp = await fetch(`api/kanban/${c.id}/checklist`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text}),
      }).then(x => x.json()).catch(() => ({}));
      if (resp.error){ alert(resp.error); return; }
      await refresh();
      await reloadPanel();
      view.querySelector("#kb-p-check-new")?.focus();
    };

    const comBox = p.querySelector("#kb-p-comments");
    // Агент — та же иконка-«звёздочка», что и его аватар в чате
    // (ICON("spark")), а не буква-инициал. Определяется по author_user_id
    // (у агента NULL): имя-то у него есть — «Агент», по пустому имени
    // его не поймать.
    comBox.innerHTML = c.comments.map(cm => {
      const isAgent = !cm.author_user_id;
      const ava = isAgent
        ? `<span class="kb-ava kb-ava-agent">${ICON("spark", "xs")}</span>`
        : `<span class="kb-ava" style="background:${avatarColor(cm.author_name)}">${MD.esc(initials(cm.author_name))}</span>`;
      return `
      <div class="kb-comment">
        <div class="kb-comment-head">
          ${ava}
          <b>${MD.esc(cm.author_name || "Агент")}</b>
          <small>${fmtWhen(cm.created_at)}</small>
        </div>
        <div class="md">${MD.render(cm.text)}</div>
      </div>`;
    }).join("") || '<i class="kb-p-none">пока нет</i>';
    const comNew = p.querySelector("#kb-p-comment-new");
    const sendComment = async () => {
      const text = comNew.value.trim();
      if (!text) return;
      comNew.value = "";
      const resp = await fetch(`api/kanban/${c.id}/comments`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text}),
      }).then(x => x.json()).catch(() => ({}));
      if (resp.error){ alert(resp.error); return; }
      await refresh();
      reloadPanel();
    };
    p.querySelector("#kb-p-comment-send").onclick = sendComment;
    comNew.onkeydown = e => {
      if (e.key === "Enter" && e.ctrlKey){ e.preventDefault(); sendComment(); }
    };

    const actsCap = p.querySelector("#kb-p-acts-cap");
    const actsBox = p.querySelector("#kb-p-acts");
    actsBox.innerHTML = c.activity.map(a => `
      <div class="kb-act">${actLine(a)}
        <small>${fmtWhen(a.created_at)}</small></div>`).join("")
      || '<i class="kb-p-none">пусто</i>';
    actsCap.onclick = () => {
      const on = actsBox.style.display === "none";
      actsBox.style.display = on ? "" : "none";
      actsCap.querySelector(".kb-p-acts-chev").innerHTML =
        ICON(on ? "chevron-down" : "chevron-right", "xs");
    };
  }

  // ------------------------------------------------------------- метки CRUD

  function labelsModal(){
    const back = document.getElementById("modal-back");
    const modal = document.getElementById("modal");
    const COLORS = ["red", "orange", "yellow", "green", "teal",
                    "blue", "purple", "pink", "gray"];
    const swatches = (sel, lid) => COLORS.map(col =>
      `<span class="kb-swatch lab-${col}${col === sel ? " on" : ""}"
         data-label="${lid}" data-color="${col}"></span>`).join("");
    const row = l => `
      <div class="kb-lab-row" data-id="${l.id}">
        <input type="text" value="${MD.esc(l.name)}" maxlength="40">
        <span class="kb-swatches">${swatches(l.color, l.id)}</span>
        <button class="kb-lab-del" title="Удалить метку">${ICON("x", "sm")}</button>
      </div>`;
    modal.innerHTML = `
      <h3>${ICON("tag")} Метки</h3>
      <div id="kb-lab-list">${labels.map(row).join("") || "<i>пока нет</i>"}</div>
      <div class="kb-lab-row kb-lab-new">
        <input type="text" id="kb-lab-new-name" placeholder="Новая метка…" maxlength="40">
        <button id="kb-lab-add">${ICON("plus", "sm")}</button>
      </div>
      <div class="row"><button id="kb-lab-close">Готово</button></div>`;
    back.style.display = "";
    const done = async () => {
      back.style.display = "none";
      await refresh();
      if (openCardId != null) reloadPanel();
    };
    modal.querySelector("#kb-lab-close").onclick = done;

    modal.querySelectorAll(".kb-lab-row[data-id]").forEach(rowEl => {
      const id = +rowEl.dataset.id;
      const input = rowEl.querySelector("input");
      input.onchange = async () => {
        const resp = await fetch(`api/kanban/labels/${id}`, {
          method: "PATCH", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name: input.value}),
        }).then(x => x.json()).catch(() => ({}));
        if (resp.error) alert(resp.error);
      };
      rowEl.querySelector(".kb-lab-del").onclick = async () => {
        if (!confirm("Удалить метку со всех карточек?")) return;
        const resp = await fetch(`api/kanban/labels/${id}`, {method: "DELETE"})
          .then(x => x.json()).catch(() => ({}));
        if (resp.error){ alert(resp.error); return; }
        labels = labels.filter(l => l.id !== id);
        rowEl.remove();
      };
    });
    modal.querySelectorAll(".kb-swatch").forEach(sw => {
      sw.onclick = async () => {
        const resp = await fetch(`api/kanban/labels/${sw.dataset.label}`, {
          method: "PATCH", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({color: sw.dataset.color}),
        }).then(x => x.json()).catch(() => ({}));
        if (resp.error){ alert(resp.error); return; }
        sw.parentElement.querySelectorAll(".kb-swatch")
          .forEach(x => x.classList.toggle("on", x === sw));
      };
    });
    const add = async () => {
      const name = modal.querySelector("#kb-lab-new-name").value.trim();
      if (!name) return;
      const resp = await fetch("api/kanban/labels", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name}),
      }).then(x => x.json()).catch(() => ({}));
      if (resp.error){ alert(resp.error); return; }
      labels.push(resp);
      labelsModal();  // перерисовать список
      back.style.display = "";
    };
    modal.querySelector("#kb-lab-add").onclick = add;
    modal.querySelector("#kb-lab-new-name").onkeydown = e => {
      if (e.key === "Enter") add();
    };
  }

  // ------------------------------------------------------------- вход

  const nav = document.getElementById("nav-kanban");
  if (nav) nav.onclick = () => (isOpen() ? close() : open());

  // Ссылка «#N» из md.js (чат, описания карточек): открыть доску и сразу
  // карточку. Если такой (неархивной) карточки нет — сообщить об этом.
  async function openCard(id){
    await open();
    if (!cards.has(id)){ alert(`Задача #${id} не найдена (или в архиве)`); return; }
    openPanel(id);
  }

  return { open, close, isOpen, onRev, refresh, openCard };
})();
