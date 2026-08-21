// Волт: дерево, просмотр/редактирование документа, загрузка файлов, wikilinks.
const VAULT = (() => {
  const treeEl = document.getElementById("vault-tree");
  const docEl = document.getElementById("vault-doc");
  const docPathEl = document.getElementById("vault-doc-path");
  const viewEl = document.getElementById("vault-doc-view");
  const editEl = document.getElementById("vault-doc-edit");
  const backBtn = document.getElementById("btn-vault-back");
  const editBtn = document.getElementById("btn-doc-edit");
  const saveBtn = document.getElementById("btn-doc-save");
  const uploadInput = document.getElementById("upload-input");

  let currentPath = null, currentMtime = null, editing = false;
  let cm = null, cmHolder = null;   // CodeMirror; без vendor-файлов — textarea
  let toast = null, toastHolder = null;  // Toast UI (WYSIWYG); каскад: Toast -> CM -> textarea
  let loadedText = "";  // текст на момент открытия: сейв без изменений не коммитим

  // Toast UI живёт от входа в правку до выхода: инстанс каждый раз новый,
  // тема — классом на холдере (дарк-css матчит по .toastui-editor-dark сверху).
  function syncToastTheme(){
    if (toastHolder)
      toastHolder.classList.toggle("toastui-editor-dark",
        document.documentElement.dataset.theme !== "light");
  }

  async function uploadImage(blob, callback){
    const fd = new FormData();
    fd.append("file", blob, blob.name || "img.png");
    const r = await fetch(`api/vault/upload_image?doc=${encodeURIComponent(currentPath)}`,
                          {method: "POST", body: fd});
    const data = await r.json().catch(() => ({error: "ошибка загрузки картинки"}));
    if (data.error){ APP.alert(data.error); return; }
    // В редактор — резолвнутый адрес (WYSIWYG грузит src от URL страницы,
    // относительный путь волта там бьётся); в md при сохранении imgFromApi
    // вернёт относительный.
    callback(MD.resolveRel(currentPath, data.rel), blob.name || "");
  }

  // Картинки в Toast UI: в файле волта живут ОТНОСИТЕЛЬНЫЕ пути (их понимает
  // агент и git-история), а редактору нужны загружаемые адреса. Туда —
  // резолвим в api/vault/file, обратно — восстанавливаем относительный путь.
  const IMG_MD = /(!\[[^\]]*\]\(\s*)([^)\s]+)((?:\s+"[^"]*")?\s*\))/g;

  function imgToApi(text){
    return text.replace(IMG_MD,
      (m, a, src, b) => a + MD.resolveRel(currentPath, src) + b);
  }

  function imgFromApi(text){
    const dirParts = currentPath ? currentPath.split("/").slice(0, -1) : [];
    return text.replace(IMG_MD, (m, a, src, b) => {
      const enc = src.match(/^api\/vault\/file\?path=(.+)$/);
      if (!enc) return m;
      const parts = decodeURIComponent(enc[1]).split("/");
      let i = 0;
      while (i < dirParts.length && i < parts.length - 1 &&
             dirParts[i] === parts[i]) i++;
      return a + "../".repeat(dirParts.length - i) + parts.slice(i).join("/") + b;
    });
  }

  function ensureToast(text){
    if (!window.toastui || !toastui.Editor) return null;
    if (!toastHolder){
      toastHolder = document.createElement("div");
      toastHolder.id = "toast-holder";
      toastHolder.className = "gd-toast";
      editEl.parentNode.insertBefore(toastHolder, editEl);
      toastHolder.addEventListener("keydown", e => {
        if (e.key === "s" && e.ctrlKey){ e.preventDefault(); save(); }
      }, true);
      new MutationObserver(syncToastTheme).observe(document.documentElement,
        {attributes: true, attributeFilter: ["data-theme"]});
    }
    if (toast){ toast.destroy(); toast = null; }
    toastHolder.style.display = "";
    toast = new toastui.Editor({
      el: toastHolder,
      height: "100%",
      initialEditType: "wysiwyg",
      previewStyle: "tab",
      initialValue: text,
      language: "ru",
      usageStatistics: false,
      autofocus: true,
      toolbarItems: [
        ["heading", "bold", "italic", "strike"],
        ["hr", "quote"],
        ["ul", "ol", "task", "indent", "outdent"],
        ["table", "image", "link"],
        ["code", "codeblock"],
      ],
      hooks: {addImageBlobHook: uploadImage},
      // Виджеты: [[вики]] и @упоминания становятся атомарными узлами и
      // сериализуются обратно как есть — иначе ToastMark экранирует скобки.
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
      customHTMLRenderer: {
        image(node, ctx){
          // В md остаётся относительный путь, показываем через резолв волта.
          const out = ctx.origin();
          out.attributes.src = MD.resolveRel(currentPath, node.destination);
          return out;
        },
      },
    });
    syncToastTheme();
    return toast;
  }

  function destroyToast(){
    if (toast){ toast.destroy(); toast = null; }
    if (toastHolder) toastHolder.style.display = "none";
  }

  function ensureCM(){
    if (cm || !window.CodeMirror) return cm;
    cmHolder = document.createElement("div");
    cmHolder.id = "cm-holder";
    cmHolder.style.display = "none";
    editEl.parentNode.insertBefore(cmHolder, editEl.nextSibling);
    cm = CodeMirror(cmHolder, {
      mode: "markdown",
      lineNumbers: true,
      lineWrapping: true,
      indentUnit: 2,
      extraKeys: {
        "Enter": "newlineAndIndentContinueMarkdownList",
        "Ctrl-S": () => save(),
      },
    });
    return cm;
  }

  // Раскрытые папки дерева: по умолчанию всё свёрнуто, выбор пользователя
  // переживает перерисовку дерева и перезагрузку страницы.
  let openDirs = new Set();
  try {
    openDirs = new Set(JSON.parse(localStorage.getItem("gd_tree_open") || "[]"));
  } catch (e) { /* битый localStorage — дефолт: всё свёрнуто */ }

  function saveOpenDirs(){
    localStorage.setItem("gd_tree_open", JSON.stringify([...openDirs]));
  }

  async function refreshTree(){
    const r = await fetch("api/vault/tree");
    if (!r.ok) return;
    const tree = await r.json();
    treeEl.innerHTML = "";
    treeEl.appendChild(renderNodes(tree));
  }

  function renderNodes(nodes){
    const box = document.createElement("div");
    for (const n of nodes){
      if (n.dir){
        const d = document.createElement("div");
        d.className = "tree-dir";
        d.innerHTML = `<div class="tree-label"></div>`;
        const label = d.querySelector(".tree-label");
        const kids = renderNodes(n.children || []);
        kids.className = "tree-children";
        d.appendChild(kids);
        const paint = open => {
          label.innerHTML = `${open ? ICON("chevron-down", "xs") : ICON("chevron-right", "xs")}${ICON("folder", "sm")} ${MD.esc(n.name)}`;
          kids.style.display = open ? "" : "none";
        };
        paint(openDirs.has(n.path));
        label.onclick = () => {
          const open = kids.style.display === "none";
          paint(open);
          if (open) openDirs.add(n.path);
          else openDirs.delete(n.path);
          saveOpenDirs();
        };
        box.appendChild(d);
      } else {
        const f = document.createElement("div");
        f.className = "tree-file";
        f.innerHTML = ICON("file", "sm") + " " + MD.esc(n.name);
        f.title = n.path;
        f.onclick = () => openDoc(n.path);
        // Перетаскивание в чат/тред — там своя dropzone (chat.js), которая
        // добавляет вложение-док вместо загрузки файла с компьютера.
        f.draggable = true;
        f.addEventListener("dragstart", e => {
          e.dataTransfer.setData("application/x-vault-path", n.path);
          e.dataTransfer.setData("text/plain", n.path);
          e.dataTransfer.effectAllowed = "copy";
        });
        f.addEventListener("contextmenu", e => {
          e.preventDefault();
          openFileCtxMenu(e.clientX, e.clientY, n);
        });
        box.appendChild(f);
      }
    }
    return box;
  }

  async function openDoc(path, quiet = false){
    if (!quiet) APP.showVault();
    localStorage.setItem("gd_last_doc", path);
    if (/\.(png|jpe?g|gif|webp|svg)$/i.test(path)){
      showDocPane(path);
      viewEl.innerHTML = `<img src="api/vault/file?path=${encodeURIComponent(path)}" style="max-width:100%">`;
      setEditing(false, true);
      return;
    }
    const r = await fetch(`api/vault/file?path=${encodeURIComponent(path)}`);
    const data = await r.json();
    if (data.error){
      // Тихое восстановление после перезагрузки: файл могли удалить.
      if (quiet) localStorage.removeItem("gd_last_doc");
      else APP.alert(data.error);
      return;
    }
    showDocPane(path);
    currentMtime = data.mtime;
    loadedText = data.text;
    viewEl.innerHTML = MD.render(data.text, {docPath: path});
    editEl.value = data.text;
    setEditing(false);
    bindWikilinks(viewEl);
    loadBacklinks(path);
  }

  async function loadBacklinks(path){
    const r = await fetch(`api/vault/backlinks?path=${encodeURIComponent(path)}`);
    if (!r.ok) return;
    const links = await r.json();
    if (currentPath !== path || !links.length) return;
    const box = document.createElement("div");
    box.id = "backlinks";
    box.innerHTML = ICON("arrow-left", "sm") + " Ссылаются: ";
    for (const l of links){
      const chip = document.createElement("span");
      chip.className = "bl";
      chip.textContent = l.name;
      chip.title = l.path;
      chip.onclick = () => openDoc(l.path);
      box.appendChild(chip);
    }
    viewEl.appendChild(box);
  }

  function showDocPane(path){
    currentPath = path;
    docPathEl.textContent = path;
    treeEl.style.display = "none";
    docEl.style.display = "flex";
    backBtn.style.display = "";
  }

  function showTree(){
    treeEl.style.display = "";
    docEl.style.display = "none";
    backBtn.style.display = "none";
    currentPath = null;
    // Иначе перезагрузка страницы «вернёт» закрытый документ.
    localStorage.removeItem("gd_last_doc");
  }

  function setEditing(on, readonly = false){
    editing = on;
    viewEl.style.display = on ? "none" : "";
    saveBtn.style.display = on ? "" : "none";
    editBtn.style.display = readonly ? "none" : (on ? "none" : "");
    const wys = on && !readonly && ensureToast(imgToApi(editEl.value));
    if (!wys) destroyToast();
    const rich = on && !wys && ensureCM();
    editEl.style.display = on && !wys && !rich ? "" : "none";
    if (cmHolder) cmHolder.style.display = rich ? "" : "none";
    if (rich){
      cm.setValue(editEl.value);
      setTimeout(() => { cm.refresh(); cm.focus(); }, 0);
    } else if (on && !wys){
      editEl.focus();
    }
  }

  function editorText(){
    if (toast) return imgFromApi(toast.getMarkdown());
    return (cm && cmHolder && cmHolder.style.display !== "none")
      ? cm.getValue() : editEl.value;
  }

  async function save(){
    if (!currentPath || !editing) return;
    const text = editorText();
    editEl.value = text;
    if (text === loadedText){ setEditing(false); return; }
    const r = await fetch("api/vault/file", {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({path: currentPath, text,
                            base_mtime: currentMtime}),
    });
    const data = await r.json();
    if (data.error){
      APP.alert(data.error + (data.conflict ? "\n(скопируйте свой текст, обновите документ и вставьте правки заново)" : ""));
      return;
    }
    currentMtime = data.mtime;
    loadedText = editEl.value;
    viewEl.innerHTML = MD.render(editEl.value, {docPath: currentPath});
    bindWikilinks(viewEl);
    setEditing(false);
    loadBacklinks(currentPath);
  }

  function copyText(text){
    if (navigator.clipboard && window.isSecureContext){
      navigator.clipboard.writeText(text);
      return;
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }

  // Правый клик по файлу в дереве. Только из действий, уже доступных в UI —
  // «История» и «Скачать docx» требуют открытого документа (используют
  // currentPath), поэтому сперва открывают его.
  function openFileCtxMenu(x, y, n){
    const items = [
      {icon: ICON("chevron-right", "sm"), label: "Открыть", act: () => openDoc(n.path)},
      {icon: ICON("clock", "sm"), label: "История правок",
       act: async () => { await openDoc(n.path); showHistory(); }},
    ];
    if (/\.md$/i.test(n.name))
      items.push({icon: ICON("download", "sm"), label: "Скачать docx",
        act: async () => { await openDoc(n.path); exportDocx(); }});
    items.push({icon: ICON("copy", "sm"), label: "Скопировать вики-ссылку",
      act: () => copyText(`[[${n.name.replace(/\.md$/i, "")}]]`)});
    APP.openContextMenu(x, y, items);
  }

  function bindWikilinks(root){
    root.querySelectorAll(".wikilink").forEach(el => {
      el.onclick = async () => {
        const r = await fetch(`api/vault/resolve?name=${encodeURIComponent(el.dataset.wiki)}`);
        const data = await r.json();
        if (data.path) openDoc(data.path);
        else APP.alert(data.error || "не найдено");
      };
    });
  }

  uploadInput.onchange = async () => {
    const file = uploadInput.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch("api/vault/upload?folder=shared", {method: "POST", body: fd});
    const data = await r.json();
    uploadInput.value = "";
    if (data.error){ APP.alert(data.error); return; }
    await refreshTree();
    openDoc(data.path);
  };

  // ------------------------------------------------------------- история

  async function showHistory(){
    if (!currentPath) return;
    const r = await fetch(`api/vault/history?path=${encodeURIComponent(currentPath)}`);
    const items = await r.json();
    if (items.error){ APP.alert(items.error); return; }
    setEditing(false);
    const back = `<div class="hist-item" id="hist-back">${ICON("arrow-left", "sm")} к документу</div>`;
    viewEl.innerHTML = back + (items.map(h => `
      <div class="hist-item" data-hash="${h.hash}">
        ${MD.esc(h.subject)}
        <div class="meta">${MD.esc(h.author)} · ${h.date.slice(0, 16).replace("T", " ")}</div>
      </div>`).join("") || "<i>истории пока нет</i>");
    viewEl.querySelector("#hist-back").onclick = () => openDoc(currentPath);
    viewEl.querySelectorAll("[data-hash]").forEach(el => el.onclick = async () => {
      const rr = await fetch(`api/vault/commit?hash=${el.dataset.hash}`);
      const data = await rr.json();
      viewEl.innerHTML = back + `<div class="diff">${DIFF.render(data.diff || data.error || "")}</div>`;
      viewEl.querySelector("#hist-back").onclick = () => openDoc(currentPath);
    });
  }

  async function exportDocx(){
    if (!currentPath) return;
    const r = await fetch(`api/vault/export?path=${encodeURIComponent(currentPath)}`);
    if (!r.ok){
      const data = await r.json().catch(() => ({}));
      APP.alert(data.error || `ошибка ${r.status}`);
      return;
    }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = currentPath.split("/").pop().replace(/\.md$/i, "") + ".docx";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  backBtn.onclick = showTree;
  editBtn.onclick = () => setEditing(true);
  saveBtn.onclick = save;
  document.getElementById("btn-doc-history").onclick = showHistory;
  document.getElementById("btn-doc-export").onclick = exportDocx;
  editEl.addEventListener("keydown", e => {
    if (e.key === "s" && e.ctrlKey){ e.preventDefault(); save(); }
  });

  return { refreshTree, openDoc, bindWikilinks };
})();
