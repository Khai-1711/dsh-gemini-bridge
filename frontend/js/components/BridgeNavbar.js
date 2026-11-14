import { store } from "../state.js";
import { t } from "../i18n.js";

/**
 * <bridge-navbar> Component
 * Top Bar, Language Toggle (EN/VI), Theme Toggle (Dark/Light), Ping Latency
 */
export class BridgeNavbar extends HTMLElement {
  connectedCallback() {
    this.render();
    this.setupEvents();

    this.unsubscribe = store.subscribe((state, prevState) => {
      // Re-render navbar if lang or theme changed
      if (!prevState || state.lang !== prevState.lang || state.theme !== prevState.theme) {
        this.render();
        this.setupEvents();
      }

      const latencyEl = this.querySelector("#nav-latency");
      if (latencyEl) {
        if (state.latency > 0) {
          latencyEl.innerText = `${state.latency} ms`;
          latencyEl.style.color = state.latency < 100 ? "var(--status-success)" : "var(--status-warning)";
        } else {
          latencyEl.innerText = t("offline", state.lang);
          latencyEl.style.color = "var(--status-danger)";
        }
      }
    });
  }

  disconnectedCallback() {
    if (this.unsubscribe) this.unsubscribe();
  }

  render() {
    const { lang, theme, latency } = store.getState();
    const isDark = theme === "dark";

    this.innerHTML = `
      <header class="top-nav">
        <div class="brand">
          <div class="brand-icon">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
            </svg>
          </div>
          <div class="brand-text">
            <h1>
              DSH Gemini Bridge
              <span class="version-pill">${t("versionTag", lang)}</span>
            </h1>
            <p>${t("brandSubtitle", lang)}</p>
          </div>
        </div>

        <div class="nav-actions">
          <!-- Language Switcher Pill -->
          <div class="toggle-pill-group">
            <button class="toggle-pill-btn ${lang === "vi" ? "active" : ""}" id="btn-lang-vi" title="Tiếng Việt">🇻🇳 VI</button>
            <button class="toggle-pill-btn ${lang === "en" ? "active" : ""}" id="btn-lang-en" title="English">🇺🇸 EN</button>
          </div>

          <!-- Theme Toggle Button -->
          <button class="btn btn-secondary btn-icon" id="btn-theme-toggle" title="${isDark ? t("themeLight", lang) : t("themeDark", lang)}">
            ${
              isDark
                ? `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>`
                : `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`
            }
          </button>

          <!-- Ping Latency Indicator -->
          <div class="latency-indicator" title="API Ping Latency">
            <span class="pulse-dot"></span>
            <span id="nav-latency">${latency > 0 ? latency + " ms" : t("offline", lang)}</span>
          </div>

          <!-- Manual Refresh Button -->
          <button class="btn btn-secondary btn-icon" id="nav-refresh-btn" title="${t("refreshData", lang)} (Ctrl+R)">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
            </svg>
          </button>
        </div>
      </header>
    `;
  }

  setupEvents() {
    this.querySelector("#btn-lang-vi")?.addEventListener("click", () => {
      store.setState({ lang: "vi" });
      store.notifyToast("Đã đổi ngôn ngữ sang Tiếng Việt", "info");
    });

    this.querySelector("#btn-lang-en")?.addEventListener("click", () => {
      store.setState({ lang: "en" });
      store.notifyToast("Language switched to English", "info");
    });

    this.querySelector("#btn-theme-toggle")?.addEventListener("click", () => {
      const currentTheme = store.getState().theme;
      const nextTheme = currentTheme === "dark" ? "light" : "dark";
      store.setState({ theme: nextTheme });
      store.notifyToast(nextTheme === "dark" ? "Dark Mode 🌙" : "Light Mode ☀️", "info", 1500);
    });

    this.querySelector("#nav-refresh-btn")?.addEventListener("click", () => {
      this.dispatchEvent(new CustomEvent("refresh-request", { bubbles: true }));
      store.notifyToast(t("refreshing", store.getState().lang), "info", 1500);
    });
  }
}

customElements.define("bridge-navbar", BridgeNavbar);
