// Quy tắc: Poll không được render lại ô nhập/chọn
import { api } from "../api.js";
import { store } from "../state.js";
import { t } from "../i18n.js";
import { displayName } from "../lane-name.js";

/**
 * <bridge-cookie-manager> Component (T40E)
 * Cookie Manager supporting slots 0..4 (Chính, Dự phòng 1..4) routed to selectedLane.
 * QUY TẮC BẮT BUỘC: Poll không được render lại ô nhập/chọn!
 */
export class BridgeCookieManager extends HTMLElement {
  connectedCallback() {
    this.render();
    this.setupEvents();

    this.unsubscribe = store.subscribe((state, prevState) => {
      if (!prevState) return;

      const langChanged = state.lang !== prevState.lang;
      const selectedLaneChanged = state.selectedLane !== prevState.selectedLane;

      if (langChanged || selectedLaneChanged) {
        // Chỉ render lại toàn bộ khi đổi ngôn ngữ hoặc người dùng chủ động chọn luồng khác
        this.render();
        this.setupEvents();
      } else {
        // Vòng poll 3s (lanes thay đổi) -> CHỈ cập nhật nhãn động tại chỗ, TUYỆT ĐỐI không thay phần tử DOM
        this.updateDynamicValues(state);
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
      .map((slot) => {
        const acc = accounts[slot];
        let name = slot === 0 ? t("slotPrimary", lang) : t("slotBackup", lang, { n: slot });
        let statusTag = "";
        let accLabel = "";

        if (acc) {
          accLabel = acc.label ? `[${acc.label}]` : "";
          const st = acc.status || "ready";
          statusTag = `· ${st}`;
        } else {
          statusTag = slot === 0 ? "· (chưa nạp)" : "· (trống - có thể thêm)";
        }

        const isSelected = String(slot) === savedSlot ? "selected" : "";
        return `<option value="${slot}" ${isSelected}>${name} ${accLabel} ${statusTag}</option>`;
      })
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

          <!-- SECURE_1PSID -->
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

          <!-- SECURE_1PSIDTS -->
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

    this.slotSelect = this.querySelector("#cm-slot");
    this.labelInput = this.querySelector("#cm-label");
    this.psidInput = this.querySelector("#cm-psid");
    this.psidtsInput = this.querySelector("#cm-psidts");
    this.saveBtn = this.querySelector("#cm-save-btn");

    if (this.slotSelect) {
      this.slotSelect.value = savedSlot;
    }
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

    // Cập nhật text của các option trong #cm-slot mà KHÔNG thay đổi select.value
    if (this.slotSelect) {
      const currentSelectedVal = this.slotSelect.value;
      const accounts = lane.accounts || [];

      for (let slot = 0; slot <= 4; slot++) {
        const opt = this.slotSelect.querySelector(`option[value="${slot}"]`);
        if (opt) {
          const acc = accounts[slot];
          let name = slot === 0 ? t("slotPrimary", state.lang) : t("slotBackup", state.lang, { n: slot });
          let statusTag = "";
          let accLabel = "";

          if (acc) {
            accLabel = acc.label ? `[${acc.label}]` : "";
            const st = acc.status || "ready";
            statusTag = `· ${st}`;
          } else {
            statusTag = slot === 0 ? "· (chưa nạp)" : "· (trống - có thể thêm)";
          }

          const newText = `${name} ${accLabel} ${statusTag}`;
          if (opt.textContent !== newText) {
            opt.textContent = newText;
          }
        }
      }

      // Giữ nguyên giá trị người dùng đang chọn
      if (this.slotSelect.value !== currentSelectedVal) {
        this.slotSelect.value = currentSelectedVal;
      }
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
  }

  async handleSave() {
    const state = store.getState();
    const { lang } = state;
    const lane = this.getCurrentLane(state);
    const laneId = lane.id || state.selectedLane || "default";
    const slot = parseInt(this.slotSelect.value, 10);
    const label = this.labelInput.value.trim();
    const psid = this.psidInput.value.trim();
    const psidts = this.psidtsInput.value.trim();

    if (!psid) {
      store.notifyToast(t("enterPsidRequired", lang), "error");
      this.psidInput.focus();
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
    this.saveBtn.innerText = t("verifyingCookies", lang);

    try {
      const res = await api.updateLaneCookies(laneId, slot, psid, psidts, label);
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
      this.saveBtn.disabled = false;
      this.saveBtn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg> ${t("saveCookiesBtn", lang)}`;
    }
  }
}

if (!customElements.get("bridge-cookie-manager")) {
  customElements.define("bridge-cookie-manager", BridgeCookieManager);
}
