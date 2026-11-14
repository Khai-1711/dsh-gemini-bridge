import { store } from "../state.js";
import { t } from "../i18n.js";

/**
 * <bridge-terminal> Component
 * Live Log Explorer with Filtering, Search Highlighting, Copy & Clear + i18n
 */
export class BridgeTerminal extends HTMLElement {
  connectedCallback() {
    this.render();
    this.setupEvents();

    this.unsubscribe = store.subscribe((state, prevState) => {
      if (prevState && state.lang !== prevState.lang) {
        this.render();
        this.setupEvents();
      }
      this.renderLogs();
    });
  }

  disconnectedCallback() {
    if (this.unsubscribe) this.unsubscribe();
  }

  render() {
    const { lang } = store.getState();

    this.innerHTML = `
      <section class="panel terminal-panel">
        <div class="panel-head">
          <div class="panel-title">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
            ${t("liveLogsConsole", lang)}
          </div>
          <div class="btn-group">
            <button class="btn btn-secondary btn-icon" id="t-copy-btn" title="${t("copyLogsTitle", lang)}">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            </button>
            <button class="btn btn-secondary btn-icon" id="t-clear-btn" title="${t("clearConsoleTitle", lang)}">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            </button>
          </div>
        </div>

        <div class="terminal-toolbar">
          <div class="filter-group">
            <button class="filter-btn active" data-level="ALL">${t("filterAll", lang)}</button>
            <button class="filter-btn" data-level="INFO">INFO</button>
            <button class="filter-btn" data-level="WARN">WARN</button>
            <button class="filter-btn" data-level="ERROR">ERROR</button>
          </div>

          <div class="search-input-wrap">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
            <input type="text" class="search-input" id="t-search" placeholder="${t("searchPlaceholder", lang)}">
          </div>

          <div class="terminal-options">
            <label class="checkbox-label" title="${t("autoScroll", lang)}">
              <input type="checkbox" id="t-autoscroll" checked>
              <span>${t("autoScroll", lang)}</span>
            </label>
          </div>
        </div>

        <div class="terminal-viewport" id="t-viewport">
          <div class="log-entry" style="color: var(--text-muted);">${t("connectingLogs", lang)}</div>
        </div>
      </section>
    `;

    this.viewport = this.querySelector("#t-viewport");
    this.searchInput = this.querySelector("#t-search");
    this.autoscrollCheck = this.querySelector("#t-autoscroll");
    this.filterBtns = this.querySelectorAll(".filter-btn");
  }

  setupEvents() {
    this.querySelector("#t-copy-btn")?.addEventListener("click", () => {
      const { rawLogs, lang } = store.getState();
      if (!rawLogs.length) {
        store.notifyToast(t("noLogsToCopy", lang), "info");
        return;
      }
      navigator.clipboard.writeText(rawLogs.join("\n")).then(() => {
        store.notifyToast(t("logsCopied", lang), "success");
      });
    });

    this.querySelector("#t-clear-btn")?.addEventListener("click", () => {
      const { lang } = store.getState();
      store.setState({ rawLogs: [] });
      store.notifyToast(t("consoleCleared", lang), "info");
    });

    this.filterBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        this.filterBtns.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        store.setState({ filterLevel: btn.getAttribute("data-level") });
      });
    });

    this.searchInput?.addEventListener("input", (e) => {
      store.setState({ searchQuery: e.target.value });
    });

    this.autoscrollCheck?.addEventListener("change", (e) => {
      store.setState({ autoScroll: e.target.checked });
    });
  }

  parseLine(line) {
    const match = line.match(/^(\d{4}-\d{2}-\d{2}\s)?(\d{2}:\d{2}:\d{2})\s*\|\s*([A-Z]+)\s*\|\s*(.*)$/);
    if (match) {
      return { time: match[2], level: match[3].trim(), msg: match[4].trim() };
    }
    return { time: "--:--:--", level: "INFO", msg: line };
  }

  renderLogs() {
    const { rawLogs, filterLevel, searchQuery, autoScroll, lang } = store.getState();
    const query = searchQuery.toLowerCase().trim();

    const filtered = rawLogs.filter((line) => {
      if (!line) return false;
      const parsed = this.parseLine(line);

      if (filterLevel !== "ALL") {
        if (filterLevel === "WARN" && !["WARN", "WARNING"].includes(parsed.level)) return false;
        if (filterLevel === "ERROR" && !["ERROR", "CRITICAL"].includes(parsed.level)) return false;
        if (filterLevel === "INFO" && parsed.level !== "INFO") return false;
      }

      if (query && !line.toLowerCase().includes(query)) return false;
      return true;
    });

    if (filtered.length === 0) {
      this.viewport.innerHTML = `<div class="log-entry" style="color: var(--text-muted);">${t("noLogsFound", lang)}</div>`;
      return;
    }

    const htmlParts = filtered.map((line) => {
      const parsed = this.parseLine(line);
      let formattedMsg = this.escapeHtml(parsed.msg);

      if (query) {
        const regex = new RegExp(`(${this.escapeRegex(query)})`, "gi");
        formattedMsg = formattedMsg.replace(regex, '<span class="log-highlight">$1</span>');
      }

      return `<div class="log-entry">
        <span class="log-time">${parsed.time}</span>
        <span class="log-level ${parsed.level}">${parsed.level}</span>
        <span class="log-msg">${formattedMsg}</span>
      </div>`;
    });

    this.viewport.innerHTML = htmlParts.join("");

    if (autoScroll) {
      this.viewport.scrollTop = this.viewport.scrollHeight;
    }
  }

  escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }
}

customElements.define("bridge-terminal", BridgeTerminal);
