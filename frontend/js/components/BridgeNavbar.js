import { store } from "../state.js";
import { t } from "../i18n.js";
import { api, getStoredApiKey, clearStoredApiKey } from "../api.js";

const getApi = () => (typeof api !== "undefined" && api ? api : (window.api || {}));

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
        state.dashboardApiStatus !== prevState.dashboardApiStatus ||
        (state.latency === null) !== (prevState.latency === null) ||
        (state.latency >= 0) !== (prevState.latency >= 0) ||
        state.selectedLane !== prevState.selectedLane ||
        JSON.stringify(state.lanes) !== JSON.stringify(prevState.lanes)
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
    const { lang, theme, latency, dashboardApiStatus, lanes, selectedLane, isGateway } = store.getState();
    const isDark = theme === "dark";
    const isOnline = latency >= 0;
    const isChecking = latency === null;
    const isGw = Boolean(isGateway || window.location.port === "10000");
    const serverLabel = isGw
      ? (lang === "vi" ? "Cổng Frontend" : "Frontend Gateway")
      : t("serverOk", lang);

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
            isChecking
              ? `
            <div class="server-status-pill" style="display:flex; align-items:center; gap:6px; padding:4px 10px; background:var(--bg-surface-hover); border:1px solid var(--border-subtle); border-radius:var(--radius-md); font-size:12px; font-weight:600; color:var(--text-secondary);">
              <span>${t("serverChecking", lang)}</span>
            </div>
          `
              : isOnline
              ? `
            <div class="server-status-pill ok" style="display:flex; align-items:center; gap:6px; padding:4px 10px; background:rgba(16,185,129,0.12); border:1px solid rgba(16,185,129,0.3); border-radius:var(--radius-md); font-size:12px; font-weight:600; color:#10b981;">
              <span class="pulse-dot" style="background:#10b981;"></span>
              <span>${serverLabel}</span>
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

          ${
            isOnline && dashboardApiStatus
              ? `
            <div class="server-status-pill error" title="${t(dashboardApiStatus === "auth_error" ? "dashboardApiAuthError" : "dashboardApiDataError", lang)}" style="display:flex; align-items:center; gap:6px; padding:4px 10px; background:rgba(245,158,11,0.12); border:1px solid rgba(245,158,11,0.3); border-radius:var(--radius-md); font-size:12px; font-weight:600; color:#f59e0b;">
              <span>${t(dashboardApiStatus === "auth_error" ? "dashboardApiAuthError" : "dashboardApiDataError", lang)}</span>
            </div>
          `
              : ""
          }

          <!-- Lane Switcher Pills với công tắc BẬT / TẮT trực tiếp -->
          <div class="toggle-pill-group lane-pill-group" style="margin-right:2px; display:inline-flex; align-items:center; gap:4px; padding:2px 4px;">
            ${(lanes && lanes.length > 0 ? lanes : [{ id: "default", label: "Default", control: "off" }])
              .map((l, i) => {
                const flowId = l.id || `acc${i + 1}`;
                const flowNum = flowId === "acc1" ? 1 : (flowId === "acc2" ? 2 : i + 1);
                const isSel = (selectedLane || lanes[0]?.id || "default") === flowId;
                const ctrl = l.control || "off";
                const isOn = ctrl === "on";
                const isReachable = l.reachable !== false;
                const isStarting = ctrl === "starting";
                const isStopping = ctrl === "stopping";
                const dotColor = !isReachable ? "#ef4444" : (!isOn ? "var(--text-muted)" : (l.auth_ok ? "#10b981" : "#f59e0b"));
                const flowTitle = l.label && l.label !== flowId ? l.label : (lang === "vi" ? `Luồng ${flowNum}` : `Flow ${flowNum}`);
                const btnText = isStarting ? "..." : (isStopping ? "..." : (isOn ? (lang === "vi" ? "BẬT" : "ON") : (lang === "vi" ? "TẮT" : "OFF")));
                return `
                  <div class="toggle-pill-btn ${isSel ? "active" : ""}" style="display:inline-flex; align-items:center; gap:6px; padding:3px 8px;">
                    <span class="lane-select-trigger" data-lane-id="${flowId}" title="${flowTitle} (${flowId}) · Port: ${l.port || (flowId === "acc2" ? 10002 : (flowId === "acc1" ? 10001 : 10000))}" style="display:inline-flex; align-items:center; gap:6px; cursor:pointer;">
                      <span style="width:7px; height:7px; border-radius:50%; background:${dotColor};"></span>
                      <span>${flowTitle}</span>
                    </span>
                    <button type="button" class="lane-power-trigger" data-lane-id="${flowId}" data-action="${isOn ? "disable" : "enable"}" ${isStarting || isStopping ? "disabled" : ""} style="padding:1px 6px; border-radius:8px; font-size:10px; font-weight:700; cursor:pointer; border:1px solid ${isOn ? "rgba(16,185,129,0.5)" : "var(--border-medium)"}; background:${isOn ? "rgba(16,185,129,0.2)" : "var(--bg-secondary)"}; color:${isOn ? "#10b981" : "var(--text-muted)"};">
                      ${btnText}
                    </button>
                  </div>
                `;
              })
              .join("")}
          </div>

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
          <button class="btn btn-secondary btn-icon" id="nav-refresh-btn" title="${t("refreshData", lang)}">
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
    this.querySelectorAll(".lane-select-trigger").forEach((el) => {
      el.addEventListener("click", () => {
        const laneId = el.getAttribute("data-lane-id");
        if (laneId && laneId !== store.getState().selectedLane) {
          store.setState({ selectedLane: laneId });
          window.dispatchEvent(new CustomEvent("refresh-request"));
        }
      });
    });

    this.querySelectorAll(".lane-power-trigger").forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const laneId = btn.getAttribute("data-lane-id");
        const action = btn.getAttribute("data-action");
        const { lang, lanes } = store.getState();
        const lane = (lanes || []).find((l) => l.id === laneId) || {};

        if (action === "enable") {
          btn.disabled = true;
          store.notifyToast(lang === "vi" ? `Đang bật luồng ${laneId}...` : `Enabling lane ${laneId}...`, "info");
          try {
            const res = await getApi().enableLane(laneId);
            if (res && res.error) {
              store.notifyToast(res.error.message || "Failed to enable lane", "error");
            } else {
              store.notifyToast(lang === "vi" ? `Luồng ${laneId} đã BẬT!` : `Lane ${laneId} enabled!`, "success");
            }
          } catch (err) {
            store.notifyToast(err.message, "error");
          } finally {
            btn.disabled = false;
            window.dispatchEvent(new CustomEvent("refresh-request"));
          }
        } else if (action === "disable") {
          const inflight = lane.inflight || 0;
          const label = lane.label || laneId;
          const msg = t("confirmDisableLane", lang, { id: laneId, label, n: inflight });
          if (!window.confirm(msg)) return;

          btn.disabled = true;
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
            btn.disabled = false;
            window.dispatchEvent(new CustomEvent("refresh-request"));
          }
        }
      });
    });

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
