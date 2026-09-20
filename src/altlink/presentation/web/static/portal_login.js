(() => {
  const configNode = document.getElementById("portal-login-config");
  if (!configNode) return;
  const config = JSON.parse(configNode.textContent);
  const root = document.documentElement;
  const statusNode = document.getElementById("portal-login-status");
  const hintNode = document.getElementById("portal-login-hint");
  const reloadLink = document.getElementById("portal-login-reload");
  const telegram = window.Telegram?.WebApp;
  const initData = telegram?.initData || new URLSearchParams(location.hash.slice(1)).get("tgWebAppData");
  const revealLogin = () => {
    clearTimeout(window.portalLoginFallback);
    root.classList.remove("portal-auth-loading");
  };
  const openPortal = () => {
    clearTimeout(window.portalLoginFallback);
    root.classList.add("portal-auth-loading");
    window.location.replace("/portal");
  };
  const request = async (url, options = {}) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(url, { ...options, cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error("Login request failed");
      return await response.json();
    } finally {
      clearTimeout(timeout);
    }
  };
  document.querySelector(".portal-login-qr-toggle")?.addEventListener("click", (event) => {
    const button = event.currentTarget;
    const expanded = button.getAttribute("aria-expanded") !== "true";
    button.setAttribute("aria-expanded", String(expanded));
    document.getElementById("portal-login-qr")?.classList.toggle("is-open", expanded);
  });
  const poll = async () => {
    if (!statusNode || !config.statusUrl) { revealLogin(); return; }
    try {
      const payload = await request(config.statusUrl);
      if (payload.status === "approved") { openPortal(); return; }
      revealLogin();
      statusNode.textContent = payload.message || "Ожидаем подтверждение в Telegram…";
      if (payload.status === "pending") { setTimeout(poll, 2000); return; }
      if (hintNode) hintNode.textContent = "Создайте новую попытку входа.";
    } catch {
      revealLogin();
      statusNode.textContent = "Не удалось проверить вход. Попробуйте ещё раз.";
    }
    if (reloadLink) reloadLink.style.display = "inline-flex";
  };
  const start = async () => {
    if (initData) {
      root.classList.add("portal-auth-loading");
      try {
        telegram?.ready?.();
        const payload = await request("/api/auth/telegram-webapp", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ init_data: initData }),
        });
        if (payload.ok) { openPortal(); return; }
        throw new Error("Login not confirmed");
      } catch {
        revealLogin();
        document.getElementById("portal-miniapp-status").hidden = false;
      }
    }
    // Do not race Mini App authentication and attempt polling: both set a session cookie.
    if (!config.autoLogin && new URLSearchParams(location.search).get("autostart") === "1"
        && matchMedia("(max-width: 760px)").matches && config.deepLink) {
      window.location.href = config.deepLink;
    }
    await poll();
  };
  start();
})();
