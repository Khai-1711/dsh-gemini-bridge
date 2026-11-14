import { store } from "../state.js";
import { t } from "../i18n.js";

/**
 * <bridge-metrics> Component
 * Displays the 4 real-time telemetry cards with i18n support
 */
export class BridgeMetrics extends HTMLElement {
  connectedCallback() {
    this.render();

    this.unsubscribe = store.subscribe((state, prevState) => {
      // Re-render if language changed
      if (prevState && state.lang !== prevState.lang) {
        this.render();
      } else {
        this.updateValues(state);
      }
    });
  }

  disconnectedCallback() {
    if (this.unsubscribe) this.unsubscribe();
  }

  render() {
    const { lang } = store.getState();

    this.innerHTML = `
      <section class="metrics-grid">
        <!-- Card 1: Active Model -->
        <div class="metric-card" style="--card-accent: var(--accent-cyan);">
          <div class="metric-header">
            <span class="metric-label">${t("activeModel", lang)}</span>
            <span class="metric-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/></svg>
            </span>
          </div>
          <div class="metric-value" id="m-model">--</div>
          <div class="metric-sub">
            <span class="badge-tag badge-cyan">${t("sessionReady", lang)}</span>
            <span id="m-turns">${t("turn", lang)}: 0</span>
          </div>
        </div>

        <!-- Card 2: Throughput & Health -->
        <div class="metric-card" style="--card-accent: var(--status-success);">
          <div class="metric-header">
            <span class="metric-label">${t("requestsAndHealth", lang)}</span>
            <span class="metric-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
            </span>
          </div>
          <div class="metric-value">
            <span id="m-requests">0</span>
            <span style="font-size: 13px; font-weight: normal; color: var(--text-muted);">${t("reqs", lang)}</span>
          </div>
          <div class="metric-sub">
            <span class="badge-tag badge-success" id="m-err-rate">0.0% ${t("errorRate", lang)}</span>
            <span id="m-err-count">0 ${t("failures", lang)}</span>
          </div>
        </div>

        <!-- Card 3: Uptime -->
        <div class="metric-card" style="--card-accent: var(--accent-indigo);">
          <div class="metric-header">
            <span class="metric-label">${t("uptimeAndSession", lang)}</span>
            <span class="metric-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            </span>
          </div>
          <div class="metric-value" id="m-uptime">0h 0m 0s</div>
          <div class="metric-sub">
            <span class="badge-tag badge-success">${t("statefulDelta", lang)}</span>
            <span id="m-backup">${t("primaryCookie", lang)}</span>
          </div>
        </div>

        <!-- Card 4: Response Cache -->
        <div class="metric-card" style="--card-accent: var(--status-warning);">
          <div class="metric-header">
            <span class="metric-label">${t("promptCache", lang)}</span>
            <span class="metric-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
            </span>
          </div>
          <div class="metric-value">
            <span id="m-cache">0</span>
            <span style="font-size: 13px; font-weight: normal; color: var(--text-muted);">${t("cached", lang)}</span>
          </div>
          <div class="metric-sub">
            <span class="badge-tag badge-warning">${t("ttlNote", lang)}</span>
            <span>${t("latencySaved", lang)}</span>
          </div>
        </div>
      </section>
    `;

    this.updateValues(store.getState());
  }

  updateValues(state) {
    const { lang } = state;
    const mModel = this.querySelector("#m-model");
    const mTurns = this.querySelector("#m-turns");
    const mRequests = this.querySelector("#m-requests");
    const mErrRate = this.querySelector("#m-err-rate");
    const mErrCount = this.querySelector("#m-err-count");
    const mUptime = this.querySelector("#m-uptime");
    const mBackup = this.querySelector("#m-backup");
    const mCache = this.querySelector("#m-cache");

    if (mModel) mModel.innerText = state.model;
    if (mTurns) mTurns.innerText = `${t("turn", lang)}: ${state.turnCount}`;
    if (mRequests) mRequests.innerText = state.totalRequests;
    if (mErrRate) {
      mErrRate.innerText = `${state.errorRate} ${t("errorRate", lang)}`;
      mErrRate.className = `badge-tag ${parseFloat(state.errorRate) > 5 ? "badge-warning" : "badge-success"}`;
    }
    if (mErrCount) mErrCount.innerText = `${state.totalErrors} ${t("failures", lang)}`;
    if (mUptime) mUptime.innerText = state.uptime;
    if (mCache) mCache.innerText = state.cacheEntries;

    if (mBackup) {
      mBackup.innerText = state.usingBackup ? t("backupCookie", lang) : t("primaryCookie", lang);
      mBackup.style.color = state.usingBackup ? "var(--status-warning)" : "var(--text-secondary)";
    }
  }
}

customElements.define("bridge-metrics", BridgeMetrics);
