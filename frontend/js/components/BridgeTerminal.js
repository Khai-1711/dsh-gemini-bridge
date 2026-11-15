// Quy tắc: Poll không được render lại ô nhập/chọn
import { store } from "../state.js";
import { t } from "../i18n.js";
import { displayName } from "../lane-name.js";

/**
 * <bridge-terminal> Component (T40E)
 * Live Log Explorer routed to selectedLane, with Date Dividers, Filtering & Search.
 * Chỉ hiện thẻ tên luồng khi có >= 2 luồng.
 * QUY TẮC: Poll không được render lại ô nhập/chọn (giữ search input & scroll)!
 */
export class BridgeTerminal extends HTMLElement {
  connectedCallback() {
    this.render();
    this.setupEvents();

    this.unsubscribe = store.subscribe((state, prevState) => {
      if (!prevState) {
        this.renderLogs();
        return;
      }

      const langChanged = state.lang !== prevState.lang;
      const selectedLaneChanged = state.selectedLane !== prevState.selectedLane;

      if (langChanged || selectedLaneChanged) {
        // Chỉ re-render skeleton khi đổi ngôn ngữ hoặc chuyển sang xem luồng khác
        this.render();
        this.setupEvents();
      } else {
        // Vòng poll 3s (lanes thay đổi) -> Cập nhật thẻ tên luồng trên tiêu đề nếu cần
        const lane = this.getCurrentLane(state);
        const laneId = lane.id || state.selectedLane || "default";
        const flowNum = laneId === "acc1" ? 1 : (laneId === "acc2" ? 2 : 1);
        const lanes = state.lanes || [];
        const showLaneTag = Array.isArray(lanes) && lanes.length >= 2;
        const tagContainer = this.querySelector(".terminal-lane-tag-container");
        if (tagContainer) {
          tagContainer.innerHTML = showLaneTag
            ? `<span class="badge-tag badge-cyan" style="text-transform:none; font-weight:600; font-size:11px; margin-left:6px;">${displayName(laneId, lane.label, flowNum, state.lang)}</span>`
            : "";
        }
      }

      this.renderLogs();
    });
  }

  disconnectedCallback() {
    if (this.unsubscribe) this.unsubscribe();
  }

  getCurrentLane(state) {
    const lanes = state.lanes || [];
    const sel = state.selectedLane;
    return lanes.find((l) => l.id === sel) || lanes[0] || {};
  }

  render() {
    const state = store.getState();
    const { lang, lanes } = state;
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";

    const flowNum = laneId === "acc1" ? 1 : (laneId === "acc2" ? 2 : 1);
    const showLaneTag = Array.isArray(lanes) && lanes.length >= 2;
    const laneTagHtml = showLaneTag
      ? `<span class="badge-tag badge-cyan" style="text-transform:none; font-weight:600; font-size:11px; margin-left:6px;">${displayName(laneId, lane.label, flowNum, lang)}</span>`
      : "";

    this.innerHTML = `
      <section class="panel terminal-panel">
        <div class="panel-head">
          <div class="panel-title" style="display:flex; align-items:center;">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
            <span>${t("liveLogsConsole", lang)}</span>
            <span class="terminal-lane-tag-container">${laneTagHtml}</span>
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
    const match = line.match(/^(\d{4}-\d{2}-\d{2})?\s*(\d{2}:\d{2}:\d{2})\s*\|\s*([A-Z]+)\s*\|\s*(.*)$/);
    if (match) {
      return {
        date: match[1] || "",
        time: match[2],
        level: match[3].trim(),
        msg: match[4].trim(),
      };
    }
    return { date: "", time: "--:--:--", level: "INFO", msg: line };
  }

  renderLogs() {
    if (!this.viewport) return;
    const state = store.getState();
    const { rawLogs, filterLevel, searchQuery, autoScroll, lang, lanes } = state;
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";
    const query = searchQuery.toLowerCase().trim();

    // LỖI 4: Chỉ hiện thẻ tên luồng ở từng dòng log khi có >= 2 luồng
    const showLineTag = Array.isArray(lanes) && lanes.length >= 2;

    const filtered = (rawLogs || []).filter((line) => {
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

    let lastDate = "";
    const htmlParts = [];

    filtered.forEach((line) => {
      const parsed = this.parseLine(line);

      if (parsed.date && parsed.date !== lastDate) {
        lastDate = parsed.date;
        htmlParts.push(
          `<div class="log-date-divider" style="text-align: center; margin: 10px 0 6px 0; color: var(--text-muted); font-size: 11px; font-family: var(--font-mono); font-weight: 600; opacity: 0.85;">── ${parsed.date} ──</div>`
        );
      }

      let formattedMsg = this.escapeHtml(parsed.msg);
      if (query) {
        const regex = new RegExp(`(${this.escapeRegex(query)})`, "gi");
        formattedMsg = formattedMsg.replace(regex, '<span class="log-highlight">$1</span>');
      }

      htmlParts.push(`
        <div class="log-entry">
          ${showLineTag ? `<span style="font-size: 10px; font-family: var(--font-mono); color: var(--text-muted); background: var(--bg-surface-hover); padding: 1px 4px; border-radius: 3px; margin-right: 4px;">${laneId}</span>` : ""}
          <span class="log-time">${parsed.time}</span>
          <span class="log-level ${parsed.level}">${parsed.level}</span>
          <span class="log-msg">${formattedMsg}</span>
        </div>
      `);
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
