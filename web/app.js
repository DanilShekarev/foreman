// Каркас: профиль, список чатов с бейджами, поллинг changefeed, модалки.
const APP = (() => {
  const listAgent = document.getElementById("list-agent");
  const listChannels = document.getElementById("list-channels");
  const listDms = document.getElementById("list-dms");
  const modalBack = document.getElementById("modal-back");
  const modal = document.getElementById("modal");
  const appEl = document.getElementById("app");
  const vaultPane = document.getElementById("vault-pane");

  let chats = [];
  let since = -1;
  let vaultVersion = null;  // версия волта с сервера: сменилась — обновить дерево

  async function boot(){
    const meResp = await fetch("api/me");
    if (!meResp.ok){ location.href = "login"; return; }
    window.ME = await meResp.json();
    document.getElementById("me-name").textContent = window.ME.display_name;
    if (window.ME.is_admin)
      document.getElementById("btn-admin").style.display = "";
    window.USERS = await (await fetch("api/users")).json();
    await refreshChats();
    await VAULT.refreshTree();
    // Навигация переживает перезагрузку: последний чат, док, видимость волта
    // и открытая доска задач. Флаг доски читаем ДО selectChat: тот закрывает
    // оверлей и стирает флаг.
    const kbWasOpen = localStorage.getItem("gd_kb_open") === "1";
    const savedId = +localStorage.getItem("gd_last_chat");
    const startChat = chats.find(c => c.id === savedId) ||
                      chats.find(c => c.kind === "agent");
    if (startChat) selectChat(startChat.id);
    if (kbWasOpen) KANBAN.open();
    if (localStorage.getItem("gd_vault_hidden") === "1"){
      appEl.classList.add("vault-hidden");
      vaultPane.classList.add("hidden");
    } else {
      const savedDoc = localStorage.getItem("gd_last_doc");
      if (savedDoc) VAULT.openDoc(savedDoc, true);
    }
    const init = await (await fetch("api/changes")).json();
    since = init.now;
    vaultVersion = init.vault ?? null;
    if (init.activity !== undefined) ACT.onCount(init.activity);
    pollLoop();
    updateNotifyBtn();
    // Список людей меняется редко — обновляем раз в минуту (новые регистрации
    // по приглашениям подтянутся сами).
    setInterval(async () => {
      const r = await fetch("api/users");
      if (r.ok){ window.USERS = await r.json(); renderUsers(); }
    }, 60000);
    initVaultResizer();
  }

  // Поллинг: 3 с в активной вкладке, 15 с в фоновой — через интернет фоновые
  // вкладки не должны молотить сервер. Возврат во вкладку опрашивает сразу.
  let pollTimer = null;
  function pollLoop(){
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => { await poll(); pollLoop(); },
                           document.hidden ? 15000 : 3000);
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
    pollLoop();
  });

  // Оверлей «сервер недоступен»: два провала поллинга подряд (~6 с) — показать,
  // первый успешный ответ — скрыть и догнать пропущенное.
  let pollFails = 0;

  function offlineOverlay(show){
    let el = document.getElementById("offline-overlay");
    if (!el){
      el = document.createElement("div");
      el.id = "offline-overlay";
      el.innerHTML = `<div class="off-box"><div class="off-spin"></div>
        <h3>Сервер недоступен</h3>
        <p>Пробуем переподключиться — окно закроется само.</p></div>`;
      document.body.appendChild(el);
    }
    el.style.display = show ? "flex" : "none";
  }

  async function poll(){
    try {
      const r = await fetch(`api/changes?since=${since}&chat=${CHAT.currentId() || 0}`);
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      if (pollFails >= 2){ offlineOverlay(false); refreshChats(); }
      pollFails = 0;
      since = data.now;
      window.ONLINE = new Set(data.online_ids || []);
      renderUsers();
      CHAT.showTyping(data.typing || []);
      CHAT.updateReads(data.reads || []);
      // Кто-то (агент, коллега, загрузка) закоммитил в волт — дерево устарело.
      if (data.vault !== undefined && data.vault !== vaultVersion){
        if (vaultVersion !== null) VAULT.refreshTree();
        vaultVersion = data.vault;
      }
      if (data.kanban !== undefined) KANBAN.onRev(data.kanban);
      if (data.activity !== undefined) ACT.onCount(data.activity);
      if (data.calls !== undefined && typeof CALL !== "undefined")
        CALL.onCalls(data.calls);
      if (data.events.length){
        refreshChats();
        CHAT.onChanges(data.events);
      }
    } catch {
      pollFails++;
      if (pollFails === 2) offlineOverlay(true);
    }
  }

  async function refreshChats(){
    const r = await fetch("api/chats");
    if (!r.ok) return;
    chats = await r.json();
    renderChatLists();
    // Бейдж непрочитанных в заголовке вкладки — работает и без HTTPS.
    const unread = chats.reduce((s, c) => s + (c.unread || 0), 0);
    document.title = (unread ? `(${unread}) ` : "") + "Foreman";
    maybeNotify();
  }

  // --------------------------------------------------------- нотификации
  // Работают только по HTTPS (danishe.sbs/gd); на http-LAN кнопка скрыта,
  // остаётся бейдж в заголовке вкладки.

  const canNotify = "Notification" in window && window.isSecureContext;
  let prevUnread = null;

  function notifyEnabled(){
    return canNotify && Notification.permission === "granted" &&
           localStorage.getItem("gd_notify") !== "off";
  }

  function updateNotifyBtn(){
    const btn = document.getElementById("btn-notify");
    if (!canNotify){ btn.style.display = "none"; return; }
    btn.style.display = "";
    btn.innerHTML = notifyEnabled() ? ICON("bell") : ICON("bell-off");
    btn.title = Notification.permission === "denied"
      ? "Уведомления запрещены в настройках браузера"
      : (notifyEnabled() ? "Уведомления включены" : "Включить уведомления");
  }

  document.getElementById("btn-notify").onclick = async () => {
    if (!canNotify) return;
    if (Notification.permission === "default"){
      await Notification.requestPermission();
      localStorage.setItem("gd_notify", "on");
    } else {
      const off = localStorage.getItem("gd_notify") === "off";
      localStorage.setItem("gd_notify", off ? "on" : "off");
    }
    updateNotifyBtn();
  };

  function maybeNotify(){
    if (prevUnread){
      let sound = null;  // за один поллинг — один звук: упоминание важнее
      for (const c of chats){
        const grew = (c.unread || 0) > (prevUnread[c.id] || 0);
        if (!grew) continue;
        // Открытый чат озвучивает CHAT.refetch по самим сообщениям (счётчик
        // непрочитанных для него гоняется с markRead) — здесь только другие.
        if (CHAT.currentId() === c.id) continue;
        sound = c.mentioned ? "mention" : (sound || "msg");
        if (!notifyEnabled()) continue;
        const icon = c.kind === "agent" ? "🤖" : c.kind === "dm" ? "✉️" : "#";
        const n = new Notification(
          c.mentioned ? "Вас упомянули" : "Новое сообщение",
          {body: `${icon} ${c.title}`, tag: "gd-chat-" + c.id});
        n.onclick = () => { window.focus(); selectChat(c.id); n.close(); };
      }
      if (sound === "mention") SOUND.mention();
      else if (sound === "msg") SOUND.msg();
    }
    prevUnread = Object.fromEntries(chats.map(c => [c.id, c.unread || 0]));
  }

  // --------------------------------------------------------------- звук

  const soundBtn = document.getElementById("btn-sound");

  function updateSoundBtn(){
    soundBtn.innerHTML = SOUND.isOn() ? ICON("volume") : ICON("volume-off");
    soundBtn.title = SOUND.isOn()
      ? `Звук включён (${Math.round(SOUND.volume() * 100)}%) — клик выключает,` +
        " колесо мыши меняет громкость"
      : "Звук выключен — клик включает";
  }

  soundBtn.onclick = () => { SOUND.toggle(); updateSoundBtn(); };
  soundBtn.addEventListener("wheel", e => {
    if (!SOUND.isOn()) return;
    e.preventDefault();
    SOUND.bumpVol(e.deltaY < 0 ? 0.05 : -0.05);
    updateSoundBtn();
  }, {passive: false});
  updateSoundBtn();

  function renderChatLists(){
    renderList(listAgent, chats.filter(c => c.kind === "agent"));
    renderList(listChannels, chats.filter(c => c.kind === "channel"));
    renderList(listDms, chats.filter(c => c.kind === "dm"));
    renderUsers();
  }

  function renderUsers(){
    const el = document.getElementById("list-users");
    el.innerHTML = "";
    for (const u of (window.USERS || [])){
      if (u.id === window.ME.id) continue;
      const div = document.createElement("div");
      div.className = "chat-item";
      div.title = "@" + u.login + " — написать в личку";
      const on = window.ONLINE && window.ONLINE.has(u.id);
      div.innerHTML = `<span class="presence${on ? " on" : ""}">${ICON("user")}</span>` +
        `<span class="name">${MD.esc(u.display_name)}</span>`;
      div.onclick = () => openDm(u.id);
      el.appendChild(div);
    }
  }

  async function openDm(userId){
    const r = await fetch("api/chats", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({kind: "dm", user_id: userId}),
    });
    const data = await r.json();
    if (data.error){ showAlert(data.error); return; }
    await refreshChats();
    selectChat(data.id);
  }

  function renderList(el, items){
    el.innerHTML = "";
    for (const c of items){
      const div = document.createElement("div");
      div.className = "chat-item" +
        (CHAT.currentId() === c.id && !KANBAN.isOpen() ? " active" : "") +
        (c.kind === "channel" && !c.is_member ? " not-member" : "");
      const icon = c.kind === "agent" ? ICON("spark") : c.kind === "dm" ? "" : (c.is_private ? ICON("lock") : ICON("hash"));
      let badge = "";
      if (c.unread > 0)
        badge = `<span class="badge${c.mentioned ? " mention" : ""}">${c.unread}</span>`;
      const call = typeof CALL !== "undefined" && CALL.hasCall(c.id)
        ? `<span class="call-mark" title="идёт звонок">${ICON("phone", "xs")}</span>` : "";
      const draft = (localStorage.getItem("gd_draft_" + c.id) || "").trim() &&
                    CHAT.currentId() !== c.id ? `<span class="draft-mark" title="черновик">${ICON("pencil", "xs")}</span>` : "";
      div.innerHTML = `<span>${icon}</span><span class="name">${MD.esc(c.title)}</span>${call}${draft}${badge}`;
      div.onclick = () => selectChat(c.id);
      div.oncontextmenu = e => { e.preventDefault(); openChatCtxMenu(e.clientX, e.clientY, c); };
      el.appendChild(div);
    }
  }

  // Правый клик по чату в сайдбаре: только из действий, уже доступных в UI
  // (кнопка входа/выхода из канала, удаление сессии, галочка «прочитано»).
  function openChatCtxMenu(x, y, c){
    const items = [{icon: ICON("chevron-right", "sm"), label: "Открыть", act: () => selectChat(c.id)}];
    if (c.kind === "channel"){
      if (c.is_member)
        items.push({icon: ICON("logout", "sm"), label: "Покинуть канал", act: () => leaveChannel(c)});
      else
        items.push({icon: ICON("plus", "sm"), label: "Вступить", act: () => joinChannel(c)});
    }
    if (c.kind === "agent")
      items.push({icon: ICON("trash", "sm"), label: "Удалить сессию", danger: true,
                  act: () => deleteAgentChat(c)});
    if (c.unread > 0)
      items.push({icon: ICON("check", "sm"), label: "Пометить прочитанным", act: () => markChatRead(c)});
    openContextMenu(x, y, items);
  }

  async function joinChannel(c){
    const r = await fetch(`api/chats/${c.id}/join`, {method: "POST"});
    const data = await r.json().catch(() => ({}));
    if (data.error){ showAlert(data.error); return; }
    await refreshChats();
  }

  async function leaveChannel(c){
    const r = await fetch(`api/chats/${c.id}/leave`, {method: "POST"});
    const data = await r.json().catch(() => ({}));
    if (data.error){ showAlert(data.error); return; }
    await refreshChats();
  }

  // Как btn-del-chat в chat.js, но для произвольной (не обязательно открытой) сессии.
  async function deleteAgentChat(c){
    if (!confirm(`Удалить сессию «${c.title}» со всей историей?`)) return;
    const r = await fetch(`api/chats/${c.id}`, {method: "DELETE"});
    const data = await r.json().catch(() => ({}));
    if (data.error){ showAlert(data.error); return; }
    localStorage.removeItem("gd_draft_" + c.id);
    if (CHAT.currentId() === c.id) afterChatDeleted();
    else refreshChats();
  }

  // Отмечает чат прочитанным без его открытия: last_message_id неизвестен
  // из списка чатов (там только счётчик) — берём id последнего сообщения
  // отдельным запросом.
  async function markChatRead(c){
    const r = await fetch(`api/chats/${c.id}/messages?limit=1`);
    if (!r.ok) return;
    const data = await r.json();
    const last = data.messages.length ? data.messages[data.messages.length - 1].id : 0;
    if (!last) return;
    await fetch(`api/chats/${c.id}/read`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({last_message_id: last}),
    });
    await refreshChats();
  }

  // await-able: лента «Активность» после выбора чата прыгает к сообщению —
  // ей нужно дождаться загрузки ленты (CHAT.open).
  async function selectChat(id){
    const chat = chats.find(c => c.id === id);
    if (!chat) return;
    KANBAN.close();  // доска — оверлей поверх чата
    if (typeof ACT !== "undefined") ACT.close();  // и панель активности тоже
    localStorage.setItem("gd_last_chat", id);
    await CHAT.open(chat);
    renderChatLists();
  }

  // ------------------------------------------------------------- модалки

  function openModal(html){
    modal.innerHTML = html;
    modalBack.style.display = "";
  }
  function closeModal(){ modalBack.style.display = "none"; }
  modalBack.onclick = e => { if (e.target === modalBack) closeModal(); };

  // Свой alert вместо стокового: отдельный слой ПОВЕРХ обычных модалок —
  // ошибка может всплыть, пока открыта модалка, и не должна затирать её
  // содержимое. Возвращает Promise, резолвится по закрытию.
  function showAlert(message, title = ""){
    return new Promise(resolve => {
      let back = document.getElementById("alert-back");
      if (!back){
        back = document.createElement("div");
        back.id = "alert-back";
        back.innerHTML = `<div id="alert-box" role="alertdialog">
          <div id="alert-title"></div>
          <div id="alert-text"></div>
          <div class="row"><button id="alert-ok" class="btn-primary">Понятно</button></div>
        </div>`;
        document.body.appendChild(back);
      }
      const titleEl = back.querySelector("#alert-title");
      titleEl.textContent = title;
      titleEl.style.display = title ? "" : "none";
      back.querySelector("#alert-text").textContent = String(message ?? "");
      back.style.display = "flex";
      const ok = back.querySelector("#alert-ok");
      const done = () => {
        back.style.display = "none";
        document.removeEventListener("keydown", onKey, true);
        resolve();
      };
      // capture + stopPropagation: Esc закрывает только alert, а не модалку,
      // панель канбана или тред под ним.
      const onKey = e => {
        if (e.key !== "Escape" && e.key !== "Enter") return;
        e.stopPropagation();
        e.preventDefault();
        done();
      };
      document.addEventListener("keydown", onKey, true);
      ok.onclick = done;
      back.onclick = e => { if (e.target === back) done(); };
      ok.focus();
    });
  }

  document.getElementById("btn-new-agent").onclick = async () => {
    const r = await fetch("api/chats", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({kind: "agent"}),
    });
    const data = await r.json();
    if (data.error){ showAlert(data.error); return; }
    await refreshChats();
    selectChat(data.id);
  };

  // Сессию удалили — открыть первую оставшуюся сессию с агентом.
  async function afterChatDeleted(){
    await refreshChats();
    const first = chats.find(c => c.kind === "agent");
    if (first) selectChat(first.id);
    else document.getElementById("chat-title").textContent = "Выберите чат";
  }

  document.getElementById("btn-new-channel").onclick = () => {
    openModal(`
      <h3>Новый канал</h3>
      <input type="text" id="ch-title" placeholder="Название">
      <label><input type="checkbox" id="ch-private"> Приватный (виден только участникам)</label>
      <div class="row"><button id="m-cancel">Отмена</button><button id="m-ok">Создать</button></div>`);
    modal.querySelector("#m-cancel").onclick = closeModal;
    modal.querySelector("#m-ok").onclick = async () => {
      const title = modal.querySelector("#ch-title").value.trim();
      if (!title) return;
      const r = await fetch("api/chats", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({kind: "channel", title,
                              is_private: modal.querySelector("#ch-private").checked}),
      });
      const data = await r.json();
      closeModal();
      if (data.error){ showAlert(data.error); return; }
      await refreshChats();
      selectChat(data.id);
    };
  };

  document.getElementById("btn-new-dm").onclick = () => {
    const others = (window.USERS || []).filter(u => u.id !== window.ME.id);
    openModal(`
      <h3>Написать</h3>
      <div class="userpick">${others.map(u =>
        `<div data-id="${u.id}">${MD.esc(u.display_name)} <small>@${u.login}</small></div>`).join("") ||
        "<i>других пользователей пока нет</i>"}</div>
      <div class="row"><button id="m-cancel">Отмена</button></div>`);
    modal.querySelector("#m-cancel").onclick = closeModal;
    modal.querySelectorAll(".userpick div").forEach(d => d.onclick = async () => {
      const r = await fetch("api/chats", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({kind: "dm", user_id: +d.dataset.id}),
      });
      const data = await r.json();
      closeModal();
      if (data.error){ showAlert(data.error); return; }
      await refreshChats();
      selectChat(data.id);
    });
  };

  // Универсальный пикер из списка: items = [{label, value}], onPick(value).
  function pickFromList(title, items, onPick){
    openModal(`
      <h3>${MD.esc(title)}</h3>
      <div class="userpick" style="max-height:340px;overflow-y:auto">${items.map((it, i) =>
        `<div data-i="${i}">${MD.esc(it.label)}</div>`).join("") || "<i>пусто</i>"}</div>
      <div class="row"><button id="m-cancel">Отмена</button></div>`);
    modal.querySelector("#m-cancel").onclick = closeModal;
    modal.querySelectorAll(".userpick div").forEach(d => d.onclick = () => {
      closeModal();
      onPick(items[+d.dataset.i].value);
    });
  }

  // ------------------------------------------------------------- тема

  document.getElementById("btn-theme").onclick = () => {
    const html = document.documentElement;
    const next = html.dataset.theme === "light" ? "dark" : "light";
    html.dataset.theme = next;
    localStorage.setItem("gd_theme", next);
  };

  // ------------------------------------------------------------- поиск

  document.getElementById("btn-search").onclick = openSearch;
  document.addEventListener("keydown", e => {
    if (e.key === "k" && e.ctrlKey){ e.preventDefault(); openSearch(); }
  });

  function openSearch(){
    openModal(`
      <h3>Поиск</h3>
      <input type="text" id="search-input" placeholder="Документы и сообщения…">
      <div id="search-results"></div>
      <div class="row"><button id="m-cancel">Закрыть</button></div>`);
    modal.querySelector("#m-cancel").onclick = closeModal;
    const input = modal.querySelector("#search-input");
    const results = modal.querySelector("#search-results");
    input.focus();
    let timer = null, seq = 0;
    input.oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = input.value.trim();
        if (q.length < 2){ results.innerHTML = ""; return; }
        const my = ++seq;
        const r = await fetch(`api/search?q=${encodeURIComponent(q)}`);
        if (!r.ok || my !== seq) return;
        const data = await r.json();
        renderResults(data);
      }, 300);
    };
    function renderResults(data){
      results.innerHTML = "";
      if (data.docs.length){
        results.insertAdjacentHTML("beforeend", `<div class="sr-section">Документы</div>`);
        for (const d of data.docs){
          const el = document.createElement("div");
          el.className = "sr-item";
          el.innerHTML = `${ICON("file", "sm")} ${MD.esc(d.name)} <span class="meta">${MD.esc(d.path)}</span>
            ${d.snippet ? `<div class="snip">${MD.esc(d.snippet)}</div>` : ""}`;
          el.onclick = () => { closeModal(); VAULT.openDoc(d.path); };
          results.appendChild(el);
        }
      }
      if (data.messages.length){
        results.insertAdjacentHTML("beforeend", `<div class="sr-section">Сообщения</div>`);
        for (const m of data.messages){
          const where = m.chat_kind === "dm" ? ICON("mail", "sm") + " ЛС" :
                        m.chat_kind === "agent" ? ICON("spark", "sm") + " " + MD.esc(m.chat_title)
                                                : ICON("hash", "sm") + " " + MD.esc(m.chat_title);
          const el = document.createElement("div");
          el.className = "sr-item";
          el.innerHTML = `<span class="meta">${where} · ${MD.esc(m.author_name)}
            · ${(m.created_at || "").slice(0, 16).replace("T", " ")}</span>
            <div class="snip">${MD.esc(m.snippet)}</div>`;
          el.onclick = () => { closeModal(); selectChat(m.chat_id); };
          results.appendChild(el);
        }
      }
      if (!data.docs.length && !data.messages.length)
        results.innerHTML = "<i>ничего не найдено</i>";
    }
  }

  // ------------------------------------------------------------- админка

  document.getElementById("btn-admin").onclick = openAdmin;

  async function openAdmin(){
    const users = await (await fetch("api/admin/users")).json();
    const invites = await (await fetch("api/admin/invites")).json();
    const inviteRow = i => {
      if (i.pending){
        // относительно текущей страницы: работает и на :8010/, и за /gd/
        const url = new URL("invite/" + i.token, location.href).href;
        return `<div class="admin-user" data-token="${i.token}">
          <input type="text" class="inv-link" readonly value="${url}" style="flex:1;margin:0">
          <button class="b-copy" title="Скопировать">${ICON("copy")}</button>
          <button class="b-revoke" title="Отозвать">${ICON("x")}</button>
        </div>`;
      }
      const status = i.used_login ? `использована — @${MD.esc(i.used_login)}` : "истекла";
      return `<div class="admin-user off"><span class="who"><small>${status}</small></span></div>`;
    };
    openModal(`
      <h3>Пользователи</h3>
      <div id="admin-list">${users.map(u => `
        <div class="admin-user${u.disabled_at ? " off" : ""}" data-id="${u.id}">
          <span class="who">${MD.esc(u.display_name)} <small>@${u.login}${u.is_admin ? " · админ" : ""}</small></span>
          <button class="b-pass" title="Сбросить пароль">${ICON("key")}</button>
          <button class="b-off" title="${u.disabled_at ? "Включить" : "Отключить"}">${u.disabled_at ? ICON("play") : ICON("pause")}</button>
        </div>`).join("")}</div>
      <hr>
      <h3>Приглашения</h3>
      <p style="font-size:12px;color:var(--fg-dim);margin:0 0 8px">Ссылка одноразовая:
      человек сам выбирает логин и пароль, после регистрации ссылка гаснет.
      Неиспользованная — истекает через 7 дней.</p>
      <div id="inv-list">${invites.map(inviteRow).join("") || "<i>приглашений нет</i>"}</div>
      <div class="row"><button id="m-cancel">Закрыть</button>
        <button id="m-llm">${ICON("spark")} Модели агента</button>
        <button id="m-invite">${ICON("plus")} Ссылка-приглашение</button></div>`);
    modal.querySelector("#m-llm").onclick = openLlm;
    modal.querySelector("#m-cancel").onclick = closeModal;
    modal.querySelectorAll(".admin-user[data-id]").forEach(row => {
      const id = row.dataset.id;
      const u = users.find(x => String(x.id) === id);
      row.querySelector(".b-pass").onclick = async () => {
        const pw = prompt(`Новый пароль для @${u.login}:`);
        if (!pw) return;
        const r = await fetch(`api/admin/users/${id}`, {
          method: "PATCH", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({password: pw}),
        });
        const data = await r.json();
        showAlert(data.error || "пароль обновлён");
      };
      row.querySelector(".b-off").onclick = async () => {
        await fetch(`api/admin/users/${id}`, {
          method: "PATCH", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({disabled: !u.disabled_at}),
        });
        openAdmin();
      };
    });
    modal.querySelectorAll(".admin-user[data-token]").forEach(row => {
      const input = row.querySelector(".inv-link");
      row.querySelector(".b-copy").onclick = () => {
        input.select();
        // clipboard API требует https; по LAN работаем через execCommand.
        if (navigator.clipboard && window.isSecureContext)
          navigator.clipboard.writeText(input.value);
        else
          document.execCommand("copy");
      };
      row.querySelector(".b-revoke").onclick = async () => {
        await fetch(`api/admin/invites/${row.dataset.token}`, {method: "DELETE"});
        openAdmin();
      };
    });
    modal.querySelector("#m-invite").onclick = async () => {
      const r = await fetch("api/admin/invites", {method: "POST"});
      const data = await r.json();
      if (data.error){ showAlert(data.error); return; }
      openAdmin();
    };
  }

  // ------------------------------------------------------------- модели агента

  const KIND_TITLE = {claude: "Claude (подписка)", openai: "OpenAI-совместимый"};

  async function openLlm(){
    const data = await (await fetch("api/admin/llm")).json();
    if (data.error){ showAlert(data.error); return; }
    const providers = data.providers || [];
    const row = p => {
      const models = p.models.length
        ? p.models.map(m => MD.esc(m.title || m.id)).join(", ")
        : "моделей нет";
      const key = p.has_key ? "" : ` · <span class="warn">без ключа</span>`;
      return `<div class="admin-user${p.enabled ? "" : " off"}" data-id="${p.id}">
        <span class="who">${MD.esc(p.title)}
          <small>${KIND_TITLE[p.kind] || p.kind} · ${models}${key}</small></span>
        <button class="b-edit" title="Настроить">${ICON("pencil")}</button>
        <button class="b-off" title="${p.enabled ? "Выключить" : "Включить"}">${p.enabled ? ICON("pause") : ICON("play")}</button>
      </div>`;
    };
    openModal(`
      <h3>Модели агента</h3>
      <p style="font-size:12px;color:var(--fg-dim);margin:0 0 8px">Провайдер —
      это способ добраться до модели. Claude работает по подписке, остальные —
      по ключу к OpenAI-совместимому API (OpenRouter, локальный сервер).
      Выключенные провайдеры не показываются в выборе модели чата.</p>
      <div id="llm-list">${providers.map(row).join("") || "<i>провайдеров нет</i>"}</div>
      <div class="row"><button id="m-cancel">Закрыть</button>
        <button id="m-add">${ICON("plus")} Провайдер</button></div>`);
    modal.querySelector("#m-cancel").onclick = closeModal;
    modal.querySelector("#m-add").onclick = () => openLlmForm(null);
    modal.querySelectorAll(".admin-user[data-id]").forEach(el => {
      const p = providers.find(x => String(x.id) === el.dataset.id);
      el.querySelector(".b-edit").onclick = () => openLlmForm(p);
      el.querySelector(".b-off").onclick = async () => {
        await patchLlm(p.id, {enabled: !p.enabled});
        openLlm();
      };
    });
  }

  async function patchLlm(id, body){
    const r = await fetch(`api/admin/llm/${id}`, {
      method: "PATCH", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (data.error){ showAlert(data.error); return null; }
    await reloadModels();
    return data;
  }

  // Каталог моделей уехал — обновляем селектор в шапке чата на месте.
  async function reloadModels(){
    const r = await fetch("api/me");
    if (r.ok){
      window.ME = await r.json();
      if (window.CHAT && CHAT.refreshModels) CHAT.refreshModels();
    }
  }

  function openLlmForm(p){
    const isNew = !p;
    const modelLines = (p ? p.models : [])
      .map(m => [m.id, m.title || "", m.vision ? "vision" : ""]
        .filter(Boolean).join(" | ")).join("\n");
    const keyHint = p && p.key_env
      ? `сейчас из переменной окружения ${MD.esc(p.key_env)}`
      : (p && p.has_key ? "ключ задан" : "ключа нет");
    openModal(`
      <h3>${isNew ? "Новый провайдер" : MD.esc(p.title)}</h3>
      <label class="field"><span>Название</span>
        <input id="f-title" type="text" value="${p ? MD.esc(p.title) : ""}"
               placeholder="OpenRouter"></label>
      ${isNew ? `
      <label class="field"><span>Ключ (латиницей, попадёт в id модели)</span>
        <input id="f-key" type="text" placeholder="openrouter"></label>
      <label class="field"><span>Вид</span>
        <select id="f-kind">
          <option value="openai">OpenAI-совместимый</option>
          <option value="claude">Claude (подписка)</option>
        </select></label>` : ""}
      <label class="field"><span>Адрес API</span>
        <input id="f-url" type="text" value="${p ? MD.esc(p.base_url) : ""}"
               placeholder="https://openrouter.ai/api/v1"></label>
      <label class="field"><span>Ключ API — ${keyHint}</span>
        <input id="f-apikey" type="text" autocomplete="off"
               placeholder="env:OPENROUTER_API_KEY или сам ключ; пусто — не менять, «-» — стереть"></label>
      <label class="field"><span>Модели, по одной в строке: id | название | vision</span>
        <textarea id="f-models" rows="5"
                  placeholder="z-ai/glm-5.2:free | GLM 5.2 (free)">${MD.esc(modelLines)}</textarea></label>
      <label class="check"><input id="f-enabled" type="checkbox"
        ${!p || p.enabled ? "checked" : ""}><span>Показывать в выборе модели</span></label>
      <div id="f-note" class="hint"></div>
      <div class="row">
        <button id="m-back">Назад</button>
        ${isNew ? "" : `<button id="m-del" class="danger">${ICON("trash")} Удалить</button>`}
        ${isNew ? "" : `<button id="m-check">Проверить связь</button>`}
        <button id="m-save" class="btn-primary">Сохранить</button>
      </div>`);
    const note = modal.querySelector("#f-note");
    modal.querySelector("#m-back").onclick = openLlm;
    modal.querySelector("#m-save").onclick = async () => {
      const body = {
        title: modal.querySelector("#f-title").value.trim(),
        base_url: modal.querySelector("#f-url").value.trim(),
        api_key: modal.querySelector("#f-apikey").value.trim(),
        models: modal.querySelector("#f-models").value,
        enabled: modal.querySelector("#f-enabled").checked,
      };
      let data;
      if (isNew){
        body.key = modal.querySelector("#f-key").value.trim();
        body.kind = modal.querySelector("#f-kind").value;
        const r = await fetch("api/admin/llm", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        });
        data = await r.json().catch(() => ({}));
        if (data.error){ showAlert(data.error); return; }
        await reloadModels();
      } else {
        data = await patchLlm(p.id, body);
        if (!data) return;
      }
      openLlm();
    };
    if (!isNew){
      modal.querySelector("#m-del").onclick = async () => {
        if (!confirm(`Удалить провайдера «${p.title}»? Чаты, где выбраны его`
                     + ` модели, вернутся к модели по умолчанию.`)) return;
        const r = await fetch(`api/admin/llm/${p.id}`, {method: "DELETE"});
        const d = await r.json().catch(() => ({}));
        if (d.error){ showAlert(d.error); return; }
        await reloadModels();
        openLlm();
      };
      modal.querySelector("#m-check").onclick = async () => {
        note.textContent = "проверяю…";
        const r = await fetch(`api/admin/llm/${p.id}/check`, {method: "POST"});
        const d = await r.json().catch(() => ({}));
        note.textContent = d.text || d.error || "нет ответа";
        note.className = "hint" + (d.ok ? "" : " warn");
      };
    }
  }

  // ------------------------------------------------------------- волт-панель

  document.getElementById("btn-vault-toggle").onclick = () => {
    // Пока открыт тред, он занимает ту же колонку сетки, что и волт —
    // не схлопываем колонку (иначе спрятался бы и тред), только запоминаем
    // желаемое состояние волта: применится в chat.js closeThread().
    if (appEl.classList.contains("thread-open")){
      const hidden = !vaultPane.classList.contains("hidden");
      vaultPane.classList.toggle("hidden", hidden);
      localStorage.setItem("gd_vault_hidden", hidden ? "1" : "0");
      return;
    }
    const hidden = appEl.classList.toggle("vault-hidden");
    vaultPane.classList.toggle("hidden", hidden);
    localStorage.setItem("gd_vault_hidden", hidden ? "1" : "0");
  };

  // Перетаскиваемая граница панели волта; ширина запоминается.
  function initVaultResizer(){
    const saved = +localStorage.getItem("gd_vault_w");
    if (saved) appEl.style.setProperty("--vault-w", saved + "px");
    const resizer = document.getElementById("vault-resizer");
    resizer.onmousedown = e => {
      e.preventDefault();
      resizer.classList.add("dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      const move = ev => {
        const w = Math.max(320, Math.min(window.innerWidth * 0.65,
                                         window.innerWidth - ev.clientX));
        appEl.style.setProperty("--vault-w", w + "px");
      };
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        resizer.classList.remove("dragging");
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
        const w = parseInt(getComputedStyle(appEl).getPropertyValue("--vault-w"));
        if (w) localStorage.setItem("gd_vault_w", w);
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    };
  }

  function showVault(){
    // Волт и тред делят третью колонку сетки — открытие документа
    // (вики-ссылка, поиск, бэклинк) вытесняет тред, если он был открыт.
    if (typeof CHAT !== "undefined" && CHAT.closeThread) CHAT.closeThread();
    appEl.classList.remove("vault-hidden");
    vaultPane.classList.remove("hidden");
    localStorage.setItem("gd_vault_hidden", "0");
  }

  // ------------------------------------------------ контекстное меню (общее)
  // Единый компонент для всех правых кликов в приложении — вёрстка и стили
  // те же, что раньше жили только в chat.js (#ctx-menu/.ctx-item в style.css).
  // items = [{icon, label, act, danger}]; header — необязательный HTML сверху
  // (например капсула эмодзи-реакций в меню сообщения — это остаётся
  // специфичным для чата и собирается в chat.js).
  function ensureCtxMenu(){
    let menu = document.getElementById("ctx-menu");
    if (menu) return menu;
    menu = document.createElement("div");
    menu.id = "ctx-menu";
    menu.style.display = "none";
    document.body.appendChild(menu);
    const close = () => { menu.style.display = "none"; };
    document.addEventListener("click", close);
    document.addEventListener("keydown", e => { if (e.key === "Escape") close(); });
    window.addEventListener("resize", close);
    // capture: scroll не всплывает, но фаза захвата всё равно проходит через
    // window — так закрываем меню при скролле любой панели (лента, дерево
    // волта, колонки канбана), не перечисляя их поимённо.
    window.addEventListener("scroll", close, true);
    return menu;
  }

  function openContextMenu(x, y, items, headerHtml = ""){
    const menu = ensureCtxMenu();
    menu.innerHTML = headerHtml + items.map((it, i) =>
      `<div class="ctx-item${it.danger ? " danger" : ""}" data-i="${i}">
         <span class="ci-icon">${it.icon}</span>${it.label}</div>`).join("");
    menu.querySelectorAll(".ctx-item").forEach(el =>
      el.onclick = () => { menu.style.display = "none"; items[+el.dataset.i].act(); });
    // Показать скрыто, замерить, вписать в окно.
    menu.style.visibility = "hidden";
    menu.style.display = "block";
    const w = menu.offsetWidth, h = menu.offsetHeight;
    menu.style.left = Math.min(x, window.innerWidth - w - 8) + "px";
    menu.style.top = Math.min(y, window.innerHeight - h - 8) + "px";
    menu.style.visibility = "";
    return menu;
  }

  boot();
  return { refreshChats, showVault, selectChat, pickFromList, afterChatDeleted,
           openContextMenu, rerenderLists: renderChatLists, alert: showAlert,
           chatById: id => chats.find(c => c.id === id) };
})();
