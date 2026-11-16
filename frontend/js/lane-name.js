/**
 * Pure helper for formatting Lane Display Names (T40D)
 * Đảm bảo không bị lặp tên, không lặp default, và tuân theo bảng kiểm của chủ dự án.
 */

/**
 * name = label nếu label không rỗng và KHÁC laneId; ngược lại name = laneId; nếu name == "default" thì name = "".
 * kết quả = "Luồng " + n + (name ? " · " + name : "")           (EN: "Flow " + n + ...)
 */
export function displayName(laneId, label, n = 1, lang = "vi") {
  const cleanId = (laneId || "").trim();
  const cleanLabel = (label || "").trim();

  let name = "";
  if (cleanLabel && cleanLabel !== cleanId) {
    name = cleanLabel;
  } else {
    name = cleanId;
  }

  if (name.toLowerCase() === "default") {
    name = "";
  }

  const prefix = lang === "en" ? `Flow ${n}` : `Luồng ${n}`;
  if (name.toLowerCase() === prefix.toLowerCase()) {
    name = cleanId;
  }
  return name ? `${prefix} · ${name}` : prefix;
}
