// Shared by the two chats (local model and agents): safe Markdown and the
// agent switcher.
(function () {
  // A small, safe subset: everything is escaped first, then code, headings,
  // lists, bold, italics and http(s) links are added back.
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function inline(raw) {
    return raw.split(/(`[^`\n]+`)/).map(function (part, i) {
      if (i % 2) return "<code>" + esc(part.slice(1, -1)) + "</code>";
      return esc(part)
        .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
        .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
                 '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    }).join("");
  }
  function md(src) {
    var out = [], lines = src.replace(/\r/g, "").split("\n"), i = 0, list = null, para = [];
    function flush() {
      if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; }
    }
    function close() { if (list) { out.push("</" + list + ">"); list = null; } }
    while (i < lines.length) {
      var line = lines[i], fence = line.match(/^\s*```\s*([\w+#.-]*)/);
      if (fence) {
        flush(); close();
        var code = [];
        for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) code.push(lines[i]);
        i++;
        out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>");
        continue;
      }
      var h = line.match(/^(#{1,4})\s+(.*)/), ul = line.match(/^\s*[-*+]\s+(.*)/), ol = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (h) {
        flush(); close();
        var n = h[1].length + 2;
        out.push("<h" + n + ">" + inline(h[2]) + "</h" + n + ">");
      } else if (ul || ol) {
        flush();
        var tag = ul ? "ul" : "ol";
        if (list !== tag) { close(); out.push("<" + tag + ">"); list = tag; }
        out.push("<li>" + inline((ul || ol)[1]) + "</li>");
      } else if (!line.trim()) {
        flush(); close();
      } else {
        close(); para.push(line);
      }
      i++;
    }
    flush(); close();
    return out.join("");
  }
  window.BatonChat = { esc: esc, md: md };

  // Agent switcher: the page is drawn for one agent; switching reloads it.
  var sw = document.querySelector("[data-agent-switch]");
  if (!sw) return;
  var params = new URLSearchParams(location.search);
  // ?model= comes back from starting a local model here: that is the local chat.
  var wanted = params.get("agent") || (params.get("model") ? "local" : "");
  try {
    var last = localStorage.getItem("baton-chat-agent");
    if (!wanted && last && last !== sw.value) { location.replace("/chat?agent=" + encodeURIComponent(last)); return; }
    localStorage.setItem("baton-chat-agent", sw.value);
  } catch (e) {}
  sw.addEventListener("change", function () {
    try { localStorage.setItem("baton-chat-agent", sw.value); } catch (e) {}
    location.href = "/chat?agent=" + encodeURIComponent(sw.value);
  });
})();
