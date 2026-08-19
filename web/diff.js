// Раскраска unified diff (git show) без библиотек.
const DIFF = (() => {
  function render(text){
    const esc = s => s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
    return (text || "").split("\n").map(line => {
      let cls = "";
      if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ")
          || line.startsWith("index ") || line.startsWith("commit ")
          || line.startsWith("Author") || line.startsWith("Date")) cls = "meta";
      else if (line.startsWith("@@")) cls = "hunk";
      else if (line.startsWith("+")) cls = "add";
      else if (line.startsWith("-")) cls = "del";
      return cls ? `<span class="${cls}">${esc(line)}</span>` : esc(line);
    }).join("\n");
  }
  return { render };
})();
