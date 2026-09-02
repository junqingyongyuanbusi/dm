(() => {
  const pollingStatuses = new Set(["PENDING", "PROCESSING"]);
  let dialogTrigger = null;

  const closeDialog = (dialog) => {
    if (!(dialog instanceof HTMLDialogElement)) {
      return;
    }
    dialog.close();
    if (dialogTrigger instanceof HTMLElement) {
      dialogTrigger.focus();
    }
    dialogTrigger = null;
  };

  document.querySelectorAll("[data-open-channel-dialog]").forEach((trigger) => {
    trigger.addEventListener("click", () => {
      const dialogId = trigger.getAttribute("data-open-channel-dialog");
      const dialog = dialogId ? document.getElementById(dialogId) : null;
      if (!(dialog instanceof HTMLDialogElement)) {
        return;
      }
      dialogTrigger = trigger;
      dialog.showModal();
      const firstInput = dialog.querySelector("input:not([type='hidden'])");
      if (firstInput instanceof HTMLElement) {
        firstInput.focus();
      }
    });
  });

  document.querySelectorAll("[data-close-channel-dialog]").forEach((trigger) => {
    trigger.addEventListener("click", () => closeDialog(trigger.closest("dialog")));
  });

  document.querySelectorAll(".saas-channel-dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) {
        closeDialog(dialog);
      }
    });
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeDialog(dialog);
    });
  });

  document
    .querySelectorAll("[data-channel-oauth-form], [data-channel-credential-form]")
    .forEach((form) => {
      form.addEventListener("submit", () => {
        const submitButton = form.querySelector("button[type='submit']");
        if (!(submitButton instanceof HTMLButtonElement)) {
          return;
        }
        const pendingLabel =
          submitButton.dataset.pendingLabel?.trim() ||
          form.dataset.pendingLabel?.trim() ||
          "…";
        submitButton.disabled = true;
        submitButton.setAttribute("aria-busy", "true");
        submitButton.textContent = pendingLabel;
      });
    });

  const refreshJob = async (jobCard) => {
    const jobUrl = jobCard.getAttribute("data-job-url");
    if (!jobUrl) {
      return false;
    }
    try {
      const response = await fetch(jobUrl, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) {
        return false;
      }
      const job = await response.json();
      const previousStatus = jobCard.getAttribute("data-job-status");
      jobCard.setAttribute("data-job-status", job.status || "");
      const step = jobCard.querySelector("[data-job-step]");
      if (step) {
        step.textContent = job.current_step || job.status;
      }
      if (
        previousStatus &&
        previousStatus !== job.status &&
        !pollingStatuses.has(job.status)
      ) {
        window.location.reload();
        return false;
      }
      return pollingStatuses.has(job.status);
    } catch (_error) {
      return false;
    }
  };

  const jobCards = Array.from(document.querySelectorAll("[data-channel-job]"));
  if (jobCards.length > 0) {
    const poll = async () => {
      const activeResults = await Promise.all(jobCards.map(refreshJob));
      if (activeResults.some(Boolean)) {
        window.setTimeout(poll, 2500);
      }
    };
    window.setTimeout(poll, 1200);
  }
})();
