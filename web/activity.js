// Лента «Активность» (#82): все @упоминания меня и ответы на мои сообщения
// по всем видимым чатам — как в Slack. Панель-оверлей рядом с сайдбаром,
// бейдж непрочитанных приходит поллингом (app.js -> onCount), клик по событию
// ведёт к сообщению: в ленту чата (CHAT.jumpToMsg) или в тред
// (CHAT.openThreadAt). Прочитанность выводится из chat_reads — события
// гаснут по мере чтения самих чатов, отдельного «отметить прочитанным» нет.
const ACT = (() => {
  let panel = null;
  let lastCount = null;

  const nav = document.getElementById("nav-activity");
  const badge = document.getElementById("act-badge");

  function isOpen(){
    return !!panel && panel.style.display !== "none";
  }

  function onCount(n){
    badge.style.display = n ? "" : "none";
    badge.textContent = n > 99 ? "99+" : n;
    // Счётчик сдвинулся (новое событие или чат дочитан) — открытая панель
    // устарела.
    if (n !== lastCount && isOpen()) refresh();
    lastCount = n;
  }

  function ensurePanel(){
    if (panel) return panel;
    panel = document.createElement("div");
    panel.id = "act-panel";
    panel.style.display = "none";
    panel.innerHTML = `
      <div id="act-head">
        <span>${ICON("bell")} Активность</span>
        <button id="act-close" title="Закрыть">${ICON("x")}</button>
      </div>
      <div id="act-list"></div>`;
    document.body.appendChild(panel);
    panel.querySelector("#act-close").onclick = close;
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && isOpen()) close();
    });
    return panel;
  }

  async function open(){
    ensurePanel().style.display = "flex";
    nav.classList.add("active");
    await refresh();
  }

  function close(){
    if (!isOpen()) return;
    panel.style.display = "none";
    nav.classList.remove("active");
  }

  async function refresh(){
    const r = await fetch("api/activity");
    if (!r.ok) return;
    const data = await r.json();
    if (!isOpen()) return;
    const list = panel.querySelector("#act-list");
    list.innerHTML = data.events.length ? ""
      : `<div class="act-empty">Пока пусто: здесь появятся @упоминания вас
         и ответы на ваши сообщения</div>`;
    for (const ev of data.events) list.appendChild(renderItem(ev));
  }

  // Сниппет: markdown-мусор в одну строку не тащим — картинки и код
  // сворачиваем в подписи, остальное чистим грубо.
  function snippet(text){
    let s = (text || "")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "[картинка]")
      .replace(/```[\s\S]*?```/g, "[код]")
      .replace(/\s+/g, " ").trim();
    return s.length > 160 ? s.slice(0, 160) + "…" : s;
  }

  function fmtWhen(iso){
    if (!iso) return "";
    const d = new Date(iso);
    const today = new Date().toDateString() === d.toDateString();
    const hm = d.toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"});
    return today ? hm
      : d.toLocaleDateString("ru-RU", {day: "numeric", month: "short"}) + " " + hm;
  }

  function renderItem(ev){
    const el = document.createElement("div");
    el.className = "act-item" + (ev.read ? "" : " unread");
    const kindIcon = ev.kind === "mention"
      ? `<span class="act-kind mention">@</span>`
      : `<span class="act-kind">${ICON("reply", "sm")}</span>`;
    const chatIcon = ev.chat_kind === "agent" ? ICON("spark", "sm")
      : ev.chat_kind === "dm" ? ICON("mail", "sm") : ICON("hash", "sm");
    const kindRu = ev.kind === "mention" ? "вас упомянули" : "ответ вам";
    el.innerHTML = `
      ${kindIcon}
      <div class="act-body">
        <div class="act-meta">${chatIcon} ${MD.esc(ev.chat_title || "")}
          ${ev.thread_root_id ? ICON("thread", "xs") : ""}
          · ${kindRu} · ${fmtWhen(ev.created_at)}</div>
        <div class="act-text"><b>${MD.esc(ev.author_name || "")}:</b>
          ${MD.esc(snippet(ev.text))}</div>
      </div>`;
    el.onclick = async () => {
      close();
      await APP.selectChat(ev.chat_id);
      if (ev.thread_root_id) await CHAT.openThreadAt(ev.thread_root_id, ev.id);
      else await CHAT.jumpToMsg(ev.id);
    };
    return el;
  }

  nav.onclick = () => (isOpen() ? close() : open());

  return { onCount, close, isOpen };
})();
