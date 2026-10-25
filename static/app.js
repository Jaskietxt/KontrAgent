(() => {
  const form = document.querySelector("#f");
  if (form) {
    form.addEventListener("submit", () => {
      const button = document.querySelector("#go");
      const progress = document.querySelector("#progress");
      if (button && progress) {
        button.disabled = true;
        button.textContent = "Формируем…";
        progress.hidden = false;
      }
    });
  }

  const refreshForm = document.querySelector("#refresh-form");
  if (refreshForm) {
    refreshForm.addEventListener("submit", () => {
      const button = document.querySelector("#refresh");
      const line = document.querySelector("#rstage");
      if (button && line) {
        button.disabled = true;
        button.textContent = "Обновляем…";
        line.hidden = false;
      }
    });
  }

  const tabs = document.querySelectorAll(".tabs [role=tab]");
  const selectTab = (button) => {
    tabs.forEach((tab) => {
      const selected = tab === button;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    document.querySelectorAll(".tabpane").forEach((panel) => {
      panel.hidden = panel.id !== `tab-${button.dataset.tab}`;
    });
  };
  tabs.forEach((button) => {
    button.addEventListener("click", () => selectTab(button));
    button.addEventListener("keydown", (event) => {
      const index = [...tabs].indexOf(button);
      let next;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      if (event.key === "ArrowLeft") next = tabs[(index - 1 + tabs.length) % tabs.length];
      if (event.key === "Home") next = tabs[0];
      if (event.key === "End") next = tabs[tabs.length - 1];
      if (next) {
        event.preventDefault();
        selectTab(next);
        next.focus();
      }
    });
  });

  const how = document.querySelector("#how");
  const dialog = document.querySelector("#dlg");
  if (how && dialog) how.addEventListener("click", () => dialog.showModal());
})();
