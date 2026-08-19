// Лёгкий markdown -> html (порт из dnd-chat) + wikilinks, упоминания, картинки.
// MD.render(text, {docPath}) — docPath нужен, чтобы относительные картинки
// документа резолвились в /api/vault/file.
const MD = (() => {
  function esc(s){return s.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}

  function resolveRel(docPath, src){
    if (/^(https?:|data:)/.test(src)) return src;
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
    s = s.replace(/!\[([^\]]*)\]\(([^)]+)\)/g,
      (m, alt, src) => `<img alt="${alt}" src="${resolveRel(opt.docPath, src)}">`);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/\[\[([^\]]+)\]\]/g,
      (m, name) => `<span class="wikilink" data-wiki="${name.replace(/"/g,'&quot;')}">${name}</span>`);
    s = s.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    s = s.replace(/(^|\s)@([a-zA-Z0-9_.-]+|агент)/g, (m, pre, login) => {
      const meCls = (window.ME && login.toLowerCase() === window.ME.login) ? " me" : "";
      return `${pre}<span class="mention${meCls}">@${login}</span>`;
    });
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
      if (m = line.match(/^\s*[-*+]\s+(.*)/)){ if (list !== "ul"){ closeList(); list = "ul"; html += "<ul>"; } html += `<li>${mdInline(m[1], opt)}</li>`; i++; continue; }
      if (m = line.match(/^\s*\d+\.\s+(.*)/)){ if (list !== "ol"){ closeList(); list = "ol"; html += "<ol>"; } html += `<li>${mdInline(m[1], opt)}</li>`; i++; continue; }
      if (line.trim() === ""){ closeList(); i++; continue; }
      closeList(); html += `<p>${mdInline(line, opt)}</p>`; i++;
    }
    closeList();
    return html;
  }

  return { render, esc };
})();
