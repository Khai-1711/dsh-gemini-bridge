import { store } from "../state.js";
import { t } from "../i18n.js";
import { api } from "../api.js";
import { displayName } from "../lane-name.js";

/**
 * <bridge-lanes> Component (T40 - Bố cục CHỦ DỰ ÁN CHỐT)
 * Khung "⚡ Quản Lý Luồng Agent (API Flows)"
 * - Mỗi luồng 1 hàng: Tên luồng, Chấm trạng thái + Chữ, Công tắc BẬT/TẮT, Dòng thống kê
 * - Bấm vào hàng để chọn luồng làm việc cho các panel bên dưới
 * - Poll không dựng lại toàn bộ DOM (in-place update)
 */
export class BridgeLanes extends HTMLElement {
  connectedCallback() {
    this.render();
    this.setupEvents();

    this.unsubscribe = store.subscribe((state, prevState) => {
      if (!prevState) return;

      const langChanged = state.lang !== prevState.lang;
      const themeChanged = state.theme !== prevState.theme;
      const selectedChanged = state.selectedLane !== prevState.selectedLane;
      const lanesChanged = JSON.stringify(state.lanes) !== JSON.stringify(prevState.lanes);

      if (langChanged || themeChanged) {
        this.render();
        this.setupEvents();
      } else if (selectedChanged || lanesChanged) {
        this.updateDynamicValues(state);
      }
    });
  }

  disconnectedCallback() {
    if (this.unsubscribe) this.unsubscribe();
  }

  getLaneTitle(laneId, label, n, lang) {
    const cleanId = (laneId || "").trim();
    const cleanLabel = (label || "").trim();
    const prefix = lang === "en" ? `API Flow ${n}` : `Luồng API ${n}`;

    let tag = "";
    if (cleanLabel && cleanLabel !== cleanId) {
      tag = cleanLabel;
    } else if (cleanId && cleanId.toLowerCase() !== "default") {
      tag = cleanId;
    }

    return tag ? `${prefix} (${tag})` : prefix;
  }

  getLanePort(lane) {
    const port = Number(lane.port);
    if (Number.isInteger(port) && port > 0) return port;

    try {
      const url = new URL(lane.url);
      return Number(url.port) || (url.protocol === "https:" ? 443 : 80);
    } catch {
      return 10000;
    }
  }

  renderToggleButton(laneId, switchState, controllable, lang) {
    if (switchState === "none") return "";

    const isChecked = switchState === "on";
    const isLocked = switchState === "locked" || !controllable;
    const action = isChecked ? "disable" : "enable";
    const label = t("toggleStateLabel", lang, {
      state: isChecked ? (lang === "vi" ? "BẬT" : "ON") : (lang === "vi" ? "TẮT" : "OFF"),
    });

    return `
      <button
        type="button"
        class="lane-toggle-btn ${isChecked ? "on" : "off"}"
        role="switch"
        aria-checked="${isChecked}"
        aria-label="${t("chipSwitchAria", lang, { id: laneId })}"
        data-lane-id="${laneId}"
        data-action="${action}"
        ${isLocked ? "disabled" : ""}
        title="${!controllable ? t("missingLaneToken", lang) : (isChecked ? t("btnDisable", lang) : t("btnEnable", lang))}"
      >
        <span class="lane-toggle-knob"></span>
        <span>${label}</span>
      </button>
    `;
  }

  updateToggleButton(row, laneId, lane, switchState, lang) {
    const wrapper = row.querySelector(".lane-toggle-wrapper");
    if (!wrapper) return;

    let button = wrapper.querySelector(".lane-toggle-btn");
    if (switchState === "none") {
      button?.remove();
      return;
    }

    if (!button) {
      wrapper.innerHTML = this.renderToggleButton(
        laneId,
        switchState,
        lane.controllable !== false,
        lang
      );
      button = wrapper.querySelector(".lane-toggle-btn");
      if (button) this.setupToggleButton(button);
      return;
    }

    const isChecked = switchState === "on";
    const isLocked = switchState === "locked" || lane.controllable === false;
    button.className = `lane-toggle-btn ${isChecked ? "on" : "off"}`;
    button.setAttribute("aria-checked", String(isChecked));
    button.setAttribute("aria-label", t("chipSwitchAria", lang, { id: laneId }));
    button.setAttribute("data-action", isChecked ? "disable" : "enable");
    button.title = lane.controllable === false
      ? t("missingLaneToken", lang)
      : (isChecked ? t("btnDisable", lang) : t("btnEnable", lang));
    button.disabled = isLocked;
    const label = button.querySelector("span:last-child");
    if (label) {
      label.textContent = t("toggleStateLabel", lang, {
        state: isChecked ? (lang === "vi" ? "BẬT" : "ON") : (lang === "vi" ? "TẮT" : "OFF"),
      });
    }
  }

  render() {
    const { lang, lanes, selectedLane, instanceId } = store.getState();
    const laneList = Array.isArray(lanes) && lanes.length > 0 ? lanes : [];

    const showNoInstanceWarning = instanceId === "default" && (!laneList[0] || laneList[0].id === "default");

    let rowsHtml = "";
    if (laneList.length === 0) {
      rowsHtml = `
        <div style="font-size: 12px; color: var(--text-muted); padding: 12px; text-align: center;">
          ${t("refreshing", lang)}
        </div>
      `;
    } else {
      rowsHtml = laneList
        .map((lane, index) => this.renderLaneRow(lane, index, lang, selectedLane, laneList))
        .join("");
    }

    this.innerHTML = `
      <div class="panel lanes-panel">
        <div class="panel-head">
          <div class="panel-title" style="display:flex; align-items:center; gap:8px;">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>
            </svg>
            <span>${t("agentFlowsTitle", lang)}</span>
          </div>
          <span class="badge-tag badge-cyan">${laneList.length} ${lang === "vi" ? "luồng" : "lanes"}</span>
        </div>

        ${showNoInstanceWarning ? `
          <div style="background: rgba(245, 158, 11, 0.12); border-left: 3px solid #f59e0b; padding: 6px 12px; font-size: 11.5px; color: #f59e0b; display: flex; align-items: center; margin: 8px 16px 0 16px; border-radius: 4px;">
            ${t("noInstanceWarning", lang)}
          </div>
        ` : ""}

        <div class="panel-body" style="padding: 8px 12px;">
          ${rowsHtml}
        </div>
      </div>
    `;
  }

  renderLaneRow(lane, index, lang, selectedLane, allLanes) {
    const laneId = lane.id || `acc${index + 1}`;
    const flowNum = laneId === "acc1" ? 1 : (laneId === "acc2" ? 2 : index + 1);
    const isSelected = selectedLane ? (selectedLane === laneId) : (index === 0);

    const isReachable = lane.reachable !== false;
    const ctrl = lane.control || "off";
    const authOk = Boolean(lane.auth_ok);
    const controllable = lane.controllable !== false;
    const inflight = lane.inflight || 0;
    const reqCount = lane.requests || 0;
    const errCount = lane.errors || 0;

    const rowTitle = this.getLaneTitle(laneId, lane.label, flowNum, lang);

    // Trạng thái (chấm + chữ)
    let dotHtml = "";
    let statusText = "";
    let switchState = "off"; // "off" | "on" | "locked" | "none"

    if (!isReachable) {
      dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border-radius:50%; border:1.5px dashed #ef4444; color:#ef4444; text-align:center; font-size:7px; line-height:8px;">✕</span>`;
      statusText = t("stateUnreachableText", lang, { id: laneId });
      switchState = "none";
    } else if (ctrl === "starting") {
      dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border:2px solid #3b82f6; border-top-color:transparent; border-radius:50%; animation:spin 0.8s linear infinite;"></span>`;
      statusText = t("stateStartingText", lang);
      switchState = "locked";
    } else if (ctrl === "stopping") {
      dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#f59e0b;"></span>`;
      statusText = t("stateStoppingText", lang, { n: inflight });
      switchState = "locked";
    } else if (ctrl === "on" && authOk) {
      dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#10b981; box-shadow:0 0 5px rgba(16,185,129,0.7);"></span>`;
      statusText = t("stateActiveText", lang);
      switchState = "on";
    } else if (ctrl === "on" && !authOk) {
      dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#f59e0b; box-shadow:0 0 5px rgba(245,158,11,0.7);"></span>`;
      statusText = t("stateUnauthText", lang);
      switchState = "on";
    } else {
      dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border-radius:50%; border:1.5px solid var(--text-muted); background:transparent;"></span>`;
      statusText = t("stateOffText", lang);
      switchState = "off";
    }

    const toggleHtml = this.renderToggleButton(laneId, switchState, controllable, lang);

    // Dòng thống kê
    let statsStr = t("statsLabel", lang, { requests: reqCount });
    if (errCount > 0) {
      statsStr = t("statsWithErrors", lang, { requests: reqCount, errors: errCount });
    }

    // Chi tiết bổ sung (A (1/3), chat key, buttons)
    const activeAccObj = lane.active_account || {};
    const accLabel = activeAccObj.label || (lang === "vi" ? "Chính" : "Primary");
    const accIdx = (activeAccObj.index !== undefined ? activeAccObj.index : 0) + 1;
    const accTotal = activeAccObj.total || 1;
    const accDisplay = `${accLabel} (${accIdx}/${accTotal})`;

    const rowClass = [
      "lane-flow-row",
      isSelected ? "selected" : "",
      !isReachable ? "unreachable" : "",
    ].filter(Boolean).join(" ");

    const hasDivider = index < allLanes.length - 1;

    return `
      <div class="${rowClass}" data-row-lane="${laneId}">
        <!-- Hàng 1: Tiêu đề luồng & Model -->
        <div class="lane-row-header">
          <div class="lane-row-title">
            <span class="lane-row-title-label">${rowTitle}</span>
            ${laneId !== "default" ? `<span class="badge-tag" style="font-size:10.5px; opacity:0.75;">${laneId}</span>` : ""}
          </div>
          <span class="lane-model" style="font-size:11px; color:var(--accent-cyan); font-family:var(--font-mono);" ${lane.model ? "" : "hidden"}>${lane.model || ""}</span>
        </div>

        <!-- Hàng 2: Trạng thái & Công tắc -->
        <div class="lane-row-status-line">
          <div class="lane-status-indicator">
            <span class="lane-dot-wrapper">${dotHtml}</span>
            <span class="lane-status-text" style="color:${ctrl === 'on' && !authOk ? '#f59e0b' : 'var(--text-primary)'};">${statusText}</span>
            <button type="button" class="btn btn-secondary btn-sm lane-action-cookie lane-action-cookie-quick" data-lane-id="${laneId}" style="padding:2px 8px; font-size:11px; margin-left:6px;" ${!authOk && ctrl === "on" ? "" : "hidden"} ${!isReachable ? "disabled" : ""}>
              ${t("btnLoadCookie", lang)}
            </button>
          </div>
          <div class="lane-toggle-wrapper">
            ${toggleHtml}
          </div>
        </div>

        <!-- Hàng 3: Dòng thống kê -->
        <div class="lane-stats-line">
          ${statsStr}
        </div>

        <!-- Chi tiết mở rộng (Ẩn mặc định) -->
        <details class="lane-details-wrapper" style="margin-top:6px;">
          <summary class="lane-details-toggle">
            ${t("detailsToggle", lang)}
          </summary>
          <div class="lane-details-box">
            <div style="display:flex; justify-content:space-between; align-items:center;">
              <span><span class="lane-active-account-label">${t("activeAccountLabel", lang)}</span> <b class="lane-active-account-value">${accDisplay}</b></span>
              <span class="lane-port" style="font-family:var(--font-mono); font-size:10.5px; opacity:0.8;">Port: ${this.getLanePort(lane)}</span>
            </div>
            <div class="lane-chat-key" ${lane.chat_key ? "" : "hidden"}><span>${t("chatKeyLabel", lang)}</span> <code style="font-size:10.5px;">${lane.chat_key || ""}</code></div>
            <div style="display:flex; gap:8px; margin-top:4px; flex-wrap:wrap;">
              <button type="button" class="btn btn-secondary btn-sm lane-action-cookie" data-lane-id="${laneId}" ${!isReachable ? "disabled" : ""}>🔑 ${t("btnLoadCookie", lang)}</button>
              <button type="button" class="btn btn-secondary btn-sm lane-action-reauth" data-lane-id="${laneId}" ${!isReachable ? "disabled" : ""}>🔄 ${t("btnTryReauth", lang)}</button>
              <button type="button" class="btn btn-secondary btn-sm lane-action-reset" data-lane-id="${laneId}" ${!isReachable ? "disabled" : ""}>⚠️ Reset</button>
            </div>
          </div>
        </details>
      </div>
      ${hasDivider ? '<div class="lane-row-divider"></div>' : ''}
    `;
  }

  updateDynamicValues(state) {
    const { lang, lanes, selectedLane } = state;
    const laneList = Array.isArray(lanes) && lanes.length > 0 ? lanes : [];

    // Kiểm tra xem số lượng và ID của các luồng có thay đổi không
    const existingRows = this.querySelectorAll(".lane-flow-row");
    const existingIds = Array.from(existingRows).map((el) => el.getAttribute("data-row-lane"));
    const newIds = laneList.map((l, i) => l.id || `acc${i + 1}`);

    if (existingIds.join(",") !== newIds.join(",")) {
      this.render();
      this.setupEvents();
      return;
    }

    // Cập nhật từng hàng tại chỗ
    laneList.forEach((lane, index) => {
      const laneId = lane.id || `acc${index + 1}`;
      const row = this.querySelector(`.lane-flow-row[data-row-lane="${laneId}"]`);
      if (!row) return;

      const isSelected = selectedLane ? selectedLane === laneId : index === 0;
      if (isSelected) {
        row.classList.add("selected");
      } else {
        row.classList.remove("selected");
      }

      const isReachable = lane.reachable !== false;
      const ctrl = lane.control || "off";
      const authOk = Boolean(lane.auth_ok);
      const inflight = lane.inflight || 0;
      const reqCount = lane.requests || 0;
      const errCount = lane.errors || 0;

      // Cập nhật trạng thái
      const dotWrapper = row.querySelector(".lane-dot-wrapper");
      const statusTextEl = row.querySelector(".lane-status-text");

      let dotHtml = "";
      let statusText = "";
      let switchState = "off";

      if (!isReachable) {
        dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border-radius:50%; border:1.5px dashed #ef4444; color:#ef4444; text-align:center; font-size:7px; line-height:8px;">✕</span>`;
        statusText = t("stateUnreachableText", lang, { id: laneId });
        switchState = "none";
      } else if (ctrl === "starting") {
        dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border:2px solid #3b82f6; border-top-color:transparent; border-radius:50%; animation:spin 0.8s linear infinite;"></span>`;
        statusText = t("stateStartingText", lang);
        switchState = "locked";
      } else if (ctrl === "stopping") {
        dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#f59e0b;"></span>`;
        statusText = t("stateStoppingText", lang, { n: inflight });
        switchState = "locked";
      } else if (ctrl === "on" && authOk) {
        dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#10b981; box-shadow:0 0 5px rgba(16,185,129,0.7);"></span>`;
        statusText = t("stateActiveText", lang);
        switchState = "on";
      } else if (ctrl === "on" && !authOk) {
        dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#f59e0b; box-shadow:0 0 5px rgba(245,158,11,0.7);"></span>`;
        statusText = t("stateUnauthText", lang);
        switchState = "on";
      } else {
        dotHtml = `<span class="lane-status-dot" style="display:inline-block; width:8px; height:8px; border-radius:50%; border:1.5px solid var(--text-muted); background:transparent;"></span>`;
        statusText = t("stateOffText", lang);
        switchState = "off";
      }

      if (dotWrapper) dotWrapper.innerHTML = dotHtml;
      if (statusTextEl) {
        statusTextEl.textContent = statusText;
        statusTextEl.style.color = ctrl === "on" && !authOk ? "#f59e0b" : "var(--text-primary)";
      }
      row.classList.toggle("unreachable", !isReachable);

      const flowNum = laneId === "acc1" ? 1 : (laneId === "acc2" ? 2 : index + 1);
      const titleEl = row.querySelector(".lane-row-title-label");
      const modelEl = row.querySelector(".lane-model");
      const activeAccountEl = row.querySelector(".lane-active-account-value");
      const portEl = row.querySelector(".lane-port");
      const chatKeyEl = row.querySelector(".lane-chat-key");
      const quickCookieButton = row.querySelector(".lane-action-cookie-quick");

      if (titleEl) titleEl.textContent = this.getLaneTitle(laneId, lane.label, flowNum, lang);
      if (modelEl) {
        modelEl.textContent = lane.model || "";
        modelEl.hidden = !lane.model;
      }
      const activeAccount = lane.active_account || {};
      if (activeAccountEl) {
        const label = activeAccount.label || (lang === "vi" ? "Chính" : "Primary");
        const accountIndex = (activeAccount.index !== undefined ? activeAccount.index : 0) + 1;
        const accountTotal = activeAccount.total || 1;
        activeAccountEl.textContent = `${label} (${accountIndex}/${accountTotal})`;
      }
      if (portEl) portEl.textContent = `Port: ${this.getLanePort(lane)}`;
      if (chatKeyEl) {
        chatKeyEl.hidden = !lane.chat_key;
        const code = chatKeyEl.querySelector("code");
        if (code) code.textContent = lane.chat_key || "";
      }
      if (quickCookieButton) quickCookieButton.hidden = !(ctrl === "on" && !authOk);
      row.querySelectorAll(".lane-action-cookie, .lane-action-reauth, .lane-action-reset").forEach((button) => {
        button.disabled = !isReachable;
      });
      this.updateToggleButton(row, laneId, lane, switchState, lang);

      // Cập nhật dòng thống kê
      const statsLine = row.querySelector(".lane-stats-line");
      if (statsLine) {
        let statsStr = t("statsLabel", lang, { requests: reqCount });
        if (errCount > 0) {
          statsStr = t("statsWithErrors", lang, { requests: reqCount, errors: errCount });
        }
        statsLine.textContent = statsStr;
      }
    });
  }

  setupToggleButton(btn) {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const laneId = btn.getAttribute("data-lane-id");
      const action = btn.getAttribute("data-action");
      const { lang, lanes } = store.getState();
      const lane = (lanes || []).find((l) => l.id === laneId) || {};

      if (action === "enable") {
        btn.disabled = true;
        try {
          await api.enableLane(laneId);
        } catch (err) {
          store.notifyToast(err.message, "error");
        } finally {
          btn.disabled = false;
        }
      } else if (action === "disable") {
        const inflight = lane.inflight || 0;
        const label = lane.label || laneId;
        const msg = t("confirmDisableLane", lang, {
          id: laneId,
          label,
          n: inflight,
        });

        if (!window.confirm(msg)) return;

        btn.disabled = true;
        try {
          await api.disableLane(laneId);
        } catch (err) {
          store.notifyToast(err.message, "error");
        } finally {
          btn.disabled = false;
        }
      }
    });
  }

  setupEvents() {
    // Click vào hàng để chọn luồng
    this.querySelectorAll(".lane-flow-row").forEach((row) => {
      row.addEventListener("click", (e) => {
        // Bỏ qua nếu bấm vào nút công tắc, nút thao tác, hoặc khối details
        if (
          e.target.closest(".lane-toggle-btn") ||
          e.target.closest("button") ||
          e.target.closest("details")
        ) {
          return;
        }

        if (row.classList.contains("unreachable")) return;

        const laneId = row.getAttribute("data-row-lane");
        if (laneId && laneId !== store.getState().selectedLane) {
          store.setState({ selectedLane: laneId });
        }
      });
    });

    // Công tắc BẬT / TẮT
    this.querySelectorAll(".lane-toggle-btn").forEach((btn) => this.setupToggleButton(btn));

    // Nút [Nạp cookie] nhanh
    this.querySelectorAll(".lane-action-cookie").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const laneId = btn.getAttribute("data-lane-id");
        if (laneId) {
          store.setState({ selectedLane: laneId });
          const cm = document.querySelector("bridge-cookie-manager");
          if (cm) cm.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
    });

    // Nút [🔄 Thử đăng nhập lại]
    this.querySelectorAll(".lane-action-reauth").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const laneId = btn.getAttribute("data-lane-id");
        if (!laneId) return;

        btn.disabled = true;
        store.notifyToast(t("verifyingCookies", store.getState().lang), "info");
        try {
          const res = await api.reauthLane(laneId);
          if (res && res.auth_ok) {
            store.notifyToast(t("cookiesUpdatedSuccess", store.getState().lang), "success");
          } else {
            store.notifyToast("Reauth: unauthenticated", "warning");
          }
        } catch (err) {
          store.notifyToast(err.message, "error");
        } finally {
          btn.disabled = false;
        }
      });
    });

    // Nút [⚠️ Reset]
    this.querySelectorAll(".lane-action-reset").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const laneId = btn.getAttribute("data-lane-id");
        if (!laneId) return;

        const msg = t("resetSessionConfirm", store.getState().lang, { id: laneId });
        if (!window.confirm(msg)) return;

        btn.disabled = true;
        try {
          const res = await api.resetLaneSession(laneId);
          if (res && res.error) {
            store.notifyToast(res.error.message || "Reset failed", "error");
          } else {
            store.notifyToast(t("sessionResetSuccess", store.getState().lang), "success");
          }
        } catch (err) {
          store.notifyToast(err.message, "error");
        } finally {
          btn.disabled = false;
        }
      });
    });
  }
}

if (!customElements.get("bridge-lanes")) {
  customElements.define("bridge-lanes", BridgeLanes);
}
