(() => {
  const connectionDialog = document.querySelector(
    ".channel-workspace dialog#add-channels",
  );
  if (!(connectionDialog instanceof HTMLDialogElement)) {
    return;
  }
  let connectionTrigger = null;

  const openConnectionDialog = (trigger = null) => {
    if (trigger instanceof HTMLElement) {
      connectionTrigger = trigger;
    }
    if (!connectionDialog.open) {
      connectionDialog.showModal();
    }
  };

  document.querySelectorAll('[data-open-channel-workspace], a[href="#add-channels"]').forEach((trigger) => {
    trigger.addEventListener("click", (event) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      event.preventDefault();
      openConnectionDialog(trigger);
    });
  });

  connectionDialog.querySelector("[data-close-channel-workspace]")?.addEventListener("click", () => {
    connectionDialog.close();
  });

  connectionDialog.addEventListener("click", (event) => {
    if (event.target !== connectionDialog) {
      return;
    }
    const bounds = connectionDialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom) {
      connectionDialog.close();
    }
  });

  connectionDialog.addEventListener("close", () => {
    if (!document.querySelector("dialog[open]") && connectionTrigger instanceof HTMLElement) {
      connectionTrigger.focus();
    }
  });

  // Let the existing credential dialog own focus instead of stacking two modals.
  connectionDialog.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) {
      return;
    }
    const credentialTrigger = event.target.closest("[data-open-channel-dialog]");
    if (!(credentialTrigger instanceof HTMLButtonElement) || credentialTrigger.disabled) {
      return;
    }
    const credentialDialog = document.getElementById(credentialTrigger.dataset.openChannelDialog);
    if (!(credentialDialog instanceof HTMLDialogElement)) {
      return;
    }
    connectionDialog.close();
    credentialDialog.addEventListener("close", () => {
      openConnectionDialog();
      credentialTrigger.focus();
    }, { once: true });
  }, true);

  window.addEventListener("hashchange", () => {
    if (window.location.hash === "#add-channels") {
      openConnectionDialog();
    }
  });
  if (window.location.hash === "#add-channels") {
    openConnectionDialog();
  }
})();
