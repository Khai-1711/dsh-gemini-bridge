// Quy tắc: Poll không được render lại ô nhập/chọn
import { api } from "../api.js";
import { store } from "../state.js";
import { t } from "../i18n.js";
import { displayName } from "../lane-name.js";

const getApi = () => (typeof api !== "undefined" && api ? api : (window.api || {}));

/**
 * <bridge-model-switch> Component (T40E)
 * Dynamic Gemini Model switching routed to selectedLane + i18n
 * QUY TẮC: Poll không được render lại ô nhập/chọn!
 */
export class BridgeModelSwitch extends HTMLElement {
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
        // Cập nhật tại chỗ
        this.updateDynamicValues(state, prevState);
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

    const flowNum = laneId === "acc1" ? 1 : (laneId === "acc2" ? 2 : 1);
    const showLaneTag = Array.isArray(lanes) && lanes.length >= 2;
    const laneTagHtml = showLaneTag
      ? `<span class="badge-tag badge-cyan" style="text-transform:none; font-weight:600; font-size:11px; margin-left:6px;">${displayName(laneId, lane.label, flowNum, lang)}</span>`
      : "";

    const isReady = lane.reachable !== false && lane.control === "on" && Boolean(lane.auth_ok);

    this.innerHTML = `
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title" style="display:flex; align-items:center;">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>
            <span>${t("switchModelTitle", lang)}</span>
            <span class="ms-lane-tag-container">${laneTagHtml}</span>
          </div>
          <span class="badge-tag badge-cyan">${t("runtimeSwitchTag", lang)}</span>
        </div>
        <div class="panel-body">
          <div class="form-field">
            <label class="field-label" for="ms-select">
              <span>${t("availableModelsLabel", lang)}</span>
              <span class="field-hint" id="ms-count">0 models</span>
            </label>
            <select class="select-control" id="ms-select"></select>
          </div>

          <div id="ms-warning-box" style="font-size: 11px; color: var(--status-warning, #f59e0b); margin-bottom: 12px; display: ${!isReady ? 'flex' : 'none'}; align-items: center; gap: 4px;">
            <span>⚠️</span>
            <span>${
              lang === "vi"
                ? "Chỉ có 1 model: có thể đang ở guest hoặc chưa đăng nhập (hoặc luồng đang TẮT)."
                : "Only 1 model: may be guest or unauthenticated (or lane is OFF)."
            }</span>
          </div>

          <button class="btn btn-primary" id="ms-apply-btn" ${!isReady ? "disabled" : ""}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
            ${t("applyModelBtn", lang)}
          </button>
        </div>
      </div>
    `;

    this.selectEl = this.querySelector("#ms-select");
    this.countEl = this.querySelector("#ms-count");
    this.applyBtn = this.querySelector("#ms-apply-btn");
    this.warningBox = this.querySelector("#ms-warning-box");

    this.updateModelDropdown(state, true);
  }

  updateDynamicValues(state, prevState) {
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";
    const flowNum = laneId === "acc1" ? 1 : (laneId === "acc2" ? 2 : 1);
    const lanes = state.lanes || [];
    const showLaneTag = Array.isArray(lanes) && lanes.length >= 2;

    const tagContainer = this.querySelector(".ms-lane-tag-container");
    if (tagContainer) {
      tagContainer.innerHTML = showLaneTag
        ? `<span class="badge-tag badge-cyan" style="text-transform:none; font-weight:600; font-size:11px; margin-left:6px;">${displayName(laneId, lane.label, flowNum, state.lang)}</span>`
        : "";
    }

    const isReady = lane.reachable !== false && lane.control === "on" && Boolean(lane.auth_ok);
    if (this.warningBox) {
      this.warningBox.style.display = !isReady ? "flex" : "none";
    }
    if (this.applyBtn && !this.applyBtn.dataset.busy) {
      this.applyBtn.disabled = !isReady;
    }

    // Chỉ cập nhật dropdown options nếu danh sách model thật sự đổi
    const modelsChanged =
      !prevState ||
      JSON.stringify(state.availableModels) !== JSON.stringify(prevState.availableModels) ||
      JSON.stringify(state.modelDetails) !== JSON.stringify(prevState.modelDetails);

    if (modelsChanged) {
      this.updateModelDropdown(state, false);
    }
  }

  updateModelDropdown(state, forceSetCurrent = false) {
    if (!this.selectEl) return;
    const lane = this.getCurrentLane(state);
    const models = state.availableModels || [];
    const details = new Map((state.modelDetails || []).map((d) => [d.id || d.name, d]));
    const currentModel = lane.model || state.model || "unspecified";

    if (this.countEl) {
      this.countEl.innerText = `${models.length} models`;
    }

    // Lưu lại lựa chọn hiện tại trước khi cập nhật
    const previousVal = this.selectEl.value || currentModel;

    this.selectEl.innerHTML = "";

    models.forEach((m) => {
      const info = details.get(m) || {};
      let label = "";
      if (m === "unspecified") {
        label = state.lang === "vi" ? "Mặc định (unspecified)" : "Default (unspecified)";
      } else {
        label = info.label || info.display_name || m;
      }

      const opt = document.createElement("option");
      opt.value = m;
      opt.innerText = label;
      if (info.description) opt.title = info.description;
      this.selectEl.appendChild(opt);
    });

    // Giữ nguyên giá trị đã chọn hoặc chọn currentModel nếu force
    if (forceSetCurrent && currentModel) {
      this.selectEl.value = currentModel;
    } else if (previousVal && models.includes(previousVal)) {
      this.selectEl.value = previousVal;
    } else if (currentModel) {
      this.selectEl.value = currentModel;
    }
  }

  setupEvents() {
    this.applyBtn?.addEventListener("click", () => this.handleSwitch());
  }

  async handleSwitch() {
    const state = store.getState();
    const { lang } = state;
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";
    const model = this.selectEl.value;

    if (lane.reachable === false) {
      store.notifyToast(t("laneUnreachableAction", lang, { id: laneId }), "warning");
      return;
    }

    if (!model) {
      store.notifyToast(t("selectValidModel", lang), "error");
      return;
    }

    this.applyBtn.disabled = true;
    this.applyBtn.dataset.busy = "true";
    this.applyBtn.innerText = t("switching", lang);

    try {
      await getApi().switchLaneModel(laneId, model, true);
      store.notifyToast(`${t("modelSwitched", lang)} ${model} (${laneId})`, "success");
      window.dispatchEvent(new CustomEvent("refresh-request"));
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
    } finally {
      this.applyBtn.disabled = false;
      delete this.applyBtn.dataset.busy;
      this.applyBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg> ${t("applyModelBtn", lang)}`;
    }
  }
}

if (!customElements.get("bridge-model-switch")) {
  customElements.define("bridge-model-switch", BridgeModelSwitch);
}
