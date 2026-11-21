// Quy tắc: Poll không được render lại ô nhập/chọn
import { api } from "../api.js";
import { store } from "../state.js";
import { t } from "../i18n.js";
import { displayName } from "../lane-name.js";

const getApi = () => (typeof api !== "undefined" && api ? api : (window.api || {}));

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * <bridge-cookie-manager> Component (T40E + Profile Vault Auto Cookie Sync)
 * Cookie Manager supporting slots 0..4 (Chính, Dự phòng 1..4) routed to selectedLane,
 * plus Profile Vault for managing N Google accounts with 0 open browser tabs.
 * QUY TẮC BẮT BUỘC: Poll không được render lại ô nhập/chọn!
 */
export class BridgeCookieManager extends HTMLElement {
  connectedCallback() {
    this._profiles = [];
    this._browserInfo = { browser_available: true, browser_name: "" };
    this._vaultSignature = "";
    this._lastVaultFetchTs = 0;

    this.render();
    this.setupEvents();
    this.refreshProfiles(true);

    this.unsubscribe = store.subscribe((state, prevState) => {
      if (!prevState) return;

      const langChanged = state.lang !== prevState.lang;
      const selectedLaneChanged = state.selectedLane !== prevState.selectedLane;

      if (langChanged || selectedLaneChanged) {
        // Chỉ render lại toàn bộ khi đổi ngôn ngữ hoặc người dùng chủ động chọn luồng khác
        this._vaultSignature = "";
        this.render();
        this.setupEvents();
        this.renderVaultList(state.lang);
      } else {
        // Vòng poll 3s (lanes thay đổi) -> CHỈ cập nhật nhãn động tại chỗ, TUYỆT ĐỐI không thay phần tử DOM
        this.updateDynamicValues(state);
        if (Date.now() - this._lastVaultFetchTs >= 4500) {
          this.refreshProfiles(false);
        }
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

  renderEnvIdBanner(lane, lang) {
    if (!lane) return "";
    const envFile = lane.env_file || (lane.id ? `.env.${lane.id}` : ".env");
    const hasEnv = Boolean(lane.has_env_id);
    const idStatus = lane.id_status || (hasEnv ? (lane.auth_ok ? "valid" : (lane.control === "on" ? "expired" : "ready")) : "missing");

    if (hasEnv && idStatus === "expired") {
      return `
        <div class="env-id-banner expired" style="margin-bottom: 14px; padding: 12px 14px; border-radius: 8px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.4); color: #ef4444;">
          <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 18px; line-height: 1;">❌</span>
              <strong style="font-size: 13px; color: #ef4444;">${t("idExpiredTitle", lang, { file: envFile })}</strong>
            </div>
            <span class="badge-tag" style="background: #ef4444; color: #fff; font-weight: 700; font-size: 10px; padding: 2px 6px; border-radius: 4px;">CẦN ID MỚI</span>
          </div>
          <div style="margin-top: 6px; font-size: 12px; color: var(--text-secondary); line-height: 1.4; padding-left: 26px;">
            ⚠️ ${t("idExpiredAction", lang, { file: envFile })}
          </div>
        </div>
      `;
    }

    if (hasEnv) {
      return `
        <div class="env-id-banner ready" style="margin-bottom: 14px; padding: 10px 14px; border-radius: 8px; background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.35); color: #10b981;">
          <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span style="font-size: 18px; line-height: 1;">✅</span>
              <div>
                <strong style="font-size: 13px; color: #10b981;">${t("idLoadedFromEnv", lang, { file: envFile })}</strong>
                <div style="font-size: 11.5px; opacity: 0.85; margin-top: 2px;">${t("idReadyDesc", lang, { file: envFile })}</div>
              </div>
            </div>
            <span class="badge-tag badge-success" style="font-size: 10.5px; font-weight: 700;">READY</span>
          </div>
        </div>
      `;
    }

    return `
      <div class="env-id-banner missing" style="margin-bottom: 14px; padding: 10px 14px; border-radius: 8px; background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.35); color: #f59e0b;">
        <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
          <div style="display: flex; align-items: center; gap: 8px;">
            <span style="font-size: 18px; line-height: 1;">⚠️</span>
            <div>
              <strong style="font-size: 13px; color: #f59e0b;">${t("idMissingFromEnv", lang, { file: envFile })}</strong>
              <div style="font-size: 11.5px; opacity: 0.85; margin-top: 2px;">${t("idMissingDesc", lang, { file: envFile })}</div>
            </div>
          </div>
          <span class="badge-tag badge-warning" style="font-size: 10.5px; font-weight: 700;">MISSING</span>
        </div>
      </div>
    `;
  }

  getSlotOptionText(slot, accounts, lang, lane) {
    const account = accounts && accounts[slot];
    const name = slot === 0 ? t("slotPrimary", lang) : t("slotBackup", lang, { n: slot });
    const accountLabel = account && account.label ? `[${account.label}] ` : "";

    if (slot === 0 && lane && lane.has_env_id) {
      const envFile = lane.env_file || (lane.id ? `.env.${lane.id}` : ".env");
      if (lane.id_status === "expired") {
        return `${name} ${accountLabel}· ${t("slotExpiredFromEnv", lang)} (${envFile})`;
      }
      return `${name} ${accountLabel}· ${t("slotLoadedFromEnv", lang, { file: envFile })}`;
    }

    if (account) {
      return `${name} ${accountLabel}· ${account.status || "ready"}`;
    }
    const accLen = accounts ? accounts.length : 0;
    if (slot === accLen) {
      return `${name} · ${t("slotCanAdd", lang)}`;
    }
    return `${name} · ${t("slotRequiresPrevious", lang, { n: slot - 1 })}`;
  }

  updateSlotOptions(accounts, lang, lane) {
    for (let slot = 0; slot <= 4; slot++) {
      const option = this.slotSelect?.querySelector(`option[value="${slot}"]`);
      if (!option) continue;

      option.textContent = this.getSlotOptionText(slot, accounts, lang, lane);
      const accLen = accounts ? accounts.length : 0;
      option.disabled = slot > Math.max(accLen, (lane && lane.has_env_id) ? 1 : 0);
    }
  }

  getSelectableSlot(slot, accountCount) {
    const value = Number(slot);
    const maxSelectable = Math.min(accountCount, 4);
    return Number.isInteger(value) && value >= 0 && value <= maxSelectable
      ? String(value)
      : String(maxSelectable);
  }

  render() {
    const state = store.getState();
    const { lang, lanes } = state;
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";
    const laneReachable = lane.reachable !== false;
    const accounts = lane.accounts || [];

    const flowNum = laneId === "acc1" ? 1 : (laneId === "acc2" ? 2 : 1);
    const showLaneTag = Array.isArray(lanes) && lanes.length >= 2;
    const laneTagHtml = showLaneTag
      ? `<span class="badge-tag badge-cyan" style="text-transform:none; font-weight:600; font-size:11px; margin-left:6px;">${displayName(laneId, lane.label, flowNum, lang)}</span>`
      : "";

    // Đọc slot đã lưu của luồng này từ state (mặc định 0)
    const savedSlot = (state.cookieSlots && state.cookieSlots[laneId] !== undefined)
      ? String(state.cookieSlots[laneId])
      : "0";

    // Tạo các tùy chọn cho slot 0..4
    const slotOptionsHtml = [0, 1, 2, 3, 4]
      .map((slot) => `<option value="${slot}"></option>`)
      .join("");

    this.innerHTML = `
      <div class="panel">
        <div class="panel-head">
          <div class="panel-title" style="display:flex; align-items:center;">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>
            <span>${t("cookieManagerTitle", lang)}</span>
            <span class="cm-lane-tag-container">${laneTagHtml}</span>
          </div>
          <span class="badge-tag badge-success">${t("hotReloadTag", lang)}</span>
        </div>
        <div class="panel-body">
          <div id="cm-env-banner-container">
            ${this.renderEnvIdBanner(lane, lang)}
          </div>

          <!-- Chọn ô tài khoản Slot 0..4 -->
          <div class="form-field">
            <label class="field-label" for="cm-slot">
              <span>${t("selectSlotLabel", lang)}</span>
              <span class="field-hint">Slot 0..4</span>
            </label>
            <select class="select-control" id="cm-slot">
              ${slotOptionsHtml}
            </select>
          </div>

          <!-- Nhãn gợi nhớ tài khoản -->
          <div class="form-field">
            <label class="field-label" for="cm-label">
              <span>${lang === "vi" ? "Nhãn tài khoản (tùy chọn):" : "Account Label (optional):"}</span>
              <span class="field-hint">vd: acc_chinh, backup_1</span>
            </label>
            <input type="text" class="input-text code-font" id="cm-label" placeholder="vd: my_account_1">
          </div>

          <!-- Profile Vault: Đăng nhập 1 lần, tự nạp ngầm không mở tab -->
          <div class="vault-box" style="margin: 12px 0 14px 0; padding: 12px; border-radius: 8px; border: 1px solid var(--border-color, rgba(148,163,184,0.22)); background: rgba(59,130,246,0.05);">
            <div style="display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:6px; flex-wrap:wrap;">
              <strong style="font-size:12.5px; color: var(--text-primary);">🔐 ${t("vaultTitle", lang)}</strong>
              <button type="button" class="btn btn-secondary" id="cm-vault-sync-all-btn" style="padding:4px 10px; font-size:11.5px;">
                🔄 ${t("vaultSyncAllBtn", lang)}
              </button>
            </div>
            <div style="font-size:11.5px; color: var(--text-secondary); margin-bottom:10px; line-height:1.4;">
              ${t("vaultDesc", lang)}
            </div>
            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:10px;">
              <input
                type="text"
                class="input-text code-font"
                id="cm-vault-pid"
                placeholder="${escapeHtml(laneId === "default" ? "acc1" : laneId)}"
                value="${escapeHtml(savedSlot === "0" ? (laneId === "default" ? "acc1" : laneId) : `${laneId}_s${savedSlot}`)}"
                style="flex:1; min-width:130px;"
                aria-label="${t("vaultProfileIdLabel", lang)}"
              />
              <button type="button" class="btn btn-primary" id="cm-vault-login-btn" style="padding:7px 12px; font-size:12px; white-space:nowrap;">
                🌐 ${t("vaultLoginOnceBtn", lang)}
              </button>
            </div>
            <div id="cm-vault-list" style="display:flex; flex-direction:column; gap:6px; max-height:210px; overflow-y:auto;"></div>
          </div>

          <div style="font-size:11.5px; color: var(--text-secondary); margin: 4px 0 8px 0; font-weight:600;">
            ${t("vaultManualSection", lang)}
          </div>

          <!-- SECURE_1PSID -->
          <div class="form-field">
            <label class="field-label" for="cm-psid">
              <span>${t("psidLabel", lang)}</span>
              <span class="field-hint">${t("psidHint", lang)}</span>
            </label>
            <div class="input-container">
              <input type="password" class="input-text code-font" id="cm-psid" placeholder="g.a000..." required>
              <button type="button" class="input-toggle-btn" data-target="cm-psid" title="${t("toggleVisibility", lang)}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
              </button>
            </div>
          </div>

          <!-- SECURE_1PSIDTS -->
          <div class="form-field">
            <label class="field-label" for="cm-psidts">
              <span>${t("psidtsLabel", lang)}</span>
              <span class="field-hint">${t("psidtsHint", lang)}</span>
            </label>
            <div class="input-container">
              <input type="password" class="input-text code-font" id="cm-psidts" placeholder="sidts-..." required>
              <button type="button" class="input-toggle-btn" data-target="cm-psidts" title="${t("toggleVisibility", lang)}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
              </button>
            </div>
          </div>

          <button class="btn btn-primary" id="cm-save-btn" ${!laneReachable ? "disabled" : ""} title="${laneReachable ? "" : t("laneUnreachableAction", lang, { id: laneId })}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
            ${t("saveCookiesBtn", lang)}
          </button>
        </div>
      </div>
    `;

    this.slotSelect = this.querySelector("#cm-slot");
    this.labelInput = this.querySelector("#cm-label");
    this.vaultPidInput = this.querySelector("#cm-vault-pid");
    this.vaultLoginBtn = this.querySelector("#cm-vault-login-btn");
    this.vaultSyncAllBtn = this.querySelector("#cm-vault-sync-all-btn");
    this.vaultListEl = this.querySelector("#cm-vault-list");
    this.psidInput = this.querySelector("#cm-psid");
    this.psidtsInput = this.querySelector("#cm-psidts");
    this.saveBtn = this.querySelector("#cm-save-btn");

    if (this.slotSelect) {
      this.updateSlotOptions(accounts, lang, lane);
      this.slotSelect.value = this.getSelectableSlot(savedSlot, Math.max(accounts.length, lane.has_env_id ? 1 : 0));
    }
    this.renderVaultList(lang);
  }

  async refreshProfiles(force = false) {
    if (!force && this._vaultFetching) return;
    if (typeof getApi().listProfiles !== "function") return;
    this._vaultFetching = true;
    this._lastVaultFetchTs = Date.now();
    try {
      const data = await getApi().listProfiles();
      this._profiles = Array.isArray(data.profiles) ? data.profiles : [];
      this._browserInfo = {
        browser_available: data.browser_available !== false,
        browser_name: data.browser_name || "",
      };
      this.renderVaultList(store.getState().lang);
    } catch (_err) {
      // Nếu mở từ LAN (403 loopback-only) hoặc chưa hỗ trợ -> giữ yên lặng không spam toast
    } finally {
      this._vaultFetching = false;
    }
  }

  renderVaultList(lang = "vi") {
    if (!this.vaultListEl) return;
    const profiles = this._profiles || [];
    const sig = JSON.stringify({ lang, profiles });
    if (sig === this._vaultSignature) return;
    this._vaultSignature = sig;

    if (!profiles.length) {
      this.vaultListEl.innerHTML = `<div style="font-size:11.5px; color: var(--text-secondary); padding:4px 2px;">${t("vaultEmpty", lang)}</div>`;
      return;
    }

    this.vaultListEl.innerHTML = profiles
      .map((p) => {
        const pid = escapeHtml(p.profile_id);
        const label = p.label && p.label !== p.profile_id ? ` (${escapeHtml(p.label)})` : "";
        const bindTag = p.lane_id
          ? `<span class="badge-tag badge-cyan" style="font-size:10px; padding:1px 5px;">${escapeHtml(p.lane_id)}:slot${p.slot ?? 0}</span>`
          : `<span class="badge-tag" style="font-size:10px; padding:1px 5px; opacity:0.7;">vault</span>`;
        let stHtml = `<span style="font-size:11px; color:#10b981;">${t("vaultReady", lang)}</span>`;
        if (p.runtime_state === "waiting_login" || p.last_status === "waiting_login") {
          stHtml = `<span style="font-size:11px; color:#f59e0b;">${t("vaultWaitingLogin", lang)}</span>`;
        } else if (p.runtime_state === "syncing") {
          stHtml = `<span style="font-size:11px; color:#38bdf8;">${t("vaultSyncing", lang)}</span>`;
        } else if (p.last_status === "login_required" || !p.has_profile_data) {
          stHtml = `<span style="font-size:11px; color:#f59e0b;">${t("vaultLoginRequired", lang)}</span>`;
        } else if (p.last_status === "error") {
          stHtml = `<span style="font-size:11px; color:#ef4444;" title="${escapeHtml(p.last_error || "")}">❌ ${escapeHtml((p.last_error || "error").slice(0, 48))}</span>`;
        }
        const isWaitingLogin = p.runtime_state === "waiting_login" || p.last_status === "waiting_login";
        const isSyncing = p.runtime_state === "syncing";
        const busy = isWaitingLogin || isSyncing;
        const primaryActionBtn = isWaitingLogin
          ? `<button type="button" class="btn btn-primary cm-vault-finish" data-pid="${pid}" style="padding:3px 9px; font-size:11px; background:#10b981; border-color:#10b981;">✅ ${t("vaultFinishLoginBtn", lang)}</button>`
          : `<button type="button" class="btn btn-secondary cm-vault-sync-one" data-pid="${pid}" ${busy ? "disabled" : ""} style="padding:3px 8px; font-size:11px;">🔄 ${t("vaultSyncBtn", lang)}</button>`;
        return `
          <div style="display:flex; align-items:center; justify-content:space-between; gap:8px; padding:6px 8px; border-radius:6px; background: rgba(15,23,42,0.28); border: 1px solid rgba(148,163,184,0.16); flex-wrap:wrap;">
            <div style="display:flex; flex-direction:column; gap:2px; min-width:130px;">
              <div style="display:flex; align-items:center; gap:6px; flex-wrap:wrap;">
                <code style="font-size:12px; font-weight:600;">${pid}</code>
                <span style="font-size:11px; opacity:0.85;">${label}</span>
                ${bindTag}
              </div>
              ${stHtml}
            </div>
            <div style="display:flex; gap:5px; align-items:center;">
              ${primaryActionBtn}
              <button type="button" class="btn btn-secondary cm-vault-relogin" data-pid="${pid}" ${busy ? "disabled" : ""} style="padding:3px 8px; font-size:11px;">
                🌐
              </button>
              <button type="button" class="btn btn-secondary cm-vault-del" data-pid="${pid}" ${busy ? "disabled" : ""} style="padding:3px 7px; font-size:11px; color:#f87171;">
                ✕
              </button>
            </div>
          </div>
        `;
      })
      .join("");
  }

  updateDynamicValues(state) {
    // Cập nhật nhãn luồng trên tiêu đề panel
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";
    const flowNum = laneId === "acc1" ? 1 : (laneId === "acc2" ? 2 : 1);
    const lanes = state.lanes || [];
    const showLaneTag = Array.isArray(lanes) && lanes.length >= 2;

    const tagContainer = this.querySelector(".cm-lane-tag-container");
    if (tagContainer) {
      tagContainer.innerHTML = showLaneTag
        ? `<span class="badge-tag badge-cyan" style="text-transform:none; font-weight:600; font-size:11px; margin-left:6px;">${displayName(laneId, lane.label, flowNum, state.lang)}</span>`
        : "";
    }

    // Cập nhật banner trạng thái ID từ .env
    const bannerContainer = this.querySelector("#cm-env-banner-container");
    if (bannerContainer) {
      bannerContainer.innerHTML = this.renderEnvIdBanner(lane, state.lang);
    }

    // Cập nhật text của các option trong #cm-slot mà KHÔNG thay đổi select.value
    if (this.slotSelect) {
      const currentSelectedVal = this.slotSelect.value;
      const accounts = lane.accounts || [];
      this.updateSlotOptions(accounts, state.lang, lane);

      // Giữ nguyên giá trị người dùng đang chọn
      this.slotSelect.value = this.getSelectableSlot(currentSelectedVal, Math.max(accounts.length, lane.has_env_id ? 1 : 0));
    }
    if (this.saveBtn && !this.saveBtn.dataset.busy) {
      this.saveBtn.disabled = lane.reachable === false;
      this.saveBtn.title = lane.reachable === false
        ? t("laneUnreachableAction", state.lang, { id: laneId })
        : "";
    }
  }

  setupEvents() {
    this.querySelectorAll(".input-toggle-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const targetId = btn.getAttribute("data-target");
        const input = this.querySelector(`#${targetId}`);
        if (!input) return;
        if (input.type === "password") {
          input.type = "text";
          btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>`;
        } else {
          input.type = "password";
          btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>`;
        }
      });
    });

    this.slotSelect?.addEventListener("change", () => {
      const state = store.getState();
      const lane = this.getCurrentLane(state);
      const laneId = lane.id || state.selectedLane || "default";
      const slotVal = this.slotSelect.value;

      // Lưu slot vào state theo từng luồng
      const cookieSlots = { ...(state.cookieSlots || {}) };
      cookieSlots[laneId] = slotVal;
      store.setState({ cookieSlots });

      // Nếu ô này đã có nhãn, tự điền gợi ý vào ô nhãn (nếu ô nhãn đang trống)
      const accounts = lane.accounts || [];
      const slot = parseInt(slotVal, 10);
      const acc = accounts[slot];
      if (acc && acc.label) {
        if (!this.labelInput.value) {
          this.labelInput.value = acc.label;
        }
      }
    });

    this.saveBtn?.addEventListener("click", () => this.handleSave());
    this.vaultLoginBtn?.addEventListener("click", () => this.handleVaultLogin());
    this.vaultSyncAllBtn?.addEventListener("click", () => this.handleVaultSyncAll());
    this.vaultListEl?.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button");
      if (!btn) return;
      const pid = btn.getAttribute("data-pid");
      if (!pid) return;
      if (btn.classList.contains("cm-vault-sync-one")) {
        this.handleVaultSyncOne(pid, btn);
      } else if (btn.classList.contains("cm-vault-finish")) {
        this.handleVaultFinishLogin(pid, btn);
      } else if (btn.classList.contains("cm-vault-relogin")) {
        this.handleVaultLogin(pid);
      } else if (btn.classList.contains("cm-vault-del")) {
        this.handleVaultDelete(pid);
      }
    });
  }

  async handleVaultFinishLogin(pid, btnEl) {
    const { lang } = store.getState();
    if (btnEl) btnEl.disabled = true;
    try {
      await getApi().finishProfileLogin(pid);
      store.notifyToast(t("vaultSyncedToast", lang, { id: pid }), "success");
      await this.refreshProfiles(true);
      window.dispatchEvent(new CustomEvent("refresh-request"));
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
      await this.refreshProfiles(true);
    } finally {
      if (btnEl) btnEl.disabled = false;
    }
  }

  async handleVaultLogin(explicitPid = "") {
    const state = store.getState();
    const { lang } = state;
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";
    const slot = parseInt(this.slotSelect?.value || "0", 10);
    const label = (this.labelInput?.value || "").trim();
    const pid = (explicitPid || this.vaultPidInput?.value || "").trim();
    if (!pid) {
      this.vaultPidInput?.focus();
      return;
    }
    try {
      const existing = (this._profiles || []).find((p) => p.profile_id === pid);
      const bindLane = explicitPid && existing?.lane_id ? existing.lane_id : laneId;
      const bindSlot = explicitPid && existing?.slot !== null && existing?.slot !== undefined ? existing.slot : slot;
      const bindLabel = label || existing?.label || "";
      await getApi().loginProfile(pid, { laneId: bindLane, slot: bindSlot, label: bindLabel });
      store.notifyToast(t("vaultLoginOpened", lang, { id: pid }), "info");
      await this.refreshProfiles(true);
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
    }
  }

  async handleVaultSyncOne(pid, btnEl) {
    const { lang } = store.getState();
    if (btnEl) btnEl.disabled = true;
    try {
      await getApi().syncProfile(pid);
      store.notifyToast(t("vaultSyncedToast", lang, { id: pid }), "success");
      await this.refreshProfiles(true);
      window.dispatchEvent(new CustomEvent("refresh-request"));
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
      await this.refreshProfiles(true);
    } finally {
      if (btnEl) btnEl.disabled = false;
    }
  }

  async handleVaultSyncAll() {
    const { lang } = store.getState();
    if (this.vaultSyncAllBtn) this.vaultSyncAllBtn.disabled = true;
    try {
      const res = await getApi().syncAllProfiles();
      store.notifyToast(
        t("vaultSyncAllDone", lang, { synced: res.synced || 0, total: res.total || 0 }),
        res.synced === res.total ? "success" : "warning"
      );
      await this.refreshProfiles(true);
      window.dispatchEvent(new CustomEvent("refresh-request"));
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
    } finally {
      if (this.vaultSyncAllBtn) this.vaultSyncAllBtn.disabled = false;
    }
  }

  async handleVaultDelete(pid) {
    const { lang } = store.getState();
    if (!confirm(t("vaultDeleteConfirm", lang, { id: pid }))) return;
    try {
      await getApi().deleteProfile(pid);
      await this.refreshProfiles(true);
    } catch (err) {
      store.notifyToast(`Error: ${err.message}`, "error");
    }
  }

  async handleSave() {
    const state = store.getState();
    const { lang } = state;
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";
    if (lane.reachable === false) {
      store.notifyToast(t("laneUnreachableAction", lang, { id: laneId }), "warning");
      return;
    }
    const slot = parseInt(this.slotSelect.value, 10);
    const label = this.labelInput.value.trim();
    const psid = this.psidInput.value.trim();
    const psidts = this.psidtsInput.value.trim();

    if (!psid) {
      store.notifyToast(t("enterPsidRequired", lang), "error");
      this.psidInput.focus();
      return;
    }
    if (!psidts) {
      store.notifyToast(t("enterPsidtsRequired", lang), "error");
      this.psidtsInput.focus();
      return;
    }

    const slotName = slot === 0 ? t("slotPrimary", lang) : t("slotBackup", lang, { n: slot });
    const confirmPrompt =
      lang === "vi"
        ? `Xác nhận nạp cookie cho luồng ${laneId} · ô ${slotName}${label ? ` (${label})` : ""}?`
        : `Confirm update cookies for lane ${laneId} · slot ${slotName}${label ? ` (${label})` : ""}?`;

    if (!confirm(confirmPrompt)) {
      return;
    }

    this.saveBtn.disabled = true;
    this.saveBtn.dataset.busy = "true";
    this.saveBtn.innerText = t("verifyingCookies", lang);

    try {
      const res = await getApi().updateLaneCookies(laneId, slot, psid, psidts, label);
      if (res.persisted === false) {
        store.notifyToast(
          lang === "vi"
            ? `⚠️ Đã nạp vào bộ nhớ nhưng KHÔNG thể ghi file .env (${res.warning || "quyền truy cập"}). Sau khi restart sẽ mất.`
            : `⚠️ Loaded into memory but could NOT write .env file. Will be lost on restart.`,
          "warning"
        );
      } else {
        store.notifyToast(t("cookiesUpdatedSuccess", lang), "success");
      }
      // CHỈ xóa giá trị ô nhập khi LƯU THÀNH CÔNG
      this.psidInput.value = "";
      this.psidtsInput.value = "";
      this.labelInput.value = "";
      window.dispatchEvent(new CustomEvent("refresh-request"));
    } catch (err) {
      // Khi lỗi: GIỮ NGUYÊN các ô nhập để người dùng không phải gõ lại
      store.notifyToast(`Error: ${err.message}`, "error");
    } finally {
      delete this.saveBtn.dataset.busy;
      this.saveBtn.disabled = this.getCurrentLane(store.getState()).reachable === false;
      this.saveBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg> ${t("saveCookiesBtn", lang)}`;
    }
  }
}

if (!customElements.get("bridge-cookie-manager")) {
  customElements.define("bridge-cookie-manager", BridgeCookieManager);
}
