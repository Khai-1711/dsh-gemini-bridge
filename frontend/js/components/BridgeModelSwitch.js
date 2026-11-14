import { api } from "../api.js";
import { store } from "../state.js";
import { t } from "../i18n.js";

/**
 * <bridge-model-switch> Component
 * Handles dynamic Gemini Model switching + i18n
 */
export class BridgeModelSwitch extends HTMLElement {
  connectedCallback() {
    this.render();
    this.setupEvents();

    this.unsubscribe = store.subscribe((state, prevState) => {
      if (prevState && state.lang !== prevState.lang) {
        this.render();
        this.setupEvents();
      }

      if (!prevState || state.availableModels.length !== prevState.availableModels.length) {
        this.updateModelDropdown(state);
      }
    });
  }

  disconnectedCallback() {
    if (this.unsubscribe) this.unsubscribe();
  }

  render() {
    const { lang } = store.getState();

    this.innerHTML = `
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>
            ${t("switchModelTitle", lang)}
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
          <button class="btn btn-primary" id="ms-apply-btn">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
            ${t("applyModelBtn", lang)}
          </button>
        </div>
      </div>
    `;

    this.selectEl = this.querySelector("#ms-select");
    this.countEl = this.querySelector("#ms-count");
    this.applyBtn = this.querySelector("#ms-apply-btn");

    this.updateModelDropdown(store.getState());
  }

  updateModelDropdown(state) {
    if (!this.selectEl) return;
    const models = state.availableModels || [];
    const details = new Map((state.modelDetails || []).map((d) => [d.name, d]));
    this.countEl.innerText = `${models.length} models`;
    const currentVal = this.selectEl.value;
    this.selectEl.innerHTML = "";
    models.forEach((m) => {
      const info = details.get(m) || {};
      const label =
        info.display_name && info.display_name !== m
          ? `${info.display_name} (${m})`
          : m;
      const opt = document.createElement("option");
      opt.value = m;
      opt.innerText = label;
      if (info.description) opt.title = info.description;
      if (m === state.model) opt.selected = true;
      this.selectEl.appendChild(opt);
    });
    if (!this.selectEl.value && currentVal) this.selectEl.value = currentVal;
  }

  setupEvents() {
    this.applyBtn?.addEventListener("click", () => this.handleSwitch());
  }

  async handleSwitch() {
    const { lang } = store.getState();
    const model = this.selectEl.value;
    if (!model) {
      store.notifyToast(t("selectValidModel", lang), "error");
      return;
    }

    this.applyBtn.disabled = true;
    this.applyBtn.innerText = t("switching", lang);

    try {
      const data = await api.switchModel(model, true);
      store.notifyToast(`${t("modelSwitched", lang)} ${data.active_model}`, "success");
      this.dispatchEvent(new CustomEvent("refresh-request", { bubbles: true }));
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
    } finally {
      this.applyBtn.disabled = false;
      this.applyBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg> ${t("applyModelBtn", lang)}`;
    }
  }
}

customElements.define("bridge-model-switch", BridgeModelSwitch);
