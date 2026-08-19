// Чат: лента, треды, вложения-доки, NDJSON-стрим хода агента, реакции, упоминания.
const CHAT = (() => {
  const messagesEl = document.getElementById("messages");
  const inputEl = document.getElementById("input");
  const titleEl = document.getElementById("chat-title");
  const agentStateEl = document.getElementById("agent-state");
  const stopBtn = document.getElementById("btn-stop");
  const mentionPop = document.getElementById("mention-pop");
  const attachChips = document.getElementById("attach-chips");
  const threadPane = document.getElementById("thread-pane");
  const threadMessagesEl = document.getElementById("thread-messages");
  const threadInputEl = document.getElementById("thread-input");

  const EMOJI = ["👍", "🔥", "❤️", "😄", "🤔", "👀"];
  let current = null;          // текущий чат (объект из /api/chats)
  let lastMsgId = 0;
  let lastList = [];           // последняя загруженная лента (для ↑-правки)
  let streaming = false;       // мой стрим идёт — поллинг ленту не трогает
  let refetchTimer = null;
  let attachments = [];        // вложения к следующему сообщению ленты
  let threadAttachments = [];  // …и треда
  let threadRoot = null;       // открытый тред: корневое сообщение

  // ------------------------------------------------------------- открытие

  async function open(chat){
    if (current) saveDraft();
    current = chat;
    closeThread();
    titleEl.textContent = chatLabel(chat);
    messagesEl.innerHTML = "<div class='msg system'>загрузка…</div>";
    document.getElementById("btn-pins").style.display = "";
    document.getElementById("btn-files").style.display = "";
    document.getElementById("btn-del-chat").style.display =
      chat.kind === "agent" ? "" : "none";
    inputEl.value = localStorage.getItem("gd_draft_" + chat.id) || "";
    autosize();
    showTyping([]);
    await refetch(true);
  }

  // Черновики: набранное не теряется при переключении чатов и перезагрузке.
  function saveDraft(){
    if (!current) return;
    const key = "gd_draft_" + current.id;
    if (inputEl.value.trim()) localStorage.setItem(key, inputEl.value);
    else localStorage.removeItem(key);
  }

  function chatLabel(c){
    if (c.kind === "agent") return "🤖 " + c.title;
    if (c.kind === "dm") return "✉️ " + c.title;
    return (c.is_private ? "🔒 " : "#") + c.title;
  }

  function appendWithDaySep(container, msgs, opts){
    let lastDay = null;
    for (const m of msgs){
      const d = m.created_at ? new Date(m.created_at) : null;
      const day = d ? d.toDateString() : null;
      if (day && day !== lastDay){
        const sep = document.createElement("div");
        sep.className = "day-sep";
        sep.innerHTML = `<span>${d.toLocaleDateString("ru-RU",
          {day: "numeric", month: "long"})}</span>`;
        container.appendChild(sep);
        lastDay = day;
      }
      container.appendChild(renderMessage(m, opts));
    }
  }

  async function refetch(scroll = false){
    if (!current) return;
    const r = await fetch(`api/chats/${current.id}/messages`);
    if (!r.ok) { messagesEl.innerHTML = "<div class='msg system'>нет доступа</div>"; return; }
    const data = await r.json();
    const stick = scroll || nearBottom(messagesEl);
    messagesEl.innerHTML = "";
    appendWithDaySep(messagesEl, data.messages, {});
    updateAgentState(data.agent);
    lastList = data.messages;
    updatePinBtn(data.pin_count || 0);
    // Автоназвание сессии могло смениться на сервере. Только для агентских:
    // у ЛС title в БД пуст (имя собеседника подставляет list_chats).
    if (data.chat && data.chat.kind === "agent" && data.chat.title &&
        data.chat.title !== current.title){
      current.title = data.chat.title;
      titleEl.textContent = chatLabel(current);
      APP.refreshChats();
    }
    if (stick) messagesEl.scrollTop = messagesEl.scrollHeight;
    if (data.messages.length){
      lastMsgId = data.messages[data.messages.length - 1].id;
      markRead();
    }
  }

  function updatePinBtn(n){
    document.getElementById("pin-count").textContent = n ? " " + n : "";
  }

  function nearBottom(el){
    return el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  }

  function markRead(){
    if (!current || !lastMsgId) return;
    fetch(`api/chats/${current.id}/read`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({last_message_id: lastMsgId}),
    }).then(() => APP.refreshChats());
  }

  function updateAgentState(st){
    if (!st) { agentStateEl.textContent = ""; stopBtn.style.display = "none"; return; }
    if (st.busy){
      agentStateEl.textContent = "агент работает…" + (st.queued > 1 ? ` (в очереди ${st.queued - 1})` : "");
      stopBtn.style.display = "";
    } else {
      agentStateEl.textContent = "";
      stopBtn.style.display = "none";
    }
  }

  // ------------------------------------------------------------- рендер

  function avatarColor(name){
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.codePointAt(0)) % 360;
    return `hsl(${h}, 40%, 46%)`;
  }

  function initials(name){
    const parts = (name || "?").trim().split(/\s+/);
    return ((parts[0][0] || "?") + (parts[1] ? parts[1][0] : "")).toUpperCase();
  }

  function renderMessage(m, {inThread = false} = {}){
    const div = document.createElement("div");
    div.className = "msg " + m.author_kind;
    div.dataset.id = m.id;
    if (m.author_kind === "system"){
      div.textContent = m.text;
      return div;
    }
    const mentionedMe = window.ME &&
      new RegExp(`@${window.ME.login}\\b`, "i").test(m.text || "");
    if (mentionedMe) div.classList.add("mention-me");
    const when = m.created_at ? new Date(m.created_at)
      .toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"}) : "";
    const canThread = !inThread && m.thread_root_id == null;
    const avatar = m.author_kind === "agent"
      ? `<div class="avatar">🤖</div>`
      : `<div class="avatar" style="background:${avatarColor(m.author_name)}">${MD.esc(initials(m.author_name))}</div>`;
    const own = m.author_kind === "user" && window.ME &&
                m.author_user_id === window.ME.id;
    if (own) div.classList.add("mine");
    if (m.deleted_at){
      div.classList.add("deleted");
      div.innerHTML = `${avatar}<div class="msg-main">
        <div class="head"><span class="author">${MD.esc(m.author_name)}</span>
          <span>${when}</span></div>
        <div class="body deleted-body">сообщение удалено</div></div>`;
      return div;
    }
    // В своей сессии с агентом можно удалять и его ответы.
    const canDel = own || (current && current.kind === "agent" &&
                           m.author_kind === "agent");
    const edited = m.edited_at
      ? `<span class="edited" title="изменено ${fmtFull(m.edited_at)}">(изм. ${fmtWhen(m.edited_at)})</span>`
      : "";
    div.innerHTML = `${avatar}<div class="msg-main">
      <div class="head">
        <span class="author">${MD.esc(m.author_name)}</span>
        <span>${when}</span>${edited}
        <span class="msg-actions"><span class="act-group act-reacts" title="Реакция">${
          EMOJI.slice(0, 3).map(e =>
            `<button data-react="${e}">${e}</button>`).join("")}</span><span
          class="act-group act-tools">${
          canThread ? '<button class="b-thread" title="Ответить в треде">💬</button>' : ""}<button
          class="b-pin" title="${m.pinned_at ? "Открепить" : "Закрепить"}">📌</button>${
          own ? '<button class="b-edit" title="Редактировать">✎</button>' : ""}${
          canDel ? '<button class="b-del" title="Удалить">🗑</button>' : ""}</span></span>
      </div>
      <div class="body md"></div>
    </div>`;
    const main = div.querySelector(".msg-main");
    const body = div.querySelector(".body");
    body.innerHTML = MD.render(m.text || "");
    VAULT.bindWikilinks(body);
    if (m.status === "streaming") body.classList.add("streaming-dot");
    if (m.status === "error") body.style.color = "var(--red)";
    for (const a of m.attachments || []){
      if (a.upload_id && a.image){
        const img = document.createElement("img");
        img.className = "attach-img";
        img.src = `api/upload/${a.upload_id}`;
        img.title = a.title || "";
        img.dataset.lb = img.src;
        img.dataset.lbTitle = a.title || "";
        img.onclick = () => openLightbox(img.dataset.lb, a.title || "", img);
        main.appendChild(img);
        continue;
      }
      const card = document.createElement("div");
      card.className = "attach-card";
      card.textContent = (a.upload_id ? "📎 " : "📄 ") + (a.title || a.path || "файл");
      card.onclick = a.upload_id
        ? () => window.open(`api/upload/${a.upload_id}`, "_blank")
        : () => VAULT.openDoc(a.path);
      main.appendChild(card);
    }
    if (m.turn_id && m.author_kind === "agent") attachTurn(div, m.turn_id);
    renderReactions(div, m);
    if (canThread && m.reply_count > 0){
      const link = document.createElement("span");
      link.className = "reply-link";
      link.textContent = `💬 ${m.reply_count} ${plural(m.reply_count, "ответ", "ответа", "ответов")}`;
      link.onclick = () => openThread(m);
      main.appendChild(link);
    }
    div.querySelectorAll("[data-react]").forEach(b =>
      b.onclick = () => react(m.id, b.dataset.react));
    const bt = div.querySelector(".b-thread");
    if (bt) bt.onclick = () => openThread(m);
    const be = div.querySelector(".b-edit");
    if (be) be.onclick = () => startEdit(div, m);
    const bd = div.querySelector(".b-del");
    if (bd) bd.onclick = () => deleteMessage(m);
    const bp = div.querySelector(".b-pin");
    if (bp) bp.onclick = async () => {
      const r = await fetch(`api/messages/${m.id}/pin`, {method: "POST"});
      const data = await r.json().catch(() => ({}));
      if (data.error) alert(data.error);
      refetch();
    };
    if (m.pinned_at) div.classList.add("pinned");
    return div;
  }

  function fmtWhen(iso){
    const d = new Date(iso);
    const t = d.toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"});
    return d.toDateString() === new Date().toDateString()
      ? t
      : d.toLocaleDateString("ru-RU", {day: "2-digit", month: "2-digit"}) + " " + t;
  }

  function fmtFull(iso){
    const d = new Date(iso);
    return d.toLocaleDateString("ru-RU") + " " +
           d.toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"});
  }

  // ------------------------------------------------- правка/удаление своих

  function startEdit(div, m){
    const body = div.querySelector(".body");
    if (!body || div.querySelector(".edit-box")) return;
    body.style.display = "none";
    const box = document.createElement("div");
    box.className = "edit-box";
    box.innerHTML = `<textarea></textarea>
      <div class="edit-hint">Enter — сохранить, Esc — отмена</div>`;
    const ta = box.querySelector("textarea");
    ta.value = m.text || "";
    body.after(box);
    ta.focus();
    ta.selectionStart = ta.selectionEnd = ta.value.length;
    ta.style.height = Math.min(ta.scrollHeight + 4, 220) + "px";
    const cancel = () => { box.remove(); body.style.display = ""; };
    const save = async () => {
      const text = ta.value.trim();
      if (!text || text === m.text){ cancel(); return; }
      const r = await fetch(`api/messages/${m.id}`, {
        method: "PATCH", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text}),
      });
      const data = await r.json().catch(() => ({}));
      if (data.error){ alert(data.error); return; }
      cancel();
      refetch();
      if (threadRoot) refetchThread();
    };
    ta.addEventListener("keydown", e => {
      if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); save(); }
      else if (e.key === "Escape"){ cancel(); }
    });
  }

  async function deleteMessage(m){
    if (!confirm("Удалить сообщение?")) return;
    const r = await fetch(`api/messages/${m.id}`, {method: "DELETE"});
    const data = await r.json().catch(() => ({}));
    if (data.error) alert(data.error);
    refetch();
    if (threadRoot) refetchThread();
  }

  function msgMain(div){
    return div.querySelector(".msg-main") || div;
  }

  // ------------------------------------------------------------- лайтбокс

  // Галерея: листание всех картинок того же контейнера (лента/тред/«Файлы»),
  // зум колесом, ←/→, счётчик.
  let lbList = [], lbIndex = 0, lbScale = 1;

  function ensureLightbox(){
    let box = document.getElementById("lightbox");
    if (box) return box;
    box = document.createElement("div");
    box.id = "lightbox";
    box.innerHTML = `
      <div class="lb-bar">
        <span class="lb-title"></span>
        <span class="lb-count"></span>
        <a class="lb-dl" title="Скачать">⬇ скачать</a>
        <button class="lb-close" title="Закрыть (Esc)">✕</button>
      </div>
      <button class="lb-nav lb-prev" title="Предыдущая (←)">‹</button>
      <div class="lb-stage"><img class="lb-img"></div>
      <button class="lb-nav lb-next" title="Следующая (→)">›</button>`;
    document.body.appendChild(box);
    box.onclick = e => {
      if (e.target === box || e.target.classList.contains("lb-stage"))
        closeLightbox();
    };
    box.querySelector(".lb-close").onclick = closeLightbox;
    box.querySelector(".lb-prev").onclick = () => lbShow(lbIndex - 1);
    box.querySelector(".lb-next").onclick = () => lbShow(lbIndex + 1);
    box.addEventListener("wheel", e => {
      e.preventDefault();
      lbScale = Math.min(6, Math.max(0.3,
        lbScale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
      box.querySelector(".lb-img").style.transform = `scale(${lbScale})`;
    }, {passive: false});
    document.addEventListener("keydown", e => {
      if (box.style.display === "none") return;
      if (e.key === "Escape") closeLightbox();
      else if (e.key === "ArrowLeft") lbShow(lbIndex - 1);
      else if (e.key === "ArrowRight") lbShow(lbIndex + 1);
    });
    return box;
  }

  function lbShow(i){
    const box = ensureLightbox();
    if (!lbList.length) return;
    lbIndex = (i + lbList.length) % lbList.length;
    const it = lbList[lbIndex];
    lbScale = 1;
    const img = box.querySelector(".lb-img");
    img.style.transform = "";
    img.src = it.url;
    box.querySelector(".lb-title").textContent = it.title;
    box.querySelector(".lb-count").textContent =
      lbList.length > 1 ? `${lbIndex + 1} / ${lbList.length}` : "";
    box.querySelector(".lb-dl").href =
      it.url + (it.url.includes("?") ? "&" : "?") + "dl=1";
    box.querySelectorAll(".lb-nav").forEach(b =>
      b.style.display = lbList.length > 1 ? "" : "none");
    box.style.display = "flex";
  }

  function openLightbox(url, title, fromEl = null){
    const scope = fromEl &&
      fromEl.closest("#messages, #thread-messages, .files-grid");
    const imgs = scope ? [...scope.querySelectorAll("img[data-lb]")] : [];
    lbList = imgs.length
      ? imgs.map(el => ({url: el.dataset.lb, title: el.dataset.lbTitle || ""}))
      : [{url, title}];
    lbShow(Math.max(0, lbList.findIndex(it => it.url === url)));
  }

  function closeLightbox(){
    const box = document.getElementById("lightbox");
    if (box){ box.style.display = "none"; box.querySelector(".lb-img").src = ""; }
  }

  function plural(n, one, few, many){
    const m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  function renderReactions(div, m){
    let box = div.querySelector(".reactions");
    if (box) box.remove();
    if (!m.reactions || !m.reactions.length) return;
    box = document.createElement("div");
    box.className = "reactions";
    for (const r of m.reactions){
      const mine = (r.user_ids || "").split(",").includes(String(window.ME?.id));
      const el = document.createElement("span");
      el.className = "reaction" + (mine ? " mine" : "");
      el.textContent = `${r.emoji} ${r.n}`;
      el.onclick = () => react(m.id, r.emoji);
      box.appendChild(el);
    }
    msgMain(div).appendChild(box);
  }

  async function react(msgId, emoji){
    await fetch(`api/messages/${msgId}/react`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({emoji}),
    });
    refetch();
    if (threadRoot) refetchThread();
  }

  async function attachTurn(div, turnId){
    const r = await fetch(`api/turn/${turnId}`);
    if (!r.ok) return;
    const turn = await r.json();
    renderTurnPlaque(div, turn);
  }

  function renderTurnPlaque(div, turn){
    let plaque = div.querySelector(".turn-plaque");
    if (!turn.files || !turn.files.length){
      if (turn.status === "error" && turn.error && !plaque){
        plaque = document.createElement("div");
        plaque.className = "turn-plaque";
        plaque.innerHTML = `<span style="color:var(--red)">⚠️ ${MD.esc(turn.error)}</span>`;
        msgMain(div).appendChild(plaque);
      }
      return;
    }
    if (!plaque){
      plaque = document.createElement("div");
      plaque.className = "turn-plaque";
      msgMain(div).appendChild(plaque);
    }
    plaque.innerHTML = `
      <div>✏️ Изменено файлов: ${turn.files.length}</div>
      <div class="files">${turn.files.map(MD.esc).join("<br>")}</div>
      <button class="b-diff">показать diff</button>
      ${turn.commit_hash ? '<button class="b-revert">откатить</button>' : ""}
      <div class="diff-box"></div>`;
    plaque.querySelector(".b-diff").onclick = async () => {
      const box = plaque.querySelector(".diff-box");
      if (box.innerHTML){ box.innerHTML = ""; return; }
      const r = await fetch(`api/turn/${turn.id}/diff`);
      const data = await r.json();
      box.innerHTML = `<div class="diff">${DIFF.render(data.diff || data.note || "")}</div>`;
    };
    const rv = plaque.querySelector(".b-revert");
    if (rv) rv.onclick = async () => {
      if (!confirm("Откатить правки этого хода агента?")) return;
      const r = await fetch(`api/turn/${turn.id}/revert`, {method: "POST"});
      const data = await r.json();
      if (data.error) alert(data.error);
      refetch();
      VAULT.refreshTree();
    };
  }

  // ------------------------------------------------------------- отправка

  // Общий путь для ленты и треда: шлёт сообщение, читает NDJSON-стрим,
  // рисует ход агента в container.
  async function sendMessage(text, {threadRootId = null, container, atts = []} = {}){
    if (!text || !current) return;
    streaming = true;
    let agentDiv = null, agentMain = null, agentBody = null, agentText = "";
    try {
      const resp = await fetch(`api/chats/${current.id}/send`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text, thread_root_id: threadRootId, attachments: atts}),
      });
      if (!resp.ok){
        const err = await resp.json().catch(() => ({}));
        alert(err.error || `ошибка ${resp.status}`);
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true){
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0){
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          const ev = JSON.parse(line);
          if (ev.type === "message"){
            container.appendChild(renderMessage(ev.message, {inThread: !!threadRootId}));
            if (!threadRootId) lastMsgId = ev.message.id;
            container.scrollTop = container.scrollHeight;
          } else if (ev.type === "start"){
            agentDiv = document.createElement("div");
            agentDiv.className = "msg agent";
            agentDiv.innerHTML = `<div class="avatar">🤖</div><div class="msg-main">
              <div class="head"><span class="author">Агент</span></div>
              <div class="body md streaming-dot"></div></div>`;
            agentDiv.dataset.turn = ev.turn;
            agentMain = agentDiv.querySelector(".msg-main");
            agentBody = agentDiv.querySelector(".body");
            container.appendChild(agentDiv);
            updateAgentState({busy: true, queued: 1});
          } else if (ev.type === "queued"){
            agentStateEl.textContent = `агент занят — сообщение в очереди (${ev.position})`;
          } else if (ev.type === "text" && agentBody){
            agentText += (agentText ? "\n\n" : "") + ev.text;
            agentBody.innerHTML = MD.render(agentText);
            if (nearBottom(container)) container.scrollTop = container.scrollHeight;
          } else if (ev.type === "tool" && agentMain){
            const t = document.createElement("div");
            t.className = "tool-line";
            t.textContent = `🔧 ${ev.name} ${shortToolInput(ev.input)}`;
            agentMain.insertBefore(t, agentBody);
          } else if (ev.type === "turn" && agentDiv){
            renderTurnPlaque(agentDiv, {id: ev.turn, files: ev.files || [],
                                        commit_hash: ev.commit, status: "done"});
            VAULT.refreshTree();
          } else if (ev.type === "error"){
            const t = document.createElement("div");
            t.className = "msg system";
            t.style.color = "var(--red)";
            t.textContent = "⚠️ " + ev.text;
            container.appendChild(t);
          }
        }
      }
    } finally {
      streaming = false;
      if (agentBody) agentBody.classList.remove("streaming-dot");
      updateAgentState({busy: false, queued: 0});
      if (threadRootId) refetchThread();
      refetch();
    }
  }

  async function send(){
    const text = inputEl.value.trim();
    if (!text) return;
    inputEl.value = "";
    if (current) localStorage.removeItem("gd_draft_" + current.id);
    autosize();
    hideMentions();
    const atts = attachments.slice();
    attachments.length = 0;  // очистка на месте: paste/drop держат ссылку
    renderAttachChips();
    await sendMessage(text, {container: messagesEl, atts});
  }

  function shortToolInput(input){
    try {
      const o = JSON.parse(input);
      return o.file_path || o.path || o.pattern || "";
    } catch { return ""; }
  }

  // авторост поля ввода до 8 строк
  function autosize(){
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 180) + "px";
  }
  inputEl.addEventListener("input", autosize);

  // ------------------------------------------------------------- вложения

  const threadAttachChips = document.getElementById("thread-attach-chips");

  function renderChips(el, list){
    el.innerHTML = "";
    for (let i = 0; i < list.length; i++){
      const chip = document.createElement("span");
      chip.className = "attach-chip";
      chip.innerHTML = `📄 ${MD.esc(list[i].title)} <b title="убрать">✕</b>`;
      chip.querySelector("b").onclick = () => {
        list.splice(i, 1);
        renderChips(el, list);
      };
      el.appendChild(chip);
    }
  }

  function renderAttachChips(){ renderChips(attachChips, attachments); }

  // Общая загрузка файлов (кнопка, drag&drop, Ctrl+V) в один из композеров.
  async function uploadFilesTo(files, list, chipsEl){
    if (!current){ alert("сначала выберите чат"); return; }
    for (const f of files){
      const fd = new FormData();
      fd.append("file", f);
      const r = await fetch(`api/chats/${current.id}/upload`,
                            {method: "POST", body: fd});
      const data = await r.json();
      if (data.error){ alert(`${f.name}: ${data.error}`); continue; }
      list.push({upload_id: data.upload_id, title: data.title,
                 image: data.image});
      renderChips(chipsEl, list);
    }
  }

  async function pickVaultDoc(list, chipsEl){
    const r = await fetch("api/vault/tree");
    const tree = await r.json();
    const files = [];
    (function walk(nodes){
      for (const n of nodes){
        if (n.dir) walk(n.children || []);
        else if (/\.md$/i.test(n.name)) files.push(n);
      }
    })(tree);
    APP.pickFromList("Прикрепить документ", files.map(f => ({label: f.path, value: f})),
      f => {
        list.push({path: f.path, title: f.name.replace(/\.md$/i, "")});
        renderChips(chipsEl, list);
      });
  }
  document.getElementById("btn-attach-doc").onclick =
    () => pickVaultDoc(attachments, attachChips);
  document.getElementById("btn-thread-attach-doc").onclick =
    () => pickVaultDoc(threadAttachments, threadAttachChips);

  const fileInput = document.getElementById("file-input");
  document.getElementById("btn-attach").onclick = () => {
    if (!current){ alert("сначала выберите чат"); return; }
    fileInput.click();
  };
  fileInput.onchange = async () => {
    await uploadFilesTo(fileInput.files, attachments, attachChips);
    fileInput.value = "";
  };

  const threadFileInput = document.getElementById("thread-file-input");
  document.getElementById("btn-thread-attach").onclick = () => threadFileInput.click();
  threadFileInput.onchange = async () => {
    await uploadFilesTo(threadFileInput.files, threadAttachments, threadAttachChips);
    threadFileInput.value = "";
  };

  // Ctrl+V со скриншотом в буфере и перетаскивание файла в окно чата/треда.
  function pasteHandler(list, chipsEl){
    return e => {
      const files = [...(e.clipboardData?.files || [])];
      if (!files.length) return;
      e.preventDefault();
      uploadFilesTo(files, list, chipsEl);
    };
  }
  inputEl.addEventListener("paste", pasteHandler(attachments, attachChips));
  threadInputEl.addEventListener("paste",
    pasteHandler(threadAttachments, threadAttachChips));

  function dropzone(el, getTarget){
    el.addEventListener("dragover", e => {
      if (![...e.dataTransfer.types].includes("Files")) return;
      e.preventDefault();
      el.classList.add("drag-over");
    });
    el.addEventListener("dragleave", () => el.classList.remove("drag-over"));
    el.addEventListener("drop", e => {
      el.classList.remove("drag-over");
      if (!e.dataTransfer.files.length) return;
      e.preventDefault();
      const [list, chips] = getTarget();
      uploadFilesTo(e.dataTransfer.files, list, chips);
    });
  }
  dropzone(document.getElementById("chat-pane"),
           () => [attachments, attachChips]);
  dropzone(threadPane, () => [threadAttachments, threadAttachChips]);

  // ------------------------------------------------------------- тред

  async function openThread(rootMsg){
    threadRoot = rootMsg;
    threadPane.style.display = "flex";
    await refetchThread(true);
  }

  function closeThread(){
    threadRoot = null;
    threadPane.style.display = "none";
    threadInputEl.value = "";
  }

  async function refetchThread(scroll = false){
    if (!threadRoot) return;
    const r = await fetch(`api/chats/${current.id}/messages?thread=${threadRoot.id}`);
    if (!r.ok) return;
    const data = await r.json();
    const stick = scroll || nearBottom(threadMessagesEl);
    threadMessagesEl.innerHTML = "";
    // Корень сверху (берём свежую версию из ленты, если есть).
    const rootDiv = renderMessage(threadRoot, {inThread: true});
    rootDiv.classList.add("thread-root");
    rootDiv.querySelector(".reply-link")?.remove();
    threadMessagesEl.appendChild(rootDiv);
    appendWithDaySep(threadMessagesEl, data.messages, {inThread: true});
    if (stick) threadMessagesEl.scrollTop = threadMessagesEl.scrollHeight;
  }

  async function threadSend(){
    const text = threadInputEl.value.trim();
    if (!text || !threadRoot) return;
    threadInputEl.value = "";
    const atts = threadAttachments.slice();
    threadAttachments.length = 0;  // см. attachments: ссылку держат замыкания
    renderChips(threadAttachChips, threadAttachments);
    await sendMessage(text, {threadRootId: threadRoot.id,
                             container: threadMessagesEl, atts});
  }

  document.getElementById("btn-thread-close").onclick = closeThread;
  document.getElementById("btn-thread-send").onclick = threadSend;
  threadInputEl.addEventListener("keydown", e => {
    if (threadMentions.active()) return;  // Enter достаётся списку упоминаний
    if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); threadSend(); }
  });

  // ------------------------------------------------------------- упоминания

  // Общий компонент: вешается на любое поле ввода со своим попапом.
  // Регистрируется ДО обработчика отправки: пока список открыт, стрелки и
  // Enter достаются ему (stopImmediatePropagation).
  function mentionize(input, pop){
    let items = [], sel = 0;

    function candidates(prefix){
      const users = (window.USERS || []).map(u => ({login: u.login, name: u.display_name}));
      users.unshift({login: "агент", name: "Агент"});
      prefix = prefix.toLowerCase();
      return users.filter(u => u.login.toLowerCase().startsWith(prefix) ||
                               u.name.toLowerCase().startsWith(prefix)).slice(0, 6);
    }

    function check(){
      const pos = input.selectionStart;
      const m = input.value.slice(0, pos).match(/(^|\s)@([\wа-яё.-]*)$/i);
      if (!m){ hide(); return; }
      const next = candidates(m[2]);
      if (!next.length){ hide(); return; }
      // Выбор сбрасываем только когда сменился набор кандидатов —
      // иначе стрелки не могут сдвинуть выделение.
      const same = next.length === items.length &&
        next.every((u, i) => u.login === items[i].login);
      if (!same) sel = 0;
      items = next;
      render();
    }

    function render(){
      pop.style.display = "";
      pop.innerHTML = items.map((u, i) =>
        `<div class="${i === sel ? "sel" : ""}" data-i="${i}">@${u.login} — ${MD.esc(u.name)}</div>`).join("");
      pop.querySelectorAll("div").forEach(d =>
        d.onclick = () => apply(+d.dataset.i));
    }

    function apply(i){
      const pos = input.selectionStart;
      const before = input.value.slice(0, pos).replace(/@[\wа-яё.-]*$/i, "@" + items[i].login + " ");
      input.value = before + input.value.slice(pos);
      input.focus();
      input.selectionStart = input.selectionEnd = before.length;
      hide();
      input.dispatchEvent(new Event("input"));  // авторост поля
    }

    function hide(){ pop.style.display = "none"; items = []; }

    input.addEventListener("input", check);
    input.addEventListener("keydown", e => {
      if (!items.length) return;
      if (e.key === "ArrowDown"){ sel = (sel + 1) % items.length; }
      else if (e.key === "ArrowUp"){ sel = (sel - 1 + items.length) % items.length; }
      else if (e.key === "Tab" || e.key === "Enter"){ apply(sel); e.preventDefault(); e.stopImmediatePropagation(); return; }
      else if (e.key === "Escape"){ hide(); e.stopImmediatePropagation(); return; }
      else return;
      e.preventDefault();
      e.stopImmediatePropagation();
      render();
    });
    input.addEventListener("blur", () => setTimeout(hide, 150));
    return { hide, active: () => items.length > 0 };
  }

  const mainMentions = mentionize(inputEl, mentionPop);
  const threadMentions = mentionize(threadInputEl,
    document.getElementById("thread-mention-pop"));
  function hideMentions(){ mainMentions.hide(); }

  // ------------------------------------------------------------- события

  inputEl.addEventListener("keydown", e => {
    if (mainMentions.active()) return;  // Enter достаётся списку упоминаний
    if (e.key === "Enter" && !e.shiftKey){
      e.preventDefault();
      send();
    } else if (e.key === "ArrowUp" && !inputEl.value){
      // ↑ в пустом композере — правка последнего своего сообщения.
      for (let i = lastList.length - 1; i >= 0; i--){
        const m = lastList[i];
        if (m.author_kind === "user" && window.ME &&
            m.author_user_id === window.ME.id && !m.deleted_at){
          const div = messagesEl.querySelector(`.msg[data-id="${m.id}"]`);
          if (div){
            e.preventDefault();
            div.scrollIntoView({block: "nearest"});
            startEdit(div, m);
          }
          break;
        }
      }
    }
  });

  // ---------------------------------------------------------- «печатает…»

  const typingLine = document.getElementById("typing-line");
  let typingSentAt = 0;

  function typingPing(){
    if (!current || current.kind === "agent") return;
    const now = Date.now();
    if (now - typingSentAt < 2500) return;
    typingSentAt = now;
    fetch(`api/chats/${current.id}/typing`, {method: "POST"});
  }
  inputEl.addEventListener("input", typingPing);
  threadInputEl.addEventListener("input", typingPing);
  inputEl.addEventListener("input", saveDraft);

  function showTyping(names){
    if (!names || !names.length){ typingLine.textContent = ""; return; }
    typingLine.textContent = names.join(", ") +
      (names.length > 1 ? " печатают…" : " печатает…");
  }
  document.getElementById("btn-send").onclick = send;
  stopBtn.onclick = () => current && fetch(`api/chats/${current.id}/stop`, {method: "POST"});

  document.getElementById("btn-del-chat").onclick = async () => {
    if (!current || current.kind !== "agent") return;
    if (!confirm(`Удалить сессию «${current.title}» со всей историей?`)) return;
    const r = await fetch(`api/chats/${current.id}`, {method: "DELETE"});
    const data = await r.json().catch(() => ({}));
    if (data.error){ alert(data.error); return; }
    localStorage.removeItem("gd_draft_" + current.id);
    current = null;
    APP.afterChatDeleted();
  };

  // ------------------------------------------------- пины и файлы чата

  function openChatModal(html){
    const back = document.getElementById("modal-back");
    document.getElementById("modal").innerHTML = html;
    back.style.display = "";
  }
  function closeChatModal(){
    document.getElementById("modal-back").style.display = "none";
  }

  document.getElementById("btn-pins").onclick = async () => {
    if (!current) return;
    const r = await fetch(`api/chats/${current.id}/pins`);
    const data = await r.json();
    const pins = data.pins || [];
    openChatModal(`<h3>📌 Закреплённые</h3>
      <div class="pin-list">${pins.length ? "" : "<i>пока пусто — 📌 на любом сообщении</i>"}</div>
      <div class="row"><button id="m-close">Закрыть</button></div>`);
    const listEl = document.querySelector("#modal .pin-list");
    for (const m of pins){
      const item = document.createElement("div");
      item.className = "pin-item";
      item.innerHTML = `<div class="pin-head"><b>${MD.esc(m.author_name)}</b>
        <span>${fmtFull(m.created_at)}</span>
        <button class="pin-off" title="Открепить">✕</button></div>
        <div class="md"></div>`;
      item.querySelector(".md").innerHTML = MD.render(m.text || "");
      item.querySelector(".pin-off").onclick = async e => {
        e.stopPropagation();
        await fetch(`api/messages/${m.id}/pin`, {method: "POST"});
        closeChatModal();
        refetch();
      };
      item.onclick = () => {
        closeChatModal();
        const div = messagesEl.querySelector(`.msg[data-id="${m.id}"]`);
        if (div){
          div.scrollIntoView({block: "center"});
          div.classList.add("flash");
          setTimeout(() => div.classList.remove("flash"), 1600);
        }
      };
      listEl.appendChild(item);
    }
    document.getElementById("m-close").onclick = closeChatModal;
  };

  document.getElementById("btn-files").onclick = async () => {
    if (!current) return;
    const r = await fetch(`api/chats/${current.id}/files`);
    const data = await r.json();
    const files = data.files || [];
    const imgs = files.filter(f => f.upload_id && f.image);
    const rest = files.filter(f => !(f.upload_id && f.image));
    openChatModal(`<h3>📁 Файлы чата</h3>
      ${files.length ? "" : "<i>вложений пока нет</i>"}
      <div class="files-grid"></div><div class="files-list"></div>
      <div class="row"><button id="m-close">Закрыть</button></div>`);
    const grid = document.querySelector("#modal .files-grid");
    for (const f of imgs){
      const img = document.createElement("img");
      img.src = `api/upload/${f.upload_id}`;
      img.title = `${f.title || ""} — ${f.author}`;
      img.dataset.lb = img.src;
      img.dataset.lbTitle = f.title || "";
      img.onclick = () => openLightbox(img.dataset.lb, f.title || "", img);
      grid.appendChild(img);
    }
    const listEl = document.querySelector("#modal .files-list");
    for (const f of rest){
      const item = document.createElement("div");
      item.className = "file-card";
      item.innerHTML = `<span>${f.upload_id ? "📎" : "📄"}</span>
        <span class="fc-title">${MD.esc(f.title || f.path || "файл")}</span>
        <small>${MD.esc(f.author)} · ${fmtWhen(f.created_at)}</small>`;
      item.onclick = f.upload_id
        ? () => window.open(`api/upload/${f.upload_id}?dl=1`, "_blank")
        : () => { closeChatModal(); VAULT.openDoc(f.path); };
      listEl.appendChild(item);
    }
    document.getElementById("m-close").onclick = closeChatModal;
  };

  function onChanges(events){
    if (!current || streaming) return;
    if (events.some(e => e.chat_id === current.id)){
      clearTimeout(refetchTimer);
      refetchTimer = setTimeout(() => {
        refetch();
        if (threadRoot) refetchThread();
      }, 200);
    }
  }

  return { open, onChanges, showTyping,
           currentId: () => current && current.id };
})();
