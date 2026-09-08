(() => {
  const initializeSidebarDrawer = () => {
    const sidebar = document.querySelector(
      "[data-sidebar], .saas-sidebar, .sidebar",
    );
    const sidebarToggleButtons = Array.from(
      document.querySelectorAll("[data-sidebar-toggle]"),
    );

    if (
      !(sidebar instanceof HTMLElement) ||
      sidebarToggleButtons.length === 0
    ) {
      return;
    }

    const sidebarCloseButtons = Array.from(
      document.querySelectorAll("[data-sidebar-close]"),
    );
    const mobileSidebarMedia = window.matchMedia("(max-width: 780px)");
    let sidebarTrigger = null;

    let sidebarBackdrop = document.querySelector("[data-sidebar-backdrop]");
    if (!(sidebarBackdrop instanceof HTMLElement)) {
      sidebarBackdrop = document.createElement("button");
      sidebarBackdrop.type = "button";
      sidebarBackdrop.className = "sidebar-backdrop";
      sidebarBackdrop.setAttribute("data-sidebar-backdrop", "");
      const fallbackCloseLabel =
        document.documentElement.lang === "en"
          ? "Close navigation menu"
          : "关闭导航菜单";
      sidebarBackdrop.setAttribute(
        "aria-label",
        document.body.dataset.sidebarBackdropLabel?.trim() ||
          fallbackCloseLabel,
      );
      document.body.append(sidebarBackdrop);
    }
    sidebarBackdrop.tabIndex = -1;
    sidebarBackdrop.setAttribute("aria-hidden", "true");

    const setToggleState = (isOpen) => {
      sidebarToggleButtons.forEach((toggleButton) => {
        toggleButton.setAttribute("aria-expanded", String(isOpen));
      });
    };

    const restoreTriggerFocus = () => {
      if (sidebarTrigger instanceof HTMLElement) {
        sidebarTrigger.focus({ preventScroll: true });
      }
      sidebarTrigger = null;
    };

    const closeSidebar = ({ restoreFocus = true } = {}) => {
      document.body.classList.remove("sidebar-drawer-open");
      setToggleState(false);

      if (mobileSidebarMedia.matches) {
        sidebar.setAttribute("aria-hidden", "true");
        sidebar.inert = true;
      } else {
        sidebar.removeAttribute("aria-hidden");
        sidebar.inert = false;
      }
      sidebarBackdrop.setAttribute("aria-hidden", "true");

      if (restoreFocus) {
        restoreTriggerFocus();
      } else {
        sidebarTrigger = null;
      }
    };

    const openSidebar = (trigger) => {
      if (!mobileSidebarMedia.matches) {
        return;
      }

      sidebarTrigger =
        trigger instanceof HTMLElement ? trigger : sidebarToggleButtons[0];
      document.body.classList.add("sidebar-drawer-open");
      sidebar.removeAttribute("aria-hidden");
      sidebar.inert = false;
      sidebarBackdrop.setAttribute("aria-hidden", "false");
      setToggleState(true);

      const firstFocusableElement = sidebar.querySelector(
        "[data-sidebar-close], a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
      );
      if (firstFocusableElement instanceof HTMLElement) {
        firstFocusableElement.focus({ preventScroll: true });
      }
    };

    sidebarToggleButtons.forEach((toggleButton) => {
      toggleButton.setAttribute("aria-expanded", "false");
      toggleButton.addEventListener("click", () => {
        if (document.body.classList.contains("sidebar-drawer-open")) {
          closeSidebar();
        } else {
          openSidebar(toggleButton);
        }
      });
    });

    sidebarCloseButtons.forEach((closeButton) => {
      closeButton.addEventListener("click", () => closeSidebar());
    });

    sidebarBackdrop.addEventListener("click", () => closeSidebar());
    sidebar.addEventListener("click", (event) => {
      if (event.target instanceof Element && event.target.closest("a[href]")) {
        closeSidebar({ restoreFocus: false });
      }
    });

    document.addEventListener("keydown", (event) => {
      if (
        event.key === "Escape" &&
        document.body.classList.contains("sidebar-drawer-open")
      ) {
        event.preventDefault();
        closeSidebar();
      }
    });

    const synchronizeSidebarMode = () => {
      closeSidebar({ restoreFocus: false });
    };

    mobileSidebarMedia.addEventListener("change", synchronizeSidebarMode);
    document.documentElement.classList.add("sidebar-drawer-ready");
    synchronizeSidebarMode();
  };

  const initializeListSearches = () => {
    const listSearchInputs = Array.from(
      document.querySelectorAll("[data-list-search]"),
    );

    listSearchInputs.forEach((listSearchInput) => {
      if (!(listSearchInput instanceof HTMLInputElement)) {
        return;
      }

      const filterScope = listSearchInput.closest("[data-list-filter]");
      if (!(filterScope instanceof HTMLElement)) {
        return;
      }

      const filterableItems = Array.from(
        filterScope.querySelectorAll("[data-filter-text]"),
      );
      const listItemsContainer = filterScope.querySelector("[data-list-items]");
      const searchEmptyState = filterScope.querySelector("[data-search-empty]");

      const normalizeSearchText = (value) =>
        value.normalize("NFKC").trim().toLocaleLowerCase();

      const filterLoadedItems = () => {
        const normalizedQuery = normalizeSearchText(listSearchInput.value);
        let visibleItemCount = 0;

        filterableItems.forEach((filterableItem) => {
          if (!(filterableItem instanceof HTMLElement)) {
            return;
          }

          const normalizedFilterText = normalizeSearchText(
            filterableItem.dataset.filterText || "",
          );
          const matchesQuery =
            normalizedQuery === "" ||
            normalizedFilterText.includes(normalizedQuery);

          filterableItem.hidden = !matchesQuery;
          filterableItem.setAttribute("aria-hidden", String(!matchesQuery));
          if (matchesQuery) {
            visibleItemCount += 1;
          }
        });

        const hasNoSearchMatches =
          normalizedQuery !== "" && visibleItemCount === 0;
        if (listItemsContainer instanceof HTMLElement) {
          listItemsContainer.hidden = hasNoSearchMatches;
        }
        if (searchEmptyState instanceof HTMLElement) {
          searchEmptyState.hidden = !hasNoSearchMatches;
          searchEmptyState.setAttribute(
            "aria-hidden",
            String(!hasNoSearchMatches),
          );
        }
      };

      listSearchInput.addEventListener("input", filterLoadedItems);
      filterLoadedItems();
    });
  };

  const initializeToolbarPopovers = () => {
    const popoverTriggers = Array.from(
      document.querySelectorAll("[data-popover-trigger]"),
    );

    if (popoverTriggers.length === 0) {
      return;
    }

    const closePopover = (trigger, { restoreFocus = false } = {}) => {
      if (!(trigger instanceof HTMLElement)) {
        return;
      }
      const popoverName = trigger.dataset.popoverTrigger;
      const popover = popoverName
        ? document.querySelector(`[data-popover="${popoverName}"]`)
        : null;
      if (popover instanceof HTMLElement) {
        popover.hidden = true;
      }
      trigger.setAttribute("aria-expanded", "false");
      if (restoreFocus) {
        trigger.focus({ preventScroll: true });
      }
    };

    const closeAllPopovers = ({ except = null, restoreFocus = false } = {}) => {
      popoverTriggers.forEach((trigger) => {
        if (trigger !== except) {
          closePopover(trigger, { restoreFocus });
        }
      });
    };

    const focusPopoverItem = (popover, position) => {
      const items = Array.from(
        popover.querySelectorAll(
          '[role="menuitemradio"], [role="menuitem"], a[href], button:not([disabled])',
        ),
      ).filter((item) => item instanceof HTMLElement && !item.hidden);
      if (items.length === 0) {
        return;
      }
      const targetIndex = position === "last" ? items.length - 1 : 0;
      items[targetIndex].focus({ preventScroll: true });
    };

    popoverTriggers.forEach((trigger) => {
      if (!(trigger instanceof HTMLElement)) {
        return;
      }
      const popoverName = trigger.dataset.popoverTrigger;
      const popover = popoverName
        ? document.querySelector(`[data-popover="${popoverName}"]`)
        : null;
      if (!(popover instanceof HTMLElement)) {
        return;
      }

      const openPopover = ({ focusFirstItem = false } = {}) => {
        closeAllPopovers({ except: trigger });
        popover.hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        if (focusFirstItem) {
          focusPopoverItem(popover, "first");
        }
      };

      trigger.addEventListener("click", () => {
        if (popover.hidden) {
          openPopover();
        } else {
          closePopover(trigger);
        }
      });

      trigger.addEventListener("keydown", (event) => {
        if (event.key === "ArrowDown") {
          event.preventDefault();
          openPopover({ focusFirstItem: true });
        }
      });

      popover.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          closePopover(trigger, { restoreFocus: true });
          return;
        }

        const menuItems = Array.from(
          popover.querySelectorAll('[role="menuitemradio"], [role="menuitem"]'),
        ).filter((item) => item instanceof HTMLElement && !item.hidden);
        const currentIndex = menuItems.indexOf(document.activeElement);
        if (currentIndex < 0) {
          return;
        }

        let nextIndex = currentIndex;
        if (event.key === "ArrowDown") {
          nextIndex = (currentIndex + 1) % menuItems.length;
        } else if (event.key === "ArrowUp") {
          nextIndex = (currentIndex - 1 + menuItems.length) % menuItems.length;
        } else if (event.key === "Home") {
          nextIndex = 0;
        } else if (event.key === "End") {
          nextIndex = menuItems.length - 1;
        } else {
          return;
        }
        event.preventDefault();
        menuItems[nextIndex].focus({ preventScroll: true });
      });
    });

    document.addEventListener("click", (event) => {
      if (!(event.target instanceof Element)) {
        return;
      }
      if (!event.target.closest(".saas-toolbar-control")) {
        closeAllPopovers();
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        const openTrigger = popoverTriggers.find(
          (trigger) => trigger.getAttribute("aria-expanded") === "true",
        );
        if (openTrigger instanceof HTMLElement) {
          event.preventDefault();
          closePopover(openTrigger, { restoreFocus: true });
        }
      }
    });

    window.replyPopovers = { closeAll: closeAllPopovers };
  };

  const initializeThemeOptions = () => {
    const themeOptions = Array.from(
      document.querySelectorAll("[data-theme-option]"),
    );
    if (themeOptions.length === 0 || !window.replyTheme) {
      return;
    }

    const synchronizeThemeOptions = () => {
      const currentPreference = window.replyTheme.getPreference();
      themeOptions.forEach((themeOption) => {
        if (!(themeOption instanceof HTMLElement)) {
          return;
        }
        const isSelected =
          themeOption.dataset.themeOption === currentPreference;
        themeOption.setAttribute("aria-checked", String(isSelected));
        themeOption.classList.toggle("selected", isSelected);
      });
    };

    themeOptions.forEach((themeOption) => {
      themeOption.addEventListener("click", () => {
        const preference = themeOption.dataset.themeOption;
        if (!preference) {
          return;
        }
        window.replyTheme.setPreference(preference);
        window.replyPopovers?.closeAll();
      });
    });

    window.addEventListener("reply-theme-change", synchronizeThemeOptions);
    synchronizeThemeOptions();
  };

  const initializeInboxKeyboardNavigation = () => {
    const inboxWorkspace = document.querySelector("[data-inbox-workspace]");
    if (!(inboxWorkspace instanceof HTMLElement)) {
      return;
    }

    const searchInput = inboxWorkspace.querySelector("[data-list-search]");
    const focusableItems = () =>
      Array.from(inboxWorkspace.querySelectorAll(".saas-work-item")).filter(
        (item) => item instanceof HTMLElement && !item.hidden,
      );
    const isTypingTarget = (target) =>
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement ||
      target?.isContentEditable;

    document.addEventListener("keydown", (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTypingTarget(event.target)) {
        return;
      }
      if (event.key === "/" && searchInput instanceof HTMLInputElement) {
        event.preventDefault();
        searchInput.focus({ preventScroll: true });
        return;
      }
      if (event.key !== "j" && event.key !== "k") {
        return;
      }
      const items = focusableItems();
      if (items.length === 0) {
        return;
      }
      event.preventDefault();
      const currentIndex = items.indexOf(document.activeElement);
      const delta = event.key === "j" ? 1 : -1;
      const nextIndex =
        currentIndex < 0
          ? 0
          : (currentIndex + delta + items.length) % items.length;
      items[nextIndex].focus({ preventScroll: true });
    });
  };

  const initializeApplication = () => {
    initializeSidebarDrawer();
    initializeListSearches();
    initializeToolbarPopovers();
    initializeThemeOptions();
    initializeInboxKeyboardNavigation();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeApplication, {
      once: true,
    });
  } else {
    initializeApplication();
  }
})();
