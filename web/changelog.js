// «Что нового» — модалка с историей изменений из CHANGELOG.md (api/changelog).
// Рендерит своим мини-парсером, а не MD.render: номера задач «(#104)» в
// changelog — это карточки доски разработки Foreman, а MD.render превратил бы
// их в ссылки на карточки локального канбана установки.
const CHANGELOG = (() => {
  const esc = s => s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');

  // CHANGELOG.md — узкое подмножество markdown: ## версия, ### категория,
  // «- » пункты с переносом-продолжением, вводные абзацы.
  function renderLog(text){
    let html = "", release = false, list = false, item = null, para = null;
    const flushItem = () => { if (item !== null){ html += `<li>${inline(item)}</li>`; item = null; } };
    const flushPara = () => { if (para !== null){ html += `<p>${inline(para)}</p>`; para = null; } };
    const closeList = () => { flushItem(); if (list){ html += "</ul>"; list = false; } };
    const closeRelease = () => { closeList(); flushPara(); if (release){ html += "</div>"; release = false; } };
    for (const line of text.split("\n")){
      let m;
      if (line.startsWith("# ")) continue;
      if (m = line.match(/^##\s+\[?([^\]]+?)\]?(?:\s+[—-]+\s+(.*))?\s*$/)){
        closeRelease();
        release = true;
        html += `<div class="cl-release"><div class="cl-ver">${esc(m[1])}` +
          (m[2] ? `<span class="cl-date">${esc(m[2])}</span>` : "") + "</div>";
        continue;
      }
      if (m = line.match(/^###\s+(.*)/)){
        closeList(); flushPara();
        html += `<div class="cl-cat">${esc(m[1])}</div>`;
        continue;
      }
      if (m = line.match(/^\s*[-*]\s+(.*)/)){
        flushItem(); flushPara();
        if (!list){ html += "<ul>"; list = true; }
        item = m[1];
        continue;
      }
      if (line.trim() === ""){ closeList(); flushPara(); continue; }
      if (item !== null){ item += " " + line.trim(); continue; }
      para = para === null ? line.trim() : para + " " + line.trim();
    }
    closeRelease();
    return html;
  }

  async function open(){
    const back = document.getElementById("modal-back");
    const modal = document.getElementById("modal");
    let body;
    try {
      const r = await fetch("api/changelog");
      if (!r.ok) throw new Error(r.status);
      body = `<div class="cl-scroll">${renderLog((await r.json()).text || "")}</div>`;
    } catch {
      body = `<p class="cl-error">Не удалось загрузить историю изменений.</p>`;
    }
    modal.innerHTML = `<h3>Что нового</h3>${body}
      <div class="row"><button id="cl-close">Закрыть</button></div>`;
    back.style.display = "";
    modal.querySelector("#cl-close").onclick = () => { back.style.display = "none"; };
  }

  document.getElementById("brand").onclick = open;

  return { open };
})();
