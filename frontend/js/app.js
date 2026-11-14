/**
 * Application Entrypoint — Coordinates Polling & Component Bootstrap
 */

import { api } from "./api.js";
import { store } from "./state.js";

// Import components to register W3C Custom Elements
import "./components/BridgeToast.js";
import "./components/BridgeNavbar.js";
import "./components/BridgeMetrics.js";
import "./components/BridgeTerminal.js";
import "./components/BridgeModelSwitch.js";
import "./components/BridgeCookieManager.js";
import "./components/BridgeQuickActions.js";

async function syncData() {
  try {
    const health = await api.getHealth();
    store.setState({
      model: health.model || "unspecified",
      availableModels: health.available_models || [],
      modelDetails: health.model_details || [],
      turnCount: health.turn_count || 0,
      totalRequests: health.total_requests || 0,
      totalErrors: health.total_errors || 0,
      errorRate: health.error_rate || "0.0%",
      uptime: health.uptime || "0h 0m 0s",
      cacheEntries: health.cache_entries || 0,
      usingBackup: Boolean(health.using_backup_cookie),
      sessionInitialized: Boolean(health.session_initialized),
      latency: health.latency || 0,
    });
  } catch (err) {
    store.setState({ latency: -1 });
  }

  try {
    const logData = await api.getLogs(60);
    store.setState({ rawLogs: logData.logs || [] });
  } catch (err) {
    console.warn("Lỗi tải logs:", err);
  }
}

function setupGlobalShortcuts() {
  window.addEventListener("refresh-request", () => {
    syncData();
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

  // Polling chu kỳ 3.5 giây
  setInterval(syncData, 3500);
});
