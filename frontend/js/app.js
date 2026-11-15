/**
 * Application Entrypoint — Coordinates Polling & Component Bootstrap (T40 Dual-Lane)
 */

import { api } from "./api.js";
import { store } from "./state.js";

// Import components to register W3C Custom Elements
import "./components/BridgeToast.js";
import "./components/BridgeNavbar.js";
import "./components/BridgeLanes.js";
import "./components/BridgeMetrics.js";
import "./components/BridgeTerminal.js";
import "./components/BridgeModelSwitch.js";
import "./components/BridgeCookieManager.js";
import "./components/BridgeQuickActions.js";

let pollTimeoutId = null;
let isSyncing = false;

async function syncData() {
  if (isSyncing) return;
  if (document.hidden) return; // Tạm dừng poll khi tab bị ẩn (T40.4)

  isSyncing = true;
  const startTime = performance.now();

  try {
    const lanesData = await api.getLanes();
    const latency = Math.round(performance.now() - startTime);

    const lanes = Array.isArray(lanesData.lanes) ? lanesData.lanes : [];
    const selfId = lanesData.self || (lanes[0] ? lanes[0].id : "acc1");

    let { selectedLane } = store.getState();
    const laneIds = lanes.map((l) => l.id);

    // Mặc định chọn luồng của chính tiến trình này nếu chưa có hoặc không hợp lệ
    if (!selectedLane || !laneIds.includes(selectedLane)) {
      selectedLane = laneIds.includes(selfId) ? selfId : (laneIds[0] || "acc1");
    }

    store.setState({
      lanes,
      selectedLane,
      latency,
      instanceId: selfId,
    });

    // Đồng bộ Logs cho luồng đang chọn
    try {
      const logData = await api.getLaneLogs(selectedLane, 60);
      store.setState({ rawLogs: logData.logs || [] });
    } catch (e) {
      console.warn(`Lỗi tải logs cho ${selectedLane}:`, e);
    }

    // Đồng bộ danh sách Models cho luồng đang chọn
    try {
      const modelData = await api.getLaneModels(selectedLane);
      const avail = modelData.available || [];
      store.setState({
        availableModels: avail.map((m) => (typeof m === "string" ? m : m.id || m.name)),
        modelDetails: avail,
        model: modelData.current || "unspecified",
      });
    } catch (e) {
      console.warn(`Lỗi tải models cho ${selectedLane}:`, e);
    }
  } catch (err) {
    store.setState({ latency: -1 });
  } finally {
    isSyncing = false;
    scheduleNextPoll();
  }
}

function scheduleNextPoll(delayMs = null) {
  if (pollTimeoutId) {
    clearTimeout(pollTimeoutId);
    pollTimeoutId = null;
  }

  if (delayMs !== null) {
    pollTimeoutId = setTimeout(syncData, delayMs);
    return;
  }

  // 1 giây nếu có luồng đang starting hoặc stopping; bình thường 3 giây
  const { lanes } = store.getState();
  const hasTransitioningLane = (lanes || []).some(
    (l) => l.control === "starting" || l.control === "stopping"
  );
  const interval = hasTransitioningLane ? 1000 : 3000;

  pollTimeoutId = setTimeout(syncData, interval);
}

function setupGlobalShortcuts() {
  window.addEventListener("refresh-request", () => {
    scheduleNextPoll(0);
  });

  // Tiếp tục polling khi người dùng mở lại tab
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      scheduleNextPoll(0);
    }
  });

  document.addEventListener("keydown", (e) => {
    // Focus search on '/'
    if (e.key === "/" && document.activeElement.tagName !== "INPUT") {
      const searchBox = document.querySelector("#t-search");
      if (searchBox) {
        e.preventDefault();
        searchBox.focus();
      }
    }
  });
}

// Bootstrap
document.addEventListener("DOMContentLoaded", () => {
  setupGlobalShortcuts();
  syncData();
});
