import { api } from "../api.js";
import { store } from "../state.js";
import { t } from "../i18n.js";

/**
 * <bridge-cookie-manager> Component
 * Handles Cookie Hot-Reloading & Password Visibility Masking + i18n
 */
export class BridgeCookieManager extends HTMLElement {
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
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>
            ${t("cookieManagerTitle", lang)}
          </div>
          <span class="badge-tag badge-success">${t("hotReloadTag", lang)}</span>
        </div>
        <div class="panel-body">
          <div class="form-field">
            <label class="field-label" for="cm-psid">
              <span>${t("psidLabel", lang)}</span>
              <span class="field-hint">${t("psidHint", lang)}</span>
            </label>
            <div class="input-container">
              <input type="password" class="input-text code-font" id="cm-psid" placeholder="g.a000...">
              <button type="button" class="input-toggle-btn" data-target="cm-psid" title="${t("toggleVisibility", lang)}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
              </button>
            </div>
          </div>

          <div class="form-field">
            <label class="field-label" for="cm-psidts">
              <span>${t("psidtsLabel", lang)}</span>
              <span class="field-hint">${t("psidtsHint", lang)}</span>
            </label>
            <div class="input-container">
              <input type="password" class="input-text code-font" id="cm-psidts" placeholder="sidts-...">
              <button type="button" class="input-toggle-btn" data-target="cm-psidts" title="${t("toggleVisibility", lang)}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
              </button>
            </div>
          </div>

          <button class="btn btn-primary" id="cm-save-btn">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
            ${t("saveCookiesBtn", lang)}
          </button>
        </div>
      </div>
    `;

    this.psidInput = this.querySelector("#cm-psid");
    this.psidtsInput = this.querySelector("#cm-psidts");
    this.saveBtn = this.querySelector("#cm-save-btn");
  }

  setupEvents() {
    this.querySelectorAll(".input-toggle-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const targetId = btn.getAttribute("data-target");
        const input = this.querySelector(`#${targetId}`);
        if (input.type === "password") {
          input.type = "text";
          btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
        } else {
          input.type = "password";
          btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
        }
      });
    });

    this.saveBtn?.addEventListener("click", () => this.handleSave());
  }

  async handleSave() {
    const { lang } = store.getState();
    const psid = this.psidInput.value.trim();
    const psidts = this.psidtsInput.value.trim();

    if (!psid) {
      store.notifyToast(t("enterPsidRequired", lang), "error");
      this.psidInput.focus();
      return;
    }

    this.saveBtn.disabled = true;
    this.saveBtn.innerText = t("verifyingCookies", lang);

    try {
      await api.updateCookies(psid, psidts);
      store.notifyToast(t("cookiesUpdatedSuccess", lang), "success");
      this.psidInput.value = "";
      this.psidtsInput.value = "";
      this.dispatchEvent(new CustomEvent("refresh-request", { bubbles: true }));
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
    } finally {
      this.saveBtn.disabled = false;
      this.saveBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg> ${t("saveCookiesBtn", lang)}`;
    }
  }
}

customElements.define("bridge-cookie-manager", BridgeCookieManager);
