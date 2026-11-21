import { store } from "../state.js";
import { t } from "../i18n.js";
import { displayName } from "../lane-name.js";

/**
 * <bridge-metrics> Component (T40D)
 * Displays the 4 real-time telemetry cards routed to selectedLane.
 * Không in lặp tên hay default trong tiêu đề viết hoa.
 */
export class BridgeMetrics extends HTMLElement {
  connectedCallback() {
    this.render();

    this.unsubscribe = store.subscribe((state, prevState) => {
      if (!prevState) return;

      const langChanged = state.lang !== prevState.lang;
      const selectedLaneChanged = state.selectedLane !== prevState.selectedLane;

      if (langChanged || selectedLaneChanged) {
        this.render();
      } else {
        this.updateValues(state);
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
      ? `<span class="badge-tag badge-cyan" style="text-transform:none; font-weight:600; font-size:10px; margin-left:6px;">${displayName(laneId, lane.label, flowNum, lang)}</span>`
      : "";

    const cacheOn = Boolean(lane.cache_enabled);

    this.innerHTML = `
      <section class="metrics-grid">
        <!-- Card 1: Active Model -->
        <div class="metric-card" style="--card-accent: var(--accent-cyan);">
          <div class="metric-header">
            <span class="metric-label">${t("activeModel", lang)}</span>
            ${laneTagHtml}
            <span class="metric-icon" style="margin-left: auto;">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/></svg>
            </span>
          </div>
          <div class="metric-value" id="m-model">--</div>
          <div class="metric-sub" style="flex-wrap: wrap; gap: 4px;">
            <span class="badge-tag" id="m-auth-badge" style="background: var(--bg-card-sub, #182030); color: var(--text-muted, #9ca3af);">${t("notConnected", lang)}</span>
            <span id="m-turns">${t("turn", lang)}: 0</span>
          </div>
        </div>

        <!-- Card 2: Requests & Errors -->
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

        <!-- Card 3: Uptime & Active Account -->
        <div class="metric-card" style="--card-accent: var(--accent-indigo);">
          <div class="metric-header">
            <span class="metric-label">${t("uptimeAndSession", lang)}</span>
            <span class="metric-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            </span>
          </div>
          <div class="metric-value" id="m-uptime">0h 0m 0s</div>
          <div class="metric-sub">
            <span class="badge-tag badge-success">${lang === "vi" ? "Gửi phần mới (delta)" : "Stateful Delta"}</span>
            <span id="m-backup">--</span>
          </div>
        </div>

        <!-- Card 4: Live Inflight & Chat Session -->
        <div class="metric-card" style="--card-accent: var(--status-warning);">
          <div class="metric-header">
            <span class="metric-label">${lang === "vi" ? "Phiên & Đang Chạy" : "Session & Inflight"}</span>
            <span class="metric-icon">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>
            </span>
          </div>
          <div class="metric-value">
            <span id="m-inflight">0</span>
            <span style="font-size: 13px; font-weight: normal; color: var(--text-muted);">${lang === "vi" ? "đang xử lý" : "inflight"}</span>
          </div>
          <div class="metric-sub">
            <span class="badge-tag" id="m-chat-key" style="background: var(--bg-card-sub, #182030); color: var(--text-muted, #9ca3af);">chat: new</span>
          </div>
        </div>
      </section>
    `;

    this.updateValues(state);
  }

  updateValues(state) {
    const { lang } = state;
    const lane = this.getCurrentLane(state);

    const mModel = this.querySelector("#m-model");
    const mAuthBadge = this.querySelector("#m-auth-badge");
    const mTurns = this.querySelector("#m-turns");
    const mRequests = this.querySelector("#m-requests");
    const mErrRate = this.querySelector("#m-err-rate");
    const mErrCount = this.querySelector("#m-err-count");
    const mUptime = this.querySelector("#m-uptime");
    const mBackup = this.querySelector("#m-backup");
    const mInflight = this.querySelector("#m-inflight");
    const mChatKey = this.querySelector("#m-chat-key");

    if (mModel) {
      mModel.innerText = lane.model || "unspecified";
    }

    if (mTurns) {
      const turns = lane.turn_count !== undefined ? lane.turn_count : (state.turnCount || 0);
      mTurns.innerText = `${t("turn", lang)}: ${turns}`;
    }

    if (mAuthBadge) {
      if (lane.control !== "on") {
        mAuthBadge.innerText = t("notConnected", lang);
        mAuthBadge.className = "badge-tag";
        mAuthBadge.style.background = "var(--bg-card-sub, #182030)";
        mAuthBadge.style.color = "var(--text-muted, #9ca3af)";
      } else if (!lane.auth_ok) {
        mAuthBadge.innerText = t("authGuest", lang);
        mAuthBadge.className = "badge-tag";
        mAuthBadge.style.background = "var(--status-warning, #f59e0b)";
        mAuthBadge.style.color = "#000000";
      } else {
        mAuthBadge.innerText = t("authOk", lang);
        mAuthBadge.className = "badge-tag badge-success";
        mAuthBadge.style.background = "";
        mAuthBadge.style.color = "";
      }
    }

    const reqs = lane.requests || 0;
    const errs = lane.errors || 0;
    const rateStr = reqs > 0 ? ((errs / reqs) * 100).toFixed(1) + "%" : "0.0%";

    if (mRequests) mRequests.innerText = reqs;
    if (mErrRate) {
      mErrRate.innerText = `${rateStr} ${t("errorRate", lang)}`;
      mErrRate.className = `badge-tag ${parseFloat(rateStr) > 5 ? "badge-warning" : "badge-success"}`;
    }
    if (mErrCount) mErrCount.innerText = `${errs} ${t("failures", lang)}`;

    if (mUptime) {
      const upSecs = lane.uptime_s || 0;
      const h = Math.floor(upSecs / 3600);
      const m = Math.floor((upSecs % 3600) / 60);
      const s = upSecs % 60;
      mUptime.innerText = `${h}h ${m}m ${s}s`;
    }

    if (mBackup) {
      const activeObj = lane.active_account || {};
      const accLabel = activeObj.label || "chinh";
      const accIdx = (activeObj.index !== undefined ? activeObj.index : 0) + 1;
      const accTotal = activeObj.total || 1;
      mBackup.innerText = `${accLabel} (${accIdx}/${accTotal})`;
    }

    if (mInflight) {
      mInflight.innerText = lane.inflight || 0;
    }
    if (mChatKey) {
      mChatKey.innerText = `chat: ${lane.chat_key || "new"}`;
    }
  }
}

customElements.define("bridge-metrics", BridgeMetrics);
