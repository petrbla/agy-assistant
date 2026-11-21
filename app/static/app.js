(() => {
  const $ = (id) => document.getElementById(id);
  const EFFORTS = ["low", "medium", "high", "xhigh"];
  const EFFORT_LABELS = { low: "Low", medium: "Medium", high: "High", xhigh: "XHigh" };

  let activeTab = "chat";
  let activeChannel = "auto";
  let currentModel = "";
  let modelGroups = {};
  let pending2faChallenge = "";
  let latestRecoveryCodes = [];
  let allFiles = [];
  let currentFile = null;
  let fileDirty = false;
  let isSending = false;
  let authPollTimer = null;
  const chatLogs = { auto: [], pracovni: [], osobni: [] };

  // Motiv (tmavý / světlý)
  function applyTheme(t) {
    const theme = t === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("agy_theme", theme);
    $("themeBtn").textContent = theme === "dark" ? "☀️" : "🌙";
  }
  applyTheme(localStorage.getItem("agy_theme") || "dark");
  $("themeBtn").onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme");
    applyTheme(cur === "dark" ? "light" : "dark");
  };

  // Toast
  let toastTimer = null;
  function toast(msg, kind = "ok") {
    const el = $("toast");
    el.textContent = msg;
    el.className = `toast show ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3600);
  }

  function getHeaders() {
    const h = { "Content-Type": "application/json" };
    const tok = (localStorage.getItem("agy_web_token") || "").trim();
    if (tok) h.Authorization = `Bearer ${tok}`;
    return h;
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, {
      ...opts,
      headers: { ...getHeaders(), ...(opts.headers || {}) },
    });
    if (res.status === 401) {
      localStorage.removeItem("agy_web_token");
      await loadAuthAndBootstrap();
      throw new Error("Vyžadováno přihlášení.");
    }
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (_) {}
    if (!res.ok) {
      throw new Error(data.detail || text || `HTTP ${res.status}`);
    }
    return data;
  }

  function fmtInt(n) {
    return Number(n || 0).toLocaleString("cs-CZ");
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function renderMarkdown(text) {
    const stash = [];
    let s = String(text || "").replace(/```([a-zA-Z0-9_+-]*)\n([\s\S]*?)```/g, (_, _l, code) => {
      const idx = stash.push(`<pre><code>${escapeHtml(code.trimEnd())}</code></pre>`) - 1;
      return `\x00BLK${idx}\x00`;
    });
    s = s.replace(/`([^`\n]+)`/g, (_, code) => {
      const idx = stash.push(`<code>${escapeHtml(code)}</code>`) - 1;
      return `\x00BLK${idx}\x00`;
    });
    s = escapeHtml(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(?<!\*)\*([^*\n]+?)\*(?!\*)/g, "<em>$1</em>");
    stash.forEach((html, i) => { s = s.replace(`\x00BLK${i}\x00`, html); });
    return s;
  }

  function renderQr(container, uri) {
    if (typeof window.qrcode === "function") {
      const qr = window.qrcode(0, "M");
      qr.addData(uri);
      qr.make();
      container.innerHTML = qr.createSvgTag({ scalable: true, margin: 2 });
      const svg = container.querySelector("svg");
      if (svg) { svg.style.width = "100%"; svg.style.height = "100%"; }
    } else {
      container.textContent = "QR knihovna chybí";
    }
  }

  // Navigace
  function switchTab(tab) {
    activeTab = tab;
    document.querySelectorAll(".nav-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === tab);
    });
    ["chat", "memory", "ops", "settings"].forEach((t) => {
      $(`tab-${t}`).classList.toggle("hidden", t !== tab);
    });
    if (tab === "memory") loadFiles();
    if (tab === "ops") loadOps();
    if (tab === "settings") { loadSettings(); loadMcp(); }
  }
  document.querySelectorAll(".nav-tab").forEach((b) => {
    b.onclick = () => switchTab(b.dataset.tab);
  });

  function showAuthScreen(mode) {
    $("authScreen").classList.remove("hidden");
    $("appMain").classList.add("hidden");
    $("logoutBtn").classList.add("hidden");
    document.querySelectorAll(".nav-tab").forEach((b) => { b.disabled = true; });
    $("initialSetupCard").classList.toggle("hidden", mode !== "initial");
    $("loginCard").classList.toggle("hidden", mode !== "login");
  }

  function showAppMain() {
    $("authScreen").classList.add("hidden");
    $("appMain").classList.remove("hidden");
    document.querySelectorAll(".nav-tab").forEach((b) => { b.disabled = false; });
    $("logoutBtn").classList.toggle("hidden", !localStorage.getItem("agy_web_token"));
  }

  function showAuthErr(msg) {
    const el = $("authError");
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  // Autentizace
  async function loadAuthAndBootstrap() {
    showAuthErr("");
    try {
      const st = await fetch("/api/auth/state").then((r) => r.json());
      $("brandTitle").textContent = st.assistant_name || "Asistent Antigravity";
      $("tokenLoginWrap").classList.toggle("hidden", !st.has_token_configured);
      if (st.needs_initial_setup) {
        $("statusPill").textContent = "První nastavení";
        $("statusPill").className = "pill warn";
        showAuthScreen("initial");
        return;
      }
      const statusData = await api("/api/status");
      showAppMain();
      applyStatus(statusData);
      await loadModels(false);
      renderChat();
    } catch (_) {
      $("statusPill").textContent = "Nepřihlášen";
      $("statusPill").className = "pill";
      showAuthScreen("login");
    }
  }

  $("initialSetupForm").onsubmit = async (e) => {
    e.preventDefault();
    showAuthErr("");
    const pw = $("initPassword").value;
    if (pw !== $("initPassword2").value) return showAuthErr("Hesla se neshodují.");
    try {
      const res = await fetch("/api/auth/initial-setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: $("initEmail").value.trim(),
          username: $("initUsername").value.trim(),
          password: pw,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return showAuthErr(data.detail || "Nastavení selhalo.");
      localStorage.setItem("agy_web_token", data.session_token);
      toast("Účet byl nastaven.");
      await loadAuthAndBootstrap();
    } catch (err) {
      showAuthErr(err.message);
    }
  };

  $("loginStep1Form").onsubmit = async (e) => {
    e.preventDefault();
    showAuthErr("");
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          identifier: $("loginIdent").value.trim(),
          password: $("loginPassword").value,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return showAuthErr(data.detail || "Přihlášení selhalo.");
      if (data.requires_2fa) {
        pending2faChallenge = data.challenge_token;
        $("loginStep1Form").classList.add("hidden");
        $("loginStep2Form").classList.remove("hidden");
        $("login2faCode").value = "";
        $("login2faCode").focus();
        return;
      }
      localStorage.setItem("agy_web_token", data.session_token);
      $("loginPassword").value = "";
      toast("Přihlášeno.");
      await loadAuthAndBootstrap();
    } catch (err) {
      showAuthErr(err.message);
    }
  };

  $("loginStep2Form").onsubmit = async (e) => {
    e.preventDefault();
    showAuthErr("");
    try {
      const res = await fetch("/api/login/verify-2fa", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          challenge_token: pending2faChallenge,
          code: $("login2faCode").value.trim(),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return showAuthErr(data.detail || "Neplatný 2FA kód.");
      localStorage.setItem("agy_web_token", data.session_token);
      $("loginStep2Form").classList.add("hidden");
      $("loginStep1Form").classList.remove("hidden");
      toast("Přihlášeno s 2FA.");
      await loadAuthAndBootstrap();
    } catch (err) {
      showAuthErr(err.message);
    }
  };

  $("login2faBackBtn").onclick = () => {
    pending2faChallenge = "";
    $("loginStep2Form").classList.add("hidden");
    $("loginStep1Form").classList.remove("hidden");
  };

  $("loginTokenBtn").onclick = async () => {
    showAuthErr("");
    const tok = $("loginWebToken").value.trim();
    if (!tok) return showAuthErr("Zadejte Web Token.");
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ web_token: tok }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return showAuthErr(data.detail || "Neplatný token.");
      localStorage.setItem("agy_web_token", data.session_token);
      $("loginWebToken").value = "";
      toast("Přihlášeno tokenem.");
      await loadAuthAndBootstrap();
    } catch (err) {
      showAuthErr(err.message);
    }
  };

  $("logoutBtn").onclick = async () => {
    try { await fetch("/api/logout", { method: "POST", headers: getHeaders() }); } catch (_) {}
    localStorage.removeItem("agy_web_token");
    await loadAuthAndBootstrap();
  };

  function applyStatus(s) {
    $("brandTitle").textContent = s.assistant_name || "Jarvis";
    const dot = $("brandDot");
    const pill = $("statusPill");
    if (!s.agy_available) {
      dot.className = "brand-dot err";
      pill.className = "pill err";
      pill.textContent = "agy nedostupné";
    } else if (!s.agy_authenticated) {
      dot.className = "brand-dot warn";
      pill.className = "pill warn";
      pill.textContent = `${s.agy_version || "agy"} • nepřihlášeno`;
    } else {
      dot.className = "brand-dot ok";
      pill.className = "pill ok";
      pill.textContent = s.agy_version ? `agy ${s.agy_version}` : "Online";
    }
    updateAgyBadges(s);
    updateSecurityUi(s);
  }

  function updateAgyBadges(info) {
    const authBadge = $("agyAuthBadge");
    const logoutBtn = $("logoutAgyBtn");
    const parts = [];
    if (info.has_agy_oauth) parts.push("OAuth");
    if (info.has_gemini_api_key) parts.push("API klíč");
    if (parts.length) {
      authBadge.textContent = `Přihlášeno (${parts.join(" + ")})`;
      authBadge.className = "pill ok";
      logoutBtn.classList.remove("hidden");
    } else if (typeof info.has_agy_oauth === "boolean") {
      authBadge.textContent = "Nepřihlášeno";
      authBadge.className = "pill err";
      logoutBtn.classList.add("hidden");
    }

    const verBadge = $("agyVerBadge");
    const updBtn = $("updateAgyBtn");
    if (info.agy_update_available && info.agy_latest_version) {
      verBadge.textContent = `Dostupná ${info.agy_latest_version}`;
      verBadge.className = "pill warn";
      updBtn.classList.remove("hidden");
    } else if (info.agy_up_to_date === true) {
      verBadge.textContent = `${info.agy_version} (aktuální)`;
      verBadge.className = "pill ok";
      updBtn.classList.add("hidden");
    } else if (info.agy_version) {
      verBadge.textContent = info.agy_version;
      verBadge.className = "pill";
      updBtn.classList.add("hidden");
    }
  }

  // Modely & váha
  function splitModel(full) {
    const s = (full || "").trim();
    for (const e of EFFORTS) {
      if (s.endsWith(`-${e}`) && s.length > e.length + 1) {
        return [s.slice(0, -(e.length + 1)), e];
      }
    }
    return [s, ""];
  }

  async function loadModels(refresh = false) {
    try {
      const data = await api(`/api/models?refresh=${refresh}`);
      currentModel = data.current_model || "";
      const list = data.models || [];
      modelGroups = {};
      list.forEach((m) => {
        const [base, eff] = splitModel(m);
        if (!base) return;
        if (!modelGroups[base]) modelGroups[base] = [];
        if (!modelGroups[base].includes(eff)) modelGroups[base].push(eff);
      });
      const sel = $("chatModelSelect");
      const dl = $("modelsDatalist");
      sel.innerHTML = `<option value="">Výchozí (agy)</option>`;
      dl.innerHTML = "";
      list.forEach((m) => {
        const opt = document.createElement("option");
        opt.value = m;
        dl.appendChild(opt);
      });
      Object.keys(modelGroups).forEach((base) => {
        const opt = document.createElement("option");
        opt.value = base;
        opt.textContent = base;
        sel.appendChild(opt);
      });
      const [curBase, curEff] = splitModel(currentModel);
      if (curBase && !modelGroups[curBase]) {
        const opt = document.createElement("option");
        opt.value = curBase;
        opt.textContent = curBase;
        sel.appendChild(opt);
      }
      sel.value = curBase;
      syncEffortUi(curBase, curEff);
    } catch (_) {}
  }

  function syncEffortUi(base, eff) {
    const effs = (modelGroups[base] || []).filter(Boolean);
    const ordered = EFFORTS.filter((e) => effs.includes(e));
    const wrap = $("effortWrap");
    if (!base || ordered.length < 2) {
      wrap.classList.add("hidden");
      return;
    }
    wrap.classList.remove("hidden");
    const range = $("effortRange");
    range.min = "0";
    range.max = String(ordered.length - 1);
    const idx = Math.max(0, ordered.indexOf(eff || "medium"));
    range.value = String(idx);
    const activeEff = ordered[idx];
    $("effortLabel").textContent = EFFORT_LABELS[activeEff] || activeEff;
    range.oninput = () => {
      const e = ordered[Number(range.value)];
      $("effortLabel").textContent = EFFORT_LABELS[e] || e;
    };
    range.onchange = async () => {
      const e = ordered[Number(range.value)];
      await setActiveModel(`${base}-${e}`);
    };
  }

  async function setActiveModel(fullModel) {
    currentModel = fullModel;
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify({ agy_model: fullModel }) });
      toast(`Model: ${fullModel || "výchozí (agy)"}`);
    } catch (err) {
      toast(err.message, "err");
    }
  }

  $("chatModelSelect").onchange = async () => {
    const base = $("chatModelSelect").value;
    if (!base) {
      syncEffortUi("", "");
      await setActiveModel("");
      return;
    }
    const effs = (modelGroups[base] || []).filter(Boolean);
    const chosen = effs.includes("medium") ? `${base}-medium` : effs[0] ? `${base}-${effs[0]}` : base;
    const [, eff] = splitModel(chosen);
    syncEffortUi(base, eff);
    await setActiveModel(chosen);
  };

  // Chat
  function renderChat() {
    const box = $("chatMessages");
    box.innerHTML = "";
    const msgs = chatLogs[activeChannel] || [];
    if (!msgs.length) {
      const empty = document.createElement("div");
      empty.className = "msg meta";
      empty.textContent = `Nová konverzace v kanálu „${activeChannel}“. Napiš první zprávu.`;
      box.appendChild(empty);
      return;
    }
    msgs.forEach((m) => {
      const div = document.createElement("div");
      div.className = `msg ${m.cls}`;
      if (m.cls === "assistant") div.innerHTML = renderMarkdown(m.text);
      else div.textContent = m.text;
      box.appendChild(div);
    });
    box.scrollTop = box.scrollHeight;
  }

  function appendMsg(text, cls) {
    chatLogs[activeChannel].push({ text, cls });
    renderChat();
  }

  document.querySelectorAll("#channelSeg button").forEach((btn) => {
    btn.onclick = () => {
      activeChannel = btn.dataset.channel;
      document.querySelectorAll("#channelSeg button").forEach((b) => b.classList.toggle("active", b === btn));
      renderChat();
    };
  });

  $("chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("chatForm").requestSubmit();
    }
  });

  $("chatForm").onsubmit = async (e) => {
    e.preventDefault();
    if (isSending) return;
    const inp = $("chatInput");
    const prompt = inp.value.trim();
    if (!prompt) return;
    inp.value = "";
    appendMsg(prompt, "user");
    isSending = true;
    $("chatSendBtn").disabled = true;

    const box = $("chatMessages");
    const typing = document.createElement("div");
    typing.className = "msg assistant";
    typing.innerHTML = `<span class="typing-dots"><span></span><span></span><span></span></span>`;
    box.appendChild(typing);
    box.scrollTop = box.scrollHeight;

    try {
      const res = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({
          prompt,
          session_key: `web:${activeChannel}`,
          context_mode: activeChannel,
          model: currentModel || null,
        }),
      });
      typing.remove();
      appendMsg(res.text || "(Prázdná odpověď)", "assistant");
    } catch (err) {
      typing.remove();
      appendMsg(`Chyba: ${err.message}`, "meta");
    } finally {
      isSending = false;
      $("chatSendBtn").disabled = false;
      inp.focus();
    }
  };

  $("chatResetBtn").onclick = async () => {
    try {
      await api("/api/chat/reset", {
        method: "POST",
        body: JSON.stringify({ session_key: `web:${activeChannel}`, context_mode: activeChannel }),
      });
      chatLogs[activeChannel] = [];
      renderChat();
      toast(`Konverzace „${activeChannel}“ byla resetována.`);
    } catch (err) {
      toast(err.message, "err");
    }
  };

  $("chatCleanupBtn").onclick = async () => {
    if (!confirm("Uložit důležité věci z aktivních konverzací do paměti a vyčistit kontext?")) return;
    toast("Provádím úklid paměti…", "warn");
    try {
      const r = await api("/api/nightly-reset/run", { method: "POST" });
      chatLogs.auto = []; chatLogs.pracovni = []; chatLogs.osobni = [];
      renderChat();
      toast(`Úklid hotov: uloženo ${r.flushed_sessions}/${r.reset_sessions} relací.`);
    } catch (err) {
      toast(err.message, "err");
    }
  };

  // Paměť
  async function loadFiles() {
    try {
      allFiles = await api("/api/files");
      renderFileList();
    } catch (err) {
      toast(err.message, "err");
    }
  }

  function renderFileList() {
    const q = ($("fileFilter").value || "").trim().toLowerCase();
    const list = $("fileList");
    list.innerHTML = "";
    allFiles
      .filter((f) => !q || f.path.toLowerCase().includes(q))
      .forEach((f) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "file-item" + (f.path === currentFile ? " active" : "");
        b.textContent = f.path;
        b.onclick = () => openFile(f.path);
        list.appendChild(b);
      });
  }
  $("fileFilter").oninput = renderFileList;
  $("refreshFilesBtn").onclick = loadFiles;

  async function openFile(path) {
    try {
      const data = await api(`/api/files/${encodeURIComponent(path)}`);
      currentFile = path;
      fileDirty = false;
      $("currentFileLabel").textContent = path;
      $("fileDirtyHint").textContent = "Načteno (Ctrl+S uloží změny).";
      $("fileEditor").disabled = false;
      $("fileEditor").value = data.content;
      $("saveFileBtn").disabled = false;
      renderFileList();
    } catch (err) {
      toast(err.message, "err");
    }
  }

  $("fileEditor").oninput = () => {
    fileDirty = true;
    $("fileDirtyHint").textContent = "Neuložené změny…";
  };

  async function saveCurrentFile() {
    if (!currentFile) return;
    try {
      await api(`/api/files/${encodeURIComponent(currentFile)}`, {
        method: "PUT",
        body: JSON.stringify({ content: $("fileEditor").value }),
      });
      fileDirty = false;
      $("fileDirtyHint").textContent = "Uloženo.";
      toast(`Soubor ${currentFile} uložen.`);
    } catch (err) {
      toast(err.message, "err");
    }
  }
  $("saveFileBtn").onclick = saveCurrentFile;
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s" && activeTab === "memory" && currentFile) {
      e.preventDefault();
      saveCurrentFile();
    }
  });

  $("resetWorkspaceBtn").onclick = async () => {
    if (!confirm("Obnovit výchozí šablony paměti a resetovat všechny konverzace?")) return;
    try {
      await api("/api/workspace/reset", { method: "POST" });
      currentFile = null;
      $("currentFileLabel").textContent = "Vyberte soubor";
      $("fileEditor").value = "";
      $("fileEditor").disabled = true;
      $("saveFileBtn").disabled = true;
      await loadFiles();
      toast("Paměť obnovena ze šablon.");
    } catch (err) {
      toast(err.message, "err");
    }
  };

  // Plánovač & Historie
  function renderKpi(valId, subId, st) {
    const tilde = st.estimated_runs > 0 && st.exact_runs === 0 ? "~" : "";
    $(valId).textContent = `${tilde}${fmtInt(st.total_tokens)} tok.`;
    $(subId).textContent = `${fmtInt(st.total_runs)} běhů • vstup ${fmtInt(st.input_tokens)} / výstup ${fmtInt(st.output_tokens)}`;
  }

  const DOW = ["ne", "po", "út", "st", "čt", "pá", "so", "ne"];

  function fmtNextRun(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const time = d.toLocaleTimeString("cs-CZ", { hour: "numeric", minute: "2-digit" });
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const day = new Date(d);
    day.setHours(0, 0, 0, 0);
    const diff = Math.round((day - today) / 86400000);
    if (diff === 0) return `dnes ${time}`;
    if (diff === 1) return `zítra ${time}`;
    if (diff > 1 && diff < 7) return `${DOW[d.getDay()]} ${time}`;
    const sameYear = d.getFullYear() === today.getFullYear();
    return `${d.getDate()}. ${d.getMonth() + 1}.${sameYear ? "" : " " + d.getFullYear()} ${time}`;
  }

  function humanDow(f) {
    if (f === "*") return "";
    if (f === "1-5") return "po–pá";
    if (f === "0,6" || f === "6,0" || f === "6-7" || f === "sat,sun") return "so–ne";
    return f.split(",").map((p) => p.includes("-")
      ? p.split("-").map((x) => DOW[+x] ?? x).join("–")
      : DOW[+p] ?? p).join(", ");
  }

  function humanSchedule(t) {
    if (t.schedule_type === "once") {
      return t.run_at ? "jednou " + fmtNextRun(t.run_at) : "jednou";
    }
    const parts = (t.cron_expr || "").trim().split(/\s+/);
    if (parts.length !== 5) return t.cron_expr;
    const [mi, h, dom, mon, dow] = parts;
    if (!/^\d+$/.test(mi) || !/^\d+$/.test(h)) return t.cron_expr;
    const time = `${+h}:${mi.padStart(2, "0")}`;
    if (dom === "*" && mon === "*") {
      return dow === "*" ? `denně ${time}` : `${humanDow(dow)} ${time}`;
    }
    if (/^\d+$/.test(dom) && /^\d+$/.test(mon) && dow === "*") return `každý ${+dom}. ${+mon}. ${time}`;
    if (/^\d+$/.test(dom) && mon === "*" && dow === "*") return `${+dom}. v měsíci ${time}`;
    return t.cron_expr;
  }

  async function loadOps() {
    try {
      const [s, runs, u1h, u1, u7, uAll, tasks] = await Promise.all([
        api("/api/status"),
        api("/api/runs?limit=40"),
        api("/api/usage?hours=1"),
        api("/api/usage?days=1"),
        api("/api/usage?days=7"),
        api("/api/usage"),
        api("/api/tasks"),
      ]);
      renderKpi("kpi1h", "kpi1hSub", u1h);
      renderKpi("kpi24h", "kpi24hSub", u1);
      renderKpi("kpi7d", "kpi7dSub", u7);
      renderKpi("kpiAll", "kpiAllSub", uAll);
      $("opsSummary").textContent = `Skilly: ${(s.skills || []).join(", ") || "žádné"} • Telegram: ${s.telegram_enabled ? "zapnut" : "vypnut"}`;
      const jobs = s.jobs || [];
      const nextByName = new Map(jobs.map((j) => [j.name, j.next_run_time]));
      const taskNames = new Set(tasks.map((t) => t.name));
      const systemJobs = jobs.filter((j) => !taskNames.has(j.name));
      const jl = $("jobsList");
      jl.innerHTML = "";
      if (systemJobs.length) {
        const label = document.createElement("span");
        label.className = "jobs-label";
        label.textContent = "Systémové úlohy";
        jl.appendChild(label);
        systemJobs
          .sort((a, b) => String(a.next_run_time || "~").localeCompare(String(b.next_run_time || "~")))
          .forEach((j) => {
            const chip = document.createElement("span");
            chip.className = "job-chip";
            chip.innerHTML = `${escapeHtml(j.name)} <span class="job-chip-time">${escapeHtml(fmtNextRun(j.next_run_time))}</span>`;
            chip.title = j.next_run_time || "";
            jl.appendChild(chip);
          });
      }
      jl.classList.toggle("hidden", !systemJobs.length);

      const tb = $("tasksBody");
      tb.innerHTML = "";
      if (!tasks.length) {
        tb.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:18px;">Zatím žádné vlastní úlohy.</td></tr>`;
      }
      const sorted = [...tasks].sort((a, b) => {
        const na = a.enabled ? nextByName.get(a.name) || "~" : "~~";
        const nb = b.enabled ? nextByName.get(b.name) || "~" : "~~";
        return String(na).localeCompare(String(nb));
      });
      sorted.forEach((t) => {
        const tr = document.createElement("tr");
        if (!t.enabled) tr.className = "task-disabled";
        const raw = t.schedule_type === "once" ? t.run_at : t.cron_expr;
        const next = t.enabled ? nextByName.get(t.name) : null;
        const typePill = t.action_type === "agent"
          ? `<span class="pill accent">AI</span>`
          : `<span class="pill">Zpráva</span>`;
        const statePill = t.enabled
          ? `<span class="pill ok">aktivní</span>`
          : `<span class="pill">pozastaveno</span>`;
        tr.innerHTML =
          `<td class="task-name">${escapeHtml(t.name)}</td>` +
          `<td class="nowrap"><div title="${escapeHtml(raw)}">${escapeHtml(humanSchedule(t))}</div>` +
          `<div class="task-next">${next ? "příště " + escapeHtml(fmtNextRun(next)) : "—"}</div></td>` +
          `<td>${typePill}</td>` +
          `<td><div class="task-prompt" title="${escapeHtml(t.prompt)}">${escapeHtml(t.prompt)}</div></td>` +
          `<td>${statePill}</td>`;
        const td = document.createElement("td");
        td.className = "task-actions";
        const mk = (label, cls, title, fn) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "btn btn-sm " + cls;
          b.textContent = label;
          b.title = title;
          b.onclick = async () => {
            try { await fn(); } catch (err) { toast(err.message, "err"); }
            loadOps();
          };
          return b;
        };
        td.append(
          mk("▶", "btn-icon-sm", "Spustit teď", async () => {
            await api(`/api/tasks/${t.id}/run`, { method: "POST" });
            toast(`Úloha „${t.name}“ spuštěna.`);
          }),
          mk(t.enabled ? "⏸" : "⏵", "btn-icon-sm", t.enabled ? "Pozastavit" : "Obnovit", async () => {
            await api(`/api/tasks/${t.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !t.enabled }) });
            toast(t.enabled ? "Úloha pozastavena." : "Úloha obnovena.");
          }),
          mk("✕", "btn-icon-sm btn-danger", "Smazat", async () => {
            if (!confirm(`Smazat úlohu „${t.name}“?`)) throw new Error("Zrušeno.");
            await api(`/api/tasks/${t.id}`, { method: "DELETE" });
            toast("Úloha smazána.");
          }),
        );
        tr.appendChild(td);
        tb.appendChild(tr);
      });

      const rb = $("runsBody");
      rb.innerHTML = "";
      runs.forEach((r) => {
        const tr = document.createElement("tr");
        const dt = r.created_at ? new Date(r.created_at).toLocaleString("cs-CZ") : "";
        const tok = `${r.tokens_estimated ? "~" : ""}${fmtInt(r.total_tokens)}`;
        const dur = `${(r.duration_ms / 1000).toFixed(1)} s`;
        tr.innerHTML = `<td class="mono">${escapeHtml(dt)}</td><td>${escapeHtml(r.source)}</td><td>${escapeHtml(r.context_mode)}</td><td>${escapeHtml((r.prompt || "").slice(0, 90))}</td><td>${escapeHtml((r.response || r.error || "").slice(0, 110))}</td><td class="mono">${tok}</td><td class="mono">${dur}</td>`;
        rb.appendChild(tr);
      });
    } catch (err) {
      toast(err.message, "err");
    }
  }

  $("taskScheduleType").onchange = () => {
    const isOnce = $("taskScheduleType").value === "once";
    $("taskCron").classList.toggle("hidden", isOnce);
    $("taskRunAt").classList.toggle("hidden", !isOnce);
  };

  $("taskForm").onsubmit = async (e) => {
    e.preventDefault();
    const stype = $("taskScheduleType").value;
    try {
      await api("/api/tasks", {
        method: "POST",
        body: JSON.stringify({
          name: $("taskName").value.trim(),
          schedule_type: stype,
          cron_expr: stype === "cron" ? $("taskCron").value.trim() : "",
          run_at: stype === "once" ? $("taskRunAt").value : "",
          action_type: $("taskActionType").value,
          prompt: $("taskPrompt").value.trim(),
          enabled: true,
        }),
      });
      $("taskName").value = "";
      $("taskPrompt").value = "";
      toast("Úloha přidána.");
      loadOps();
    } catch (err) {
      toast(err.message, "err");
    }
  };

  $("refreshOpsBtn").onclick = loadOps;
  $("runHeartbeatBtn").onclick = async () => {
    toast("Spouštím Heartbeat…", "warn");
    const r = await api("/api/heartbeat/run", { method: "POST" });
    toast(r.text || "Heartbeat dokončen.");
    loadOps();
  };
  $("runBriefingBtn").onclick = async () => {
    toast("Generuji Ranní briefing…", "warn");
    const r = await api("/api/briefing/run", { method: "POST" });
    toast("Briefing dokončen.");
    loadOps();
  };
  $("runNightlyBtn").onclick = async () => {
    toast("Spouštím noční úklid…", "warn");
    const r = await api("/api/nightly-reset/run", { method: "POST" });
    toast(`Uloženo ${r.flushed_sessions}/${r.reset_sessions} relací.`);
    loadOps();
  };

  // Nastavení & MCP & OAuth & 2FA
  async function loadSettings() {
    try {
      const s = await api("/api/settings");
      $("cfgAssistantName").value = s.assistant_name || "Jarvis";
      $("cfgTz").value = s.tz || "Europe/Prague";
      $("cfgDefaultContext").value = ["pracovni", "osobni"].includes(s.default_context_mode)
        ? s.default_context_mode
        : "auto";
      $("cfgModel").value = s.agy_model || "";
      $("cfgTgUsers").value = s.telegram_allowed_users || "";
      $("cfgTgNotify").value = s.telegram_notify_chat_id || "";
      $("cfgHbEnabled").checked = Boolean(s.heartbeat_enabled);
      $("cfgHbInterval").value = s.heartbeat_interval_minutes || 60;
      $("cfgBriefEnabled").checked = Boolean(s.morning_briefing_enabled);
      $("cfgBriefCron").value = s.morning_briefing_cron || "0 8 * * *";
      $("cfgNightlyEnabled").checked = s.nightly_reset_enabled !== false;
      $("cfgNightlyCron").value = s.nightly_reset_cron || "15 0 * * *";
      updateAgyBadges(s);
      updateSecurityUi(s);
    } catch (err) {
      toast(err.message, "err");
    }
  }

  $("settingsForm").onsubmit = async (e) => {
    e.preventDefault();
    const body = {
      assistant_name: $("cfgAssistantName").value.trim() || "Jarvis",
      tz: $("cfgTz").value.trim() || "Europe/Prague",
      default_context_mode: $("cfgDefaultContext").value,
      agy_model: $("cfgModel").value.trim(),
      telegram_allowed_users: $("cfgTgUsers").value.trim(),
      telegram_notify_chat_id: $("cfgTgNotify").value.trim(),
      heartbeat_enabled: $("cfgHbEnabled").checked,
      heartbeat_interval_minutes: Number($("cfgHbInterval").value || 60),
      morning_briefing_enabled: $("cfgBriefEnabled").checked,
      morning_briefing_cron: $("cfgBriefCron").value.trim() || "0 8 * * *",
      nightly_reset_enabled: $("cfgNightlyEnabled").checked,
      nightly_reset_cron: $("cfgNightlyCron").value.trim() || "15 0 * * *",
    };
    const tgTok = $("cfgTgToken").value.trim();
    if (tgTok) body.telegram_bot_token = tgTok;
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
      $("cfgTgToken").value = "";
      toast("Nastavení uloženo a aktivováno.");
      await loadAuthAndBootstrap();
    } catch (err) {
      toast(err.message, "err");
    }
  };

  $("geminiKeyForm").onsubmit = async (e) => {
    e.preventDefault();
    const key = $("cfgGeminiKey").value.trim();
    if (!key) return toast("Zadejte klíč.", "warn");
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify({ gemini_api_key: key }) });
      $("cfgGeminiKey").value = "";
      toast("Gemini API klíč uložen.");
      await loadSettings();
      await loadModels(true);
    } catch (err) {
      toast(err.message, "err");
    }
  };

  $("clearGeminiKeyBtn").onclick = async () => {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ gemini_api_key: "" }) });
    toast("Gemini API klíč smazán.");
    await loadSettings();
  };

  // OAuth PTY
  function renderOauthState(st) {
    $("oauthOutput").textContent = st.error ? `Chyba: ${st.error}` : (st.output || (st.running ? "Spouštím…" : "Připraveno."));
    $("oauthOutput").scrollTop = $("oauthOutput").scrollHeight;
    const urls = $("oauthUrls");
    urls.innerHTML = "";
    (st.urls || []).forEach((u, i) => {
      const a = document.createElement("a");
      a.href = u;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.className = "btn btn-primary btn-sm";
      a.textContent = `Otevřít přihlašovací URL ${i + 1}`;
      urls.appendChild(a);
    });
    if (!st.running && authPollTimer) {
      clearInterval(authPollTimer);
      authPollTimer = null;
      loadSettings();
      loadModels(true);
    }
  }

  $("startOauthBtn").onclick = async () => {
    try {
      const st = await api("/api/auth/start", { method: "POST", body: JSON.stringify({ mode: "interactive" }) });
      renderOauthState(st);
      if (authPollTimer) clearInterval(authPollTimer);
      authPollTimer = setInterval(async () => {
        try {
          const cur = await api("/api/auth/status");
          renderOauthState(cur);
          updateAgyBadges(cur);
        } catch (_) {}
      }, 1000);
    } catch (err) {
      toast(err.message, "err");
    }
  };
  $("oauthEnterBtn").onclick = async () => renderOauthState(await api("/api/auth/input", { method: "POST", body: JSON.stringify({ value: "" }) }));
  $("stopOauthBtn").onclick = async () => { await api("/api/auth/stop", { method: "POST" }); renderOauthState(await api("/api/auth/status")); };
  $("oauthInputForm").onsubmit = async (e) => {
    e.preventDefault();
    const v = $("oauthCodeInput").value.trim();
    $("oauthCodeInput").value = "";
    renderOauthState(await api("/api/auth/input", { method: "POST", body: JSON.stringify({ value: v }) }));
  };
  $("logoutAgyBtn").onclick = async () => {
    if (!confirm("Odhlásit se z Antigravity (smaže OAuth i Gemini API klíč)?")) return;
    await api("/api/auth/logout", { method: "POST" });
    toast("Odhlášeno z Antigravity.");
    await loadSettings();
  };
  $("checkVerBtn").onclick = async () => {
    const info = await api("/api/agy/version?refresh=true");
    updateAgyBadges(info);
    toast(info.agy_update_available ? `Dostupná nová verze ${info.agy_latest_version}` : `agy ${info.agy_version || "nedostupné"}`);
  };
  $("updateAgyBtn").onclick = async () => {
    if (!confirm("Aktualizovat agy CLI na nejnovější verzi?")) return;
    toast("Aktualizuji agy…", "warn");
    try {
      const res = await api("/api/agy/update", { method: "POST" });
      updateAgyBadges(res);
      toast(`Aktualizováno na ${res.agy_version || "OK"}.`);
    } catch (err) {
      toast(err.message, "err");
    }
  };

  // MCP
  $("mcpTransport").onchange = () => {
    const isStdio = $("mcpTransport").value === "stdio";
    $("mcpUrlWrap").classList.toggle("hidden", isStdio);
    $("mcpTokenWrap").classList.toggle("hidden", isStdio);
    $("mcpCmdWrap").classList.toggle("hidden", !isStdio);
    $("mcpArgsWrap").classList.toggle("hidden", !isStdio);
  };

  async function loadMcp() {
    try {
      const items = await api("/api/mcp");
      const tb = $("mcpBody");
      tb.innerHTML = "";
      const labels = { all: "Všechny", pracovni: "Pracovní", osobni: "Osobní" };
      items.forEach((srv) => {
        const tr = document.createElement("tr");
        const target = srv.transport === "stdio" ? `${srv.command} ${(srv.args || []).join(" ")}` : srv.url;
        tr.innerHTML = `<td><strong>${escapeHtml(srv.name)}</strong></td><td>${labels[srv.scope] || "Všechny"}</td><td class="mono">${escapeHtml(target)}</td>`;
        const td = document.createElement("td");
        const del = document.createElement("button");
        del.className = "btn btn-sm btn-danger";
        del.textContent = "Smazat";
        del.onclick = async () => {
          await api(`/api/mcp/${srv.id}`, { method: "DELETE" });
          toast("MCP server smazán.");
          loadMcp();
        };
        td.appendChild(del);
        tr.appendChild(td);
        tb.appendChild(tr);
      });
    } catch (_) {}
  }

  $("mcpForm").onsubmit = async (e) => {
    e.preventDefault();
    const tr = $("mcpTransport").value;
    const body = {
      name: $("mcpName").value.trim(),
      scope: $("mcpScope").value,
      transport: tr,
      enabled: $("mcpEnabled").checked,
    };
    try {
      if (tr === "stdio") {
        body.command = $("mcpCommand").value.trim();
        const raw = $("mcpArgs").value.trim();
        body.args = raw ? JSON.parse(raw) : [];
      } else {
        body.url = $("mcpUrl").value.trim();
        const tok = $("mcpToken").value.trim();
        if (tok) body.auth_token = tok;
      }
      await api("/api/mcp", { method: "POST", body: JSON.stringify(body) });
      $("mcpName").value = ""; $("mcpUrl").value = ""; $("mcpToken").value = ""; $("mcpCommand").value = ""; $("mcpArgs").value = "";
      toast("MCP server uložen.");
      loadMcp();
    } catch (err) {
      toast(err.message, "err");
    }
  };

  // Zabezpečení (Heslo & 2FA)
  function updateSecurityUi(s) {
    if ($("accEmail")) $("accEmail").value = s.email || "";
    $("pwdCurrentWrap").classList.toggle("hidden", !s.has_password);
    $("disable2faPwd").classList.toggle("hidden", !s.has_password);
    $("totpBadge").textContent = s.totp_enabled ? "2FA aktivní" : "2FA vypnuto";
    $("totpBadge").className = `pill ${s.totp_enabled ? "ok" : ""}`;
    $("totpDisabledBox").classList.toggle("hidden", Boolean(s.totp_enabled));
    $("totpEnabledBox").classList.toggle("hidden", !s.totp_enabled);
  }

  $("accountEmailForm").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/account/email", { method: "POST", body: JSON.stringify({ email: $("accEmail").value.trim() }) });
      updateSecurityUi(r.owner);
      toast("E-mail uložen.");
    } catch (err) { toast(err.message, "err"); }
  };

  $("accountPasswordForm").onsubmit = async (e) => {
    e.preventDefault();
    const pw = $("accNewPassword").value;
    if (pw !== $("accConfirmPassword").value) return toast("Hesla se neshodují.", "err");
    try {
      const r = await api("/api/account/password", {
        method: "POST",
        body: JSON.stringify({ current_password: $("accCurrentPassword").value, new_password: pw }),
      });
      $("accCurrentPassword").value = ""; $("accNewPassword").value = ""; $("accConfirmPassword").value = "";
      updateSecurityUi(r.owner);
      toast("Heslo uloženo.");
    } catch (err) { toast(err.message, "err"); }
  };

  $("start2faBtn").onclick = async () => {
    try {
      const r = await api("/api/2fa/setup", { method: "POST" });
      renderQr($("totpQrBox"), r.otpauth_uri);
      $("totpSecretCode").textContent = r.secret;
      $("totpSetupArea").classList.remove("hidden");
      $("recoveryCodesBox").classList.add("hidden");
    } catch (err) { toast(err.message, "err"); }
  };

  $("copy2faSecretBtn").onclick = async () => {
    await navigator.clipboard.writeText($("totpSecretCode").textContent.trim());
    toast("Klíč zkopírován.");
  };

  $("enable2faForm").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/2fa/enable", { method: "POST", body: JSON.stringify({ code: $("enable2faCode").value.trim() }) });
      if (r.session_token) localStorage.setItem("agy_web_token", r.session_token);
      $("enable2faCode").value = "";
      $("totpSetupArea").classList.add("hidden");
      latestRecoveryCodes = r.recovery_codes || [];
      $("recoveryCodesList").innerHTML = latestRecoveryCodes.map((c) => `<div>${escapeHtml(c)}</div>`).join("");
      $("recoveryCodesBox").classList.toggle("hidden", !latestRecoveryCodes.length);
      updateSecurityUi(r.owner);
      toast("2FA aktivováno!");
    } catch (err) { toast(err.message, "err"); }
  };

  $("copyRecoveryBtn").onclick = async () => {
    await navigator.clipboard.writeText(latestRecoveryCodes.join("\n"));
    toast("Záložní kódy zkopírovány.");
  };

  $("disable2faForm").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/2fa/disable", {
        method: "POST",
        body: JSON.stringify({ password: $("disable2faPwd").value, code: $("disable2faCode").value.trim() }),
      });
      $("disable2faPwd").value = ""; $("disable2faCode").value = "";
      $("recoveryCodesBox").classList.add("hidden");
      updateSecurityUi(r.owner);
      toast("2FA vypnuto.");
    } catch (err) { toast(err.message, "err"); }
  };

  loadAuthAndBootstrap();
})();
