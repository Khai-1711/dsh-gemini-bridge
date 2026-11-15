import { store } from "../state.js";
import { t } from "../i18n.js";
import { getStoredApiKey, clearStoredApiKey } from "../api.js";

/**
 * <bridge-navbar> Component
 * Top Bar: Brand, Language (EN/VI), Theme (Dark/Light), Server Health Badge, Refresh, Forget Key.
 * (Nút BẬT/TẮT chung đã chuyển sang BridgeLanes theo từng luồng).
 */
export class BridgeNavbar extends HTMLElement {
  connectedCallback() {
    this.render();
    this.setupEvents();

    this.unsubscribe = store.subscribe((state, prevState) => {
      if (
        !prevState ||
        state.lang !== prevState.lang ||
        state.theme !== prevState.theme ||
        (state.latency >= 0) !== (prevState.latency >= 0)
      ) {
        this.render();
        this.setupEvents();
      } else {
        const latencyEl = this.querySelector("#nav-latency");
        if (latencyEl && state.latency >= 0) {
          latencyEl.innerText = `${state.latency} ms`;
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
    const isOnline = latency >= 0;

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
          <!-- Server Health Badge: ● Server OK / ✕ Mất liên lạc với server -->
          ${
            isOnline
              ? `
            <div class="server-status-pill ok" style="display:flex; align-items:center; gap:6px; padding:4px 10px; background:rgba(16,185,129,0.12); border:1px solid rgba(16,185,129,0.3); border-radius:var(--radius-md); font-size:12px; font-weight:600; color:#10b981;">
              <span class="pulse-dot" style="background:#10b981;"></span>
              <span>${t("serverOk", lang)}</span>
              <span id="nav-latency" style="font-size:11px; opacity:0.8; font-family:var(--font-mono);">${latency > 0 ? latency + " ms" : ""}</span>
            </div>
          `
              : `
            <div class="server-status-pill error" style="display:flex; align-items:center; gap:6px; padding:4px 10px; background:rgba(239,68,68,0.12); border:1px solid rgba(239,68,68,0.3); border-radius:var(--radius-md); font-size:12px; font-weight:600; color:#ef4444;">
              <span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#ef4444;"></span>
              <span>${t("serverLost", lang)}</span>
            </div>
          `
          }

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

          <!-- Manual Refresh Button -->
          <button class="btn btn-secondary btn-icon" id="nav-refresh-btn" title="${t("refreshData", lang)} (Ctrl+R)">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
            </svg>
          </button>

          ${
            getStoredApiKey()
              ? `
          <button class="btn btn-secondary" id="nav-forget-key-btn" title="${t("forgetKey", lang)}" style="font-size: 11px; padding: 4px 8px; color: var(--status-warning, #f59e0b);">
            🔑 ${t("forgetKey", lang)}
          </button>`
              : ""
          }
        </div>
      </header>
    `;
  }

  setupEvents() {
    this.querySelector("#nav-forget-key-btn")?.addEventListener("click", () => {
      clearStoredApiKey();
      store.notifyToast(t("accessKeyCleared", store.getState().lang), "info");
      setTimeout(() => window.location.reload(), 400);
    });

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
      window.dispatchEvent(new CustomEvent("refresh-request"));
      store.notifyToast(t("refreshing", store.getState().lang), "info", 1500);
    });
  }
}

customElements.define("bridge-navbar", BridgeNavbar);
