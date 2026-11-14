/**
 * API Service Layer — Handles all HTTP communication with FastAPI Backend
 */

const API_BASE = window.location.origin;

export const api = {
  /**
   * Lấy tình trạng sức khỏe, metrics và models hiện có
   */
  async getHealth() {
    const startTime = performance.now();
    const res = await fetch(`${API_BASE}/health`);
    const latency = Math.round(performance.now() - startTime);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    return { ...data, latency };
  },

  /**
   * Lấy các dòng nhật ký mới nhất
   */
  async getLogs(lines = 60) {
    const res = await fetch(`${API_BASE}/v1/logs?lines=${lines}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  },

  /**
   * Chuyển đổi Model Gemini
   */
  async switchModel(modelName, keepChat = true) {
    const res = await fetch(`${API_BASE}/v1/switch-model`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: modelName, keep_chat: keepChat }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Không thể chuyển model");
    return data;
  },

  /**
   * Cập nhật Cookie (Hot-Reload)
   */
  async updateCookies(psid, psidts) {
    const res = await fetch(`${API_BASE}/v1/update-cookies`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ SECURE_1PSID: psid, SECURE_1PSIDTS: psidts }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Cập nhật cookie thất bại");
    return data;
  },

  /**
   * Reset Chat Session
   */
  async resetSession() {
    const res = await fetch(`${API_BASE}/v1/reset`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Reset session thất bại");
    return data;
  },

  /**
   * Xóa bộ nhớ đệm Cache
   */
  async clearCache() {
    const res = await fetch(`${API_BASE}/v1/cache/clear`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Xóa cache thất bại");
    return data;
  },
};
