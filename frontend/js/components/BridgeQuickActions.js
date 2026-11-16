// Quy tắc: Poll không được render lại ô nhập/chọn
import { api } from "../api.js";
import { store } from "../state.js";
import { t } from "../i18n.js";
import { displayName } from "../lane-name.js";

const getApi = () => (typeof api !== "undefined" && api ? api : (window.api || {}));

/**
 * <bridge-quick-actions> Component (T40E)
 * Handles Session Reset & Cache Invalidation routed to selectedLane.
 */
export class BridgeQuickActions extends HTMLElement {
  connectedCallback() {
    this.render();
    this.setupEvents();

    this.unsubscribe = store.subscribe((state, prevState) => {
      if (!prevState) return;

      const langChanged = state.lang !== prevState.lang;
      const selectedLaneChanged = state.selectedLane !== prevState.selectedLane;

      if (langChanged || selectedLaneChanged) {
        this.render();
        this.setupEvents();
      } else {
        const lane = this.getCurrentLane(state);
        const laneId = lane.id || state.selectedLane || "default";
        const flowNum = laneId === "acc1" ? 1 : (laneId === "acc2" ? 2 : 1);
        const lanes = state.lanes || [];
        const showLaneTag = Array.isArray(lanes) && lanes.length >= 2;
        const tagContainer = this.querySelector(".qa-lane-tag-container");
        if (tagContainer) {
          tagContainer.innerHTML = showLaneTag
            ? `<span class="badge-tag badge-cyan" style="text-transform:none; font-weight:600; font-size:11px; margin-left:6px;">${displayName(laneId, lane.label, flowNum, state.lang)}</span>`
            : "";
        }
        this.updateAvailability(state);
      }
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
    const laneReachable = lane.reachable !== false;
    const ctrl = lane.control || "off";
    const isOn = ctrl === "on";
    const isStarting = ctrl === "starting";
    const isStopping = ctrl === "stopping";
    const isLocked = isStarting || isStopping;
    const unavailableTitle = t("laneUnreachableAction", lang, { id: laneId });

    let powerText = "";
    let powerIcon = "";
    let powerClass = "";

    if (!laneReachable) {
      powerText = lang === "vi" ? `Khởi động Luồng (${laneId})` : `Start Lane (${laneId})`;
      powerIcon = `▶`;
      powerClass = "btn-primary";
    } else if (isStarting) {
      powerText = lang === "vi" ? "Đang bật..." : "Starting...";
      powerIcon = `◐`;
      powerClass = "btn-secondary";
    } else if (isStopping) {
      powerText = lang === "vi" ? "Đang tắt..." : "Stopping...";
      powerIcon = `◐`;
      powerClass = "btn-secondary";
    } else if (isOn) {
      powerText = lang === "vi" ? `TẮT Luồng (${laneId})` : `Disable Lane (${laneId})`;
      powerIcon = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>`;
      powerClass = "btn-danger";
    } else {
      powerText = lang === "vi" ? `BẬT Luồng (${laneId})` : `Enable Lane (${laneId})`;
      powerIcon = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>`;
      powerClass = "btn-primary";
    }

    const flowNum = laneId === "acc1" ? 1 : (laneId === "acc2" ? 2 : 1);
    const showLaneTag = Array.isArray(lanes) && lanes.length >= 2;
    const laneTagHtml = showLaneTag
      ? `<span class="badge-tag badge-cyan" style="text-transform:none; font-weight:600; font-size:11px; margin-left:6px;">${displayName(laneId, lane.label, flowNum, lang)}</span>`
      : "";

    this.innerHTML = `
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title" style="display:flex; align-items:center;">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
            <span>${t("quickActionsTitle", lang)}</span>
            <span class="qa-lane-tag-container">${laneTagHtml}</span>
          </div>
        </div>
        <div class="panel-body">
          <!-- Công tắc BẬT / TẮT luồng -->
          <div style="margin-bottom: 10px;">
            <button class="btn ${powerClass}" id="qa-power-btn" style="width: 100%; justify-content: center; font-weight: 600; padding: 8px 12px; gap: 8px;" ${isLocked ? "disabled" : ""}>
              ${powerIcon}
              <span>${powerText}</span>
            </button>
          </div>

          <div class="btn-group">
            <button class="btn btn-secondary" id="qa-reset-btn" style="flex: 1;" title="${laneReachable ? t("resetSessionTitle", lang) : unavailableTitle}" ${!laneReachable ? "disabled" : ""}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
              ${t("resetSessionBtn", lang)}
            </button>
            <button class="btn btn-secondary" id="qa-clear-cache-btn" style="flex: 1;" title="${laneReachable ? t("clearCacheTitle", lang) : unavailableTitle}" ${!laneReachable ? "disabled" : ""}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
              ${t("clearCacheBtn", lang)}
            </button>
          </div>
        </div>
      </div>
    `;
  }

  setupEvents() {
    this.querySelector("#qa-power-btn")?.addEventListener("click", () => this.handlePowerToggle());
    this.querySelector("#qa-reset-btn")?.addEventListener("click", () => this.handleReset());
    this.querySelector("#qa-clear-cache-btn")?.addEventListener("click", () => this.handleClearCache());
  }

  async handlePowerToggle() {
    const state = store.getState();
    const { lang, lanes } = state;
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";

    const ctrl = lane.control || "off";
    const btn = this.querySelector("#qa-power-btn");

    if (ctrl === "off") {
      if (btn) btn.disabled = true;
      store.notifyToast(lang === "vi" ? `Đang bật luồng ${laneId}...` : `Enabling lane ${laneId}...`, "info");
      try {
        const res = await getApi().enableLane(laneId);
        if (res && res.error) {
          store.notifyToast(res.error.message || "Failed to enable lane", "error");
        } else {
          store.notifyToast(lang === "vi" ? `Luồng ${laneId} đã BẬT thành công!` : `Lane ${laneId} enabled!`, "success");
        }
      } catch (err) {
        store.notifyToast(err.message, "error");
      } finally {
        if (btn) btn.disabled = false;
        window.dispatchEvent(new CustomEvent("refresh-request"));
      }
    } else if (ctrl === "on") {
      const inflight = lane.inflight || 0;
      const label = lane.label || laneId;
      const msg = t("confirmDisableLane", lang, {
        id: laneId,
        label,
        n: inflight,
      });

      if (!window.confirm(msg)) return;

      if (btn) btn.disabled = true;
      try {
        const res = await getApi().disableLane(laneId);
        if (res && res.error) {
          store.notifyToast(res.error.message || "Failed to disable lane", "error");
        } else {
          store.notifyToast(lang === "vi" ? `Luồng ${laneId} đã TẮT.` : `Lane ${laneId} disabled.`, "info");
        }
      } catch (err) {
        store.notifyToast(err.message, "error");
      } finally {
        if (btn) btn.disabled = false;
        window.dispatchEvent(new CustomEvent("refresh-request"));
      }
    }
  }

  updateAvailability(state) {
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";
    const laneReachable = lane.reachable !== false;
    const buttons = [
      ["#qa-reset-btn", "resetSessionTitle"],
      ["#qa-clear-cache-btn", "clearCacheTitle"],
    ];

    buttons.forEach(([selector, titleKey]) => {
      const button = this.querySelector(selector);
      if (!button) return;
      button.disabled = !laneReachable;
      button.title = laneReachable
        ? t(titleKey, state.lang)
        : t("laneUnreachableAction", state.lang, { id: laneId });
    });
  }

  async handleReset() {
    const state = store.getState();
    const { lang } = state;
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";
    if (lane.reachable === false) {
      store.notifyToast(t("laneUnreachableAction", lang, { id: laneId }), "warning");
      return;
    }

    const confirmMsg =
      lang === "vi"
        ? `⚠️ Bạn có chắc muốn đóng đoạn chat hiện tại trên Gemini Web của luồng ${laneId} và tạo đoạn chat mới?`
        : `⚠️ Are you sure you want to close current chat session for lane ${laneId} and start a new one?`;

    if (!confirm(confirmMsg)) {
      return;
    }

    try {
      await getApi().resetLaneSession(laneId);
      store.notifyToast(t("sessionResetSuccess", lang), "success");
      window.dispatchEvent(new CustomEvent("refresh-request"));
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
    }
  }

  async handleClearCache() {
    const state = store.getState();
    const { lang } = state;
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";
    if (lane.reachable === false) {
      store.notifyToast(t("laneUnreachableAction", lang, { id: laneId }), "warning");
      return;
    }

    try {
      const data = await getApi().clearLaneCache(laneId);
      store.notifyToast(t("cacheClearedSuccess", lang, { count: data.cleared_entries || 0 }), "success");
      window.dispatchEvent(new CustomEvent("refresh-request"));
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
    }
  }
}

customElements.define("bridge-quick-actions", BridgeQuickActions);
