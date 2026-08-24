// Лёгкий markdown -> html (порт из dnd-chat) + wikilinks, упоминания, картинки.
// MD.render(text, {docPath}) — docPath нужен, чтобы относительные картинки
// документа резолвились в /api/vault/file.
const MD = (() => {
  function esc(s){return s.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}

  function resolveRel(docPath, src){
    // api/… — уже резолвнутый адрес (редактор волта), второй раз не трогаем.
    if (/^(https?:|data:|api\/)/.test(src)) return src;
    const base = docPath ? docPath.split("/").slice(0,-1) : [];
    const parts = base.concat(src.split("/"));
    const out = [];
    for (const p of parts){
      if (p === "" || p === ".") continue;
      if (p === "..") out.pop(); else out.push(p);
    }
    return "api/vault/file?path=" + encodeURIComponent(out.join("/"));
  }

  function mdInline(s, opt){
    s = esc(s);
    // Готовый HTML прячется в плашки \x00N\x00 до конца замен: иначе
    // последующие правила находят «m123» или «#12» ВНУТРИ src/href (base64
    // data-URI, внешние URL) и рвут тег вставленным <span>.
    const stash = [];
    const keep = html => `\x00${stash.push(html) - 1}\x00`;
    s = s.replace(/!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)/g,
      (m, alt, src) => keep(`<img alt="${alt}" src="${resolveRel(opt.docPath, src)}">`));
    s = s.replace(/`([^`]+)`/g, (m, code) => keep(`<code>${code}</code>`));
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/\[\[([^\]|]+)(?:\|([^\]]*))?\]\]/g,
      (m, target, alias) => keep(`<span class="wikilink" data-wiki="${target.replace(/"/g,'&quot;')}">${alias || target}</span>`));
    s = s.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g,
      (m, text, href) => keep(`<a href="${href}" target="_blank">${text}</a>`));
    s = s.replace(/(^|\s)@([a-zA-Z0-9_.-]+|агент)/g, (m, pre, login) => {
      const meCls = (window.ME && login.toLowerCase() === window.ME.login) ? " me" : "";
      return `${pre}<span class="mention${meCls}">@${login}</span>`;
    });
    // Ссылка на задачу канбана: #6 в чате или в описании другой задачи.
    // Lookbehind (?<![\w&#]) — «#6» узнаётся после скобок, кавычек и
    // markdown-разметки (агент часто пишет «(#6)», «**#6**»), но не внутри
    // слов, HTML-сущностей (&#39;) и hex-цветов #fff (там не только цифры).
    s = s.replace(/(?<![\w&#])#(\d+)\b/g, (m, id) =>
      `<span class="card-link" data-card="${id}">#${id}</span>`);
    // Ссылка на сообщение: m123 — нотация, которой агент адресует сообщения
    // (см. prompts.channel_context). Клик ведёт к сообщению в ленте.
    s = s.replace(/(?<![\w&#])m(\d+)\b/g, (x, id) =>
      `<span class="msg-link" data-msg="${id}">m${id}</span>`);
    s = s.replace(/\x00(\d+)\x00/g, (m, i) => stash[+i]);
    return s;
  }

  function render(text, opt = {}){
    const lines = (text || "").split("\n");
    let html = "", list = null, i = 0;
    const closeList = () => { if (list){ html += `</${list}>`; list = null; } };
    const isTableSep = s => /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$/.test(s);
    const splitRow = s => s.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());
    const fence = /^\s*(```|~~~)/;

    while (i < lines.length){
      const line = lines[i];
      if (fence.test(line)){
        i++; const code = [];
        while (i < lines.length && !fence.test(lines[i])){ code.push(lines[i]); i++; }
        i++;
        closeList(); html += `<pre><code>${esc(code.join("\n"))}</code></pre>`;
        continue;
      }
      if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])){
        closeList();
        const headers = splitRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== ""){ rows.push(splitRow(lines[i])); i++; }
        html += "<table><thead><tr>" + headers.map(h => `<th>${mdInline(h, opt)}</th>`).join("") + "</tr></thead><tbody>";
        for (const r of rows) html += "<tr>" + r.map(c => `<td>${mdInline(c, opt)}</td>`).join("") + "</tr>";
        html += "</tbody></table>";
        continue;
      }
      let m;
      if (m = line.match(/^(#{1,6})\s+(.*)/)){ closeList(); const n = Math.min(m[1].length, 3); html += `<h${n}>${mdInline(m[2], opt)}</h${n}>`; i++; continue; }
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)){ closeList(); html += "<hr>"; i++; continue; }
      if (m = line.match(/^\s*>\s?(.*)/)){ closeList(); html += `<blockquote>${mdInline(m[1], opt)}</blockquote>`; i++; continue; }
      if (m = line.match(/^\s*[-*+]\s+\[([ xX])\]\s+(.*)/)){ if (list !== "ul"){ closeList(); list = "ul"; html += "<ul>"; } html += `<li class="task"><input type="checkbox" disabled${m[1] === " " ? "" : " checked"}> ${mdInline(m[2], opt)}</li>`; i++; continue; }
      if (m = line.match(/^\s*[-*+]\s+(.*)/)){ if (list !== "ul"){ closeList(); list = "ul"; html += "<ul>"; } html += `<li>${mdInline(m[1], opt)}</li>`; i++; continue; }
      if (m = line.match(/^\s*\d+\.\s+(.*)/)){ if (list !== "ol"){ closeList(); list = "ol"; html += "<ol>"; } html += `<li>${mdInline(m[1], opt)}</li>`; i++; continue; }
      if (line.trim() === ""){ closeList(); i++; continue; }
      closeList(); html += `<p>${mdInline(line, opt)}</p>`; i++;
    }
    closeList();
    return html;
  }

  return { render, esc, resolveRel };
})();

// Клик по ссылке на задачу (#N) — открывает доску с этой карточкой.
// Делегирование на document: работает для любого места, где отрендерен
// MD.render (лента, тред, панель канбана), без ручной привязки после
// вставки HTML; к моменту клика KANBAN уже точно загружен.
document.addEventListener("click", e => {
  // KANBAN/CHAT — top-level const, в window они НЕ попадают — только typeof.
  const ml = e.target.closest(".msg-link");
  if (ml){
    e.preventDefault();
    if (typeof CHAT !== "undefined" && CHAT.jumpToMsg)
      CHAT.jumpToMsg(+ml.dataset.msg);
    return;
  }
  const el = e.target.closest(".card-link");
  if (!el) return;
  e.preventDefault();
  if (typeof KANBAN !== "undefined" && KANBAN.openCard)
    KANBAN.openCard(+el.dataset.card);
});
