import { api } from "../api.js";
import { store } from "../state.js";
import { t } from "../i18n.js";

/**
 * <bridge-quick-actions> Component
 * Handles Session Reset & Cache Invalidation + i18n
 */
export class BridgeQuickActions extends HTMLElement {
  connectedCallback() {
    this.render();
    this.setupEvents();

    this.unsubscribe = store.subscribe((state, prevState) => {
      if (prevState && state.lang !== prevState.lang) {
        this.render();
        this.setupEvents();
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
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
            ${t("quickActionsTitle", lang)}
          </div>
        </div>
        <div class="panel-body">
          <div class="btn-group">
            <button class="btn btn-danger" id="qa-reset-btn" style="flex: 1;" title="${t("resetSessionTitle", lang)}">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
              ${t("resetSessionBtn", lang)}
            </button>
            <button class="btn btn-secondary" id="qa-clear-cache-btn" style="flex: 1;" title="${t("clearCacheTitle", lang)}">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
              ${t("clearCacheBtn", lang)}
            </button>
          </div>
        </div>
      </div>
    `;
  }

  setupEvents() {
    this.querySelector("#qa-reset-btn")?.addEventListener("click", () => this.handleReset());
    this.querySelector("#qa-clear-cache-btn")?.addEventListener("click", () => this.handleClearCache());
  }

  async handleReset() {
    const { lang } = store.getState();
    if (!confirm(t("confirmResetSession", lang))) {
      return;
    }

    try {
      await api.resetSession();
      store.notifyToast(t("sessionResetSuccess", lang), "success");
      this.dispatchEvent(new CustomEvent("refresh-request", { bubbles: true }));
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
    }
  }

  async handleClearCache() {
    const { lang } = store.getState();
    try {
      const data = await api.clearCache();
      store.notifyToast(t("cacheClearedSuccess", lang, { count: data.cleared_entries }), "success");
      this.dispatchEvent(new CustomEvent("refresh-request", { bubbles: true }));
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
    }
  }
}

customElements.define("bridge-quick-actions", BridgeQuickActions);
