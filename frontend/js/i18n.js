/**
 * Internationalization (i18n) Dictionary — English & Vietnamese
 */

export const translations = {
  vi: {
    brandSubtitle: "Cổng Proxy Tương Thích OpenAI & Bộ Điều Phối Coding Agent",
    versionTag: "v3.0 Doanh Nghiệp",
    offline: "Mất kết nối",
    refreshData: "Làm mới dữ liệu",
    refreshing: "Đang làm mới dữ liệu...",
    
    // Metrics
    activeModel: "Model Đang Chạy",
    sessionReady: "Phiên Sẵn Sàng",
    turn: "Lượt",
    requestsAndHealth: "Yêu Cầu & Sức Khỏe",
    reqs: "lượt",
    errorRate: "Lỗi",
    failures: "thất bại",
    uptimeAndSession: "Thời Gian Chạy & Phiên",
    statefulDelta: "Gia Tăng Ngữ Cảnh",
    primaryCookie: "Cookie Chính",
    backupCookie: "⚠️ Cookie Dự Phòng",
    promptCache: "Bộ Nhớ Đệm Prompt",
    cached: "đã lưu",
    ttlNote: "TTL: 5 Phút",
    latencySaved: "Tiết kiệm ~95% trễ",

    // Terminal
    liveLogsConsole: "Bảng Nhật Ký Trực Tiếp",
    copyLogsTitle: "Sao chép toàn bộ Logs",
    clearConsoleTitle: "Xóa màn hình console",
    filterAll: "TẤT CẢ",
    searchPlaceholder: "Lọc nhật ký... (nhấn /)",
    autoScroll: "Tự động cuộn",
    noLogsFound: "Không tìm thấy nhật ký phù hợp với bộ lọc.",
    connectingLogs: "Đang kết nối tới logs/bridge.log...",
    logsCopied: "Đã sao chép toàn bộ logs vào Clipboard!",
    noLogsToCopy: "Không có nhật ký nào để sao chép!",
    consoleCleared: "Đã xóa màn hình console!",

    // Model Switcher
    switchModelTitle: "Chuyển Đổi Model",
    runtimeSwitchTag: "Đổi Tức Thì",
    availableModelsLabel: "Model khả dụng:",
    applyModelBtn: "Áp Dụng Model Ngay",
    switching: "Đang chuyển đổi...",
    modelSwitched: "Đã chuyển sang model:",
    selectValidModel: "Vui lòng chọn model hợp lệ!",

    // Cookie Manager
    cookieManagerTitle: "Cập Nhật Cookie Gemini Web",
    hotReloadTag: "Nạp Nóng",
    psidLabel: "SECURE_1PSID:",
    psidHint: "Token phiên chính",
    psidtsLabel: "SECURE_1PSIDTS:",
    psidtsHint: "Timestamp token",
    toggleVisibility: "Ẩn/hiện mật khẩu",
    saveCookiesBtn: "Lưu & Khởi Tạo Lại Client",
    verifyingCookies: "Đang kiểm tra & nạp cookie...",
    cookiesUpdatedSuccess: "✅ Đã cập nhật Cookie và tái kết nối client thành công!",
    enterPsidRequired: "Vui lòng nhập SECURE_1PSID!",

    // Quick Actions
    quickActionsTitle: "Thao Tác Bảo Trì",
    resetSessionBtn: "Reset Session",
    resetSessionTitle: "Đóng đoạn chat hiện tại trên Gemini Web và mở đoạn chat mới",
    clearCacheBtn: "Xóa Cache",
    clearCacheTitle: "Dọn dẹp các câu trả lời đang lưu trong cache",
    confirmResetSession: "⚠️ Bạn có chắc muốn đóng đoạn chat hiện tại trên Gemini Web và tạo đoạn chat mới?",
    sessionResetSuccess: "Đã khởi tạo đoạn ChatSession mới!",
    cacheClearedSuccess: "Đã xóa {count} mục trong bộ nhớ cache!",

    // Theme & Lang
    themeLight: "Chế độ Sáng",
    themeDark: "Chế độ Tối",
    langVi: "Tiếng Việt",
    langEn: "English",
  },

  en: {
    brandSubtitle: "OpenAI-Compatible Reverse Proxy & Agent Execution Engine",
    versionTag: "v3.0 Enterprise",
    offline: "Offline",
    refreshData: "Refresh Data",
    refreshing: "Refreshing telemetry data...",

    // Metrics
    activeModel: "Active Model",
    sessionReady: "Session Ready",
    turn: "Turn",
    requestsAndHealth: "Requests & Health",
    reqs: "reqs",
    errorRate: "Errors",
    failures: "failed",
    uptimeAndSession: "Uptime & Session",
    statefulDelta: "Stateful Delta",
    primaryCookie: "Primary Cookie",
    backupCookie: "⚠️ Backup Cookie",
    promptCache: "Prompt Cache",
    cached: "cached",
    ttlNote: "TTL: 5 Mins",
    latencySaved: "Saves ~95% latency",

    // Terminal
    liveLogsConsole: "Live Logs Console",
    copyLogsTitle: "Copy all logs",
    clearConsoleTitle: "Clear terminal console",
    filterAll: "ALL",
    searchPlaceholder: "Filter logs... (press /)",
    autoScroll: "Auto-scroll",
    noLogsFound: "No logs matching current filter.",
    connectingLogs: "Connecting to logs/bridge.log...",
    logsCopied: "All logs copied to clipboard!",
    noLogsToCopy: "No logs available to copy!",
    consoleCleared: "Terminal display cleared!",

    // Model Switcher
    switchModelTitle: "Switch Gemini Model",
    runtimeSwitchTag: "Runtime Switch",
    availableModelsLabel: "Available models:",
    applyModelBtn: "Apply Model Now",
    switching: "Switching model...",
    modelSwitched: "Successfully switched to model:",
    selectValidModel: "Please select a valid model!",

    // Cookie Manager
    cookieManagerTitle: "Update Gemini Web Cookies",
    hotReloadTag: "Hot-Reload",
    psidLabel: "SECURE_1PSID:",
    psidHint: "Primary session token",
    psidtsLabel: "SECURE_1PSIDTS:",
    psidtsHint: "Timestamp token",
    toggleVisibility: "Toggle password visibility",
    saveCookiesBtn: "Save & Reconnect Client",
    verifyingCookies: "Verifying & reloading cookies...",
    cookiesUpdatedSuccess: "✅ Cookies updated and client reconnected successfully!",
    enterPsidRequired: "SECURE_1PSID is required!",

    // Quick Actions
    quickActionsTitle: "Maintenance Operations",
    resetSessionBtn: "Reset Session",
    resetSessionTitle: "Close current chat session on Gemini Web and open a clean one",
    clearCacheBtn: "Clear Cache",
    clearCacheTitle: "Invalidate all cached prompt responses",
    confirmResetSession: "⚠️ Are you sure you want to close the current chat session and start a new one?",
    sessionResetSuccess: "New ChatSession initialized successfully!",
    cacheClearedSuccess: "Cleared {count} cache entries!",

    // Theme & Lang
    themeLight: "Light Mode",
    themeDark: "Dark Mode",
    langVi: "Tiếng Việt",
    langEn: "English",
  },
};

export function t(key, lang = "vi", params = {}) {
  const currentLang = translations[lang] || translations.vi;
  let text = currentLang[key] || translations.en[key] || key;
  for (const [k, v] of Object.entries(params)) {
    text = text.replace(new RegExp(`\\{${k}\\}`, "g"), v);
  }
  return text;
}
