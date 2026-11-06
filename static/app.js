(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const thread = $("thread"), promptEl = $("prompt"), sendBtn = $("send");
  const tierEl = $("tier"), rulesEl = $("rules"), gate = $("gate");
  const pwdEl = $("pwd"), gateMsg = $("gateMsg");
  const box = $("box"), filesEl = $("files"), fileErr = $("fileErr"), fileInput = $("fileInput");

  let turns = [];
  let pending = []; // fichiers joints au prochain message : { name, media_type, data, size }
  let busy = false;
  let ctl = null;
  let locked = false;

  const SUGGESTIONS = [
    "Explique-moi le RAG en trois phrases.",
    "Écris une fonction Python qui dédoublonne une liste en gardant l’ordre.",
    "Donne-moi cinq idées de noms pour une boutique en ligne.",
  ];

  // ------------------------------------------------ préférences locales
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
    del: (k) => { try { localStorage.removeItem(k); } catch {} },
  };
  if (store.get("pc-rules")) { rulesEl.value = store.get("pc-rules"); $("rulesBox").open = true; }
  if (store.get("pc-tier")) tierEl.value = store.get("pc-tier");
  rulesEl.addEventListener("input", () => store.set("pc-rules", rulesEl.value));
  tierEl.addEventListener("change", () => store.set("pc-tier", tierEl.value));

  // ------------------------------------------------ markdown minimal
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

  function blocks(text) {
    let out = "", para = [], list = null;
    const flushPara = () => { if (para.length) { out += "<p>" + inline(para.join("\n")).replace(/\n/g, "<br>") + "</p>"; para = []; } };
    const flushList = () => { if (list) { out += `<${list.type}>` + list.items.map((x) => "<li>" + inline(x) + "</li>").join("") + `</${list.type}>`; list = null; } };
    for (const line of text.split("\n")) {
      const h = line.match(/^#{1,4}\s+(.*)/);
      const ul = line.match(/^\s*[-*]\s+(.*)/);
      const ol = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (h) { flushPara(); flushList(); out += "<h3>" + inline(h[1]) + "</h3>"; }
      else if (ul || ol) {
        flushPara();
        const type = ul ? "ul" : "ol";
        if (!list || list.type !== type) { flushList(); list = { type, items: [] }; }
        list.items.push((ul || ol)[1]);
      }
      else if (!line.trim()) { flushPara(); flushList(); }
      else { flushList(); para.push(line); }
    }
    flushPara(); flushList();
    return out;
  }

  function md(src) {
    return src.split("```").map((part, i) =>
      i % 2 ? "<pre><code>" + esc(part.replace(/^[\w+-]*\n/, "").replace(/\n$/, "")) + "</code></pre>" : blocks(part)
    ).join("");
  }

  // ------------------------------------------------ affichage
  function renderEmpty() {
    thread.innerHTML = "";
    const box = document.createElement("div");
    box.className = "empty";
    const p = document.createElement("p");
    p.textContent = "Pas encore de message. Essaie par exemple :";
    const chips = document.createElement("div");
    chips.className = "chips";
    for (const s of SUGGESTIONS) {
      const b = document.createElement("button");
      b.className = "chip"; b.type = "button"; b.textContent = s;
      b.onclick = () => { promptEl.value = s; autosize(); updateBtn(); promptEl.focus(); };
      chips.appendChild(b);
    }
    box.append(p, chips);
    thread.appendChild(box);
  }

  function addYou(text, files) {
    thread.querySelector(".empty")?.remove();
    const d = document.createElement("div");
    d.className = "msg you";
    if (files.length) {
      const list = document.createElement("div");
      list.className = "sent-files";
      for (const f of files) {
        const s = document.createElement("span");
        s.textContent = "📄 " + f.name;
        list.appendChild(s);
      }
      d.appendChild(list);
    }
    if (text) d.appendChild(document.createTextNode(text));
    thread.appendChild(d);
  }

  function addClaude() {
    const el = document.createElement("div");
    el.className = "msg claude";
    const body = document.createElement("div");
    body.className = "body";
    body.innerHTML = '<span class="thinking">Claude réfléchit…</span>';
    const meta = document.createElement("div");
    meta.className = "meta hidden";
    el.append(body, meta);
    thread.appendChild(el);
    return { el, body, meta };
  }

  function addCopy(view, text) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = "Copier";
    b.onclick = () => navigator.clipboard.writeText(text).then(() => {
      b.textContent = "Copié"; setTimeout(() => (b.textContent = "Copier"), 1500);
    }).catch(() => {});
    view.meta.appendChild(b);
    view.meta.classList.remove("hidden");
  }

  function addNote(view, text) {
    const s = document.createElement("span");
    s.textContent = text;
    view.meta.appendChild(s);
    view.meta.classList.remove("hidden");
  }

  function addError(view, text) {
    const e = document.createElement("div");
    e.className = "err";
    e.textContent = text;
    view.el.appendChild(e);
  }

  function updateBtn() {
    if (busy) {
      sendBtn.textContent = "Arrêter"; sendBtn.classList.add("stop"); sendBtn.disabled = false;
    } else {
      sendBtn.textContent = "Envoyer"; sendBtn.classList.remove("stop");
      sendBtn.disabled = locked || (!promptEl.value.trim() && !pending.length);
    }
  }

  function autosize() {
    promptEl.style.height = "auto";
    promptEl.style.height = Math.min(promptEl.scrollHeight, window.innerHeight * 0.4) + "px";
  }

  function showGate(msg) {
    locked = true;
    gateMsg.textContent = msg || "Cette app est protégée. Entre le mot de passe pour l’utiliser.";
    gate.classList.remove("hidden");
    pwdEl.focus();
    updateBtn();
  }

  // ------------------------------------------------ envoi
  async function send() {
    if (busy) { ctl?.abort(); return; }
    const text = promptEl.value.trim();
    if ((!text && !pending.length) || locked) return;

    const files = pending;
    addYou(text, files);
    promptEl.value = ""; autosize();
    pending = []; renderFiles(); showFileError("");
    const turn = { role: "user", content: text };
    if (files.length) turn.attachments = files.map(({ name, media_type, data }) => ({ name, media_type, data }));
    turns.push(turn);

    const view = addClaude();
    view.el.scrollIntoView({ block: "nearest" });
    busy = true; ctl = new AbortController(); updateBtn();

    let full = "";
    let failed = null;
    let stopReason = null;

    try {
      const headers = { "Content-Type": "application/json" };
      const pwd = store.get("pc-pwd");
      if (pwd) headers["X-App-Password"] = pwd;

      const res = await fetch("/api/chat", {
        method: "POST",
        headers,
        signal: ctl.signal,
        body: JSON.stringify({ messages: turns, system: rulesEl.value || null, mode: tierEl.value }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        const detail = typeof data.detail === "string" ? data.detail
          : Array.isArray(data.detail) && data.detail[0]?.msg ? data.detail[0].msg.replace(/^Value error, /, "")
          : res.status === 413 ? "Fichiers trop lourds."
          : "La demande a été refusée par le serveur.";
        if (res.status === 401) { store.del("pc-pwd"); showGate(detail); }
        throw new Error(detail);
      }

      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n")) >= 0) {
          const raw = buf.slice(0, i); buf = buf.slice(i + 1);
          if (!raw.trim()) continue;
          const ev = JSON.parse(raw);
          if (ev.type === "text") { full += ev.text; view.body.innerHTML = md(full); }
          else if (ev.type === "done") { stopReason = ev.stop_reason; }
          else if (ev.type === "error") { failed = ev.message; }
        }
      }
    } catch (e) {
      if (e.name === "AbortError") failed = "stopped";
      else failed = e.message || "Impossible de joindre le serveur.";
    }

    if (full) {
      view.body.innerHTML = md(full);
      turns.push({ role: "assistant", content: full });
      addCopy(view, full);
      if (stopReason === "max_tokens") addNote(view, "Réponse coupée : demande moins à la fois.");
    } else {
      turns.pop(); // pas de réponse : le message ne reste pas dans le contexte
      if (files.length && !pending.length) { pending = files; renderFiles(); } // on rend les fichiers pour réessayer
      view.body.innerHTML = failed === "stopped" ? '<span class="thinking">Arrêté.</span>' : "";
    }
    if (failed && failed !== "stopped") addError(view, failed);

    busy = false; ctl = null; updateBtn();
  }

  // ------------------------------------------------ fichiers joints
  const MAX_TOTAL = 20 * 1000 * 1000; // doit correspondre à MAX_UPLOAD_MB côté serveur
  const MAX_FILES = 10;

  const fmtSize = (n) => n < 1000 ? n + " o" : n < 1e6 ? Math.round(n / 1000) + " Ko" : (n / 1e6).toFixed(1) + " Mo";

  function showFileError(msg) {
    fileErr.textContent = msg;
    fileErr.classList.toggle("hidden", !msg);
  }

  function renderFiles() {
    filesEl.innerHTML = "";
    pending.forEach((f, i) => {
      const chip = document.createElement("span");
      chip.className = "file";
      const name = document.createElement("span");
      name.className = "fname"; name.textContent = f.name; name.title = f.name;
      const size = document.createElement("span");
      size.className = "fsize"; size.textContent = fmtSize(f.size);
      const rm = document.createElement("button");
      rm.type = "button"; rm.textContent = "✕"; rm.setAttribute("aria-label", "Retirer " + f.name);
      rm.onclick = () => { pending.splice(i, 1); renderFiles(); updateBtn(); };
      chip.append(name, size, rm);
      filesEl.appendChild(chip);
    });
  }

  function readBase64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    });
  }

  async function addFiles(list) {
    showFileError("");
    // Les fichiers déjà envoyés restent dans la conversation et comptent dans la limite.
    const sent = turns.reduce((n, t) => n + (t.attachments || []).reduce((m, a) => m + a.data.length * 3 / 4, 0), 0);
    let total = sent + pending.reduce((n, f) => n + f.size, 0);
    for (const file of list) {
      if (pending.length >= MAX_FILES) { showFileError(`${MAX_FILES} fichiers maximum par message.`); break; }
      if (!file.size) { showFileError(`« ${file.name} » est vide.`); continue; }
      if (total + file.size > MAX_TOTAL) {
        showFileError(`« ${file.name} » dépasse la limite de ${fmtSize(MAX_TOTAL)} de fichiers par conversation.`);
        continue;
      }
      try {
        const data = await readBase64(file);
        pending.push({ name: file.name, media_type: file.type || "", data, size: file.size });
        total += file.size;
      } catch {
        showFileError(`Impossible de lire « ${file.name} ».`);
      }
    }
    renderFiles(); updateBtn(); promptEl.focus();
  }

  $("attach").addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => { addFiles([...fileInput.files]); fileInput.value = ""; });

  let dragDepth = 0;
  box.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; box.classList.add("dragging"); });
  box.addEventListener("dragover", (e) => e.preventDefault());
  box.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; box.classList.remove("dragging"); } });
  box.addEventListener("drop", (e) => {
    e.preventDefault(); dragDepth = 0; box.classList.remove("dragging");
    if (e.dataTransfer?.files?.length) addFiles([...e.dataTransfer.files]);
  });
  // Coller une capture d'écran directement dans la zone de texte
  promptEl.addEventListener("paste", (e) => {
    const imgs = [...(e.clipboardData?.files || [])];
    if (imgs.length) { e.preventDefault(); addFiles(imgs); }
  });

  // ------------------------------------------------ événements
  sendBtn.addEventListener("click", send);
  promptEl.addEventListener("input", () => { autosize(); updateBtn(); });
  promptEl.addEventListener("keydown", (e) => {
    // Entrée envoie ; Maj+Entrée ajoute une ligne. On ignore Entrée pendant la
    // composition (accents, saisie asiatique) pour ne pas envoyer trop tôt.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $("newChat").addEventListener("click", () => {
    ctl?.abort();
    turns = []; pending = []; renderFiles(); showFileError(""); renderEmpty(); updateBtn(); promptEl.focus();
  });

  function unlock() {
    const v = pwdEl.value;
    if (!v) return;
    store.set("pc-pwd", v);
    pwdEl.value = "";
    gate.classList.add("hidden");
    locked = false;
    updateBtn();
    promptEl.focus();
  }
  $("pwdBtn").addEventListener("click", unlock);
  pwdEl.addEventListener("keydown", (e) => { if (e.key === "Enter") unlock(); });

  renderEmpty();
  updateBtn();

  fetch("/api/config").then((r) => r.json()).then((cfg) => {
    if (cfg.password_required && !store.get("pc-pwd")) showGate();
  }).catch(() => {});
})();
