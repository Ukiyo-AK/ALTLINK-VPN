(() => {
  const configElement = document.getElementById("subscription-connect-config");
  const selector = document.querySelector("[data-platform-select]");
  if (!configElement || !selector) return;
  const config = JSON.parse(configElement.textContent);
  const cards = [...document.querySelectorAll("[data-client-card]")];
  const choices = [...document.querySelectorAll("[data-client-choice]")];
  const platformStatus = document.querySelector("[data-platform-status]");

  const detectPlatform = () => {
    const agent = navigator.userAgent || "";
    const system = navigator.userAgentData?.platform || navigator.platform || "";
    // iPadOS can report a desktop Mac user agent, unlike a Mac it has touch points.
    if (/iPhone|iPad|iPod/i.test(agent) || (/Mac/i.test(system + agent) && navigator.maxTouchPoints > 1)) return "ios";
    if (/Android/i.test(system + agent)) return "android";
    if (/Windows|Win32|Win64/i.test(system + agent)) return "windows";
    if (/Mac/i.test(system + agent)) return "macos";
    if (/Linux/i.test(system + agent) && !/CrOS/i.test(agent)) return "linux";
    return "other";
  };

  const applyPlatform = () => {
    const automatic = selector.value === "auto";
    const platform = automatic ? detectPlatform() : selector.value;
    const label = config.platforms[platform] || config.platforms.other;
    platformStatus.textContent = automatic
      ? (platform === "other" ? "Выберите платформу" : `Определено: ${label}`)
      : `Выбрано: ${label}`;
    cards.forEach((card) => {
      const links = config.downloads[card.dataset.clientCard];
      const download = links[platform] || links.other;
      card.querySelector("[data-app-download]").href = download.url;
      card.querySelector("[data-download-store]").textContent = download.store;
      const note = card.querySelector("[data-download-note]");
      note.textContent = download.note;
      note.hidden = !download.note;
    });
  };

  selector.addEventListener("change", () => {
    applyPlatform();
    // Keep an explicit choice when returning from an app store, without storing the subscription.
    try {
      const url = new URL(window.location.href);
      if (selector.value === "auto") url.searchParams.delete("platform");
      else url.searchParams.set("platform", selector.value);
      window.history.replaceState(null, "", url);
    } catch (_) { /* Restricted WebViews can still change the links in place. */ }
  });
  document.querySelector("[data-platform-form]").addEventListener("submit", (event) => {
    event.preventDefault();
    applyPlatform();
  });
  choices.forEach((button) => {
    button.addEventListener("click", () => {
      const selected = button.dataset.clientChoice;
      choices.forEach((choice) => {
        const active = choice.dataset.clientChoice === selected;
        choice.classList.toggle("is-selected", active);
        choice.setAttribute("aria-pressed", String(active));
      });
      cards.forEach((card) => card.classList.toggle("is-selected", card.dataset.clientCard === selected));
    });
  });
  applyPlatform();
  document.querySelector("[data-client-picker]").hidden = false;
  document.querySelector("[data-platform-submit]").hidden = true;
  document.body.classList.add("is-connect-enhanced");

  const root = document.querySelector("[data-copy-root]");
  const button = root.querySelector("[data-copy-button]");
  const buttonLabel = button.querySelector("[data-copy-label]");
  const status = root.querySelector("[data-copy-status]");
  const linkInput = root.querySelector("[data-subscription-link]");
  const value = root.dataset.copyText || "";
  let resetCopyTimer;

  const fallbackCopy = () => {
    linkInput.focus();
    linkInput.select();
    linkInput.setSelectionRange(0, value.length);
    return document.execCommand("copy");
  };
  button.addEventListener("click", async () => {
    window.clearTimeout(resetCopyTimer);
    try {
      let copied = false;
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(value);
          copied = true;
        } catch (_) { /* Some WebViews expose the API but deny clipboard permission. */ }
      }
      if (!copied && !fallbackCopy()) throw new Error("copy failed");
      buttonLabel.textContent = "Скопировано";
      status.textContent = "Ссылка скопирована. Добавьте её в приложение вручную.";
      resetCopyTimer = window.setTimeout(() => { buttonLabel.textContent = "Скопировать"; }, 2400);
    } catch (_) {
      buttonLabel.textContent = "Скопировать";
      status.textContent = "Не удалось скопировать автоматически. Нажмите и удерживайте ссылку.";
    }
  });
})();
