/**
 * Reactive State Store & Event Bus
 * Pure ES6+ PubSub Pattern for Component Decoupling
 */

const savedTheme = localStorage.getItem("bridge_theme") || "dark";
const savedLang = localStorage.getItem("bridge_lang") || "vi";

// Áp dụng theme lên documentElement ngay lập tức để tránh chớp trắng
document.documentElement.setAttribute("data-theme", savedTheme);

class Store extends EventTarget {
  constructor() {
    super();
    this.state = {
      lang: savedLang,
      theme: savedTheme,
      model: "unspecified",
      availableModels: [],
      modelDetails: [],
      turnCount: 0,
      totalRequests: 0,
      totalErrors: 0,
      errorRate: "0.0%",
      uptime: "0h 0m 0s",
      cacheEntries: 0,
      usingBackup: false,
      sessionInitialized: false,
      latency: 0,
      rawLogs: [],
      filterLevel: "ALL",
      searchQuery: "",
      autoScroll: true,
    };
  }

  getState() {
    return { ...this.state };
  }

  setState(partial) {
    const prevState = { ...this.state };
    this.state = { ...this.state, ...partial };

    if (partial.theme && partial.theme !== prevState.theme) {
      localStorage.setItem("bridge_theme", partial.theme);
      document.documentElement.setAttribute("data-theme", partial.theme);
    }

    if (partial.lang && partial.lang !== prevState.lang) {
      localStorage.setItem("bridge_lang", partial.lang);
    }

    this.dispatchEvent(new CustomEvent("statechange", { detail: { prevState, state: this.state } }));
  }

  subscribe(callback) {
    const handler = (e) => callback(e.detail.state, e.detail.prevState);
    this.addEventListener("statechange", handler);
    return () => this.removeEventListener("statechange", handler);
  }

  notifyToast(message, type = "info") {
    this.dispatchEvent(new CustomEvent("toast", { detail: { message, type } }));
  }

  onToast(callback) {
    const handler = (e) => callback(e.detail.message, e.detail.type);
    this.addEventListener("toast", handler);
    return () => this.removeEventListener("toast", handler);
  }
}

export const store = new Store();
