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
    btn.textContent = notifyEnabled() ? "🔔" : "🔕";
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
        // Не дёргаем, если чат открыт прямо сейчас во вкладке в фокусе.
        if (!document.hidden && CHAT.currentId() === c.id) continue;
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
    soundBtn.textContent = SOUND.isOn() ? "🔊" : "🔇";
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
      div.innerHTML = `<span class="presence${on ? " on" : ""}">👤</span>` +
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
    if (data.error){ alert(data.error); return; }
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
      const icon = c.kind === "agent" ? "🤖" : c.kind === "dm" ? "✉️" : (c.is_private ? "🔒" : "#");
      let badge = "";
      if (c.unread > 0)
        badge = `<span class="badge${c.mentioned ? " mention" : ""}">${c.unread}</span>`;
      const draft = (localStorage.getItem("gd_draft_" + c.id) || "").trim() &&
                    CHAT.currentId() !== c.id ? '<span class="draft-mark" title="черновик">✏</span>' : "";
      div.innerHTML = `<span>${icon}</span><span class="name">${MD.esc(c.title)}</span>${draft}${badge}`;
      div.onclick = () => selectChat(c.id);
      el.appendChild(div);
    }
  }

  function selectChat(id){
    const chat = chats.find(c => c.id === id);
    if (!chat) return;
    KANBAN.close();  // доска — оверлей поверх чата
    localStorage.setItem("gd_last_chat", id);
    CHAT.open(chat);
    renderChatLists();
  }

  // ------------------------------------------------------------- модалки

  function openModal(html){
    modal.innerHTML = html;
    modalBack.style.display = "";
  }
  function closeModal(){ modalBack.style.display = "none"; }
  modalBack.onclick = e => { if (e.target === modalBack) closeModal(); };

  document.getElementById("btn-new-agent").onclick = async () => {
    const r = await fetch("api/chats", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({kind: "agent"}),
    });
    const data = await r.json();
    if (data.error){ alert(data.error); return; }
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
      if (data.error){ alert(data.error); return; }
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
      if (data.error){ alert(data.error); return; }
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
          el.innerHTML = `📄 ${MD.esc(d.name)} <span class="meta">${MD.esc(d.path)}</span>
            ${d.snippet ? `<div class="snip">${MD.esc(d.snippet)}</div>` : ""}`;
          el.onclick = () => { closeModal(); VAULT.openDoc(d.path); };
          results.appendChild(el);
        }
      }
      if (data.messages.length){
        results.insertAdjacentHTML("beforeend", `<div class="sr-section">Сообщения</div>`);
        for (const m of data.messages){
          const where = m.chat_kind === "dm" ? "✉️ ЛС" :
                        m.chat_kind === "agent" ? "🤖 " + MD.esc(m.chat_title)
                                                : "#" + MD.esc(m.chat_title);
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
          <button class="b-copy" title="Скопировать">📋</button>
          <button class="b-revoke" title="Отозвать">✕</button>
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
          <button class="b-pass" title="Сбросить пароль">🔑</button>
          <button class="b-off" title="${u.disabled_at ? "Включить" : "Отключить"}">${u.disabled_at ? "▶" : "⏸"}</button>
        </div>`).join("")}</div>
      <hr>
      <h3>Приглашения</h3>
      <p style="font-size:12px;color:var(--fg-dim);margin:0 0 8px">Ссылка одноразовая:
      человек сам выбирает логин и пароль, после регистрации ссылка гаснет.
      Неиспользованная — истекает через 7 дней.</p>
      <div id="inv-list">${invites.map(inviteRow).join("") || "<i>приглашений нет</i>"}</div>
      <div class="row"><button id="m-cancel">Закрыть</button><button id="m-invite">➕ Ссылка-приглашение</button></div>`);
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
        alert(data.error || "пароль обновлён");
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
      if (data.error){ alert(data.error); return; }
      openAdmin();
    };
  }

  // ------------------------------------------------------------- волт-панель

  document.getElementById("btn-vault-toggle").onclick = () => {
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
    appEl.classList.remove("vault-hidden");
    vaultPane.classList.remove("hidden");
    localStorage.setItem("gd_vault_hidden", "0");
  }

  boot();
  return { refreshChats, showVault, selectChat, pickFromList, afterChatDeleted,
           rerenderLists: renderChatLists };
})();
