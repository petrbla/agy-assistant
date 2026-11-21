(() => {
  const $ = (id) => document.getElementById(id);
  const EFFORTS = ["low", "medium", "high", "xhigh"];
  const SYSTEM_JOB_KEY_BY_ID = {
    heartbeat: "job_name_heartbeat",
    briefing: "job_name_briefing",
    nightly_reset: "job_name_nightly",
  };

  let currentLang = "en";
  let currentMeta = { code: "en", name: "English", short: "EN", date_locale: "en-US" };
  let availableLanguages = [
    { code: "en", name: "English", short: "EN", date_locale: "en-US" },
    { code: "cs", name: "Čeština", short: "CS", date_locale: "cs-CZ" },
    { code: "de", name: "Deutsch", short: "DE", date_locale: "de-DE" },
    { code: "es", name: "Español", short: "ES", date_locale: "es-ES" },
    { code: "pl", name: "Polski", short: "PL", date_locale: "pl-PL" },
    { code: "sk", name: "Slovenčina", short: "SK", date_locale: "sk-SK" },
  ];
  let uiDict = {};
  const localeCache = {};
  let isAuthenticated = false;
  let inAgyOnboarding = false;
  let lastStatus = null;
  let lastAppVerInfo = null;
  let lastOauthState = null;
  let authScreenStatusKey = "";

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
  let pluginsCatalog = [];
  let activePluginCategory = "all";
  let pluginConnectTimer = null;
  let lastPluginConnectState = null;
  let activeConnectPlugin = null;
  const chatLogs = { auto: [], pracovni: [], osobni: [] };

  function t(key, vars = {}) {
    let text = typeof uiDict[key] === "string" ? uiDict[key] : key;
    Object.entries(vars).forEach(([k, v]) => {
      text = text.replaceAll(`{${k}}`, String(v ?? ""));
    });
    return text;
  }

  function channelLabel(ch) {
    if (ch === "pracovni") return t("channel_work");
    if (ch === "osobni") return t("channel_personal");
    return t("channel_auto");
  }

  function displayBrandTitle(name) {
    const cleaned = (name || "").trim();
    const defaults = new Set(["assistant", "asistent", "assistent", "asistente", "asystent"]);
    if (!cleaned || defaults.has(cleaned.toLowerCase())) {
      return t("brand_default");
    }
    return cleaned;
  }

  function updateAppVersionBadge(verOrInfo) {
    if (!verOrInfo) return;
    let info;
    if (typeof verOrInfo === "string") {
      info = lastAppVerInfo && lastAppVerInfo.app_version === verOrInfo
        ? { ...lastAppVerInfo }
        : { app_version: verOrInfo };
    } else if (typeof verOrInfo === "object" && verOrInfo.app_version) {
      if ("app_up_to_date" in verOrInfo || "app_latest_version" in verOrInfo) {
        info = {
          app_version: verOrInfo.app_version,
          app_latest_version: verOrInfo.app_latest_version ?? null,
          app_up_to_date: verOrInfo.app_up_to_date ?? null,
          app_update_available: Boolean(verOrInfo.app_update_available),
        };
      } else {
        info = lastAppVerInfo && lastAppVerInfo.app_version === verOrInfo.app_version
          ? { ...lastAppVerInfo }
          : { app_version: verOrInfo.app_version };
      }
    } else {
      return;
    }
    lastAppVerInfo = info;

    const el = $("appVersionBadge");
    if (el && info.app_version) {
      if (info.app_update_available && info.app_latest_version) {
        el.textContent = `v${info.app_version} → v${info.app_latest_version}`;
        el.className = "version-tag warn";
        el.title = t("tooltip_app_ver_update", {
          current: info.app_version,
          latest: info.app_latest_version,
        });
      } else if (info.app_up_to_date === true) {
        el.textContent = `v${info.app_version} ✓`;
        el.className = "version-tag ok";
        el.title = t("tooltip_app_ver_current", { ver: info.app_version });
      } else {
        el.textContent = `v${info.app_version}`;
        el.className = "version-tag";
        el.title = t("tooltip_app_ver_check", { ver: info.app_version });
      }
    }

    const badge = $("appVerBadge");
    if (badge && info.app_version) {
      if (info.app_update_available && info.app_latest_version) {
        badge.textContent = t("app_ver_available", { ver: info.app_latest_version });
        badge.className = "pill warn";
        badge.classList.remove("hidden");
      } else if (info.app_up_to_date === true) {
        badge.textContent = t("app_ver_current", { ver: info.app_version });
        badge.className = "pill ok";
        badge.classList.remove("hidden");
      } else {
        badge.textContent = t("app_ver_plain", { ver: info.app_version });
        badge.className = "pill";
        badge.classList.remove("hidden");
      }
    }

    const banner = $("appUpdateBanner");
    if (banner) {
      const dismissedVer = sessionStorage.getItem("agy_dismissed_app_ver") || "";
      const showBanner = Boolean(
        info.app_update_available
        && info.app_latest_version
        && dismissedVer !== info.app_latest_version
      );
      banner.classList.toggle("hidden", !showBanner);
      if (showBanner && $("appUpdateBannerText")) {
        $("appUpdateBannerText").textContent = t("app_update_banner", {
          current: info.app_version,
          latest: info.app_latest_version,
        });
      }
    }
  }

  function renderLanguageControls(langs = availableLanguages) {
    if (Array.isArray(langs) && langs.length) {
      availableLanguages = langs;
    }
    const activeItem = availableLanguages.find((item) => item.code === currentLang);
    const shortLabel = (activeItem && activeItem.short) || currentMeta.short || currentLang.toUpperCase();
    const codeLbl = $("langCodeLabel");
    if (codeLbl) {
      codeLbl.textContent = shortLabel;
    }

    const topSel = $("langSelect");
    if (topSel) {
      topSel.innerHTML = "";
      availableLanguages.forEach((item) => {
        const opt = document.createElement("option");
        opt.value = item.code;
        opt.textContent = `${item.short || item.code.toUpperCase()} · ${item.name || item.code}`;
        topSel.appendChild(opt);
      });
      topSel.value = currentLang;
      topSel.onchange = () => applyLanguage(topSel.value, { persistBackend: true });
    }

    const sel = $("cfgLanguage");
    if (sel) {
      sel.innerHTML = "";
      availableLanguages.forEach((item) => {
        const opt = document.createElement("option");
        opt.value = item.code;
        opt.textContent = item.name || item.code.toUpperCase();
        sel.appendChild(opt);
      });
      sel.value = currentLang;
    }
  }

  function applyStaticTranslations() {
    document.documentElement.lang = currentLang;
    document.title = t("doc_title");

    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const k = el.getAttribute("data-i18n");
      if (k && typeof uiDict[k] === "string") el.textContent = uiDict[k];
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      const k = el.getAttribute("data-i18n-placeholder");
      if (k && typeof uiDict[k] === "string") el.placeholder = uiDict[k];
    });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => {
      const k = el.getAttribute("data-i18n-title");
      if (k && typeof uiDict[k] === "string") el.title = uiDict[k];
    });
    const langSel = $("langSelect");
    if (langSel) langSel.setAttribute("aria-label", t("label_language"));

    if (authScreenStatusKey) {
      $("statusPill").textContent = t(authScreenStatusKey);
    }
    if (!currentFile) {
      $("currentFileLabel").textContent = t("select_file");
      $("fileDirtyHint").textContent = t("file_hint_shortcut");
    } else {
      $("fileDirtyHint").textContent = fileDirty ? t("file_hint_dirty") : t("file_hint_loaded");
    }
    if (!lastOauthState) {
      $("oauthOutput").textContent = t("oauth_ready");
      if ($("onboardOauthOutput")) $("onboardOauthOutput").textContent = t("oauth_ready");
    }

    const defaultOpt = $("chatModelSelect")?.querySelector('option[value=""]');
    if (defaultOpt) defaultOpt.textContent = t("model_default");
    const [curBase, curEff] = splitModel(currentModel);
    if (curBase) syncEffortUi(curBase, curEff);
    if (pluginsCatalog.length) renderPlugins();
    if (lastAppVerInfo) updateAppVersionBadge(lastAppVerInfo);
    if (lastPluginConnectState && $("pluginConnectModal") && !$("pluginConnectModal").classList.contains("hidden")) {
      renderPluginConnectState(lastPluginConnectState);
    }
  }

  async function loadLocaleBundle(lang) {
    const code = (lang || "en").trim().toLowerCase() || "en";
    if (localeCache[code]) return localeCache[code];
    const res = await fetch(`/api/i18n/${encodeURIComponent(code)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    localeCache[data.language] = data;
    localeCache[code] = data;
    return data;
  }

  async function applyLanguage(lang, { persistBackend = false } = {}) {
    try {
      const bundle = await loadLocaleBundle(lang);
      currentLang = bundle.language || "en";
      currentMeta = bundle.meta || currentMeta;
      uiDict = bundle.ui || {};
      localStorage.setItem("agy_lang", currentLang);
      renderLanguageControls(bundle.available_languages);
      applyStaticTranslations();

      if (lastStatus) {
        applyStatus(lastStatus);
      } else {
        $("brandTitle").textContent = t("brand_default");
      }
      renderChat();
      if (lastOauthState) renderOauthState(lastOauthState);

      if (persistBackend && isAuthenticated) {
        await api("/api/settings", {
          method: "POST",
          body: JSON.stringify({ language: currentLang }),
        });
        if (lastStatus) lastStatus.language = currentLang;
        toast(t("toast_language_switched", { name: currentMeta.name }));
      }
      if (isAuthenticated && activeTab === "ops") loadOps();
      if (isAuthenticated && activeTab === "settings") { loadMcp(); loadPlugins(); }
    } catch (err) {
      toast(err.message, "err");
    }
  }

  // Theme (dark / light)
  function applyTheme(tMode) {
    const theme = tMode === "light" ? "light" : "dark";
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
      throw new Error(t("err_login_required"));
    }
    const text = await res.text();
    let data = {};
    try { data = text ? JSON.parse(text) : {}; } catch (_) {}
    if (!res.ok) {
      throw new Error(data.detail || text || `HTTP ${res.status}`);
    }
    return data;
  }

  function dateLocale() {
    return currentMeta.date_locale || (currentLang === "cs" ? "cs-CZ" : "en-US");
  }

  function fmtInt(n) {
    return Number(n || 0).toLocaleString(dateLocale());
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
      container.textContent = t("qr_lib_missing");
    }
  }

  // Navigation
  function switchTab(tab) {
    activeTab = tab;
    document.querySelectorAll(".nav-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === tab);
    });
    ["chat", "memory", "ops", "settings"].forEach((tabId) => {
      $(`tab-${tabId}`).classList.toggle("hidden", tabId !== tab);
    });
    if (tab === "memory") loadFiles();
    if (tab === "ops") loadOps();
    if (tab === "settings") { loadSettings(); loadMcp(); loadPlugins(); }
  }
  document.querySelectorAll(".nav-tab").forEach((b) => {
    b.onclick = () => switchTab(b.dataset.tab);
  });

  function showAuthScreen(mode) {
    inAgyOnboarding = mode === "agy-onboarding";
    isAuthenticated = inAgyOnboarding;
    $("authScreen").classList.remove("hidden");
    $("authScreen").classList.toggle("wide", inAgyOnboarding);
    $("appMain").classList.add("hidden");
    $("logoutBtn").classList.toggle("hidden", !inAgyOnboarding || !localStorage.getItem("agy_web_token"));
    document.querySelectorAll(".nav-tab").forEach((b) => { b.disabled = true; });
    $("initialSetupCard").classList.toggle("hidden", mode !== "initial");
    $("loginCard").classList.toggle("hidden", mode !== "login");
    if ($("agyOnboardingCard")) $("agyOnboardingCard").classList.toggle("hidden", !inAgyOnboarding);
  }

  function showAppMain() {
    inAgyOnboarding = false;
    isAuthenticated = true;
    authScreenStatusKey = "";
    $("authScreen").classList.add("hidden");
    $("authScreen").classList.remove("wide");
    $("appMain").classList.remove("hidden");
    document.querySelectorAll(".nav-tab").forEach((b) => { b.disabled = false; });
    $("logoutBtn").classList.toggle("hidden", !localStorage.getItem("agy_web_token"));
  }

  function showAuthErr(msg) {
    const el = $("authError");
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }

  // Auth & Bootstrap
  async function loadAuthAndBootstrap() {
    showAuthErr("");
    try {
      const st = await fetch("/api/auth/state").then((r) => r.json());
      updateAppVersionBadge(st.app_version);
      if (Array.isArray(st.available_languages)) {
        availableLanguages = st.available_languages;
      }
      const preferredLang = localStorage.getItem("agy_lang") || st.language || "en";
      await applyLanguage(preferredLang, { persistBackend: false });

      $("brandTitle").textContent = displayBrandTitle(st.assistant_name);
      $("tokenLoginWrap").classList.toggle("hidden", !st.has_token_configured);
      if (st.needs_initial_setup) {
        authScreenStatusKey = "status_initial_setup";
        $("statusPill").textContent = t(authScreenStatusKey);
        $("statusPill").className = "pill warn";
        showAuthScreen("initial");
        return;
      }
      const statusData = await api("/api/status");
      lastStatus = statusData;
      if (statusData.language && statusData.language !== currentLang) {
        await applyLanguage(statusData.language, { persistBackend: false });
      }
      const skipped = sessionStorage.getItem("agy_onboarding_skipped") === "1";
      if (statusData.agy_available && !statusData.agy_authenticated && !skipped) {
        applyStatus(statusData);
        showAuthScreen("agy-onboarding");
        return;
      }
      showAppMain();
      applyStatus(statusData);
      await loadModels(false);
      renderChat();
    } catch (_) {
      isAuthenticated = false;
      inAgyOnboarding = false;
      lastStatus = null;
      if ($("appUpdateBanner")) $("appUpdateBanner").classList.add("hidden");
      authScreenStatusKey = "status_signed_out";
      $("statusPill").textContent = t(authScreenStatusKey);
      $("statusPill").className = "pill";
      showAuthScreen("login");
    }
  }

  $("initialSetupForm").onsubmit = async (e) => {
    e.preventDefault();
    showAuthErr("");
    const pw = $("initPassword").value;
    if (pw !== $("initPassword2").value) return showAuthErr(t("err_passwords_mismatch"));
    try {
      const res = await fetch("/api/auth/initial-setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: $("initEmail").value.trim(),
          username: $("initUsername").value.trim(),
          language: currentLang,
          password: pw,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return showAuthErr(data.detail || t("err_setup_failed"));
      localStorage.setItem("agy_web_token", data.session_token);
      sessionStorage.removeItem("agy_onboarding_skipped");
      toast(t("toast_setup_done"));
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
      if (!res.ok) return showAuthErr(data.detail || t("err_login_failed"));
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
      toast(t("toast_signed_in"));
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
      if (!res.ok) return showAuthErr(data.detail || t("err_invalid_2fa"));
      localStorage.setItem("agy_web_token", data.session_token);
      $("loginStep2Form").classList.add("hidden");
      $("loginStep1Form").classList.remove("hidden");
      toast(t("toast_signed_in_2fa"));
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
    if (!tok) return showAuthErr(t("err_enter_web_token"));
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ web_token: tok }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return showAuthErr(data.detail || t("err_invalid_token"));
      localStorage.setItem("agy_web_token", data.session_token);
      $("loginWebToken").value = "";
      toast(t("toast_signed_in_token"));
      await loadAuthAndBootstrap();
    } catch (err) {
      showAuthErr(err.message);
    }
  };

  $("logoutBtn").onclick = async () => {
    if (authPollTimer) { clearInterval(authPollTimer); authPollTimer = null; }
    if (pluginConnectTimer) { clearInterval(pluginConnectTimer); pluginConnectTimer = null; }
    inAgyOnboarding = false;
    try { await fetch("/api/logout", { method: "POST", headers: getHeaders() }); } catch (_) {}
    localStorage.removeItem("agy_web_token");
    sessionStorage.removeItem("agy_onboarding_skipped");
    lastStatus = null;
    lastAppVerInfo = null;
    if ($("appUpdateBanner")) $("appUpdateBanner").classList.add("hidden");
    await loadAuthAndBootstrap();
  };

  function applyStatus(s) {
    lastStatus = s;
    updateAppVersionBadge(s);
    $("brandTitle").textContent = displayBrandTitle(s.assistant_name);
    const dot = $("brandDot");
    const pill = $("statusPill");
    if (!s.agy_available) {
      dot.className = "brand-dot err";
      pill.className = "pill err";
      pill.textContent = t("status_agy_unavailable");
    } else if (!s.agy_authenticated) {
      dot.className = "brand-dot warn";
      pill.className = "pill warn";
      pill.textContent = t("status_agy_unauthenticated", { version: s.agy_version || "agy" });
    } else {
      dot.className = "brand-dot ok";
      pill.className = "pill ok";
      pill.textContent = s.agy_version ? `agy ${s.agy_version}` : t("status_online");
    }
    updateAgyBadges(s);
    updateSecurityUi(s);
    const banner = $("chatAgyAuthBanner");
    if (banner) banner.classList.toggle("hidden", !s.agy_available || Boolean(s.agy_authenticated));
  }

  function updateAgyBadges(info) {
    const authBadge = $("agyAuthBadge");
    const onboardBadge = $("onboardAgyBadge");
    const finishBtn = $("finishAgyOnboardBtn");
    const logoutBtn = $("logoutAgyBtn");
    const parts = [];
    if (info.has_agy_oauth) parts.push(t("agy_auth_oauth"));
    if (info.has_gemini_api_key) parts.push(t("agy_auth_api_key"));
    if (parts.length) {
      const label = t("agy_auth_signed_in", { parts: parts.join(" + ") });
      authBadge.textContent = label;
      authBadge.className = "pill ok";
      if (onboardBadge) {
        onboardBadge.textContent = label;
        onboardBadge.className = "pill ok";
      }
      if (finishBtn) finishBtn.classList.remove("hidden");
      logoutBtn.classList.remove("hidden");
    } else if (typeof info.has_agy_oauth === "boolean") {
      authBadge.textContent = t("agy_auth_signed_out");
      authBadge.className = "pill err";
      if (onboardBadge) {
        onboardBadge.textContent = t("agy_auth_signed_out");
        onboardBadge.className = "pill err";
      }
      if (finishBtn) finishBtn.classList.add("hidden");
      logoutBtn.classList.add("hidden");
    }

    const verBadge = $("agyVerBadge");
    const updBtn = $("updateAgyBtn");
    if (info.agy_update_available && info.agy_latest_version) {
      verBadge.textContent = t("agy_ver_available", { ver: info.agy_latest_version });
      verBadge.className = "pill warn";
      updBtn.classList.remove("hidden");
    } else if (info.agy_up_to_date === true) {
      verBadge.textContent = t("agy_ver_current", { ver: info.agy_version });
      verBadge.className = "pill ok";
      updBtn.classList.add("hidden");
    } else if (info.agy_version) {
      verBadge.textContent = info.agy_version;
      verBadge.className = "pill";
      updBtn.classList.add("hidden");
    }
  }

  // Models & Effort
  function splitModel(full) {
    const s = (full || "").trim();
    for (const e of EFFORTS) {
      if (s.endsWith(`-${e}`) && s.length > e.length + 1) {
        return [s.slice(0, -(e.length + 1)), e];
      }
    }
    return [s, ""];
  }

  function effortLabel(eff) {
    return t(`effort_${eff}`) || eff;
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
      sel.innerHTML = `<option value="">${escapeHtml(t("model_default"))}</option>`;
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
    $("effortLabel").textContent = effortLabel(activeEff);
    range.oninput = () => {
      const e = ordered[Number(range.value)];
      $("effortLabel").textContent = effortLabel(e);
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
      toast(t("toast_model_set", { model: fullModel || t("model_default") }));
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
      empty.textContent = t("chat_empty", { channel: channelLabel(activeChannel) });
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
      if (res.assistant_name) {
        if (lastStatus) lastStatus.assistant_name = res.assistant_name;
        $("brandTitle").textContent = displayBrandTitle(res.assistant_name);
      }
      appendMsg(res.text || t("empty_reply"), "assistant");
    } catch (err) {
      typing.remove();
      appendMsg(t("chat_error_prefix", { msg: err.message }), "meta");
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
      toast(t("toast_chat_reset", { channel: channelLabel(activeChannel) }));
    } catch (err) {
      toast(err.message, "err");
    }
  };

  $("chatCleanupBtn").onclick = async () => {
    if (!confirm(t("confirm_cleanup"))) return;
    toast(t("toast_cleanup_running"), "warn");
    try {
      const r = await api("/api/nightly-reset/run", { method: "POST" });
      chatLogs.auto = []; chatLogs.pracovni = []; chatLogs.osobni = [];
      renderChat();
      toast(t("toast_cleanup_done", { flushed: r.flushed_sessions, reset: r.reset_sessions }));
    } catch (err) {
      toast(err.message, "err");
    }
  };

  // Memory
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
      $("fileDirtyHint").textContent = t("file_hint_loaded");
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
    $("fileDirtyHint").textContent = t("file_hint_dirty");
  };

  async function saveCurrentFile() {
    if (!currentFile) return;
    try {
      await api(`/api/files/${encodeURIComponent(currentFile)}`, {
        method: "PUT",
        body: JSON.stringify({ content: $("fileEditor").value }),
      });
      fileDirty = false;
      $("fileDirtyHint").textContent = t("file_hint_saved");
      toast(t("toast_file_saved", { path: currentFile }));
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
    if (!confirm(t("confirm_workspace_reset"))) return;
    try {
      await api("/api/workspace/reset", { method: "POST" });
      currentFile = null;
      fileDirty = false;
      $("currentFileLabel").textContent = t("select_file");
      $("fileDirtyHint").textContent = t("file_hint_shortcut");
      $("fileEditor").value = "";
      $("fileEditor").disabled = true;
      $("saveFileBtn").disabled = true;
      await loadFiles();
      toast(t("toast_workspace_reset"));
    } catch (err) {
      toast(err.message, "err");
    }
  };

  // Scheduler & History
  function renderKpi(valId, subId, st) {
    const tilde = st.estimated_runs > 0 && st.exact_runs === 0 ? "~" : "";
    $(valId).textContent = `${tilde}${fmtInt(st.total_tokens)} ${t("kpi_tok_unit")}`;
    $(subId).textContent = t("kpi_sub", {
      runs: fmtInt(st.total_runs),
      in_tok: fmtInt(st.input_tokens),
      out_tok: fmtInt(st.output_tokens),
    });
  }

  function dowNames() {
    return [0, 1, 2, 3, 4, 5, 6].map((i) => t(`dow_${i}`));
  }

  function fmtNextRun(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    const loc = dateLocale();
    const time = d.toLocaleTimeString(loc, { hour: "numeric", minute: "2-digit" });
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const day = new Date(d);
    day.setHours(0, 0, 0, 0);
    const diff = Math.round((day - today) / 86400000);
    if (diff === 0) return t("sched_today", { time });
    if (diff === 1) return t("sched_tomorrow", { time });
    const dows = dowNames();
    if (diff > 1 && diff < 7) return `${dows[d.getDay()]} ${time}`;
    return `${d.toLocaleDateString(loc)} ${time}`;
  }

  function humanDow(f) {
    if (f === "*") return "";
    if (f === "1-5") return t("sched_mon_fri");
    if (f === "0,6" || f === "6,0" || f === "6-7" || f === "sat,sun") return t("sched_sat_sun");
    const dows = dowNames();
    const dayAt = (x) => dows[(+x) % 7] ?? x;
    return f.split(",").map((p) => p.includes("-")
      ? p.split("-").map(dayAt).join("–")
      : dayAt(p)).join(", ");
  }

  function humanSchedule(task) {
    if (task.schedule_type === "once") {
      return task.run_at ? t("sched_once_prefix") + fmtNextRun(task.run_at) : t("sched_once_plain");
    }
    const parts = (task.cron_expr || "").trim().split(/\s+/);
    if (parts.length !== 5) return task.cron_expr;
    const [mi, h, dom, mon, dow] = parts;
    if (!/^\d+$/.test(mi) || !/^\d+$/.test(h)) return task.cron_expr;
    const time = `${+h}:${mi.padStart(2, "0")}`;
    if (dom === "*" && mon === "*") {
      return dow === "*" ? t("sched_daily", { time }) : `${humanDow(dow)} ${time}`;
    }
    if (/^\d+$/.test(dom) && /^\d+$/.test(mon) && dow === "*") {
      return t("sched_yearly", { day: +dom, month: +mon, time });
    }
    if (/^\d+$/.test(dom) && mon === "*" && dow === "*") {
      return t("sched_monthly", { day: +dom, time });
    }
    return task.cron_expr;
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
      $("opsSummary").textContent = t("ops_summary", {
        skills: (s.skills || []).join(", ") || t("none"),
        tg_state: s.telegram_enabled ? t("state_on") : t("state_off"),
      });
      const jobs = s.jobs || [];
      const nextByName = new Map(jobs.map((j) => [j.name, j.next_run_time]));
      const taskNames = new Set(tasks.map((task) => task.name));
      const systemJobs = jobs.filter((j) => !taskNames.has(j.name));
      const jl = $("jobsList");
      jl.innerHTML = "";
      if (systemJobs.length) {
        const label = document.createElement("span");
        label.className = "jobs-label";
        label.textContent = t("system_jobs_label");
        jl.appendChild(label);
        systemJobs
          .sort((a, b) => String(a.next_run_time || "~").localeCompare(String(b.next_run_time || "~")))
          .forEach((j) => {
            const chip = document.createElement("span");
            chip.className = "job-chip";
            const sysKey = SYSTEM_JOB_KEY_BY_ID[j.id];
            const jobDisplayName = sysKey ? t(sysKey) : j.name;
            chip.innerHTML = `${escapeHtml(jobDisplayName)} <span class="job-chip-time">${escapeHtml(fmtNextRun(j.next_run_time))}</span>`;
            chip.title = j.next_run_time || "";
            jl.appendChild(chip);
          });
      }
      jl.classList.toggle("hidden", !systemJobs.length);

      const tb = $("tasksBody");
      tb.innerHTML = "";
      if (!tasks.length) {
        tb.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:18px;">${escapeHtml(t("tasks_empty"))}</td></tr>`;
      }
      const sorted = [...tasks].sort((a, b) => {
        const na = a.enabled ? nextByName.get(a.name) || "~" : "~~";
        const nb = b.enabled ? nextByName.get(b.name) || "~" : "~~";
        return String(na).localeCompare(String(nb));
      });
      sorted.forEach((task) => {
        const tr = document.createElement("tr");
        if (!task.enabled) tr.className = "task-disabled";
        const raw = task.schedule_type === "once" ? task.run_at : task.cron_expr;
        const next = task.enabled ? nextByName.get(task.name) : null;
        const typePill = task.action_type === "agent"
          ? `<span class="pill accent">${escapeHtml(t("task_type_ai"))}</span>`
          : `<span class="pill">${escapeHtml(t("task_type_msg"))}</span>`;
        const statePill = task.enabled
          ? `<span class="pill ok">${escapeHtml(t("task_state_active"))}</span>`
          : `<span class="pill">${escapeHtml(t("task_state_paused"))}</span>`;
        tr.innerHTML =
          `<td class="task-name">${escapeHtml(task.name)}</td>` +
          `<td class="nowrap"><div title="${escapeHtml(raw)}">${escapeHtml(humanSchedule(task))}</div>` +
          `<div class="task-next">${next ? escapeHtml(t("task_next_prefix", { time: fmtNextRun(next) })) : "—"}</div></td>` +
          `<td>${typePill}</td>` +
          `<td><div class="task-prompt" title="${escapeHtml(task.prompt)}">${escapeHtml(task.prompt)}</div></td>` +
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
          mk("▶", "btn-icon-sm", t("task_action_run"), async () => {
            await api(`/api/tasks/${task.id}/run`, { method: "POST" });
            toast(t("toast_task_run", { name: task.name }));
          }),
          mk(task.enabled ? "⏸" : "⏵", "btn-icon-sm", task.enabled ? t("task_action_pause") : t("task_action_resume"), async () => {
            await api(`/api/tasks/${task.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !task.enabled }) });
            toast(task.enabled ? t("toast_task_paused") : t("toast_task_resumed"));
          }),
          mk("✕", "btn-icon-sm btn-danger", t("task_action_delete"), async () => {
            if (!confirm(t("confirm_delete_task", { name: task.name }))) throw new Error(t("toast_cancelled"));
            await api(`/api/tasks/${task.id}`, { method: "DELETE" });
            toast(t("toast_task_deleted"));
          }),
        );
        tr.appendChild(td);
        tb.appendChild(tr);
      });

      const rb = $("runsBody");
      rb.innerHTML = "";
      const loc = dateLocale();
      runs.forEach((r) => {
        const tr = document.createElement("tr");
        const dt = r.created_at ? new Date(r.created_at).toLocaleString(loc) : "";
        const tok = `${r.tokens_estimated ? "~" : ""}${fmtInt(r.total_tokens)}`;
        const dur = `${(r.duration_ms / 1000).toFixed(1)} s`;
        tr.innerHTML = `<td class="mono">${escapeHtml(dt)}</td><td>${escapeHtml(r.source)}</td><td>${escapeHtml(channelLabel(r.context_mode))}</td><td>${escapeHtml((r.prompt || "").slice(0, 90))}</td><td>${escapeHtml((r.response || r.error || "").slice(0, 110))}</td><td class="mono">${tok}</td><td class="mono">${dur}</td>`;
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
      toast(t("toast_task_added"));
      loadOps();
    } catch (err) {
      toast(err.message, "err");
    }
  };

  $("refreshOpsBtn").onclick = loadOps;
  $("runHeartbeatBtn").onclick = async () => {
    toast(t("toast_hb_running"), "warn");
    const r = await api("/api/heartbeat/run", { method: "POST" });
    toast(r.text || t("toast_hb_done"));
    loadOps();
  };
  $("runBriefingBtn").onclick = async () => {
    toast(t("toast_briefing_running"), "warn");
    await api("/api/briefing/run", { method: "POST" });
    toast(t("toast_briefing_done"));
    loadOps();
  };
  $("runNightlyBtn").onclick = async () => {
    toast(t("toast_nightly_running"), "warn");
    const r = await api("/api/nightly-reset/run", { method: "POST" });
    toast(t("toast_nightly_done", { flushed: r.flushed_sessions, reset: r.reset_sessions }));
    loadOps();
  };

  // Settings & MCP & OAuth & 2FA
  async function loadSettings() {
    try {
      const s = await api("/api/settings");
      $("cfgAssistantName").value = s.assistant_name || t("placeholder_assistant_name");
      if ($("cfgLanguage")) $("cfgLanguage").value = s.language || currentLang;
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
    const chosenLang = $("cfgLanguage") ? $("cfgLanguage").value : currentLang;
    const body = {
      assistant_name: $("cfgAssistantName").value.trim() || t("placeholder_assistant_name"),
      language: chosenLang,
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
      localStorage.setItem("agy_lang", chosenLang);
      await loadAuthAndBootstrap();
      toast(t("toast_settings_saved"));
    } catch (err) {
      toast(err.message, "err");
    }
  };

  $("geminiKeyForm").onsubmit = async (e) => {
    e.preventDefault();
    const key = $("cfgGeminiKey").value.trim();
    if (!key) return toast(t("toast_enter_key"), "warn");
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify({ gemini_api_key: key }) });
      $("cfgGeminiKey").value = "";
      toast(t("toast_key_saved"));
      await loadSettings();
      await loadModels(true);
      try { applyStatus(await api("/api/status")); } catch (_) {}
    } catch (err) {
      toast(err.message, "err");
    }
  };

  $("clearGeminiKeyBtn").onclick = async () => {
    await api("/api/settings", { method: "POST", body: JSON.stringify({ gemini_api_key: "" }) });
    toast(t("toast_key_cleared"));
    await loadSettings();
    try { applyStatus(await api("/api/status")); } catch (_) {}
  };

  // OAuth PTY
  function renderOauthBox(outputId, urlsId, st) {
    const outEl = $(outputId);
    const urlsEl = $(urlsId);
    if (outEl) {
      outEl.textContent = st.error
        ? t("chat_error_prefix", { msg: st.error })
        : (st.output || (st.running ? t("oauth_starting") : t("oauth_idle")));
      outEl.scrollTop = outEl.scrollHeight;
    }
    if (urlsEl) {
      urlsEl.innerHTML = "";
      (st.urls || []).forEach((u, i) => {
        const a = document.createElement("a");
        a.href = u;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.className = "btn btn-primary btn-sm";
        a.textContent = t("oauth_open_url", { idx: i + 1 });
        urlsEl.appendChild(a);
      });
    }
  }

  function renderOauthState(st) {
    lastOauthState = st;
    renderOauthBox("oauthOutput", "oauthUrls", st);
    renderOauthBox("onboardOauthOutput", "onboardOauthUrls", st);
  }

  async function handleAgyAuthenticated() {
    if (authPollTimer) {
      clearInterval(authPollTimer);
      authPollTimer = null;
    }
    toast(t("toast_agy_auth_done"));
    if (inAgyOnboarding) {
      sessionStorage.removeItem("agy_onboarding_skipped");
      await loadAuthAndBootstrap();
    } else {
      await loadSettings();
      await loadModels(true);
      try { applyStatus(await api("/api/status")); } catch (_) {}
    }
  }

  async function checkAndFinalizeOauth(wasAuthenticated) {
    const cur = await api("/api/auth/status");
    renderOauthState(cur);
    updateAgyBadges(cur);
    if (!wasAuthenticated && cur.agy_authenticated) {
      if (cur.running) {
        setTimeout(() => { api("/api/auth/stop", { method: "POST" }).catch(() => {}); }, 1200);
      }
      await handleAgyAuthenticated();
      return true;
    }
    if (!cur.running && authPollTimer) {
      clearInterval(authPollTimer);
      authPollTimer = null;
      loadSettings();
      loadModels(true);
    }
    return false;
  }

  async function startOauthFlow() {
    const wasAuthenticated = Boolean(lastStatus && lastStatus.agy_authenticated);
    let transitionDone = false;
    try {
      const st = await api("/api/auth/start", { method: "POST", body: JSON.stringify({ mode: "interactive" }) });
      renderOauthState(st);
      if (authPollTimer) clearInterval(authPollTimer);
      authPollTimer = setInterval(async () => {
        try {
          if (transitionDone) return;
          const ok = await checkAndFinalizeOauth(wasAuthenticated);
          if (ok) transitionDone = true;
        } catch (_) {}
      }, 1000);
    } catch (err) {
      toast(err.message, "err");
    }
  }

  async function sendOauthInputValue(value = "") {
    const wasAuthenticated = Boolean(lastStatus && lastStatus.agy_authenticated);
    try {
      const st = await api("/api/auth/input", { method: "POST", body: JSON.stringify({ value }) });
      renderOauthState(st);
      if (!st.running) {
        await checkAndFinalizeOauth(wasAuthenticated);
      }
    } catch (err) {
      toast(err.message, "err");
    }
  }

  async function stopOauthFlow() {
    if (authPollTimer) {
      clearInterval(authPollTimer);
      authPollTimer = null;
    }
    try {
      await api("/api/auth/stop", { method: "POST" });
      renderOauthState(await api("/api/auth/status"));
    } catch (err) {
      toast(err.message, "err");
    }
  }

  $("startOauthBtn").onclick = startOauthFlow;
  $("oauthEnterBtn").onclick = () => sendOauthInputValue("");
  $("stopOauthBtn").onclick = stopOauthFlow;
  $("oauthInputForm").onsubmit = async (e) => {
    e.preventDefault();
    const v = $("oauthCodeInput").value.trim();
    $("oauthCodeInput").value = "";
    await sendOauthInputValue(v);
  };

  if ($("onboardStartOauthBtn")) $("onboardStartOauthBtn").onclick = startOauthFlow;
  if ($("onboardOauthEnterBtn")) $("onboardOauthEnterBtn").onclick = () => sendOauthInputValue("");
  if ($("onboardStopOauthBtn")) $("onboardStopOauthBtn").onclick = stopOauthFlow;
  if ($("onboardOauthInputForm")) {
    $("onboardOauthInputForm").onsubmit = async (e) => {
      e.preventDefault();
      const v = $("onboardOauthCodeInput").value.trim();
      $("onboardOauthCodeInput").value = "";
      await sendOauthInputValue(v);
    };
  }
  if ($("onboardGeminiKeyForm")) {
    $("onboardGeminiKeyForm").onsubmit = async (e) => {
      e.preventDefault();
      const key = $("onboardGeminiKey").value.trim();
      if (!key) return toast(t("toast_enter_key"), "warn");
      try {
        await api("/api/settings", { method: "POST", body: JSON.stringify({ gemini_api_key: key }) });
        $("onboardGeminiKey").value = "";
        sessionStorage.removeItem("agy_onboarding_skipped");
        toast(t("toast_key_saved"));
        await loadAuthAndBootstrap();
      } catch (err) {
        toast(err.message, "err");
      }
    };
  }
  if ($("skipAgyOnboardBtn")) {
    $("skipAgyOnboardBtn").onclick = async () => {
      sessionStorage.setItem("agy_onboarding_skipped", "1");
      if (authPollTimer) { clearInterval(authPollTimer); authPollTimer = null; }
      await loadAuthAndBootstrap();
    };
  }
  if ($("finishAgyOnboardBtn")) {
    $("finishAgyOnboardBtn").onclick = async () => {
      sessionStorage.removeItem("agy_onboarding_skipped");
      await loadAuthAndBootstrap();
    };
  }
  if ($("chatAuthorizeAgyBtn")) {
    $("chatAuthorizeAgyBtn").onclick = () => {
      sessionStorage.removeItem("agy_onboarding_skipped");
      if (lastStatus) applyStatus(lastStatus);
      showAuthScreen("agy-onboarding");
    };
  }

  $("logoutAgyBtn").onclick = async () => {
    if (!confirm(t("confirm_agy_logout"))) return;
    await api("/api/auth/logout", { method: "POST" });
    toast(t("toast_agy_logged_out"));
    await loadSettings();
    try { applyStatus(await api("/api/status")); } catch (_) {}
  };
  if ($("dismissAppUpdateBtn")) {
    $("dismissAppUpdateBtn").onclick = () => {
      if (lastAppVerInfo && lastAppVerInfo.app_latest_version) {
        sessionStorage.setItem("agy_dismissed_app_ver", lastAppVerInfo.app_latest_version);
      }
      if ($("appUpdateBanner")) $("appUpdateBanner").classList.add("hidden");
    };
  }
  if ($("appVersionBadge")) {
    $("appVersionBadge").onclick = async () => {
      if (!isAuthenticated || inAgyOnboarding) return;
      try {
        const info = await api("/api/app/version?refresh=true");
        sessionStorage.removeItem("agy_dismissed_app_ver");
        if (lastStatus) Object.assign(lastStatus, info);
        updateAppVersionBadge(info);
        if (info.app_update_available && info.app_latest_version) {
          toast(
            t("toast_app_update_avail", {
              current: info.app_version,
              latest: info.app_latest_version,
            }),
            "warn"
          );
        } else if (info.app_up_to_date === true) {
          toast(t("toast_app_up_to_date", { ver: info.app_version }), "ok");
        } else {
          toast(t("toast_app_check_failed", { ver: info.app_version }), "warn");
        }
      } catch (err) {
        toast(err.message, "err");
      }
    };
  }
  $("checkVerBtn").onclick = async () => {
    try {
      const [agyInfo, appInfo] = await Promise.all([
        api("/api/agy/version?refresh=true"),
        api("/api/app/version?refresh=true"),
      ]);
      sessionStorage.removeItem("agy_dismissed_app_ver");
      if (lastStatus) Object.assign(lastStatus, agyInfo, appInfo);
      updateAgyBadges(agyInfo);
      updateAppVersionBadge(appInfo);
      const appPart = appInfo.app_update_available && appInfo.app_latest_version
        ? t("toast_app_update_avail", {
            current: appInfo.app_version,
            latest: appInfo.app_latest_version,
          })
        : appInfo.app_up_to_date === true
          ? t("toast_app_up_to_date", { ver: appInfo.app_version })
          : t("app_ver_plain", { ver: appInfo.app_version });
      const agyPart = agyInfo.agy_update_available && agyInfo.agy_latest_version
        ? t("toast_ver_update_avail", { ver: agyInfo.agy_latest_version })
        : `agy ${agyInfo.agy_version || t("toast_ver_unavailable")}`;
      toast(
        `${appPart} · ${agyPart}`,
        appInfo.app_update_available || agyInfo.agy_update_available ? "warn" : "ok"
      );
    } catch (err) {
      toast(err.message, "err");
    }
  };
  $("updateAgyBtn").onclick = async () => {
    if (!confirm(t("confirm_agy_update"))) return;
    toast(t("toast_agy_updating"), "warn");
    try {
      const res = await api("/api/agy/update", { method: "POST" });
      updateAgyBadges(res);
      toast(t("toast_agy_updated", { ver: res.agy_version || "OK" }));
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
      const labels = {
        all: t("scope_all"),
        pracovni: t("scope_work"),
        osobni: t("scope_personal"),
      };
      items.forEach((srv) => {
        const tr = document.createElement("tr");
        const target = srv.transport === "stdio" ? `${srv.command} ${(srv.args || []).join(" ")}` : srv.url;
        tr.innerHTML = `<td><strong>${escapeHtml(srv.name)}</strong></td><td>${escapeHtml(labels[srv.scope] || labels.all)}</td><td class="mono">${escapeHtml(target)}</td>`;
        const td = document.createElement("td");
        const del = document.createElement("button");
        del.className = "btn btn-sm btn-danger";
        del.textContent = t("btn_delete");
        del.onclick = async () => {
          await api(`/api/mcp/${srv.id}`, { method: "DELETE" });
          toast(t("toast_mcp_deleted"));
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
      toast(t("toast_mcp_saved"));
      loadMcp();
    } catch (err) {
      toast(err.message, "err");
    }
  };

  // Plugins & Integrations
  function pluginDescription(p) {
    if (p.desc_key && typeof uiDict[p.desc_key] === "string") return t(p.desc_key);
    return p.description || p.install_target || p.name;
  }

  async function loadPlugins() {
    if (!$("pluginsGrid")) return;
    try {
      const data = await api("/api/plugins");
      pluginsCatalog = Array.isArray(data.plugins) ? data.plugins : [];
      renderPlugins();
    } catch (_) {}
  }

  function renderPlugins() {
    const grid = $("pluginsGrid");
    if (!grid) return;
    grid.innerHTML = "";
    const query = ($("pluginSearchInput")?.value || "").trim().toLowerCase();

    const filtered = pluginsCatalog.filter((p) => {
      if (activePluginCategory === "installed" && !p.installed) return false;
      if (activePluginCategory !== "all" && activePluginCategory !== "installed" && p.category !== activePluginCategory) {
        return false;
      }
      if (!query) return true;
      const hay = `${p.name} ${p.display_name} ${pluginDescription(p)} ${p.marketplace}`.toLowerCase();
      return hay.includes(query);
    });

    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "muted";
      empty.textContent = t("plugins_empty");
      grid.appendChild(empty);
      return;
    }

    filtered.forEach((p) => {
      const card = document.createElement("div");
      card.className = "plugin-card";

      const head = document.createElement("div");
      head.className = "plugin-head";

      const titleWrap = document.createElement("div");
      titleWrap.className = "plugin-title-wrap";
      const iconEl = document.createElement("span");
      iconEl.className = "plugin-icon";
      if (p.logo_url) {
        const img = document.createElement("img");
        img.className = "plugin-logo";
        img.src = p.logo_url;
        img.alt = p.display_name || p.name;
        img.loading = "lazy";
        img.referrerPolicy = "no-referrer";
        img.onerror = () => {
          iconEl.classList.remove("has-logo");
          iconEl.textContent = p.icon || "🧩";
        };
        iconEl.classList.add("has-logo");
        iconEl.appendChild(img);
      } else {
        iconEl.textContent = p.icon || "🧩";
      }
      const textBox = document.createElement("div");
      const acctPart = p.installed && p.connected && p.account_label ? ` · ${p.account_label}` : "";
      const metaText = (p.version ? `v${p.version} · ${p.marketplace}` : p.marketplace) + acctPart;
      textBox.innerHTML = `<div class="plugin-name">${escapeHtml(p.display_name || p.name)}</div><div class="plugin-meta mono">${escapeHtml(metaText)}</div>`;
      titleWrap.append(iconEl, textBox);

      const badge = document.createElement("span");
      if (p.installed && p.connected) {
        badge.className = "pill ok";
        badge.textContent = t("plugin_status_connected");
        if (p.account_label) badge.title = p.account_label;
      } else if (p.installed && p.enabled) {
        badge.className = "pill accent";
        badge.textContent = t("plugin_status_installed");
      } else if (p.installed) {
        badge.className = "pill warn";
        badge.textContent = t("plugin_status_disabled");
      } else {
        badge.className = "pill";
        badge.textContent = t("plugin_status_available");
      }
      head.append(titleWrap, badge);

      const desc = document.createElement("p");
      desc.className = "plugin-desc";
      desc.textContent = pluginDescription(p);

      const actions = document.createElement("div");
      actions.className = "plugin-actions";

      const makeBtn = (label, cls, handler) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = `btn btn-sm ${cls}`.trim();
        b.textContent = label;
        b.onclick = async () => {
          b.disabled = true;
          try { await handler(); } finally { b.disabled = false; }
        };
        return b;
      };

      if (!p.installed) {
        if (p.supports_oauth) {
          actions.appendChild(
            makeBtn(t("plugin_btn_connect"), "btn-primary", () =>
              openPluginConnectModal(p, { forceReconnect: false })
            )
          );
        }
        actions.appendChild(
          makeBtn(t("plugin_btn_install"), p.supports_oauth ? "" : "btn-primary", async () => {
            try {
              toast(t("toast_plugin_installing", { name: p.display_name || p.name }), "warn");
              await api("/api/plugins/install", {
                method: "POST",
                body: JSON.stringify({ target: p.install_target || p.name }),
              });
              toast(t("toast_plugin_installed", { name: p.display_name || p.name }));
              await loadPlugins();
            } catch (err) { toast(err.message, "err"); }
          })
        );
      } else {
        if (p.supports_oauth) {
          actions.appendChild(
            makeBtn(
              p.connected ? t("plugin_btn_reconnect") : t("plugin_btn_connect"),
              p.connected ? "" : "btn-primary",
              () => openPluginConnectModal(p, { forceReconnect: Boolean(p.connected) })
            )
          );
        }
        actions.appendChild(
          makeBtn(p.enabled ? t("plugin_btn_disable") : t("plugin_btn_enable"), "", async () => {
            try {
              await api(`/api/plugins/${encodeURIComponent(p.name)}/toggle`, {
                method: "POST",
                body: JSON.stringify({ enabled: !p.enabled }),
              });
              toast(t(p.enabled ? "toast_plugin_disabled" : "toast_plugin_enabled", { name: p.display_name || p.name }));
              await loadPlugins();
            } catch (err) { toast(err.message, "err"); }
          })
        );
        actions.appendChild(
          makeBtn(t("plugin_btn_uninstall"), "btn-danger", async () => {
            if (!confirm(t("confirm_uninstall_plugin", { name: p.display_name || p.name }))) return;
            try {
              await api(`/api/plugins/${encodeURIComponent(p.name)}`, { method: "DELETE" });
              toast(t("toast_plugin_uninstalled", { name: p.display_name || p.name }));
              await loadPlugins();
            } catch (err) { toast(err.message, "err"); }
          })
        );
      }

      card.append(head, desc, actions);

      if (p.installed && Array.isArray(p.permissions_schema) && p.permissions_schema.length) {
        const permsBox = document.createElement("div");
        permsBox.className = "plugin-perms";
        const permTitle = document.createElement("div");
        permTitle.className = "plugin-perm-title";
        permTitle.textContent = t("plugin_perms_title");
        permsBox.appendChild(permTitle);

        p.permissions_schema.forEach((spec) => {
          const row = document.createElement("div");
          row.className = "plugin-perm-row";
          const lbl = document.createElement("label");
          lbl.className = "switch";
          const span = document.createElement("span");
          span.textContent = t(spec.label_key);
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.checked = Boolean((p.permissions || {})[spec.key]);
          cb.onchange = async () => {
            const prev = !cb.checked;
            const updatedPerms = { ...(p.permissions || {}), [spec.key]: cb.checked };
            try {
              const res = await api(`/api/plugins/${encodeURIComponent(p.name)}/permissions`, {
                method: "PUT",
                body: JSON.stringify({ permissions: updatedPerms }),
              });
              p.permissions = res.permissions || updatedPerms;
              toast(t("toast_plugin_perms_saved", { name: p.display_name || p.name }));
            } catch (err) {
              cb.checked = prev;
              toast(err.message, "err");
            }
          };
          lbl.append(span, cb);
          row.appendChild(lbl);
          permsBox.appendChild(row);
        });
        card.appendChild(permsBox);
      }

      grid.appendChild(card);
    });
  }

  document.querySelectorAll("#pluginCategoryBar .filter-pill").forEach((btn) => {
    btn.onclick = () => {
      activePluginCategory = btn.dataset.cat || "all";
      document.querySelectorAll("#pluginCategoryBar .filter-pill").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      renderPlugins();
    };
  });
  if ($("pluginSearchInput")) $("pluginSearchInput").oninput = renderPlugins;
  if ($("refreshPluginsBtn")) $("refreshPluginsBtn").onclick = loadPlugins;

  if ($("customPluginForm")) {
    $("customPluginForm").onsubmit = async (e) => {
      e.preventDefault();
      const target = $("customPluginTarget").value.trim();
      if (!target) return;
      try {
        toast(t("toast_plugin_installing", { name: target }), "warn");
        await api("/api/plugins/install", {
          method: "POST",
          body: JSON.stringify({ target }),
        });
        $("customPluginTarget").value = "";
        toast(t("toast_plugin_installed", { name: target }));
        await loadPlugins();
      } catch (err) { toast(err.message, "err"); }
    };
  }

  function renderPluginConnectState(st) {
    lastPluginConnectState = st;
    const badge = $("pluginConnectBadge");
    const urlWrap = $("pluginConnectUrlWrap");
    const cbForm = $("pluginCallbackForm");
    const codeForm = $("pluginCodeForm");
    const outEl = $("pluginConnectOutput");

    if (badge) {
      if (st.connected) {
        badge.className = "pill ok";
        badge.textContent = t("plugin_connect_done");
      } else if (st.error) {
        badge.className = "pill err";
        badge.textContent = st.error;
      } else if (st.auth_url) {
        badge.className = "pill accent";
        badge.textContent = t("plugin_connect_waiting_user");
      } else {
        badge.className = "pill";
        badge.textContent = t("plugin_connect_preparing");
      }
    }

    if (urlWrap) {
      urlWrap.innerHTML = "";
      if (st.auth_url) {
        const a = document.createElement("a");
        a.href = st.auth_url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.className = "btn btn-primary";
        const disp = (activeConnectPlugin && activeConnectPlugin.display_name) || st.plugin_name || "Google";
        a.textContent = t("plugin_connect_open_google", { name: disp });
        urlWrap.appendChild(a);
      } else {
        const hint = document.createElement("span");
        hint.className = "muted";
        hint.textContent = t("plugin_connect_starting_hint");
        urlWrap.appendChild(hint);
      }
    }

    if ($("pluginConnectStep1")) {
      $("pluginConnectStep1").classList.toggle("hidden", Boolean(st.connected));
    }
    if (cbForm) cbForm.classList.toggle("hidden", st.flow_type !== "localhost_callback" || Boolean(st.connected));
    if (codeForm) codeForm.classList.toggle("hidden", st.flow_type !== "code_paste" || Boolean(st.connected));

    if (outEl) {
      outEl.textContent = st.error
        ? t("chat_error_prefix", { msg: st.error })
        : (st.output || t("oauth_starting"));
      outEl.scrollTop = outEl.scrollHeight;
    }
  }

  async function openPluginConnectModal(plugin, { forceReconnect = false } = {}) {
    activeConnectPlugin = plugin;
    const modal = $("pluginConnectModal");
    if (!modal) return;
    $("pluginConnectTitle").textContent = t("plugin_connect_title", { name: plugin.display_name || plugin.name });
    $("pluginCallbackInput").value = "";
    $("pluginCodeInput").value = "";
    $("pluginManualInput").value = "";
    renderPluginConnectState({
      running: true,
      plugin_name: plugin.name,
      flow_type: "",
      auth_url: "",
      output: "",
      connected: false,
      error: null,
    });
    modal.classList.remove("hidden");

    if (pluginConnectTimer) { clearInterval(pluginConnectTimer); pluginConnectTimer = null; }
    let connectSuccessHandled = false;
    const handleConnected = async (curState) => {
      if (connectSuccessHandled) return;
      connectSuccessHandled = true;
      if (pluginConnectTimer) { clearInterval(pluginConnectTimer); pluginConnectTimer = null; }
      const acctSuffix = curState.account ? ` (${curState.account})` : "";
      const disp = ((plugin && plugin.display_name) || curState.plugin_name || "Plugin") + acctSuffix;
      toast(t("toast_plugin_connected", { name: disp }));
      await loadPlugins();
      setTimeout(() => { closePluginConnectModal(); }, 900);
    };

    try {
      const st = await api(`/api/plugins/${encodeURIComponent(plugin.name)}/connect/start`, {
        method: "POST",
        body: JSON.stringify({
          install_target: plugin.install_target || null,
          force_reconnect: Boolean(forceReconnect),
        }),
      });
      renderPluginConnectState(st);
      await loadPlugins();
      if (st.connected) {
        await handleConnected(st);
        return;
      }
      pluginConnectTimer = setInterval(async () => {
        try {
          const cur = await api("/api/plugins/connect/status");
          renderPluginConnectState(cur);
          if (cur.connected) {
            await handleConnected(cur);
          } else if (!cur.running && pluginConnectTimer) {
            clearInterval(pluginConnectTimer);
            pluginConnectTimer = null;
          }
        } catch (_) {}
      }, 1000);
    } catch (err) {
      toast(err.message, "err");
      renderPluginConnectState({
        running: false,
        plugin_name: plugin.name,
        flow_type: "",
        auth_url: "",
        output: "",
        connected: false,
        error: err.message,
      });
    }
  }

  async function closePluginConnectModal() {
    if (pluginConnectTimer) { clearInterval(pluginConnectTimer); pluginConnectTimer = null; }
    const modal = $("pluginConnectModal");
    if (modal) modal.classList.add("hidden");
    activeConnectPlugin = null;
    try { await api("/api/plugins/connect/stop", { method: "POST" }); } catch (_) {}
  }

  if ($("closePluginConnectBtn")) $("closePluginConnectBtn").onclick = closePluginConnectModal;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("pluginConnectModal") && !$("pluginConnectModal").classList.contains("hidden")) {
      closePluginConnectModal();
    }
  });

  if ($("pluginCallbackForm")) {
    $("pluginCallbackForm").onsubmit = async (e) => {
      e.preventDefault();
      const urlVal = $("pluginCallbackInput").value.trim();
      if (!urlVal) return;
      try {
        const res = await api("/api/plugins/connect/callback", {
          method: "POST",
          body: JSON.stringify({ callback_url: urlVal }),
        });
        const disp = (activeConnectPlugin && activeConnectPlugin.display_name) || res.plugin || "Plugin";
        toast(t("toast_plugin_connected", { name: disp }));
        await closePluginConnectModal();
        await loadPlugins();
      } catch (err) { toast(err.message, "err"); }
    };
  }

  if ($("pluginCodeForm")) {
    $("pluginCodeForm").onsubmit = async (e) => {
      e.preventDefault();
      const val = $("pluginCodeInput").value.trim();
      if (!val) return;
      try {
        const st = await api("/api/plugins/connect/input", {
          method: "POST",
          body: JSON.stringify({ value: val }),
        });
        $("pluginCodeInput").value = "";
        renderPluginConnectState(st);
        toast(t("toast_plugin_code_sent"));
        await loadPlugins();
      } catch (err) { toast(err.message, "err"); }
    };
  }

  document.querySelectorAll(".plugin-key-btn").forEach((btn) => {
    btn.onclick = async () => {
      try {
        const st = await api("/api/plugins/connect/input", {
          method: "POST",
          body: JSON.stringify({ key: btn.dataset.key }),
        });
        renderPluginConnectState(st);
      } catch (err) { toast(err.message, "err"); }
    };
  });

  if ($("pluginManualInputForm")) {
    $("pluginManualInputForm").onsubmit = async (e) => {
      e.preventDefault();
      const val = $("pluginManualInput").value;
      try {
        const st = await api("/api/plugins/connect/input", {
          method: "POST",
          body: JSON.stringify({ value: val }),
        });
        $("pluginManualInput").value = "";
        renderPluginConnectState(st);
      } catch (err) { toast(err.message, "err"); }
    };
  }

  // Security (Password & 2FA)
  function updateSecurityUi(s) {
    if ($("accEmail")) $("accEmail").value = s.email || "";
    $("pwdCurrentWrap").classList.toggle("hidden", !s.has_password);
    $("disable2faPwd").classList.toggle("hidden", !s.has_password);
    $("totpBadge").textContent = s.totp_enabled ? t("totp_on") : t("totp_off");
    $("totpBadge").className = `pill ${s.totp_enabled ? "ok" : ""}`;
    $("totpDisabledBox").classList.toggle("hidden", Boolean(s.totp_enabled));
    $("totpEnabledBox").classList.toggle("hidden", !s.totp_enabled);
  }

  $("accountEmailForm").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/account/email", { method: "POST", body: JSON.stringify({ email: $("accEmail").value.trim() }) });
      updateSecurityUi(r.owner);
      toast(t("toast_email_saved"));
    } catch (err) { toast(err.message, "err"); }
  };

  $("accountPasswordForm").onsubmit = async (e) => {
    e.preventDefault();
    const pw = $("accNewPassword").value;
    if (pw !== $("accConfirmPassword").value) return toast(t("err_passwords_mismatch"), "err");
    try {
      const r = await api("/api/account/password", {
        method: "POST",
        body: JSON.stringify({ current_password: $("accCurrentPassword").value, new_password: pw }),
      });
      if (r.session_token) localStorage.setItem("agy_web_token", r.session_token);
      $("accCurrentPassword").value = ""; $("accNewPassword").value = ""; $("accConfirmPassword").value = "";
      updateSecurityUi(r.owner);
      toast(t("toast_password_saved"));
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
    toast(t("toast_key_copied"));
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
      toast(t("toast_2fa_enabled"));
    } catch (err) { toast(err.message, "err"); }
  };

  $("copyRecoveryBtn").onclick = async () => {
    await navigator.clipboard.writeText(latestRecoveryCodes.join("\n"));
    toast(t("toast_codes_copied"));
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
      toast(t("toast_2fa_disabled"));
    } catch (err) { toast(err.message, "err"); }
  };

  loadAuthAndBootstrap();
})();
