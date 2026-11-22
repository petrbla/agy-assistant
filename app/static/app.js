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
  let memoryMode = localStorage.getItem("agy_mem_mode") || (window.innerWidth >= 992 ? "split" : "edit");
  if (!["edit", "split", "preview"].includes(memoryMode)) memoryMode = "split";
  let isSending = false;
  let authPollTimer = null;
  let settingsOauthTerm = null;
  let onboardOauthTerm = null;
  let pluginConnectTerm = null;
  const FEATURED_PLUGINS = ["gmail", "gcalendar", "gdrive"];
  const BS_ICONS = {
    sun: "bi-sun",
    moon: "bi-moon-stars",
    globe: "bi-globe2",
    chevronDown: "bi-chevron-down",
    chevronUp: "bi-chevron-up",
    play: "bi-play-fill",
    pause: "bi-pause-fill",
    trash: "bi-trash",
    sliders: "bi-sliders",
    plus: "bi-plus-lg",
    puzzle: "bi-puzzle",
    x: "bi-x-lg",
    info: "bi-info-circle",
    sparkles: "bi-stars",
    logOut: "bi-box-arrow-right",
    link: "bi-link-45deg",
    refresh: "bi-arrow-clockwise",
    shieldCheck: "bi-shield-check",
    search: "bi-search",
  };

  function uiIcon(name, extraCls = "") {
    const biCls = BS_ICONS[name] || "bi-dot";
    const cls = extraCls ? `bi ${biCls} ${extraCls}` : `bi ${biCls}`;
    return `<i class="${cls}" aria-hidden="true"></i>`;
  }

  function badgeCls(kind = "") {
    const base = "badge rounded-pill text-nowrap flex-shrink-0";
    if (kind === "ok" || kind === "success") return `${base} text-bg-success`;
    if (kind === "warn" || kind === "warning") return `${base} text-bg-warning`;
    if (kind === "err" || kind === "danger") return `${base} text-bg-danger`;
    if (kind === "accent" || kind === "primary") return `${base} text-bg-primary`;
    return `${base} text-bg-secondary`;
  }

  let pluginsCatalog = [];
  let activePluginCategory = "all";
  let showAllPlugins = false;
  let permsModalState = null;
  const addAccountOpenFor = new Set();
  const customUrlOpenFor = new Set();
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
        el.className = "badge rounded-pill border-0 font-monospace version-tag text-bg-warning";
        el.title = t("tooltip_app_ver_update", {
          current: info.app_version,
          latest: info.app_latest_version,
        });
      } else if (info.app_up_to_date === true) {
        el.textContent = `v${info.app_version} ✓`;
        el.className = "badge rounded-pill border-0 font-monospace version-tag text-bg-success";
        el.title = t("tooltip_app_ver_current", { ver: info.app_version });
      } else {
        el.textContent = `v${info.app_version}`;
        el.className = "badge rounded-pill border-0 font-monospace version-tag text-bg-secondary";
        el.title = t("tooltip_app_ver_check", { ver: info.app_version });
      }
    }

    const badge = $("appVerBadge");
    if (badge && info.app_version) {
      if (info.app_update_available && info.app_latest_version) {
        badge.textContent = t("app_ver_available", { ver: info.app_latest_version });
        badge.className = badgeCls("warn");
        badge.classList.remove("d-none");
      } else if (info.app_up_to_date === true) {
        badge.textContent = t("app_ver_current", { ver: info.app_version });
        badge.className = badgeCls("ok");
        badge.classList.remove("d-none");
      } else {
        badge.textContent = t("app_ver_plain", { ver: info.app_version });
        badge.className = badgeCls();
        badge.classList.remove("d-none");
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
      banner.classList.toggle("d-none", !showBanner);
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
    if (lastPluginConnectState && $("pluginConnectModal") && !$("pluginConnectModal").classList.contains("d-none")) {
      renderPluginConnectState(lastPluginConnectState);
    }
    if (typeof syncMemoryModeUi === "function") syncMemoryModeUi();
    if (settingsOauthTerm) settingsOauthTerm.refreshStatusLabel();
    if (onboardOauthTerm) onboardOauthTerm.refreshStatusLabel();
    if (pluginConnectTerm) pluginConnectTerm.refreshStatusLabel();
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
      if (isAuthenticated && activeTab === "plugin-perms") {
        await syncRouteFromLocation({ reloadPlugins: true });
      }
    } catch (err) {
      toast(err.message, "err");
    }
  }

  // Theme (dark / light)
  function applyTheme(tMode) {
    const theme = tMode === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-bs-theme", theme);
    localStorage.setItem("agy_theme", theme);
    $("themeBtn").innerHTML = theme === "dark" ? uiIcon("sun") : uiIcon("moon");
  }
  applyTheme(localStorage.getItem("agy_theme") || "dark");
  $("themeBtn").onclick = () => {
    const cur = document.documentElement.getAttribute("data-bs-theme");
    applyTheme(cur === "dark" ? "light" : "dark");
  };

  // Toast
  let toastTimer = null;
  function toast(msg, kind = "ok") {
    const el = $("toast");
    el.textContent = msg;
    el.className = `app-toast show ${kind}`;
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

  // Navigation & URL routing
  function isModifiedClick(e) {
    return Boolean(e && (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0));
  }

  function pluginPermsUrl(pluginName, { accountId = null, mcpMode = false } = {}) {
    const base = `/settings/plugins/${encodeURIComponent(pluginName)}`;
    const qs = new URLSearchParams();
    if (mcpMode) {
      qs.set("mode", "mcp");
    } else if (accountId != null && String(accountId).trim() !== "") {
      qs.set("account", String(accountId));
    }
    const q = qs.toString();
    return q ? `${base}?${q}` : base;
  }

  function parseCurrentRoute() {
    const path = (window.location.pathname || "/").replace(/\/+$/, "") || "/";
    const params = new URLSearchParams(window.location.search || "");
    const pluginMatch = path.match(/^\/settings\/plugins\/([^/]+)$/);
    if (pluginMatch) {
      let pluginName = pluginMatch[1];
      try {
        pluginName = decodeURIComponent(pluginName);
      } catch (_) {}
      const rawAcct = params.get("account");
      const parsedAcct = rawAcct && /^\d+$/.test(rawAcct) ? Number(rawAcct) : null;
      return {
        tab: "plugin-perms",
        pluginName,
        accountId: parsedAcct,
        mcpMode: params.get("mode") === "mcp",
      };
    }
    if (path === "/memory") return { tab: "memory" };
    if (path === "/ops") return { tab: "ops" };
    if (path === "/settings") return { tab: "settings" };
    return { tab: "chat" };
  }

  function activateTabDom(tab) {
    activeTab = tab;
    const navActive = tab === "plugin-perms" ? "settings" : tab;
    document.querySelectorAll(".nav-tab").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === navActive);
    });
    ["chat", "memory", "ops", "settings", "plugin-perms"].forEach((tabId) => {
      const sec = $(`tab-${tabId}`);
      if (sec) sec.classList.toggle("d-none", tabId !== tab);
    });
  }

  async function syncRouteFromLocation({ reloadPlugins = false } = {}) {
    const route = parseCurrentRoute();
    if (route.tab === "plugin-perms") {
      await openPluginPermsPage({
        pluginName: route.pluginName,
        accountId: route.accountId,
        mcpMode: route.mcpMode,
        reloadPlugins,
      });
      return;
    }
    permsModalState = null;
    activateTabDom(route.tab);
    if (route.tab === "chat") renderChat();
    else if (route.tab === "memory") loadFiles();
    else if (route.tab === "ops") loadOps();
    else if (route.tab === "settings") { loadSettings(); loadMcp(); loadPlugins(); }
  }

  function navigateTo(url, { replace = false } = {}) {
    const target = url || "/chat";
    const current = `${window.location.pathname}${window.location.search}`;
    if (current !== target) {
      history[replace ? "replaceState" : "pushState"](null, "", target);
    }
    if (isAuthenticated && !inAgyOnboarding) {
      syncRouteFromLocation();
    }
  }

  function switchTab(tab, { pushHistory = true } = {}) {
    const validTab = ["chat", "memory", "ops", "settings"].includes(tab) ? tab : "chat";
    if (pushHistory) {
      navigateTo(`/${validTab}`);
      return;
    }
    permsModalState = null;
    activateTabDom(validTab);
    if (validTab === "chat") renderChat();
    else if (validTab === "memory") loadFiles();
    else if (validTab === "ops") loadOps();
    else if (validTab === "settings") { loadSettings(); loadMcp(); loadPlugins(); }
  }

  document.querySelectorAll(".nav-tab").forEach((b) => {
    b.addEventListener("click", (e) => {
      if (b.classList.contains("disabled") || !isAuthenticated || inAgyOnboarding) {
        e.preventDefault();
        return;
      }
      if (isModifiedClick(e)) return;
      e.preventDefault();
      switchTab(b.dataset.tab);
    });
  });

  window.addEventListener("popstate", () => {
    if (isAuthenticated && !inAgyOnboarding) {
      syncRouteFromLocation();
    }
  });

  function showAuthScreen(mode) {
    inAgyOnboarding = mode === "agy-onboarding";
    isAuthenticated = inAgyOnboarding;
    $("authScreen").classList.remove("d-none");
    $("authScreen").classList.toggle("wide", inAgyOnboarding);
    $("appMain").classList.add("d-none");
    $("logoutBtn").classList.toggle("d-none", !inAgyOnboarding || !localStorage.getItem("agy_web_token"));
    document.querySelectorAll(".nav-tab").forEach((b) => {
      b.classList.add("disabled");
      b.setAttribute("aria-disabled", "true");
      b.setAttribute("tabindex", "-1");
    });
    $("initialSetupCard").classList.toggle("d-none", mode !== "initial");
    $("loginCard").classList.toggle("d-none", mode !== "login");
    if ($("agyOnboardingCard")) $("agyOnboardingCard").classList.toggle("d-none", !inAgyOnboarding);
  }

  function showAppMain() {
    inAgyOnboarding = false;
    isAuthenticated = true;
    authScreenStatusKey = "";
    $("authScreen").classList.add("d-none");
    $("authScreen").classList.remove("wide");
    $("appMain").classList.remove("d-none");
    document.querySelectorAll(".nav-tab").forEach((b) => {
      b.classList.remove("disabled");
      b.setAttribute("aria-disabled", "false");
      b.removeAttribute("tabindex");
    });
    $("logoutBtn").classList.toggle("d-none", !localStorage.getItem("agy_web_token"));
  }

  function showAuthErr(msg) {
    const el = $("authError");
    el.textContent = msg || "";
    el.classList.toggle("d-none", !msg);
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
      $("tokenLoginWrap").classList.toggle("d-none", !st.has_token_configured);
      if (st.needs_initial_setup) {
        authScreenStatusKey = "status_initial_setup";
        $("statusPill").textContent = t(authScreenStatusKey);
        $("statusPill").className = badgeCls("warn");
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
      if (window.location.pathname === "/" || window.location.pathname === "") {
        history.replaceState(null, "", "/chat");
      }
      await syncRouteFromLocation();
    } catch (_) {
      isAuthenticated = false;
      inAgyOnboarding = false;
      lastStatus = null;
      if ($("appUpdateBanner")) $("appUpdateBanner").classList.add("d-none");
      authScreenStatusKey = "status_signed_out";
      $("statusPill").textContent = t(authScreenStatusKey);
      $("statusPill").className = badgeCls();
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
        $("loginStep1Form").classList.add("d-none");
        $("loginStep2Form").classList.remove("d-none");
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
      $("loginStep2Form").classList.add("d-none");
      $("loginStep1Form").classList.remove("d-none");
      toast(t("toast_signed_in_2fa"));
      await loadAuthAndBootstrap();
    } catch (err) {
      showAuthErr(err.message);
    }
  };

  $("login2faBackBtn").onclick = () => {
    pending2faChallenge = "";
    $("loginStep2Form").classList.add("d-none");
    $("loginStep1Form").classList.remove("d-none");
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
    if ($("appUpdateBanner")) $("appUpdateBanner").classList.add("d-none");
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
      pill.className = badgeCls("err");
      pill.textContent = t("status_agy_unavailable");
    } else if (!s.agy_authenticated) {
      dot.className = "brand-dot warn";
      pill.className = badgeCls("warn");
      pill.textContent = t("status_agy_unauthenticated", { version: s.agy_version || "agy" });
    } else {
      dot.className = "brand-dot ok";
      pill.className = badgeCls("ok");
      pill.textContent = s.agy_version ? `agy ${s.agy_version}` : t("status_online");
    }
    updateAgyBadges(s);
    updateSecurityUi(s);
    const banner = $("chatAgyAuthBanner");
    if (banner) banner.classList.toggle("d-none", !s.agy_available || Boolean(s.agy_authenticated));
  }

  function updateAgyBadges(info) {
    const authBadge = $("agyAuthBadge");
    const onboardBadge = $("onboardAgyBadge");
    const finishBtn = $("finishAgyOnboardBtn");
    const logoutBtn = $("logoutAgyBtn");
    const authBody = $("agyAuthBody");
    const authHeader = $("agyAuthHeader");
    const parts = [];
    if (info.has_agy_oauth) parts.push(t("agy_auth_oauth"));
    if (info.has_gemini_api_key) parts.push(t("agy_auth_api_key"));
    if (parts.length) {
      const label = t("agy_auth_signed_in", { parts: parts.join(" + ") });
      authBadge.textContent = label;
      authBadge.className = badgeCls("ok");
      if (onboardBadge) {
        onboardBadge.textContent = label;
        onboardBadge.className = badgeCls("ok");
      }
      if (finishBtn) finishBtn.classList.remove("d-none");
      logoutBtn.classList.remove("d-none");
      if (authBody) authBody.classList.add("d-none");
      if (authHeader) authHeader.classList.add("border-bottom-0");
    } else if (typeof info.has_agy_oauth === "boolean") {
      authBadge.textContent = t("agy_auth_signed_out");
      authBadge.className = badgeCls("err");
      if (onboardBadge) {
        onboardBadge.textContent = t("agy_auth_signed_out");
        onboardBadge.className = badgeCls("err");
      }
      if (finishBtn) finishBtn.classList.add("d-none");
      logoutBtn.classList.add("d-none");
      if (authBody) authBody.classList.remove("d-none");
      if (authHeader) authHeader.classList.remove("border-bottom-0");
    }

    const verBadge = $("agyVerBadge");
    const updBtn = $("updateAgyBtn");
    if (info.agy_update_available && info.agy_latest_version) {
      verBadge.textContent = t("agy_ver_available", { ver: info.agy_latest_version });
      verBadge.className = badgeCls("warn");
      updBtn.classList.remove("d-none");
    } else if (info.agy_up_to_date === true) {
      verBadge.textContent = t("agy_ver_current", { ver: info.agy_version });
      verBadge.className = badgeCls("ok");
      updBtn.classList.add("d-none");
    } else if (info.agy_version) {
      verBadge.textContent = info.agy_version;
      verBadge.className = badgeCls();
      updBtn.classList.add("d-none");
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
      wrap.classList.add("d-none");
      return;
    }
    wrap.classList.remove("d-none");
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

  // Memory & Markdown editor
  function isMarkdownFile(path) {
    return Boolean(path && /\.(md|markdown)$/i.test(String(path).trim()));
  }

  function escapeAttr(str) {
    return escapeHtml(str).replace(/"/g, "&quot;");
  }

  function renderInlineMd(raw) {
    const stash = [];
    let s = String(raw || "").replace(/`([^`\n]+)`/g, (_, code) => {
      const idx = stash.push(`<code>${escapeHtml(code)}</code>`) - 1;
      return `\x00IC${idx}\x00`;
    });
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) => {
      const idx = stash.push(
        `<a href="${escapeAttr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`
      ) - 1;
      return `\x00IC${idx}\x00`;
    });
    s = escapeHtml(s)
      .replace(/\*\*([^*\n]+?)\*\*/g, "<strong>$1</strong>")
      .replace(/~~([^~\n]+?)~~/g, "<del>$1</del>")
      .replace(/(?<!\*)\*([^*\n]+?)\*(?!\*)/g, "<em>$1</em>");
    stash.forEach((html, i) => {
      s = s.replaceAll(`\x00IC${i}\x00`, html);
    });
    return s;
  }

  function renderRichMarkdown(src) {
    const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;
    let taskCounter = 0;

    const isTableSep = (line) => /^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$/.test(line);
    const splitTableRow = (line) => {
      let trimmed = line.trim();
      if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
      if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
      return trimmed.split("|").map((c) => c.trim());
    };

    while (i < lines.length) {
      const line = lines[i];

      if (!line.trim()) {
        i += 1;
        continue;
      }

      // Fenced code block
      const fenceMatch = line.match(/^```([a-zA-Z0-9_+-]*)\s*$/);
      if (fenceMatch) {
        i += 1;
        const codeLines = [];
        while (i < lines.length && !/^```\s*$/.test(lines[i])) {
          codeLines.push(lines[i]);
          i += 1;
        }
        if (i < lines.length) i += 1;
        out.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        continue;
      }

      // Heading h1..h4
      const headMatch = line.match(/^(#{1,4})\s+(.+?)\s*#*\s*$/);
      if (headMatch) {
        const lvl = headMatch[1].length;
        out.push(`<h${lvl}>${renderInlineMd(headMatch[2])}</h${lvl}>`);
        i += 1;
        continue;
      }

      // Horizontal rule
      if (/^(?:---|\*\*\*|___)\s*$/.test(line.trim())) {
        out.push("<hr />");
        i += 1;
        continue;
      }

      // GFM Table
      if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        const headers = splitTableRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
          rows.push(splitTableRow(lines[i]));
          i += 1;
        }
        const ths = headers.map((h) => `<th>${renderInlineMd(h)}</th>`).join("");
        const trs = rows.map(
          (r) => `<tr>${headers.map((_, ci) => `<td>${renderInlineMd(r[ci] || "")}</td>`).join("")}</tr>`
        ).join("");
        out.push(
          `<div class="table-responsive my-2"><table class="table table-sm table-bordered mb-0"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table></div>`
        );
        continue;
      }

      // Blockquote
      if (/^\s*>\s?/.test(line)) {
        const qLines = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          qLines.push(lines[i].replace(/^\s*>\s?/, ""));
          i += 1;
        }
        out.push(`<blockquote>${qLines.map((ql) => renderInlineMd(ql)).join("<br />")}</blockquote>`);
        continue;
      }

      // Unordered / checklist
      if (/^\s*[-*+]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          const cur = lines[i];
          const taskMatch = cur.match(/^\s*[-*+]\s+\[( |x|X)\]\s+(.*)$/);
          if (taskMatch) {
            const checked = taskMatch[1].toLowerCase() === "x";
            const idx = taskCounter++;
            items.push(
              `<li class="md-task-item"><label class="d-inline-flex align-items-start gap-2"><input type="checkbox" class="form-check-input md-task-cb mt-1" data-task-index="${idx}" ${checked ? "checked" : ""} /><span>${renderInlineMd(taskMatch[2])}</span></label></li>`
            );
          } else {
            const txt = cur.replace(/^\s*[-*+]\s+/, "");
            items.push(`<li>${renderInlineMd(txt)}</li>`);
          }
          i += 1;
        }
        out.push(`<ul>${items.join("")}</ul>`);
        continue;
      }

      // Ordered list
      if (/^\s*\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          items.push(`<li>${renderInlineMd(lines[i].replace(/^\s*\d+\.\s+/, ""))}</li>`);
          i += 1;
        }
        out.push(`<ol>${items.join("")}</ol>`);
        continue;
      }

      // Paragraph
      const pLines = [];
      while (
        i < lines.length
        && lines[i].trim()
        && !/^```/.test(lines[i])
        && !/^#{1,4}\s+/.test(lines[i])
        && !/^(?:---|\*\*\*|___)\s*$/.test(lines[i].trim())
        && !/^\s*>\s?/.test(lines[i])
        && !/^\s*[-*+]\s+/.test(lines[i])
        && !/^\s*\d+\.\s+/.test(lines[i])
        && !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]))
      ) {
        pLines.push(lines[i]);
        i += 1;
      }
      out.push(`<p>${pLines.map((pl) => renderInlineMd(pl)).join("<br />")}</p>`);
    }

    return out.join("");
  }

  function renderMemoryPreview() {
    const prevEl = $("memoryPreview");
    const ed = $("fileEditor");
    if (!prevEl || !ed) return;
    if (!currentFile) {
      prevEl.innerHTML = `<div class="text-body-secondary small">${escapeHtml(t("mem_preview_empty"))}</div>`;
      return;
    }
    prevEl.innerHTML = renderRichMarkdown(ed.value);
    prevEl.querySelectorAll(".md-task-cb").forEach((cb) => {
      cb.onchange = () => {
        const targetIdx = Number(cb.dataset.taskIndex);
        let seen = 0;
        const updated = ed.value.replace(/^(\s*[-*+]\s+\[)( |x|X)(\])/gm, (full, pre, _mark, post) => {
          if (seen++ === targetIdx) {
            return `${pre}${cb.checked ? "x" : " "}${post}`;
          }
          return full;
        });
        if (updated !== ed.value) {
          ed.value = updated;
          fileDirty = true;
          $("fileDirtyHint").textContent = t("file_hint_dirty");
          renderMemoryPreview();
        }
      };
    });
  }

  function syncMemoryModeUi() {
    const ws = $("memoryWorkspace");
    const tb = $("memoryToolbar");
    const seg = $("memoryModeSeg");
    const hasFile = Boolean(currentFile);
    const isMd = hasFile && isMarkdownFile(currentFile);
    const effMode = isMd ? memoryMode : "edit";

    if (ws) ws.className = `memory-workspace mode-${effMode}`;
    if (tb) tb.classList.toggle("d-none", !(isMd && effMode !== "preview"));
    if (seg) {
      seg.querySelectorAll("[data-mem-mode]").forEach((btn) => {
        const m = btn.dataset.memMode;
        btn.classList.toggle("active", m === effMode);
        btn.disabled = !hasFile || (!isMd && m !== "edit");
      });
    }
    renderMemoryPreview();
  }

  function setMemoryMode(mode, { persist = true } = {}) {
    if (!["edit", "split", "preview"].includes(mode)) return;
    memoryMode = mode;
    if (persist) localStorage.setItem("agy_mem_mode", mode);
    syncMemoryModeUi();
    if (mode !== "preview" && $("fileEditor") && !$("fileEditor").disabled) {
      $("fileEditor").focus();
    }
  }

  document.querySelectorAll("#memoryModeSeg [data-mem-mode]").forEach((btn) => {
    btn.onclick = () => setMemoryMode(btn.dataset.memMode);
  });

  function applyMarkdownAction(action) {
    const ed = $("fileEditor");
    if (!ed || ed.disabled) return;
    const val = ed.value;
    const start = ed.selectionStart || 0;
    const end = ed.selectionEnd || 0;
    const selected = val.slice(start, end);

    const wrapSelection = (before, after, placeholder) => {
      const inner = selected || placeholder;
      const inserted = `${before}${inner}${after}`;
      ed.value = val.slice(0, start) + inserted + val.slice(end);
      ed.selectionStart = start + before.length;
      ed.selectionEnd = start + before.length + inner.length;
    };

    const toggleLinePrefix = (prefix, { replaceHeading = false } = {}) => {
      const lineStart = val.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
      const lineEndIdx = val.indexOf("\n", end);
      const lineEnd = lineEndIdx === -1 ? val.length : lineEndIdx;
      let block = val.slice(lineStart, lineEnd);
      if (replaceHeading) {
        block = block.split("\n").map((ln) => prefix + ln.replace(/^#{1,6}\s+/, "")).join("\n");
      } else {
        block = block.split("\n").map((ln) => (ln.startsWith(prefix) ? ln.slice(prefix.length) : prefix + ln)).join("\n");
      }
      ed.value = val.slice(0, lineStart) + block + val.slice(lineEnd);
      ed.selectionStart = lineStart;
      ed.selectionEnd = lineStart + block.length;
    };

    if (action === "h1") toggleLinePrefix("# ", { replaceHeading: true });
    else if (action === "h2") toggleLinePrefix("## ", { replaceHeading: true });
    else if (action === "bold") wrapSelection("**", "**", "text");
    else if (action === "italic") wrapSelection("*", "*", "text");
    else if (action === "ul") toggleLinePrefix("- ");
    else if (action === "ol") toggleLinePrefix("1. ");
    else if (action === "task") toggleLinePrefix("- [ ] ");
    else if (action === "quote") toggleLinePrefix("> ");
    else if (action === "code") {
      if (selected.includes("\n")) {
        wrapSelection("```\n", "\n```", "code");
      } else {
        wrapSelection("`", "`", "code");
      }
    } else if (action === "link") {
      const txt = selected || "text";
      const inserted = `[${txt}](https://)`;
      ed.value = val.slice(0, start) + inserted + val.slice(end);
      ed.selectionStart = start + txt.length + 3;
      ed.selectionEnd = start + inserted.length - 1;
    }

    fileDirty = true;
    $("fileDirtyHint").textContent = t("file_hint_dirty");
    renderMemoryPreview();
    ed.focus();
  }

  document.querySelectorAll("#memoryToolbar .md-tb-btn").forEach((btn) => {
    btn.onclick = () => applyMarkdownAction(btn.dataset.mdAction);
  });

  async function loadFiles() {
    try {
      allFiles = await api("/api/files");
      renderFileList();
      if (!currentFile && allFiles.length > 0) {
        const pref = allFiles.find((f) => f.path.toLowerCase() === "memory.md") || allFiles[0];
        if (pref) await openFile(pref.path);
      } else {
        syncMemoryModeUi();
      }
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
        b.className = "list-group-item list-group-item-action font-monospace small py-2" + (f.path === currentFile ? " active" : "");
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
      syncMemoryModeUi();
    } catch (err) {
      toast(err.message, "err");
    }
  }

  $("fileEditor").oninput = () => {
    fileDirty = true;
    $("fileDirtyHint").textContent = t("file_hint_dirty");
    renderMemoryPreview();
  };

  $("fileEditor").addEventListener("keydown", (e) => {
    const ed = $("fileEditor");
    if (e.key === "Tab") {
      e.preventDefault();
      const s = ed.selectionStart || 0;
      const end = ed.selectionEnd || 0;
      ed.value = `${ed.value.slice(0, s)}  ${ed.value.slice(end)}`;
      ed.selectionStart = ed.selectionEnd = s + 2;
      fileDirty = true;
      $("fileDirtyHint").textContent = t("file_hint_dirty");
      renderMemoryPreview();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
      e.preventDefault();
      applyMarkdownAction("bold");
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "i") {
      e.preventDefault();
      applyMarkdownAction("italic");
    }
  });

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
      syncMemoryModeUi();
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
        label.className = "small fw-semibold text-uppercase text-body-secondary me-1";
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
      jl.classList.toggle("d-none", !systemJobs.length);

      const tb = $("tasksBody");
      tb.innerHTML = "";
      if (!tasks.length) {
        tb.innerHTML = `<tr><td colspan="6" class="text-body-secondary text-center py-3">${escapeHtml(t("tasks_empty"))}</td></tr>`;
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
          ? `<span class="${badgeCls("primary")}">${escapeHtml(t("task_type_ai"))}</span>`
          : `<span class="${badgeCls()}">${escapeHtml(t("task_type_msg"))}</span>`;
        const statePill = task.enabled
          ? `<span class="${badgeCls("ok")}">${escapeHtml(t("task_state_active"))}</span>`
          : `<span class="${badgeCls()}">${escapeHtml(t("task_state_paused"))}</span>`;
        tr.innerHTML =
          `<td class="task-name fw-semibold">${escapeHtml(task.name)}</td>` +
          `<td class="text-nowrap"><div title="${escapeHtml(raw)}">${escapeHtml(humanSchedule(task))}</div>` +
          `<div class="small text-body-secondary">${next ? escapeHtml(t("task_next_prefix", { time: fmtNextRun(next) })) : "—"}</div></td>` +
          `<td>${typePill}</td>` +
          `<td><div class="task-prompt" title="${escapeHtml(task.prompt)}">${escapeHtml(task.prompt)}</div></td>` +
          `<td>${statePill}</td>`;
        const td = document.createElement("td");
        td.className = "text-nowrap text-end";
        const mk = (iconName, cls, title, fn) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "btn btn-sm ms-1 " + cls;
          b.innerHTML = uiIcon(iconName);
          b.title = title;
          b.setAttribute("aria-label", title);
          b.onclick = async () => {
            try { await fn(); } catch (err) { toast(err.message, "err"); }
            loadOps();
          };
          return b;
        };
        td.append(
          mk("play", "btn-outline-secondary btn-icon-sm", t("task_action_run"), async () => {
            await api(`/api/tasks/${task.id}/run`, { method: "POST" });
            toast(t("toast_task_run", { name: task.name }));
          }),
          mk(task.enabled ? "pause" : "play", "btn-outline-secondary btn-icon-sm", task.enabled ? t("task_action_pause") : t("task_action_resume"), async () => {
            await api(`/api/tasks/${task.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !task.enabled }) });
            toast(task.enabled ? t("toast_task_paused") : t("toast_task_resumed"));
          }),
          mk("trash", "btn-outline-danger btn-icon-sm", t("task_action_delete"), async () => {
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
        tr.innerHTML = `<td class="font-monospace">${escapeHtml(dt)}</td><td>${escapeHtml(r.source)}</td><td>${escapeHtml(channelLabel(r.context_mode))}</td><td>${escapeHtml((r.prompt || "").slice(0, 90))}</td><td>${escapeHtml((r.response || r.error || "").slice(0, 110))}</td><td class="font-monospace">${tok}</td><td class="font-monospace">${dur}</td>`;
        rb.appendChild(tr);
      });
    } catch (err) {
      toast(err.message, "err");
    }
  }

  $("taskScheduleType").onchange = () => {
    const isOnce = $("taskScheduleType").value === "once";
    $("taskCron").classList.toggle("d-none", isOnce);
    $("taskRunAt").classList.toggle("d-none", !isOnce);
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

  // Živý interaktivní terminál (xterm.js přes WebSocket -> PTY)
  class LivePtyTerminal {
    constructor({ containerId, fallbackPreId, statusBadgeId, wsPath, onExit = null }) {
      this.containerId = containerId;
      this.fallbackPreId = fallbackPreId;
      this.statusBadgeId = statusBadgeId;
      this.wsPath = wsPath;
      this.onExit = onExit;
      this.term = null;
      this.fitAddon = null;
      this.ws = null;
      this.live = false;
      this._resizeTimer = null;
      this._resizeObserver = null;
      this._lastErrorShown = "";
    }

    isXtermAvailable() {
      return typeof window.Terminal === "function";
    }

    ensureInit() {
      if (this.term || !this.isXtermAvailable()) return;
      const container = $(this.containerId);
      if (!container) return;

      const fallback = $(this.fallbackPreId);
      if (fallback) fallback.classList.add("d-none");
      container.classList.remove("d-none");

      const term = new window.Terminal({
        cursorBlink: true,
        fontSize: 13,
        lineHeight: 1.2,
        scrollback: 3000,
        convertEol: false,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
        theme: {
          background: "#0b1220",
          foreground: "#e2e8f0",
          cursor: "#38bdf8",
          selectionBackground: "rgba(56, 189, 248, 0.35)",
        },
      });

      const FitCtor = window.FitAddon && window.FitAddon.FitAddon;
      if (typeof FitCtor === "function") {
        this.fitAddon = new FitCtor();
        term.loadAddon(this.fitAddon);
      }
      const WebLinksCtor = window.WebLinksAddon && window.WebLinksAddon.WebLinksAddon;
      if (typeof WebLinksCtor === "function") {
        term.loadAddon(new WebLinksCtor());
      }

      term.attachCustomKeyEventHandler((e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "c" && term.hasSelection()) {
          navigator.clipboard?.writeText(term.getSelection()).catch(() => {});
          term.clearSelection();
          return false;
        }
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") {
          return false;
        }
        return true;
      });

      term.open(container);
      term.onData((data) => this.sendInput(data));
      term.onResize(({ cols, rows }) => this.sendResize(cols, rows));

      if (typeof ResizeObserver === "function") {
        this._resizeObserver = new ResizeObserver(() => {
          clearTimeout(this._resizeTimer);
          this._resizeTimer = setTimeout(() => { this.fit(); }, 80);
        });
        this._resizeObserver.observe(container);
      }

      this.term = term;
      this.fit();
    }

    fit() {
      const container = $(this.containerId);
      if (this.fitAddon && container && container.offsetParent !== null && container.clientWidth > 0 && container.clientHeight > 0) {
        try {
          this.fitAddon.fit();
        } catch (_) {}
      }
      return {
        cols: (this.term && this.term.cols) ? this.term.cols : 100,
        rows: (this.term && this.term.rows) ? this.term.rows : 26,
      };
    }

    refreshStatusLabel() {
      const el = $(this.statusBadgeId);
      if (!el) return;
      el.className = this.live ? badgeCls("ok") : badgeCls();
      el.textContent = t(this.live ? "term_status_live" : "term_status_offline");
    }

    setLive(live) {
      this.live = Boolean(live);
      this.refreshStatusLabel();
    }

    connect({ reset = true } = {}) {
      this.ensureInit();
      this.disconnect({ keepContent: true });
      if (!this.term) return false;
      this._lastErrorShown = "";
      if (reset) this.term.reset();

      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${window.location.host}${this.wsPath}`);
      this.ws = ws;

      ws.onopen = () => {
        if (this.ws !== ws) return;
        const tok = (localStorage.getItem("agy_web_token") || "").trim();
        ws.send(JSON.stringify({ type: "auth", token: tok }));
      };

      ws.onmessage = (evt) => {
        if (this.ws !== ws) return;
        let msg = null;
        try { msg = JSON.parse(evt.data); } catch (_) { return; }
        if (!msg || typeof msg !== "object") return;
        if (msg.type === "ready") {
          this.setLive(Boolean(msg.running));
          const dims = this.fit();
          this.sendResize(dims.cols, dims.rows);
          if (msg.running) this.term.focus();
        } else if (msg.type === "output" && typeof msg.data === "string" && msg.data) {
          this.term.write(msg.data);
        } else if (msg.type === "exit") {
          this.setLive(false);
          if (typeof this.onExit === "function") this.onExit(msg.exit_code);
        }
      };

      ws.onclose = () => {
        if (this.ws === ws) {
          this.ws = null;
          this.setLive(false);
        }
      };

      ws.onerror = () => {
        if (this.ws === ws) this.setLive(false);
      };
      return true;
    }

    disconnect({ keepContent = true } = {}) {
      const ws = this.ws;
      this.ws = null;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        try { ws.close(); } catch (_) {}
      }
      this.setLive(false);
      if (!keepContent && this.term) {
        this.term.reset();
        this._lastErrorShown = "";
      }
    }

    isOpen() {
      return Boolean(this.ws && this.ws.readyState === WebSocket.OPEN);
    }

    sendInput(data) {
      if (!this.isOpen() || !data) return false;
      this.ws.send(JSON.stringify({ type: "input", data }));
      return true;
    }

    sendResize(cols, rows) {
      if (!this.isOpen() || !cols || !rows) return;
      this.ws.send(JSON.stringify({ type: "resize", cols, rows }));
    }

    writeNotice(text) {
      if (!text || this._lastErrorShown === text) return;
      this._lastErrorShown = text;
      if (this.term) {
        this.term.write(`\r\n\x1b[33m${text}\x1b[0m\r\n`);
      }
    }

    focus() {
      if (this.term) this.term.focus();
    }
  }

  settingsOauthTerm = new LivePtyTerminal({
    containerId: "oauthTerminal",
    fallbackPreId: "oauthOutput",
    statusBadgeId: "oauthTermStatus",
    wsPath: "/ws/pty/auth",
  });
  onboardOauthTerm = new LivePtyTerminal({
    containerId: "onboardOauthTerminal",
    fallbackPreId: "onboardOauthOutput",
    statusBadgeId: "onboardTermStatus",
    wsPath: "/ws/pty/auth",
  });

  function activeOauthTerminal() {
    return inAgyOnboarding ? onboardOauthTerm : settingsOauthTerm;
  }

  // OAuth PTY
  function renderOauthBox(outputId, urlsId, term, st) {
    const outEl = $(outputId);
    const urlsEl = $(urlsId);
    if (term && term.term) {
      if (st.error && !term.isOpen()) {
        term.writeNotice(t("chat_error_prefix", { msg: st.error }));
      }
    } else if (outEl) {
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
    renderOauthBox("oauthOutput", "oauthUrls", settingsOauthTerm, st);
    renderOauthBox("onboardOauthOutput", "onboardOauthUrls", onboardOauthTerm, st);
  }

  async function handleAgyAuthenticated() {
    if (authPollTimer) {
      clearInterval(authPollTimer);
      authPollTimer = null;
    }
    if (settingsOauthTerm) settingsOauthTerm.disconnect();
    if (onboardOauthTerm) onboardOauthTerm.disconnect();
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
    const term = activeOauthTerminal();
    let dims = { cols: 100, rows: 26 };
    if (term) {
      term.ensureInit();
      dims = term.fit();
    }
    let transitionDone = false;
    try {
      const st = await api("/api/auth/start", {
        method: "POST",
        body: JSON.stringify({ mode: "interactive", cols: dims.cols, rows: dims.rows }),
      });
      renderOauthState(st);
      if (term) {
        if (st.running) {
          term.connect({ reset: true });
        } else if (st.error) {
          term.writeNotice(st.error);
        }
      }
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
    const term = activeOauthTerminal();
    const payload = value ? `${value.replace(/[\r\n]+$/, "")}\r` : "\r";
    if (term && term.sendInput(payload)) {
      term.focus();
      return;
    }
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
    if (settingsOauthTerm) settingsOauthTerm.disconnect();
    if (onboardOauthTerm) onboardOauthTerm.disconnect();
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
      if (onboardOauthTerm) onboardOauthTerm.disconnect();
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
      if ($("appUpdateBanner")) $("appUpdateBanner").classList.add("d-none");
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
    $("mcpUrlWrap").classList.toggle("d-none", isStdio);
    $("mcpTokenWrap").classList.toggle("d-none", isStdio);
    $("mcpCmdWrap").classList.toggle("d-none", !isStdio);
    $("mcpArgsWrap").classList.toggle("d-none", !isStdio);
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
        tr.innerHTML = `<td><strong>${escapeHtml(srv.name)}</strong></td><td>${escapeHtml(labels[srv.scope] || labels.all)}</td><td class="font-monospace">${escapeHtml(target)}</td>`;
        const td = document.createElement("td");
        const del = document.createElement("button");
        del.className = "btn btn-outline-danger btn-sm";
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

  function countToolModes(tools) {
    let allow = 0;
    let ask = 0;
    let block = 0;
    (tools || []).forEach((item) => {
      if (item.mode === "allow") allow += 1;
      else if (item.mode === "ask") ask += 1;
      else block += 1;
    });
    return { allow, ask, block };
  }

  async function loadPlugins() {
    if (!$("pluginsGrid")) return;
    try {
      const data = await api(`/api/plugins?lang=${encodeURIComponent(currentLang || "en")}`);
      pluginsCatalog = Array.isArray(data.plugins) ? data.plugins : [];
      renderPlugins();
    } catch (_) {}
  }

  function scopeLabel(sc) {
    if (sc === "pracovni") return t("scope_work");
    if (sc === "osobni") return t("scope_personal");
    return t("scope_all_chats");
  }

  function pluginSortRank(p) {
    if (p.custom_url_connected || (p.installed && p.enabled && p.connected)) return 0;
    if (p.installed && p.enabled) return 1;
    if (p.installed) return 2;
    const featIdx = FEATURED_PLUGINS.indexOf(p.name);
    if (featIdx >= 0) return 3 + featIdx;
    return 100;
  }

  function renderPlugins() {
    const grid = $("pluginsGrid");
    if (!grid) return;
    grid.innerHTML = "";
    const query = ($("pluginSearchInput")?.value || "").trim().toLowerCase();

    if ($("pluginsFilterBar")) $("pluginsFilterBar").classList.toggle("d-none", !showAllPlugins);
    if ($("customPluginWrap")) $("customPluginWrap").classList.toggle("d-none", !showAllPlugins);

    const hiddenCount = pluginsCatalog.filter(
      (p) => !p.installed && !p.custom_url_connected && !FEATURED_PLUGINS.includes(p.name)
    ).length;
    let displayList;
    if (!showAllPlugins) {
      displayList = pluginsCatalog
        .filter((p) => p.installed || Boolean(p.custom_url_connected) || FEATURED_PLUGINS.includes(p.name))
        .sort((a, b) => pluginSortRank(a) - pluginSortRank(b));
    } else {
      displayList = pluginsCatalog
        .filter((p) => {
          if (activePluginCategory === "installed" && !p.installed && !p.custom_url_connected) return false;
          if (activePluginCategory !== "all" && activePluginCategory !== "installed" && p.category !== activePluginCategory) {
            return false;
          }
          if (!query) return true;
          const hay = `${p.name} ${p.display_name} ${pluginDescription(p)} ${p.marketplace}`.toLowerCase();
          return hay.includes(query);
        })
        .sort((a, b) => pluginSortRank(a) - pluginSortRank(b));
    }

    const moreWrap = $("pluginsMoreWrap");
    const moreBtn = $("toggleMorePluginsBtn");
    if (moreWrap && moreBtn) {
      if (showAllPlugins || hiddenCount > 0) {
        moreWrap.classList.remove("d-none");
        moreBtn.innerHTML = showAllPlugins
          ? `${uiIcon("chevronUp", "me-1")}<span>${escapeHtml(t("plugins_show_less"))}</span>`
          : `${uiIcon("chevronDown", "me-1")}<span>${escapeHtml(t("plugins_show_more", { count: hiddenCount }))}</span>`;
      } else {
        moreWrap.classList.add("d-none");
      }
    }

    if (!displayList.length) {
      const empty = document.createElement("div");
      empty.className = "text-body-secondary small";
      empty.textContent = t("plugins_empty");
      grid.appendChild(empty);
      return;
    }

    displayList.forEach((p) => {
      const card = document.createElement("div");
      card.className = (p.installed && p.enabled) || p.custom_url_connected
        ? "card h-100 shadow-sm border-primary plugin-card"
        : "card h-100 shadow-sm plugin-card";

      const cardBody = document.createElement("div");
      cardBody.className = "card-body d-flex flex-column gap-2 p-3";

      const head = document.createElement("div");
      head.className = "d-flex align-items-start justify-content-between gap-2";

      const titleWrap = document.createElement("div");
      titleWrap.className = "d-flex align-items-center gap-2 min-w-0";
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
          iconEl.innerHTML = uiIcon("puzzle", "fs-5");
        };
        iconEl.classList.add("has-logo");
        iconEl.appendChild(img);
      } else {
        iconEl.innerHTML = uiIcon("puzzle", "fs-5");
      }

      const accounts = Array.isArray(p.accounts) ? p.accounts : [];
      const connectedCount = accounts.filter((a) => a.connected).length;
      const textBox = document.createElement("div");
      textBox.className = "min-w-0";
      const acctPart = p.installed && connectedCount === 1 && p.account_label ? ` · ${p.account_label}` : "";
      const metaText = (p.version ? `v${p.version} · ${p.marketplace}` : p.marketplace) + acctPart;
      textBox.innerHTML = `<div class="fw-bold small lh-sm">${escapeHtml(p.display_name || p.name)}</div><div class="small text-body-secondary font-monospace text-truncate" style="font-size:0.72rem;" title="${escapeHtml(metaText)}">${escapeHtml(metaText)}</div>`;
      titleWrap.append(iconEl, textBox);

      const badge = document.createElement("span");
      if (p.installed && connectedCount > 1) {
        badge.className = badgeCls("ok");
        badge.textContent = t("plugin_status_connected_multi", { count: connectedCount });
      } else if (p.custom_url_connected || (p.installed && p.connected)) {
        badge.className = badgeCls("ok");
        badge.textContent = t("plugin_status_connected");
        if (p.account_label) badge.title = p.account_label;
      } else if (p.installed && p.enabled) {
        badge.className = badgeCls("primary");
        badge.textContent = t("plugin_status_installed");
      } else if (p.installed) {
        badge.className = badgeCls("warn");
        badge.textContent = t("plugin_status_disabled");
      } else {
        badge.className = badgeCls();
        badge.textContent = t("plugin_status_available");
      }
      head.append(titleWrap, badge);

      const desc = document.createElement("p");
      desc.className = "small text-body-secondary mb-auto";
      desc.textContent = pluginDescription(p);

      const actions = document.createElement("div");
      actions.className = "d-flex flex-wrap gap-2 mt-1";

      const makeBtn = (labelHtml, cls, handler) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = `btn btn-sm ${cls || "btn-outline-secondary"}`.trim();
        b.innerHTML = labelHtml;
        b.onclick = async () => {
          b.disabled = true;
          try { await handler(); } finally { b.disabled = false; }
        };
        return b;
      };

      const appendCustomUrlButton = (primaryStyle) => {
        if (!p.supports_custom_url) return;
        const hasUrl = Boolean(p.custom_mcp && p.custom_mcp.url);
        actions.appendChild(
          makeBtn(
            `${uiIcon("link", "me-1")}<span>${escapeHtml(hasUrl ? t("plugin_btn_edit_url") : t("plugin_btn_custom_url"))}</span>`,
            primaryStyle ? "btn-primary" : "",
            async () => {
              if (customUrlOpenFor.has(p.name)) customUrlOpenFor.delete(p.name);
              else customUrlOpenFor.add(p.name);
              renderPlugins();
            }
          )
        );
      };

      if (!p.installed) {
        if (p.supports_custom_url) {
          appendCustomUrlButton(true);
        }
        if (p.supports_oauth) {
          actions.appendChild(
            makeBtn(
              escapeHtml(t("plugin_btn_connect")),
              p.supports_custom_url ? "" : "btn-primary",
              () => openPluginConnectModal(p, { forceReconnect: false })
            )
          );
        }
        actions.appendChild(
          makeBtn(
            escapeHtml(t("plugin_btn_install")),
            p.supports_oauth || p.supports_custom_url ? "" : "btn-primary",
            async () => {
              try {
                toast(t("toast_plugin_installing", { name: p.display_name || p.name }), "warn");
                await api("/api/plugins/install", {
                  method: "POST",
                  body: JSON.stringify({ target: p.install_target || p.name }),
                });
                toast(t("toast_plugin_installed", { name: p.display_name || p.name }));
                await loadPlugins();
              } catch (err) { toast(err.message, "err"); }
            }
          )
        );
      } else {
        if (p.supports_custom_url) {
          appendCustomUrlButton(false);
        }
        if (p.supports_oauth) {
          actions.appendChild(
            makeBtn(
              `${uiIcon("plus", "me-1")}<span>${escapeHtml(t("plugin_btn_add_account"))}</span>`,
              "",
              async () => {
                if (addAccountOpenFor.has(p.name)) addAccountOpenFor.delete(p.name);
                else addAccountOpenFor.add(p.name);
                renderPlugins();
              }
            )
          );
        }
        actions.appendChild(
          makeBtn(escapeHtml(p.enabled ? t("plugin_btn_disable") : t("plugin_btn_enable")), "", async () => {
            try {
              const res = await api(`/api/plugins/${encodeURIComponent(p.name)}/toggle`, {
                method: "POST",
                body: JSON.stringify({ enabled: !p.enabled }),
              });
              if (res && res.needs_config) {
                await openPluginConnectModal(p, { interactiveConfig: true });
                return;
              }
              toast(t(p.enabled ? "toast_plugin_disabled" : "toast_plugin_enabled", { name: p.display_name || p.name }));
              await loadPlugins();
            } catch (err) { toast(err.message, "err"); }
          })
        );
        actions.appendChild(
          makeBtn(escapeHtml(t("plugin_btn_uninstall")), "btn-outline-danger", async () => {
            if (!confirm(t("confirm_uninstall_plugin", { name: p.display_name || p.name }))) return;
            try {
              await api(`/api/plugins/${encodeURIComponent(p.name)}`, { method: "DELETE" });
              toast(t("toast_plugin_uninstalled", { name: p.display_name || p.name }));
              await loadPlugins();
            } catch (err) { toast(err.message, "err"); }
          })
        );
      }

      cardBody.append(head, desc, actions);

      // Uložená vlastní MCP URL instance
      if (p.custom_mcp && p.custom_mcp.url) {
        const urlBox = document.createElement("div");
        urlBox.className = "p-2 rounded border bg-body-tertiary d-flex flex-column gap-1 mt-1";

        const urlHead = document.createElement("div");
        urlHead.className = "d-flex align-items-center justify-content-between gap-2";

        const urlBadges = document.createElement("div");
        urlBadges.className = "d-flex align-items-center flex-wrap gap-1 min-w-0";
        const linkMeta = document.createElement("span");
        linkMeta.className = "small fw-semibold d-inline-flex align-items-center gap-1";
        linkMeta.innerHTML = `${uiIcon("link", "text-primary")}<span>${escapeHtml(t("plugin_custom_url_title"))}</span>`;
        const scPill = document.createElement("span");
        scPill.className = badgeCls();
        scPill.textContent = scopeLabel(p.custom_mcp.scope);
        urlBadges.append(linkMeta, scPill);

        if (p.custom_mcp.has_auth_token) {
          const tokPill = document.createElement("span");
          tokPill.className = badgeCls("primary");
          tokPill.textContent = t("plugin_custom_url_token_set");
          urlBadges.appendChild(tokPill);
        }

        const rmUrlBtn = document.createElement("button");
        rmUrlBtn.type = "button";
        rmUrlBtn.className = "btn btn-sm btn-outline-danger btn-icon-sm flex-shrink-0";
        rmUrlBtn.innerHTML = uiIcon("trash");
        rmUrlBtn.title = t("btn_delete");
        rmUrlBtn.setAttribute("aria-label", t("btn_delete"));
        rmUrlBtn.onclick = async () => {
          if (!confirm(t("plugin_custom_url_remove_confirm", { name: p.display_name || p.name }))) return;
          rmUrlBtn.disabled = true;
          try {
            await api(`/api/plugins/${encodeURIComponent(p.name)}/custom-url`, { method: "DELETE" });
            customUrlOpenFor.delete(p.name);
            toast(t("toast_plugin_url_removed", { name: p.display_name || p.name }));
            await loadPlugins();
            await loadMcp();
          } catch (err) {
            toast(err.message, "err");
          } finally {
            rmUrlBtn.disabled = false;
          }
        };

        urlHead.append(urlBadges, rmUrlBtn);

        const urlVal = document.createElement("div");
        urlVal.className = "small font-monospace text-body-secondary text-break";
        urlVal.textContent = p.custom_mcp.url;

        const mcpTools = Array.isArray(p.custom_mcp.tools) ? p.custom_mcp.tools : [];
        const urlFoot = document.createElement("div");
        urlFoot.className = "d-flex align-items-center justify-content-between flex-wrap gap-2 mt-1";
        const mcpPermsBtn = document.createElement("a");
        const mcpHref = pluginPermsUrl(p.name, { mcpMode: true });
        mcpPermsBtn.href = mcpHref;
        mcpPermsBtn.className = "btn btn-sm btn-outline-primary";
        const mcpPermsLbl = mcpTools.length
          ? t("plugin_btn_mcp_tools", { count: mcpTools.length })
          : t("plugin_btn_mcp_tools_empty");
        mcpPermsBtn.innerHTML = `${uiIcon("sliders", "me-1")}<span>${escapeHtml(mcpPermsLbl)}</span>`;
        mcpPermsBtn.onclick = (e) => {
          if (isModifiedClick(e)) return;
          e.preventDefault();
          navigateTo(mcpHref);
        };
        urlFoot.appendChild(mcpPermsBtn);
        if (mcpTools.length) {
          const mcpSum = document.createElement("span");
          mcpSum.className = "small text-body-secondary";
          mcpSum.textContent = t("perms_summary_counts", countToolModes(mcpTools));
          urlFoot.appendChild(mcpSum);
        }

        urlBox.append(urlHead, urlVal, urlFoot);
        cardBody.appendChild(urlBox);
      }

      // Inline formulář pro zadání / úpravu vlastní MCP URL a volitelného tokenu
      if (p.supports_custom_url && customUrlOpenFor.has(p.name)) {
        const urlForm = document.createElement("form");
        urlForm.className = "p-2 rounded border bg-body-tertiary d-flex flex-column gap-2 mt-1";

        const urlTitle = document.createElement("div");
        urlTitle.className = "plugin-perm-title";
        urlTitle.textContent = t("plugin_custom_url_title");

        const urlGroup = document.createElement("div");
        const urlLbl = document.createElement("label");
        urlLbl.className = "form-label small text-body-secondary mb-1";
        urlLbl.textContent = t("plugin_custom_url_label");
        const urlInp = document.createElement("input");
        urlInp.type = "url";
        urlInp.required = true;
        urlInp.className = "form-control form-control-sm font-monospace";
        urlInp.placeholder = p.custom_url_placeholder || "https://mcp.example.com/mcp";
        urlInp.value = (p.custom_mcp && p.custom_mcp.url) || "";
        urlGroup.append(urlLbl, urlInp);

        const extraGrid = document.createElement("div");
        extraGrid.className = "row g-2";

        const tokCol = document.createElement("div");
        tokCol.className = "col-sm-7";
        const tokLbl = document.createElement("label");
        tokLbl.className = "form-label small text-body-secondary mb-1";
        tokLbl.textContent = t("plugin_custom_url_token_label");
        const tokInp = document.createElement("input");
        tokInp.type = "password";
        tokInp.className = "form-control form-control-sm";
        tokInp.placeholder = t("plugin_custom_url_token_ph");
        tokCol.append(tokLbl, tokInp);

        const scCol = document.createElement("div");
        scCol.className = "col-sm-5";
        const scLbl = document.createElement("label");
        scLbl.className = "form-label small text-body-secondary mb-1";
        scLbl.textContent = t("plugin_detail_scope");
        const urlScopeSel = document.createElement("select");
        urlScopeSel.className = "form-select form-select-sm";
        [
          ["all", t("scope_all_chats")],
          ["pracovni", t("scope_work")],
          ["osobni", t("scope_personal")],
        ].forEach(([val, txt]) => {
          const opt = document.createElement("option");
          opt.value = val;
          opt.textContent = txt;
          urlScopeSel.appendChild(opt);
        });
        urlScopeSel.value = (p.custom_mcp && ["pracovni", "osobni"].includes(p.custom_mcp.scope))
          ? p.custom_mcp.scope
          : "all";
        scCol.append(scLbl, urlScopeSel);
        extraGrid.append(tokCol, scCol);

        const urlBtns = document.createElement("div");
        urlBtns.className = "d-flex align-items-center flex-wrap gap-2";
        const saveUrlBtn = document.createElement("button");
        saveUrlBtn.type = "submit";
        saveUrlBtn.className = "btn btn-sm btn-primary";
        saveUrlBtn.textContent = t("plugin_custom_url_save");
        const cancelUrlBtn = document.createElement("button");
        cancelUrlBtn.type = "button";
        cancelUrlBtn.className = "btn btn-sm btn-outline-secondary";
        cancelUrlBtn.textContent = t("plugin_account_btn_cancel");
        cancelUrlBtn.onclick = () => {
          customUrlOpenFor.delete(p.name);
          renderPlugins();
        };
        urlBtns.append(saveUrlBtn, cancelUrlBtn);

        urlForm.onsubmit = async (e) => {
          e.preventDefault();
          const urlVal = urlInp.value.trim();
          if (!urlVal) return;
          saveUrlBtn.disabled = true;
          try {
            const urlRes = await api(`/api/plugins/${encodeURIComponent(p.name)}/custom-url`, {
              method: "POST",
              body: JSON.stringify({
                url: urlVal,
                auth_token: tokInp.value.trim(),
                scope: urlScopeSel.value,
              }),
            });
            customUrlOpenFor.delete(p.name);
            if (urlRes && urlRes.discovered_tools_count > 0) {
              toast(
                t("toast_mcp_tools_loaded", {
                  count: urlRes.discovered_tools_count,
                  name: p.display_name || p.name,
                })
              );
            } else {
              toast(t("toast_plugin_url_saved", { name: p.display_name || p.name }));
            }
            await loadPlugins();
            await loadMcp();
          } catch (err) {
            toast(err.message, "err");
          } finally {
            saveUrlBtn.disabled = false;
          }
        };

        urlForm.append(urlTitle, urlGroup, extraGrid, urlBtns);
        cardBody.appendChild(urlForm);
      }

      // Inline formulář pro přidání nového účtu (např. Pracovní / Osobní)
      if (p.installed && addAccountOpenFor.has(p.name)) {
        const addBox = document.createElement("form");
        addBox.className = "p-2 rounded border bg-body-tertiary d-flex flex-column gap-2 mt-1";
        const addTitle = document.createElement("div");
        addTitle.className = "plugin-perm-title";
        addTitle.textContent = t("plugin_account_add_title");

        const rowFields = document.createElement("div");
        rowFields.className = "d-flex align-items-center flex-wrap gap-2";
        const labelInp = document.createElement("input");
        labelInp.type = "text";
        labelInp.className = "form-control form-control-sm flex-grow-1";
        labelInp.style.minWidth = "130px";
        labelInp.placeholder = t("plugin_account_label_ph");

        const scopeSel = document.createElement("select");
        scopeSel.className = "form-select form-select-sm";
        scopeSel.style.maxWidth = "155px";
        [
          ["pracovni", t("scope_work")],
          ["osobni", t("scope_personal")],
          ["all", t("scope_all_chats")],
        ].forEach(([val, txt]) => {
          const opt = document.createElement("option");
          opt.value = val;
          opt.textContent = txt;
          scopeSel.appendChild(opt);
        });
        const hasPersonalOrAll = accounts.some((a) => a.scope === "osobni" || a.scope === "all");
        scopeSel.value = hasPersonalOrAll ? "pracovni" : "osobni";
        rowFields.append(labelInp, scopeSel);

        const rowBtns = document.createElement("div");
        rowBtns.className = "d-flex align-items-center flex-wrap gap-2";
        const createBtn = document.createElement("button");
        createBtn.type = "submit";
        createBtn.className = "btn btn-sm btn-primary";
        createBtn.textContent = t("plugin_account_btn_create_connect");
        const cancelBtn = document.createElement("button");
        cancelBtn.type = "button";
        cancelBtn.className = "btn btn-sm btn-outline-secondary";
        cancelBtn.textContent = t("plugin_account_btn_cancel");
        cancelBtn.onclick = () => {
          addAccountOpenFor.delete(p.name);
          renderPlugins();
        };
        rowBtns.append(createBtn, cancelBtn);

        addBox.onsubmit = async (e) => {
          e.preventDefault();
          createBtn.disabled = true;
          try {
            const labelVal = labelInp.value.trim();
            const scopeVal = scopeSel.value;
            const res = await api(`/api/plugins/${encodeURIComponent(p.name)}/accounts`, {
              method: "POST",
              body: JSON.stringify({ label: labelVal, scope: scopeVal }),
            });
            addAccountOpenFor.delete(p.name);
            const shownName = labelVal || scopeLabel(scopeVal);
            toast(t("toast_plugin_account_added", { label: shownName }));
            await loadPlugins();
            if (res && res.account) {
              await openPluginConnectModal(p, {
                forceReconnect: false,
                accountId: res.account.id,
                accountLabel: res.account.label || scopeLabel(res.account.scope),
              });
            }
          } catch (err) {
            toast(err.message, "err");
          } finally {
            createBtn.disabled = false;
          }
        };

        addBox.append(addTitle, rowFields, rowBtns);
        cardBody.appendChild(addBox);
      }

      // Účty a oprávnění nainstalovaného pluginu
      if (p.installed && accounts.length) {
        const schema = Array.isArray(p.permissions_schema) ? p.permissions_schema : [];
        const basicSpecs = schema.filter((sp) => (sp.tier || "basic") === "basic");

        const acctsWrap = document.createElement("div");
        acctsWrap.className = "border-top pt-2 mt-1 d-flex flex-column gap-2";

        accounts.forEach((acct) => {
          const box = document.createElement("div");
          box.className = "p-2 rounded border bg-body-tertiary d-flex flex-column gap-2";

          const acctHead = document.createElement("div");
          acctHead.className = "d-flex align-items-start justify-content-between flex-wrap gap-2";

          const metaBox = document.createElement("div");
          metaBox.className = "d-flex flex-column min-w-0";

          const titleRow = document.createElement("div");
          titleRow.className = "d-flex align-items-center flex-wrap gap-1 fw-semibold small";
          const titleSpan = document.createElement("span");
          titleSpan.textContent = acct.label || t("plugin_account_default_label");
          const scopeBadge = document.createElement("span");
          scopeBadge.className = badgeCls();
          scopeBadge.textContent = scopeLabel(acct.scope);
          const stateBadge = document.createElement("span");
          if (!acct.enabled) {
            stateBadge.className = badgeCls("warn");
            stateBadge.textContent = t("plugin_account_status_paused");
          } else if (acct.connected) {
            stateBadge.className = badgeCls("ok");
            stateBadge.textContent = t("plugin_account_status_connected");
          } else {
            stateBadge.className = badgeCls();
            stateBadge.textContent = t("plugin_account_status_disconnected");
          }
          titleRow.append(titleSpan, scopeBadge, stateBadge);
          metaBox.appendChild(titleRow);

          if (acct.account_email) {
            const emailDiv = document.createElement("div");
            emailDiv.className = "small text-body-secondary font-monospace text-break";
            emailDiv.style.fontSize = "0.75rem";
            emailDiv.textContent = acct.account_email;
            metaBox.appendChild(emailDiv);
          }

          const acctActions = document.createElement("div");
          acctActions.className = "d-flex align-items-center flex-wrap gap-1";
          if (p.supports_oauth) {
            acctActions.appendChild(
              makeBtn(
                escapeHtml(acct.connected ? t("plugin_btn_reconnect") : t("plugin_btn_connect")),
                acct.connected ? "" : "btn-primary",
                () =>
                  openPluginConnectModal(p, {
                    forceReconnect: Boolean(acct.connected),
                    accountId: acct.id,
                    accountLabel: acct.label || acct.account_email || scopeLabel(acct.scope),
                  })
              )
            );
          }
          if (accounts.length > 1) {
            const rmBtn = document.createElement("button");
            rmBtn.type = "button";
            rmBtn.className = "btn btn-sm btn-outline-danger btn-icon-sm";
            rmBtn.innerHTML = uiIcon("trash");
            rmBtn.title = t("btn_delete");
            rmBtn.setAttribute("aria-label", t("btn_delete"));
            rmBtn.onclick = async () => {
              const lbl = acct.label || acct.account_email || t("plugin_account_default_label");
              if (!confirm(t("plugin_account_remove_confirm", { label: lbl }))) return;
              rmBtn.disabled = true;
              try {
                await api(`/api/plugins/${encodeURIComponent(p.name)}/accounts/${acct.id}`, {
                  method: "DELETE",
                });
                toast(t("toast_plugin_account_deleted"));
                await loadPlugins();
              } catch (err) {
                toast(err.message, "err");
              } finally {
                rmBtn.disabled = false;
              }
            };
            acctActions.appendChild(rmBtn);
          }

          acctHead.append(metaBox, acctActions);
          box.appendChild(acctHead);

          const appendPermRow = (parentEl, spec) => {
            const row = document.createElement("div");
            row.className = "form-check form-switch d-flex justify-content-between align-items-center gap-2 ps-0 mb-0";
            const swId = `perm_${p.name}_${acct.id}_${spec.key}`;
            const lbl = document.createElement("label");
            lbl.className = "form-check-label small flex-grow-1 mb-0";
            lbl.htmlFor = swId;
            lbl.textContent = t(spec.label_key);
            const cb = document.createElement("input");
            cb.className = "form-check-input m-0 flex-shrink-0";
            cb.type = "checkbox";
            cb.role = "switch";
            cb.id = swId;
            cb.checked = Boolean((acct.permissions || {})[spec.key]);
            cb.onchange = async () => {
              const prev = !cb.checked;
              const updatedPerms = { ...(acct.permissions || {}), [spec.key]: cb.checked };
              try {
                const res = await api(`/api/plugins/${encodeURIComponent(p.name)}/accounts/${acct.id}`, {
                  method: "PUT",
                  body: JSON.stringify({ permissions: updatedPerms }),
                });
                if (res && res.account) {
                  Object.assign(acct, res.account);
                } else {
                  acct.permissions = updatedPerms;
                }
                const sumSpan = box.querySelector(`[data-acct-summary-for="${acct.id}"]`);
                if (sumSpan && Array.isArray(acct.tools) && acct.tools.length) {
                  sumSpan.textContent = t("perms_summary_counts", countToolModes(acct.tools));
                }
                toast(t("toast_plugin_perms_saved", { name: p.display_name || p.name }));
              } catch (err) {
                cb.checked = prev;
                toast(err.message, "err");
              }
            };
            row.append(lbl, cb);
            parentEl.appendChild(row);
          };

          if (basicSpecs.length) {
            const permTitle = document.createElement("div");
            permTitle.className = "plugin-perm-title";
            permTitle.textContent = t("plugin_perms_title");
            box.appendChild(permTitle);
            basicSpecs.forEach((sp) => appendPermRow(box, sp));
          }

          const acctTools = Array.isArray(acct.tools) ? acct.tools : [];
          const footerRow = document.createElement("div");
          footerRow.className = "d-flex align-items-center justify-content-between flex-wrap gap-2 mt-1";

          const detailBtn = document.createElement("a");
          const detHref = pluginPermsUrl(p.name, { accountId: acct.id, mcpMode: false });
          detailBtn.href = detHref;
          detailBtn.className = "btn btn-sm btn-outline-secondary";
          const detLabel = acctTools.length
            ? t("plugin_btn_detailed_perms", { count: acctTools.length })
            : t("plugin_detail_toggle");
          detailBtn.innerHTML = `${uiIcon("sliders", "me-1")}<span>${escapeHtml(detLabel)}</span>`;
          detailBtn.onclick = (e) => {
            if (isModifiedClick(e)) return;
            e.preventDefault();
            navigateTo(detHref);
          };
          footerRow.appendChild(detailBtn);

          if (acctTools.length) {
            const summarySpan = document.createElement("span");
            summarySpan.className = "small text-body-secondary";
            summarySpan.dataset.acctSummaryFor = String(acct.id);
            summarySpan.textContent = t("perms_summary_counts", countToolModes(acctTools));
            footerRow.appendChild(summarySpan);
          }

          box.appendChild(footerRow);
          acctsWrap.appendChild(box);
        });

        cardBody.appendChild(acctsWrap);
      }

      card.appendChild(cardBody);
      grid.appendChild(card);
    });
  }

  document.querySelectorAll("#pluginCategoryBar .filter-pill").forEach((btn) => {
    btn.onclick = () => {
      activePluginCategory = btn.dataset.cat || "all";
      document.querySelectorAll("#pluginCategoryBar .filter-pill").forEach((b) => {
        const isActive = b === btn;
        b.classList.toggle("active", isActive);
        b.classList.toggle("btn-primary", isActive);
        b.classList.toggle("btn-outline-secondary", !isActive);
      });
      renderPlugins();
    };
  });
  if ($("pluginSearchInput")) $("pluginSearchInput").oninput = renderPlugins;
  if ($("refreshPluginsBtn")) $("refreshPluginsBtn").onclick = loadPlugins;
  if ($("toggleMorePluginsBtn")) {
    $("toggleMorePluginsBtn").onclick = () => {
      showAllPlugins = !showAllPlugins;
      if (!showAllPlugins) {
        activePluginCategory = "all";
        if ($("pluginSearchInput")) $("pluginSearchInput").value = "";
        document.querySelectorAll("#pluginCategoryBar .filter-pill").forEach((b) => {
          const isAll = (b.dataset.cat || "all") === "all";
          b.classList.toggle("active", isAll);
          b.classList.toggle("btn-primary", isAll);
          b.classList.toggle("btn-outline-secondary", !isAll);
        });
      }
      renderPlugins();
    };
  }

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

  pluginConnectTerm = new LivePtyTerminal({
    containerId: "pluginConnectTerminal",
    fallbackPreId: "pluginConnectOutput",
    statusBadgeId: "pluginTermStatus",
    wsPath: "/ws/pty/plugin",
  });

  function renderPluginConnectState(st) {
    lastPluginConnectState = st;
    const badge = $("pluginConnectBadge");
    const urlWrap = $("pluginConnectUrlWrap");
    const cbForm = $("pluginCallbackForm");
    const codeForm = $("pluginCodeForm");
    const outEl = $("pluginConnectOutput");

    if (badge) {
      if (st.connected) {
        badge.className = badgeCls("ok");
        badge.textContent = t("plugin_connect_done");
      } else if (st.error) {
        badge.className = badgeCls("err");
        badge.textContent = st.error;
      } else if (st.auth_url) {
        badge.className = badgeCls("primary");
        badge.textContent = t("plugin_connect_waiting_user");
      } else {
        badge.className = badgeCls();
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
        hint.className = "text-body-secondary small";
        hint.textContent = t("plugin_connect_starting_hint");
        urlWrap.appendChild(hint);
      }
    }

    const isInteractiveCfg = st.flow_type === "interactive_config";
    if ($("pluginConfigHint")) {
      $("pluginConfigHint").classList.toggle("d-none", !isInteractiveCfg || Boolean(st.connected));
    }
    if ($("pluginConnectStep1")) {
      $("pluginConnectStep1").classList.toggle("d-none", Boolean(st.connected) || isInteractiveCfg);
    }
    if (cbForm) cbForm.classList.toggle("d-none", st.flow_type !== "localhost_callback" || Boolean(st.connected));
    if (codeForm) codeForm.classList.toggle("d-none", st.flow_type !== "code_paste" || Boolean(st.connected));

    if (pluginConnectTerm && pluginConnectTerm.term) {
      if (st.error && !pluginConnectTerm.isOpen()) {
        pluginConnectTerm.writeNotice(t("chat_error_prefix", { msg: st.error }));
      }
    } else if (outEl) {
      outEl.textContent = st.error
        ? t("chat_error_prefix", { msg: st.error })
        : (st.output || t("oauth_starting"));
      outEl.scrollTop = outEl.scrollHeight;
    }
  }

  async function openPluginConnectModal(
    plugin,
    { forceReconnect = false, accountId = null, accountLabel = "", interactiveConfig = false } = {}
  ) {
    activeConnectPlugin = plugin;
    const modal = $("pluginConnectModal");
    if (!modal) return;
    const titleSuffix = accountLabel ? ` — ${accountLabel}` : "";
    $("pluginConnectTitle").textContent = t("plugin_connect_title", {
      name: `${plugin.display_name || plugin.name}${titleSuffix}`,
    });
    $("pluginCallbackInput").value = "";
    $("pluginCodeInput").value = "";
    $("pluginManualInput").value = "";
    renderPluginConnectState({
      running: true,
      plugin_name: plugin.name,
      flow_type: interactiveConfig ? "interactive_config" : "",
      auth_url: "",
      output: "",
      connected: false,
      error: null,
    });
    modal.classList.remove("d-none");

    let dims = { cols: 100, rows: 26 };
    if (pluginConnectTerm) {
      pluginConnectTerm.ensureInit();
      dims = pluginConnectTerm.fit();
      if (pluginConnectTerm.term) pluginConnectTerm.term.reset();
    }

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
          account_id: accountId,
          interactive_config: Boolean(interactiveConfig),
          cols: dims.cols,
          rows: dims.rows,
        }),
      });
      renderPluginConnectState(st);
      if (pluginConnectTerm) {
        if (st.running) {
          pluginConnectTerm.connect({ reset: true });
        } else if (st.error) {
          pluginConnectTerm.writeNotice(st.error);
        }
      }
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
    if (pluginConnectTerm) pluginConnectTerm.disconnect({ keepContent: false });
    const modal = $("pluginConnectModal");
    if (modal) modal.classList.add("d-none");
    activeConnectPlugin = null;
    try { await api("/api/plugins/connect/stop", { method: "POST" }); } catch (_) {}
    loadPlugins().catch(() => {});
  }

  // Granulární 3-stavová oprávnění pluginů a MCP nástrojů
  function setPermsGroupCollapsed(group, collapsed) {
    if (!permsModalState) return;
    permsModalState.collapsed[group] = collapsed;
    const listEl = $(group === "readonly" ? "permsReadonlyList" : "permsWriteList");
    const chevEl = $(group === "readonly" ? "permsReadonlyChevron" : "permsWriteChevron");
    const togEl = $(group === "readonly" ? "permsReadonlyToggle" : "permsWriteToggle");
    if (listEl) listEl.classList.toggle("d-none", collapsed);
    if (chevEl) chevEl.className = `bi ${collapsed ? "bi-chevron-right" : "bi-chevron-down"} small`;
    if (togEl) togEl.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }

  function syncPermsModalSummaryAndBulk() {
    if (!permsModalState) return;
    const allTools = permsModalState.tools || [];
    [
      ["readonly", "permsReadonlyCount", "permsReadonlyBulk"],
      ["write", "permsWriteCount", "permsWriteBulk"],
    ].forEach(([grp, countId, bulkId]) => {
      const grpTools = allTools.filter((item) => (item.group === "readonly" ? "readonly" : "write") === grp);
      if ($(countId)) $(countId).textContent = String(grpTools.length);
      const bulkSel = $(bulkId);
      if (bulkSel && grpTools.length) {
        const uniq = new Set(grpTools.map((x) => x.mode));
        const mode = uniq.size === 1 ? [...uniq][0] : "mixed";
        bulkSel.value = mode;
        bulkSel.dataset.mode = mode;
      }
    });
    const sumText = allTools.length
      ? t("perms_summary_counts", countToolModes(allTools))
      : "";
    if ($("permsFooterSummary")) $("permsFooterSummary").textContent = sumText;
    if ($("permsHeaderSummary")) $("permsHeaderSummary").textContent = sumText;
  }

  function renderPermsModalTools() {
    if (!permsModalState) return;
    const query = ($("permsSearchInput")?.value || "").trim().toLowerCase();
    const allTools = permsModalState.tools || [];
    const roAll = allTools.filter((item) => item.group === "readonly");
    const wrAll = allTools.filter((item) => item.group !== "readonly");
    const matchFn = (item) => {
      if (!query) return true;
      const hay = `${item.key} ${item.title || ""} ${item.description || ""}`.toLowerCase();
      return hay.includes(query);
    };
    const roVis = roAll.filter(matchFn);
    const wrVis = wrAll.filter(matchFn);

    if ($("permsEmptyMcp")) {
      $("permsEmptyMcp").classList.toggle("d-none", !(permsModalState.mcpMode && allTools.length === 0));
    }

    const renderGroup = (secId, listId, items) => {
      const sec = $(secId);
      const listEl = $(listId);
      if (!sec || !listEl) return;
      sec.classList.toggle("d-none", items.length === 0);
      listEl.innerHTML = "";
      items.forEach((tool) => {
        const row = document.createElement("div");
        row.className = `perm-tool-row${tool.mode === "block" ? " is-blocked" : ""}`;

        const info = document.createElement("div");
        info.className = "min-w-0 flex-grow-1";
        const titleDiv = document.createElement("div");
        titleDiv.className = "fw-semibold small";
        titleDiv.textContent = tool.title || tool.key;
        if (tool.title && tool.title !== tool.key) {
          const keyCode = document.createElement("code");
          keyCode.className = "ms-2 small text-body-secondary fw-normal";
          keyCode.style.fontSize = "0.72rem";
          keyCode.textContent = tool.key;
          titleDiv.appendChild(keyCode);
        }
        const descDiv = document.createElement("div");
        descDiv.className = "small text-body-secondary text-truncate";
        descDiv.title = tool.description || tool.title || tool.key;
        descDiv.textContent = tool.description || "";
        info.append(titleDiv, descDiv);

        const sel = document.createElement("select");
        sel.className = "form-select form-select-sm perm-mode-select";
        sel.dataset.mode = tool.mode;
        [
          ["allow", t("perms_mode_allow")],
          ["ask", t("perms_mode_ask")],
          ["block", t("perms_mode_block")],
        ].forEach(([val, lbl]) => {
          const opt = document.createElement("option");
          opt.value = val;
          opt.textContent = lbl;
          sel.appendChild(opt);
        });
        sel.value = tool.mode;
        sel.onchange = () => {
          tool.mode = sel.value;
          sel.dataset.mode = sel.value;
          row.classList.toggle("is-blocked", sel.value === "block");
          syncPermsModalSummaryAndBulk();
        };

        row.append(info, sel);
        listEl.appendChild(row);
      });
    };

    renderGroup("permsReadonlySection", "permsReadonlyList", roVis);
    renderGroup("permsWriteSection", "permsWriteList", wrVis);

    if ($("permsNoMatches")) {
      $("permsNoMatches").classList.toggle(
        "d-none",
        !(allTools.length > 0 && roVis.length === 0 && wrVis.length === 0)
      );
    }

    const showControls = allTools.length > 0;
    ["permsPresetReadonly", "permsPresetSafe", "permsPresetBlockAll"].forEach((id) => {
      if ($(id)) $(id).disabled = !showControls;
    });

    syncPermsModalSummaryAndBulk();
  }

  async function discoverMcpToolsInModal({ quietEmpty = false } = {}) {
    if (!permsModalState || !permsModalState.mcpMode) return;
    const p = permsModalState.plugin;
    const btns = [$("permsDiscoverBtn"), $("permsDiscoverEmptyBtn")].filter(Boolean);
    btns.forEach((b) => { b.disabled = true; });
    try {
      const res = await api(`/api/plugins/${encodeURIComponent(p.name)}/tools/discover`, {
        method: "POST",
      });
      const tools = (res && res.custom_mcp && Array.isArray(res.custom_mcp.tools))
        ? res.custom_mcp.tools
        : [];
      permsModalState.tools = tools.map((item) => ({ ...item }));
      renderPermsModalTools();
      if (tools.length > 0) {
        toast(t("toast_mcp_tools_loaded", { count: tools.length, name: p.display_name || p.name }));
      } else if (!quietEmpty) {
        toast(t("toast_mcp_tools_none", { name: p.display_name || p.name }), "warn");
      }
      await loadPlugins();
    } catch (err) {
      if (!quietEmpty) toast(err.message, "err");
    } finally {
      btns.forEach((b) => { b.disabled = false; });
    }
  }

  async function openPluginPermsPage({ pluginName, accountId = null, mcpMode = false, reloadPlugins = false } = {}) {
    if (!pluginsCatalog.length || reloadPlugins) {
      await loadPlugins();
    }
    const targetName = String(pluginName || "").trim().toLowerCase();
    const plugin = pluginsCatalog.find((item) => (item.name || "").toLowerCase() === targetName);
    if (!plugin) {
      navigateTo("/settings", { replace: true });
      return;
    }

    const accounts = Array.isArray(plugin.accounts) ? plugin.accounts : [];
    let useMcp = Boolean(mcpMode) || (!accounts.length && Boolean(plugin.custom_mcp));
    let account = null;
    if (!useMcp) {
      account = (accountId != null ? accounts.find((a) => a.id === accountId) : null) || accounts[0] || null;
      if (!account && plugin.custom_mcp) {
        useMcp = true;
      }
    }

    const srcTools = useMcp
      ? ((plugin.custom_mcp && Array.isArray(plugin.custom_mcp.tools)) ? plugin.custom_mcp.tools : [])
      : (account && Array.isArray(account.tools) ? account.tools : []);

    permsModalState = {
      plugin,
      account,
      mcpMode: Boolean(useMcp),
      tools: srcTools.map((item) => ({ ...item })),
      collapsed: { readonly: false, write: false },
    };

    const iconEl = $("permsModalIcon");
    if (iconEl) {
      iconEl.innerHTML = "";
      iconEl.className = "plugin-icon";
      if (plugin.logo_url) {
        const img = document.createElement("img");
        img.className = "plugin-logo";
        img.src = plugin.logo_url;
        img.alt = plugin.display_name || plugin.name;
        img.referrerPolicy = "no-referrer";
        img.onerror = () => {
          iconEl.classList.remove("has-logo");
          iconEl.innerHTML = uiIcon("puzzle", "fs-5");
        };
        iconEl.classList.add("has-logo");
        iconEl.appendChild(img);
      } else {
        iconEl.innerHTML = uiIcon("puzzle", "fs-5");
      }
    }

    if ($("permsModalTitle")) {
      $("permsModalTitle").textContent = t("perms_modal_title", {
        name: plugin.display_name || plugin.name,
      });
    }
    if ($("permsModalSubtitle")) {
      $("permsModalSubtitle").textContent = useMcp
        ? ((plugin.custom_mcp && plugin.custom_mcp.url) || "")
        : [
            account ? (account.label || t("plugin_account_default_label")) : "",
            account && account.account_email ? account.account_email : "",
            account ? scopeLabel(account.scope) : "",
          ].filter(Boolean).join(" · ");
    }

    if ($("permsSearchInput")) $("permsSearchInput").value = "";
    if ($("permsDiscoverBtn")) $("permsDiscoverBtn").classList.toggle("d-none", !useMcp);

    const showAccountSection = !useMcp && Boolean(account);
    if ($("permsAccountSection")) $("permsAccountSection").classList.toggle("d-none", !showAccountSection);
    if (showAccountSection && account) {
      if ($("permsAccountLabel")) $("permsAccountLabel").value = account.label || "";
      if ($("permsAccountScope")) {
        $("permsAccountScope").value = ["pracovni", "osobni"].includes(account.scope) ? account.scope : "all";
      }
      if ($("permsAccountEnabled")) $("permsAccountEnabled").checked = account.enabled !== false;
      if ($("permsAccountRules")) $("permsAccountRules").value = account.custom_rules || "";
    }

    activateTabDom("plugin-perms");
    window.scrollTo({ top: 0, behavior: "instant" });
    setPermsGroupCollapsed("readonly", false);
    setPermsGroupCollapsed("write", false);
    renderPermsModalTools();

    if (useMcp && permsModalState.tools.length === 0) {
      discoverMcpToolsInModal({ quietEmpty: true });
    }
  }

  ["permsBackLink", "cancelPluginPermsBtn"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("click", (e) => {
      if (isModifiedClick(e)) return;
      e.preventDefault();
      navigateTo("/settings");
    });
  });
  if ($("permsSearchInput")) $("permsSearchInput").oninput = renderPermsModalTools;
  if ($("permsDiscoverBtn")) $("permsDiscoverBtn").onclick = () => discoverMcpToolsInModal();
  if ($("permsDiscoverEmptyBtn")) $("permsDiscoverEmptyBtn").onclick = () => discoverMcpToolsInModal();

  if ($("permsReadonlyToggle")) {
    const toggleRo = () => setPermsGroupCollapsed("readonly", !(permsModalState && permsModalState.collapsed.readonly));
    $("permsReadonlyToggle").onclick = toggleRo;
    $("permsReadonlyToggle").onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleRo(); }
    };
  }
  if ($("permsWriteToggle")) {
    const toggleWr = () => setPermsGroupCollapsed("write", !(permsModalState && permsModalState.collapsed.write));
    $("permsWriteToggle").onclick = toggleWr;
    $("permsWriteToggle").onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleWr(); }
    };
  }

  if ($("permsReadonlyBulk")) {
    $("permsReadonlyBulk").onchange = () => {
      if (!permsModalState) return;
      const val = $("permsReadonlyBulk").value;
      if (!["allow", "ask", "block"].includes(val)) return;
      permsModalState.tools.forEach((item) => {
        if (item.group === "readonly") item.mode = val;
      });
      renderPermsModalTools();
    };
  }
  if ($("permsWriteBulk")) {
    $("permsWriteBulk").onchange = () => {
      if (!permsModalState) return;
      const val = $("permsWriteBulk").value;
      if (!["allow", "ask", "block"].includes(val)) return;
      permsModalState.tools.forEach((item) => {
        if (item.group !== "readonly") item.mode = val;
      });
      renderPermsModalTools();
    };
  }

  const applyPermsPreset = (preset) => {
    if (!permsModalState) return;
    permsModalState.tools.forEach((item) => {
      if (preset === "readonly") {
        item.mode = item.group === "readonly" ? "allow" : "block";
      } else if (preset === "safe") {
        item.mode = item.group === "readonly" ? "allow" : "ask";
      } else if (preset === "block_all") {
        item.mode = "block";
      }
    });
    renderPermsModalTools();
  };
  if ($("permsPresetReadonly")) $("permsPresetReadonly").onclick = () => applyPermsPreset("readonly");
  if ($("permsPresetSafe")) $("permsPresetSafe").onclick = () => applyPermsPreset("safe");
  if ($("permsPresetBlockAll")) $("permsPresetBlockAll").onclick = () => applyPermsPreset("block_all");

  async function savePluginPerms() {
    if (!permsModalState) return;
    const saveBtns = [$("savePluginPermsBtn"), $("savePluginPermsTopBtn")].filter(Boolean);
    const p = permsModalState.plugin;
    const toolPerms = {};
    permsModalState.tools.forEach((item) => {
      toolPerms[item.key] = item.mode;
    });
    const payload = { tool_permissions: toolPerms };
    if (!permsModalState.mcpMode && permsModalState.account) {
      payload.account_id = permsModalState.account.id;
      payload.label = ($("permsAccountLabel")?.value || "").trim();
      payload.scope = $("permsAccountScope")?.value || "all";
      payload.enabled = Boolean($("permsAccountEnabled")?.checked);
      payload.custom_rules = ($("permsAccountRules")?.value || "").trim();
    }
    saveBtns.forEach((b) => { b.disabled = true; });
    try {
      await api(`/api/plugins/${encodeURIComponent(p.name)}/tool-permissions`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      toast(t("toast_plugin_perms_saved", { name: p.display_name || p.name }));
      await loadPlugins();
      await loadMcp();
      navigateTo("/settings");
    } catch (err) {
      toast(err.message, "err");
    } finally {
      saveBtns.forEach((b) => { b.disabled = false; });
    }
  }
  if ($("savePluginPermsBtn")) $("savePluginPermsBtn").onclick = savePluginPerms;
  if ($("savePluginPermsTopBtn")) $("savePluginPermsTopBtn").onclick = savePluginPerms;

  if ($("closePluginConnectBtn")) $("closePluginConnectBtn").onclick = closePluginConnectModal;
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (e.target && typeof e.target.closest === "function" && e.target.closest(".xterm")) {
      return;
    }
    if ($("pluginConnectModal") && !$("pluginConnectModal").classList.contains("d-none")) {
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

  const PLUGIN_KEY_SEQS = {
    enter: "\r",
    up: "\x1b[A",
    down: "\x1b[B",
    left: "\x1b[D",
    right: "\x1b[C",
    tab: "\t",
    esc: "\x1b",
    space: " ",
    ctrl_c: "\x03",
  };
  document.querySelectorAll(".plugin-key-btn").forEach((btn) => {
    btn.onclick = async () => {
      const key = (btn.dataset.key || "").trim();
      const seq = PLUGIN_KEY_SEQS[key];
      if (seq && pluginConnectTerm && pluginConnectTerm.sendInput(seq)) {
        pluginConnectTerm.focus();
        return;
      }
      try {
        const st = await api("/api/plugins/connect/input", {
          method: "POST",
          body: JSON.stringify({ key }),
        });
        renderPluginConnectState(st);
      } catch (err) { toast(err.message, "err"); }
    };
  });

  if ($("pluginManualInputForm")) {
    $("pluginManualInputForm").onsubmit = async (e) => {
      e.preventDefault();
      const val = $("pluginManualInput").value;
      const payload = val ? `${val.replace(/[\r\n]+$/, "")}\r` : "\r";
      if (pluginConnectTerm && pluginConnectTerm.sendInput(payload)) {
        $("pluginManualInput").value = "";
        pluginConnectTerm.focus();
        return;
      }
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
    $("pwdCurrentWrap").classList.toggle("d-none", !s.has_password);
    $("disable2faPwd").classList.toggle("d-none", !s.has_password);
    $("totpBadge").textContent = s.totp_enabled ? t("totp_on") : t("totp_off");
    $("totpBadge").className = badgeCls(s.totp_enabled ? "ok" : "");
    $("totpDisabledBox").classList.toggle("d-none", Boolean(s.totp_enabled));
    $("totpEnabledBox").classList.toggle("d-none", !s.totp_enabled);
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
      $("totpSetupArea").classList.remove("d-none");
      $("recoveryCodesBox").classList.add("d-none");
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
      $("totpSetupArea").classList.add("d-none");
      latestRecoveryCodes = r.recovery_codes || [];
      $("recoveryCodesList").innerHTML = latestRecoveryCodes.map((c) => `<div>${escapeHtml(c)}</div>`).join("");
      $("recoveryCodesBox").classList.toggle("d-none", !latestRecoveryCodes.length);
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
      $("recoveryCodesBox").classList.add("d-none");
      updateSecurityUi(r.owner);
      toast(t("toast_2fa_disabled"));
    } catch (err) { toast(err.message, "err"); }
  };

  loadAuthAndBootstrap();
})();
