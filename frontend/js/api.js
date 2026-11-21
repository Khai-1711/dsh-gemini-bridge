/**
 * API Service Layer — Handles all HTTP communication with FastAPI Backend
 */

const API_BASE = window.location.origin;
const KEY_STORAGE_NAME = "bridge_api_key";

export function getStoredApiKey() {
  return sessionStorage.getItem(KEY_STORAGE_NAME) || "";
}

export function setStoredApiKey(key) {
  if (key && key.trim()) {
    sessionStorage.setItem(KEY_STORAGE_NAME, key.trim());
  } else {
    sessionStorage.removeItem(KEY_STORAGE_NAME);
  }
}

export function clearStoredApiKey() {
  sessionStorage.removeItem(KEY_STORAGE_NAME);
}

let _authModalPromise = null;

export function showAuthModal(errorMsg = "") {
  if (_authModalPromise) return _authModalPromise;

  _authModalPromise = new Promise((resolve) => {
    let modal = document.getElementById("bridge-auth-modal");
    if (!modal) {
      modal = document.createElement("div");
      modal.id = "bridge-auth-modal";
      modal.style.cssText = `
        position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
        background: rgba(0,0,0,0.7); backdrop-filter: blur(4px);
        display: flex; align-items: center; justify-content: center; z-index: 99999;
      `;
      document.body.appendChild(modal);
    }

    const lang = localStorage.getItem("bridge_lang") || "vi";
    const isVi = lang === "vi";
    const title = isVi ? "Nhập khóa truy cập" : "Enter access key";
    const desc = isVi ? "Máy chủ yêu cầu API Key để truy cập các tính năng điều khiển:" : "Server requires API Key for access:";
    const placeholder = isVi ? "Nhập API Key..." : "Enter API Key...";
    const btnSubmit = isVi ? "Xác nhận" : "Submit";
    const btnForget = isVi ? "Quên khóa" : "Forget key";
    const btnCancel = isVi ? "Đóng" : "Close";

    modal.innerHTML = `
      <div style="background: var(--bg-card, #1e293b); border: 1px solid var(--border-color, #334155); border-radius: 8px; padding: 24px; width: 90%; max-width: 420px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); color: var(--text-primary, #f8fafc); font-family: sans-serif;">
        <h3 style="margin: 0 0 8px 0; font-size: 18px; display: flex; align-items: center; gap: 8px;">
          <span>🔒</span> ${title}
        </h3>
        <p style="margin: 0 0 16px 0; font-size: 13px; color: var(--text-secondary, #94a3b8);">${desc}</p>
        <div id="auth-modal-error" style="color: #ef4444; font-size: 13px; margin-bottom: 12px; display: ${errorMsg ? 'block' : 'none'};">
          ${errorMsg}
        </div>
        <input type="password" id="auth-key-input" placeholder="${placeholder}" value="${getStoredApiKey()}" style="width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 6px; border: 1px solid var(--border-color, #475569); background: var(--bg-input, #0f172a); color: var(--text-primary, #fff); font-size: 14px; margin-bottom: 16px; outline: none;" />
        <div style="display: flex; gap: 8px; justify-content: flex-end;">
          <button id="btn-modal-forget" type="button" style="padding: 8px 12px; border-radius: 6px; border: 1px solid #64748b; background: transparent; color: #94a3b8; font-size: 13px; cursor: pointer;">${btnForget}</button>
          <button id="btn-modal-cancel" type="button" style="padding: 8px 12px; border-radius: 6px; border: 1px solid #475569; background: transparent; color: #cbd5e1; font-size: 13px; cursor: pointer;">${btnCancel}</button>
          <button id="btn-modal-submit" type="button" style="padding: 8px 16px; border-radius: 6px; border: none; background: #3b82f6; color: #fff; font-size: 13px; font-weight: 600; cursor: pointer;">${btnSubmit}</button>
        </div>
      </div>
    `;

    modal.style.display = "flex";
    const inputEl = modal.querySelector("#auth-key-input");
    inputEl.focus();

    const cleanup = () => {
      modal.style.display = "none";
      _authModalPromise = null;
    };

    modal.querySelector("#btn-modal-forget").onclick = () => {
      clearStoredApiKey();
      inputEl.value = "";
      cleanup();
      resolve(null);
    };

    modal.querySelector("#btn-modal-cancel").onclick = () => {
      cleanup();
      resolve(null);
    };

    const doSubmit = () => {
      const val = inputEl.value.trim();
      setStoredApiKey(val);
      cleanup();
      resolve(val);
    };

    modal.querySelector("#btn-modal-submit").onclick = doSubmit;
    inputEl.onkeydown = (e) => {
      if (e.key === "Enter") doSubmit();
      if (e.key === "Escape") { cleanup(); resolve(null); }
    };
  });

  return _authModalPromise;
}

export async function authFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const key = getStoredApiKey();
  if (key && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${key}`);
  }
  options.headers = headers;

  let res = await fetch(url, options);
  if (res.status === 401) {
    const isVi = (localStorage.getItem("bridge_lang") || "vi") === "vi";
    const errorMsg = isVi
      ? "Khóa truy cập không chính xác (401 Unauthorized)."
      : "Invalid access key (401 Unauthorized).";
    const newKey = await showAuthModal(errorMsg);
    if (newKey) {
      headers.set("Authorization", `Bearer ${newKey}`);
      options.headers = headers;
      res = await fetch(url, options);
    }
  }
  return res;
}

function extractErrorMessage(data, fallback) {
  if (!data) return fallback;
  if (typeof data.error === "string" && data.error.trim()) return data.error;
  return data.error?.message || data.detail || data.message || fallback;
}

export const api = {
  /**
   * Lấy tình trạng sức khỏe tối thiểu công khai (/health)
   */
  async getHealth() {
    const startTime = performance.now();
    const res = await authFetch(`${API_BASE}/health`);
    const latency = Math.round(performance.now() - startTime);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return { ...data, latency };
  },

  /**
   * Lấy tình trạng trạng thái chi tiết (/v1/status - yêu cầu auth nếu có API_KEY)
   */
  async getStatus() {
    const startTime = performance.now();
    const res = await authFetch(`${API_BASE}/v1/status`);
    const latency = Math.round(performance.now() - startTime);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return { ...data, latency };
  },

  /**
   * Lấy các dòng nhật ký mới nhất
   */
  async getLogs(lines = 60) {
    const res = await authFetch(`${API_BASE}/v1/logs?lines=${lines}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  /**
   * Chuyển đổi Model Gemini
   */
  async switchModel(modelName, keepChat = true) {
    const res = await authFetch(`${API_BASE}/v1/switch-model`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: modelName, keep_chat: keepChat }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(extractErrorMessage(data, "Không thể chuyển model"));
    return data;
  },

  /**
   * Cập nhật Cookie (Hot-Reload)
   */
  async updateCookies(psid, psidts) {
    const res = await authFetch(`${API_BASE}/v1/update-cookies`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ SECURE_1PSID: psid, SECURE_1PSIDTS: psidts }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(extractErrorMessage(data, "Cập nhật cookie thất bại"));
    return data;
  },

  /**
   * Reset Chat Session
   */
  async resetSession() {
    const res = await authFetch(`${API_BASE}/v1/reset`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(extractErrorMessage(data, "Reset session thất bại"));
    return data;
  },

  /**
   * Xóa bộ nhớ đệm Cache
   */
  async clearCache() {
    const res = await authFetch(`${API_BASE}/v1/cache/clear`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(extractErrorMessage(data, "Xóa cache thất bại"));
    return data;
  },

  /**
   * Lấy trạng thái Bật/Tắt API Control
   */
  async getControlStatus() {
    const res = await authFetch(`${API_BASE}/v1/control`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  /**
   * Bật API Bridge
   */
  async enableBridge() {
    const res = await authFetch(`${API_BASE}/v1/control/enable`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      const msg = data.error?.message || data.error || data.detail || `Lỗi ${res.status}`;
      throw new Error(msg);
    }
    return data;
  },

  /**
   * Tắt API Bridge (Graceful Drain)
   */
  async disableBridge() {
    const res = await authFetch(`${API_BASE}/v1/control/disable`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      const msg = data.error?.message || data.error || data.detail || `Lỗi ${res.status}`;
      throw new Error(msg);
    }
    return data;
  },

  /**
   * Lấy danh sách trạng thái các luồng (T37)
   */
  async getLanes(forceRefresh = false) {
    const refreshQuery = forceRefresh ? "?refresh=true" : "";
    const res = await authFetch(`${API_BASE}/v1/lanes${refreshQuery}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  /**
   * Bật luồng chỉ định
   */
  async enableLane(laneId) {
    const res = await authFetch(`${API_BASE}/v1/lanes/${encodeURIComponent(laneId)}/enable`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error?.message || data.error || `HTTP ${res.status}`);
    return data;
  },

  /**
   * Tắt luồng chỉ định (graceful drain)
   */
  async disableLane(laneId) {
    const res = await authFetch(`${API_BASE}/v1/lanes/${encodeURIComponent(laneId)}/disable`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error?.message || data.error || `HTTP ${res.status}`);
    return data;
  },

  /**
   * Thử đăng nhập lại luồng chỉ định (T37/T42)
   */
  async reauthLane(laneId) {
    const res = await authFetch(`${API_BASE}/v1/lanes/${encodeURIComponent(laneId)}/reauth`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error?.message || data.error || `HTTP ${res.status}`);
    return data;
  },

  /**
   * Lấy logs của luồng chỉ định (T38)
   */
  async getLaneLogs(laneId, limit = 60) {
    const res = await authFetch(`${API_BASE}/v1/lanes/${encodeURIComponent(laneId)}/logs?limit=${limit}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  /**
   * Lấy nhật ký công việc của luồng chỉ định (T38)
   */
  async getLaneActivity(laneId, limit = 50) {
    const res = await authFetch(`${API_BASE}/v1/lanes/${encodeURIComponent(laneId)}/activity?limit=${limit}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  /**
   * Lấy danh sách model khả dụng của luồng chỉ định (T38)
   */
  async getLaneModels(laneId) {
    const res = await authFetch(`${API_BASE}/v1/lanes/${encodeURIComponent(laneId)}/models`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  /**
   * Chuyển model của luồng chỉ định (T38)
   */
  async switchLaneModel(laneId, modelName, keepChat = true) {
    const res = await authFetch(`${API_BASE}/v1/lanes/${encodeURIComponent(laneId)}/switch-model`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: modelName, keep_chat: keepChat }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error?.message || data.error || `HTTP ${res.status}`);
    return data;
  },

  /**
   * Reset session chat của luồng chỉ định (T38)
   */
  async resetLaneSession(laneId) {
    const res = await authFetch(`${API_BASE}/v1/lanes/${encodeURIComponent(laneId)}/reset`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error?.message || data.error || `HTTP ${res.status}`);
    return data;
  },

  /**
   * Xóa cache của luồng chỉ định (T38)
   */
  async clearLaneCache(laneId) {
    const res = await authFetch(`${API_BASE}/v1/lanes/${encodeURIComponent(laneId)}/cache/clear`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error?.message || data.error || `HTTP ${res.status}`);
    return data;
  },

  /**
   * Nạp cookie cho slot tài khoản của luồng chỉ định (T39)
   */
  async updateLaneCookies(laneId, slot, psid, psidts, label = "") {
    const res = await authFetch(`${API_BASE}/v1/lanes/${encodeURIComponent(laneId)}/accounts/${slot}/cookies`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ psid, psidts, label }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error?.message || data.error || `HTTP ${res.status}`);
    return data;
  },

  /**
   * Tắt Cổng Frontend 10000
   */
  async shutdownGateway() {
    const res = await authFetch(`${API_BASE}/v1/gateway/shutdown`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error?.message || data.error || `HTTP ${res.status}`);
    return data;
  },

  /**
   * Thử bật lại Cổng 10000 thông qua các luồng worker
   */
  async spawnGateway() {
    const headers = { "Content-Type": "application/json" };
    const key = getStoredApiKey();
    if (key) headers["Authorization"] = `Bearer ${key}`;
    for (const port of [10001, 10002]) {
      try {
        const res = await fetch(`http://127.0.0.1:${port}/v1/gateway/spawn`, {
          method: "POST",
          headers,
        });
        if (res.ok) {
          return await res.json();
        }
      } catch (e) {
        // thử cổng tiếp theo
      }
    }
    throw new Error("Không thể kết nối tới các luồng để khởi chạy lại Cổng 10000.");
  },

  /**
   * Profile Vault & Auto Cookie Sync (loopback-only)
   */
  async listProfiles() {
    const res = await authFetch(`${API_BASE}/v1/profiles`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(extractErrorMessage(data, `HTTP ${res.status}`));
    return data;
  },

  async loginProfile(profileId, { laneId = "", slot = 0, label = "" } = {}) {
    const res = await authFetch(`${API_BASE}/v1/profiles/${encodeURIComponent(profileId)}/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lane_id: laneId, slot, label }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(extractErrorMessage(data, `HTTP ${res.status}`));
    return data;
  },

  async finishProfileLogin(profileId) {
    const res = await authFetch(`${API_BASE}/v1/profiles/${encodeURIComponent(profileId)}/finish-login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(extractErrorMessage(data, `HTTP ${res.status}`));
    return data;
  },

  async syncProfile(profileId, { laneId, slot, label } = {}) {
    const body = {};
    if (laneId !== undefined) body.lane_id = laneId;
    if (slot !== undefined) body.slot = slot;
    if (label !== undefined) body.label = label;
    const res = await authFetch(`${API_BASE}/v1/profiles/${encodeURIComponent(profileId)}/sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(extractErrorMessage(data, `HTTP ${res.status}`));
    return data;
  },

  async syncAllProfiles(laneId = "") {
    const res = await authFetch(`${API_BASE}/v1/profiles/sync-all`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(laneId ? { lane_id: laneId } : {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(extractErrorMessage(data, `HTTP ${res.status}`));
    return data;
  },

  async deleteProfile(profileId) {
    const res = await authFetch(`${API_BASE}/v1/profiles/${encodeURIComponent(profileId)}`, {
      method: "DELETE",
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(extractErrorMessage(data, `HTTP ${res.status}`));
    return data;
  },
};

if (typeof window !== "undefined") {
  window.api = api;
}

