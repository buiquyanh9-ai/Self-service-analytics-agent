/* Sola — trợ lý dữ liệu AI (vanilla JS + Chart.js)
 * Bố cục theo thiết kế "Mạch Analytics Workspace": thanh bên (hội thoại gần đây + công cụ), màn chào, mỗi câu hỏi là một
 * thẻ trả lời (diễn giải, KPI, biểu đồ/bảng, cách tính, hỏi tiếp). Các trang công cụ và quản trị ở tools.js. */
"use strict";

const S = { title: "", token: null, user: null, cfg: null, conv: null, convs: [], charts: [], busy: false, page: null, globe: null };
const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) { /* bỏ qua */ } },
};

// ------------------------------------------------------------------ tiện ích DOM
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const $ = (sel, root = document) => root.querySelector(sel);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function md(s) {
  let t = esc(s || "").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  let out = "", inList = false;
  for (const ln of t.split("\n")) {
    if (/^\s*- /.test(ln)) { if (!inList) { out += "<ul>"; inList = true; } out += "<li>" + ln.replace(/^\s*- /, "") + "</li>"; }
    else { if (inList) { out += "</ul>"; inList = false; } out += ln ? ln + "<br>" : "<br>"; }
  }
  if (inList) out += "</ul>";
  return out.replace(/(<br>)+$/, "");
}
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function toast(msg, ms = 2400) { const t = h("div", { class: "toast" }, msg); document.body.append(t); setTimeout(() => t.remove(), ms); }

// Biểu tượng (đường vẽ theo bộ lucide, 24×24, nét 2)
const P = {
  bars: '<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
  panel: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/><path d="m16 15-3-3 3-3"/>',
  plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  message: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M13 8H7"/><path d="M17 12H7"/>',
  book: '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',
  database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
  folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  list: '<path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M3 6h.01"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M8 6h13"/>',
  code: '<path d="m18 16 4-4-4-4"/><path d="m6 8-4 4 4 4"/><path d="m14.5 4-5 16"/>',
  history: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  layers: '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/>',
  target: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
  settings: '<path d="M20 7h-9"/><path d="M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>',
  table: '<path d="M12 3v18"/><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/>',
  zap: '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
  help: '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
  moon: '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
  menu: '<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/>',
  sparkles: '<path d="M9.94 15.5A2 2 0 0 0 8.5 14.06l-6.14-1.58a.5.5 0 0 1 0-.96L8.5 9.94A2 2 0 0 0 9.94 8.5l1.58-6.14a.5.5 0 0 1 .96 0L14.06 8.5A2 2 0 0 0 15.5 9.94l6.14 1.58a.5.5 0 0 1 0 .96L15.5 14.06a2 2 0 0 0-1.44 1.44l-1.58 6.14a.5.5 0 0 1-.96 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/>',
  arrowUp: '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
  arrowRight: '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
  arrowUpRight: '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
  arrowDownRight: '<path d="m7 7 10 10"/><path d="M17 7v10H7"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  fileCode: '<path d="M10 12.5 8 15l2 2.5"/><path d="m14 12.5 2 2.5-2 2.5"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  bulb: '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>',
  download: '<path d="M12 17V3"/><path d="m6 11 6 6 6-6"/><path d="M19 21H5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  thumbUp: '<path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/>',
  thumbDown: '<path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  lock: '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  ban: '<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>',
  pause: '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
  compass: '<circle cx="12" cy="12" r="10"/><path d="m16.24 7.76-2.12 6.36-6.36 2.12 2.12-6.36 6.36-2.12z"/>',
};
function ic(name, size = 16, sw = 2) {
  const s = h("span", { "aria-hidden": "true", style: { display: "inline-grid", flex: "none" } });
  s.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round">${P[name] || ""}</svg>`;
  return s;
}

// ------------------------------------------------------------------ API
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: Object.assign({ "Content-Type": "application/json" }, S.token ? { Authorization: "Bearer " + S.token } : {}),
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && path !== "/api/login") { logout(); throw new Error("Phiên đăng nhập đã hết hạn"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || ("Lỗi " + res.status));
  return data;
}

// ------------------------------------------------------------------ định dạng số (vi-VN)
const nf0 = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 1, minimumFractionDigits: 1 });
const nf2 = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
const nfa = new Intl.NumberFormat("vi-VN", { maximumFractionDigits: 2 });
function fmtCompact(x, f) {
  if (x == null || isNaN(x)) return "—";
  if (f === "currency") { const a = Math.abs(x); if (a >= 1e9) return nf2.format(x / 1e9) + " tỷ ₫"; if (a >= 1e6) return nf1.format(x / 1e6) + " tr ₫"; return nf0.format(x) + " ₫"; }
  if (f === "percent") return nf1.format(x) + "%";
  if (f === "ratio") return nf2.format(x);
  return Number.isInteger(x) ? nf0.format(x) : nfa.format(x);
}
function fmtAxis(x, f) {
  if (f === "currency") { const a = Math.abs(x); if (a >= 1e9) return nfa.format(x / 1e9) + " tỷ"; if (a >= 1e6) return nfa.format(x / 1e6) + " tr"; return nf0.format(x); }
  return f === "percent" ? nfa.format(x) + "%" : nfa.format(x);
}
function fmtCell(v, f) {
  if (v == null) return "—";
  if (typeof v !== "number") return String(v);
  if (f === "currency") return nf0.format(v);
  if (f === "percent") return nf2.format(v) + "%";
  if (f === "ratio") return nf2.format(v);
  return Number.isInteger(v) ? nf0.format(v) : nfa.format(v);
}
function fmtBytes(n) { n = Number(n || 0); const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return (i ? nf1.format(n) : nf0.format(n)) + " " + u[i]; }
const fmtDate = iso => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || ""); return m ? `${m[3]}/${m[2]}/${m[1]}` : (iso || ""); };
// "tháng M/YYYY" của tháng gần nhất có đủ dữ liệu (tháng chứa ngày mới nhất mà chưa hết tháng thì lùi một tháng),
// dùng cho các câu "Hỏi thử" để con số không bị thấp bất thường vì tháng mới có dữ liệu được vài ngày
function anchorMonth() {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(S.cfg.anchor || ""); if (!m) return "";
  let y = +m[1], mo = +m[2];
  if (+m[3] < new Date(y, mo, 0).getDate()) { mo -= 1; if (!mo) { mo = 12; y -= 1; } }
  return `tháng ${mo}/${y}`;
}
// Tên gọi trong lời chào: tên người thật (mọi chữ viết hoa đầu, 2–4 chữ) thì lấy chữ cuối ("Nguyễn Văn An" → "An");
// tên vai trò như "Quản trị dữ liệu", "Chuyên viên (toàn quốc)" thì gọi "bạn"
function callName(u) {
  const w = (u.full_name || "").trim().split(/\s+/).filter(Boolean);
  const person = w.length >= 2 && w.length <= 4 && w.every(x => /^\p{Lu}\p{Ll}*$/u.test(x));
  return person ? w[w.length - 1] : "bạn";
}
// Tên trợ lý AI (hồ sơ doanh nghiệp: assistant.name, mặc định "Sola") và khối thương hiệu ở góc trên trái
// Câu ví dụ của chính doanh nghiệp đang mở (câu đầu của nhóm "Tra cứu"), không viết cứng chỉ số của một ngành
function exampleQuestion() {
  const st = ((S.cfg || {}).profile || {}).starters || [];
  const g = st.find(x => x.key === "lookup") || st[0];
  return (g && g.questions[0]) || ((S.cfg || {}).samples || [])[0] || "";
}
const assistantName = () => "Sola";        // tên trợ lý AI cố định, không phụ thuộc hồ sơ doanh nghiệp
const brandCopy = () => h("div", { class: "brand-copy" }, h("strong", {}, "Sola"));
const initial = u => ((callName(u) === "bạn" ? (u.full_name || u.username || "?") : callName(u)).trim().charAt(0) || "?").toUpperCase();
const metricByName = n => (S.cfg.metrics || []).find(m => m.name === n);
const isDark = () => (document.documentElement.getAttribute("data-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
function norm(s) { return String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/g, "d"); }

// ------------------------------------------------------------------ biểu đồ & bảng
function renderChart(container, spec) {
  if (!spec || spec.type === "kpi") return;
  if (typeof Chart === "undefined") { container.append(h("div", { class: "note warn" }, ic("alert"), "Không tải được thư viện biểu đồ, hãy xem dạng bảng.")); return; }
  const colors = [1, 2, 3, 4, 5, 6, 7, 8].map(i => cssVar("--series-" + i));
  const n = spec.labels.length;
  const box = h("div", { class: "chart-box", style: { height: (spec.horizontal ? Math.max(240, Math.min(900, n * 28 + 70)) : 300) + "px" } });
  const cv = h("canvas", { role: "img", "aria-label": spec.title || "Biểu đồ" });
  box.append(cv);
  // ô đọc số liệu cố định ở đầu biểu đồ (thay cho hộp chú thích nổi đè lên các thanh khác)
  const hint = "Di chuột lên biểu đồ để xem số liệu";
  const readout = h("div", { class: "chart-readout", "aria-live": "polite" }, h("span", { class: "muted" }, hint));
  container.append(h("div", { class: "chart-heading" }, spec.title ? h("span", { class: "chart-label" }, spec.title) : h("span"), readout));
  container.append(box);
  const surface = cssVar("--surface"), grid = cssVar("--line-2"), muted = cssVar("--muted"), sec = cssVar("--text-2");
  const isLine = spec.type === "line", isPie = spec.type === "doughnut";
  const datasets = spec.datasets.map((d, i) => {
    const base = { label: d.label, data: d.data };
    if (isPie) return Object.assign(base, { backgroundColor: spec.labels.map((_, k) => colors[k % 8]), borderColor: surface, borderWidth: 2 });
    let col = colors[i % 8];
    if (d.role === "previous") col = cssVar("--series-muted");
    if (d.role === "current") col = colors[0];
    if (isLine) return Object.assign(base, { borderColor: col, backgroundColor: col, borderWidth: 2, pointRadius: n <= 24 ? 3 : 0, pointHoverRadius: 5, pointBorderColor: surface, pointBorderWidth: 2, tension: .25, spanGaps: true });
    const bg = d.role === "delta" ? d.data.map(v => (v >= 0 ? cssVar("--pos") : cssVar("--neg"))) : col;
    return Object.assign(base, { backgroundColor: bg, borderRadius: 4, borderSkipped: "start", maxBarThickness: 34, categoryPercentage: 0.72, barPercentage: 0.9, borderColor: surface, borderWidth: spec.stacked ? { top: 2 } : 0 });
  });
  const valAxis = { grid: { color: grid, drawTicks: false }, border: { display: false }, ticks: { color: muted, padding: 6, font: { size: 11 }, callback: v => fmtAxis(v, spec.format) }, stacked: !!spec.stacked, beginAtZero: !isLine };
  const catAxis = { grid: { display: false }, border: { color: grid }, ticks: { color: muted, autoSkip: true, maxRotation: 0, font: { size: 11 } }, stacked: !!spec.stacked };
  S.charts.push(new Chart(cv, {
    type: isPie ? "doughnut" : (isLine ? "line" : "bar"),
    data: { labels: spec.labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 300 },
      indexAxis: spec.horizontal ? "y" : "x", cutout: isPie ? "62%" : undefined,
      // cả hàng (ngang) / cả cột (dọc) là vùng chọn: rê chuột không cần trúng đúng thanh
      interaction: isPie ? { mode: "nearest", intersect: true } : { mode: "index", intersect: false, axis: spec.horizontal ? "y" : "x" },
      onHover: (e, els) => { e.native && (e.native.target.style.cursor = els.length ? "pointer" : "default"); },
      plugins: {
        legend: { display: isPie || datasets.length > 1, position: isPie ? "right" : "top", align: "start", labels: { color: sec, usePointStyle: true, pointStyle: "rectRounded", boxWidth: 9, boxHeight: 9, font: { size: 11.5 } } },
        tooltip: { enabled: false, external: ({ tooltip: t }) => {
          if (!t || t.opacity === 0 || !(t.dataPoints || []).length) { readout.replaceChildren(h("span", { class: "muted" }, hint)); return; }
          const pts = t.dataPoints;
          readout.replaceChildren(h("b", { class: "ro-title", title: pts[0].label }, pts[0].label), ...pts.map(c => h("span", { class: "ro-item" },
            h("i", { style: { background: Array.isArray(c.dataset.backgroundColor) ? c.dataset.backgroundColor[c.dataIndex] : (c.dataset.borderColor && isLine ? c.dataset.borderColor : c.dataset.backgroundColor) } }),
            (isPie ? "" : c.dataset.label + ": "), h("strong", {}, fmtCompact(isPie ? c.parsed : (spec.horizontal ? c.parsed.x : c.parsed.y), spec.format)))));
        } },
      },
      scales: isPie ? {} : (spec.horizontal ? { x: valAxis, y: catAxis } : { x: catAxis, y: valAxis }),
    },
  }));
}
const deltaEm = (pct, suffix) => pct == null ? null : h("em", { class: pct >= 0 ? "up" : "down" }, (pct >= 0 ? "↗ +" : "↘ ") + nf1.format(pct) + "%" + (suffix ? " " + suffix : ""));
function kpiTiles(spec) {
  return h("div", { class: "answer-kpis" }, spec.items.map(it => h("div", { class: "answer-kpi" },
    h("span", {}, it.label), h("strong", {}, fmtCompact(it.value, it.format)),
    it.delta_pct != null ? deltaEm(it.delta_pct, "so với " + (it.prev_label || "kỳ trước") + " (" + fmtCompact(it.prev, it.format) + ")") : null)));
}
function dataTable(columns, rows, meta, highlights) {
  const mm = {}; (meta || []).forEach(m => (mm[m.name] = m));
  const hl = {}; (highlights || []).forEach(x => (hl[x.row] = x.type === "down" ? "hl-down" : "hl-up"));
  return h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", {}, h("tr", {}, columns.map(c => h("th", { title: c, class: (mm[c] || {}).kind === "metric" ? "num" : "" }, (mm[c] && mm[c].label) || c)))),
    h("tbody", {}, rows.slice(0, 500).map((r, i) => h("tr", { class: hl[i] || "" }, r.map((v, j) => h("td", { class: typeof v === "number" ? "num" : (String(v ?? "").length <= 28 ? "nw" : "") }, fmtCell(v, (mm[columns[j]] || {}).format))))))));
}
function downloadCSV(columns, rows, name = "ket_qua.csv") {
  const q = v => (v == null ? "" : /[",\n;]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v));
  const a = h("a", { href: URL.createObjectURL(new Blob(["﻿" + [columns.map(q).join(","), ...rows.map(r => r.map(q).join(","))].join("\n")], { type: "text/csv" })), download: name });
  document.body.append(a); a.click(); a.remove();
}
function copyText(text, okMsg = "Đã sao chép") {
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast(okMsg), () => toast("Trình duyệt không cho sao chép"));
  else toast("Trình duyệt không hỗ trợ sao chép");
}
function codeBlock(text) {
  return h("div", { class: "code" }, h("button", { class: "btn sm copy", onclick: () => copyText(text) }, ic("copy", 13), "Sao chép"), h("pre", {}, text));
}
function sqlBlock(sql, title = "Truy vấn đã chạy", editable = false) {
  const body = editable ? h("textarea", { rows: Math.min(16, (sql || "").split("\n").length + 1), spellcheck: "false" }, sql || "") : h("pre", {}, sql || "");
  return h("div", { class: "sql-code-block" },
    h("div", { class: "code-title" }, h("span", {}, ic("fileCode", 14), title),
      h("span", { class: "row" }, h("span", { class: "read-only-pill" }, "CHỈ ĐỌC"),
        editable ? null : h("button", { class: "icon-button compact", title: "Sao chép SQL", "aria-label": "Sao chép SQL", onclick: () => copyText(sql) }, ic("copy", 14)))),
    body);
}

// ------------------------------------------------------------------ hộp thoại & ngăn bên
function modalCard(eyebrow, title, body, wide = false) {
  closeModal();
  const bg = h("div", { class: "modal-backdrop", id: "modal", onmousedown: e => { if (e.target === bg) closeModal(); } },
    h("section", { class: "modal-card" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true", "aria-label": title },
      h("div", { class: "modal-heading" }, h("div", {}, h("span", { class: "modal-eyebrow" }, eyebrow), h("h2", {}, title)),
        h("button", { class: "icon-button", "aria-label": "Đóng", onclick: closeModal }, ic("x", 18))),
      body));
  document.body.append(bg);
  return bg;
}
function closeModal() { const m = $("#modal"); m && m.remove(); }
function drawer(eyebrow, title, body) {
  closeDrawer();
  const bg = h("div", { class: "drawer-bg", id: "drawer-bg", onclick: closeDrawer });
  const d = h("aside", { class: "drawer", id: "drawer", role: "dialog", "aria-label": title },
    h("div", { class: "drawer-hd" }, h("div", {}, h("span", { class: "modal-eyebrow" }, eyebrow), h("h2", {}, title)),
      h("button", { class: "icon-button", "aria-label": "Đóng", onclick: closeDrawer }, ic("x", 18))),
    h("div", { class: "drawer-bd" }, body));
  document.body.append(bg, d);
  return d;
}
function closeDrawer() { ["#drawer", "#drawer-bg"].forEach(s => { const e = $(s); e && e.remove(); }); }
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { closeDrawer(); closeModal(); const m = $(".modal-bg"); m && m.remove(); closeMobile(); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && S.user) { e.preventDefault(); newConv(); }
});

// Từ điển chỉ số: các từ dễ nhầm lên đầu, sau đó toàn bộ chỉ số, có ô tìm
function openGlossary(focus) {
  const p = S.cfg.profile || {}, month = anchorMonth();
  const list = h("div", {});
  const tryChip = m => h("button", { class: "chip", onclick: () => { closeDrawer(); send(m.label + (month ? " " + month : "")); } }, ic("arrowRight", 13), "Hỏi thử: " + m.label + (month ? " " + month : ""));
  const termRow = m => h("div", { class: "term" + (m.name === focus ? " hit" : ""), id: "term-" + m.name },
    h("div", { class: "hd" }, h("b", {}, m.label), m.owner ? h("span", { class: "pill" }, m.owner) : null, h("span", { class: "pill ok" }, ic("check", 11), "Đã duyệt")),
    m.description ? h("div", { class: "desc" }, m.description) : null, tryChip(m));
  const draw = q => {
    const nq = norm(q);
    const all = (S.cfg.metrics || []).filter(m => !nq || norm(m.label + " " + m.name + " " + (m.description || "") + " " + (m.owner || "")).includes(nq));
    const conf = !nq ? (p.confusable_terms || []) : [];
    list.replaceChildren(...[      // replaceChildren không tự làm phẳng mảng: phải trải ra từng phần tử
      conf.map(t => [h("h4", {}, "Dễ nhầm: “" + t.term + "” có " + t.options.length + " cách tính"), compareCards(t, false)]),
      h("h4", {}, nq ? `Kết quả tìm (${all.length})` : `Tất cả chỉ số (${all.length})`),
      all.length ? all.map(termRow) : h("div", { class: "muted" }, "Không có chỉ số nào khớp."),
    ].flat(Infinity));
  };
  const search = h("input", { type: "search", placeholder: "Tìm chỉ số, ví dụ: lợi nhuận, trả hàng, tồn kho…", oninput: e => draw(e.target.value) });
  const d = drawer("Các định nghĩa đã duyệt", "Từ điển chỉ số", [h("p", { class: "sec small", style: { margin: "0 0 12px" } },
    "Mỗi chỉ số có một định nghĩa thống nhất trong công ty. Gõ đúng tên chỉ số trong câu hỏi thì trợ lý trả lời ngay, không phải hỏi lại."), search, h("div", { style: { marginTop: "6px" } }, list)]);
  draw("");
  if (focus) { const t = $("#term-" + focus, d); t && t.scrollIntoView({ block: "center" }); } else search.focus();
}

// Thẻ so sánh các cách tính của một từ dễ nhầm; thẻ đầu tiên là cách được khuyên dùng
function compareCards(t, withTry = true) {
  const month = anchorMonth();
  return h("div", { class: "compare" }, t.options.map((o, i) => h("div", { class: "cmp" + (i === 0 ? " rec" : "") },
    h("div", { class: "hd" }, h("b", {}, o.label), o.owner ? h("span", { class: "pill" }, o.owner) : null, i === 0 ? h("span", { class: "pill info" }, "Khuyên dùng") : null),
    o.hint ? h("div", { class: "hint" }, o.hint) : null,
    h("div", { class: "desc" }, o.description),
    withTry ? h("button", { class: "chip", onclick: () => send(o.label + (month ? " " + month : "")) }, "Hỏi thử: " + o.label.toLowerCase() + (month ? " " + month : "")) : null)));
}

// Cách tính & kiểm tra của một câu trả lời
const SRC = { semantic: "Theo định nghĩa chỉ số chung", "semantic+repair": "Theo định nghĩa chỉ số chung (đã tự sửa)", fewshot: "Theo truy vấn mẫu đã được xác nhận", lookup: "Đọc thẳng từ bảng danh mục", llm: "Trợ lý tự viết truy vấn", user: "Câu lệnh do người dùng viết", user_edit: "Câu lệnh đã được người dùng sửa" };
function openHow(r) {
  const v = r.validation || {};
  const defs = (r.definitions || []).length ? r.definitions : (r.plan && r.plan.metrics || []).map(metricByName).filter(Boolean);
  const checks = v.checks || [];
  const meta = (icn, k, val, cls) => h("div", {}, h("span", {}, ic(icn, 13), k), h("strong", { class: cls || "" }, val));
  modalCard("Trợ lý dữ liệu", "Cách tính câu trả lời này", [
    defs.map(d => h("div", { class: "metric-definition" }, h("div", { class: "definition-icon" }, ic("book")),
      h("div", {}, h("span", {}, "Định nghĩa được duyệt" + (d.owner ? " · " + d.owner : "")), h("strong", {}, d.label), h("p", {}, d.description || "Chưa có mô tả.")))),
    h("div", { class: "calc-meta-grid" },
      meta("database", "Nguồn truy vấn", SRC[r.sql_source] || "—"),
      meta("clock", "Thời gian trả lời", nf1.format((r.elapsed_ms || 0) / 1000) + " giây"),
      checks.length ? meta("shield", "Kiểm tra trước khi chạy", `${checks.filter(c => c.passed).length}/${checks.length} bước đạt`, checks.every(c => c.passed) ? "positive-text" : "") : null,
      r.cost && r.cost.human ? meta("zap", "Dữ liệu đã đọc", "Khoảng " + r.cost.human) : null),
    r.audit ? h("div", { class: "note " + (r.audit.ok ? "ok" : "bad") }, ic(r.audit.ok ? "shield" : "alert"), h("div", {}, r.audit.ok
      ? `Đã tính lại ${r.audit.facts_checked} con số trong lời giải thích từ bảng kết quả, tất cả đều khớp.`
      : "Có số chưa đối chiếu được: " + (r.audit.unverified_numbers || []).join(", ") + ". Hãy dựa vào bảng kết quả.")) : null,
    checks.length ? [h("h4", {}, "Các bước kiểm tra"), h("ul", { class: "checks" }, checks.map(c => h("li", {}, h("span", { class: c.passed ? "ok-ic" : "bad-ic" }, ic(c.passed ? "check" : "x", 14)), h("span", {}, c.name), h("span", { class: "muted small" }, c.detail || ""))))] : null,
    r.sql ? [h("h4", {}, "Câu lệnh SQL"), sqlBlock(r.sql)] : null,
    r.variance && (r.variance.queries || []).length ? [h("h4", {}, `Các truy vấn đã chạy (${r.variance.queries.length})`), r.variance.queries.map(q => h("details", { style: { marginTop: "6px" } }, h("summary", {}, q.purpose, h("span", { class: "muted small" }, " · " + fmtBytes(q.bytes) + " · " + q.rows + " dòng")), codeBlock(q.sql)))] : null,
    h("h4", {}, "Chi tiết kỹ thuật"), h("div", { class: "tech" }, techDetails(r)),
  ], true);
}
function openSql(r) {
  const v = r.validation || {};
  modalCard("Bảo mật là ưu tiên", "Truy vấn SQL", [
    h("div", { class: "safety-banner" }, ic("shield"), h("span", {}, "Truy vấn chạy ở chế độ chỉ đọc, đã qua kiểm tra quyền truy cập" + ((v.rls_applied || []).length ? " và lọc theo phạm vi dữ liệu của bạn" : "") + ".")),
    sqlBlock(r.sql),
    h("div", { class: "review-details" },
      (v.tables || []).length ? h("span", {}, ic("database", 13), v.tables.join(", ")) : null,
      r.cost && r.cost.human ? h("span", {}, ic("zap", 13), "Đọc khoảng " + r.cost.human) : null,
      (v.checks || []).length ? h("span", {}, ic("shield", 13), `${v.checks.filter(c => c.passed).length}/${v.checks.length} bước kiểm tra đạt`) : null),
    h("div", { class: "modal-actions" }, h("button", { class: "secondary-button", onclick: () => { closeModal(); openHow(r); } }, ic("help", 14), "Xem cách tính"),
      h("button", { class: "primary-button", onclick: () => copyText(r.sql, "Đã sao chép SQL") }, ic("copy", 14), "Sao chép SQL")),
  ], true);
}

// ------------------------------------------------------------------ các phần của câu trả lời
const HEAD = {
  result: ["sparkles", "Phân tích dữ liệu", ""], variance: ["compass", "Phân tích biến động", ""], multi: ["sparkles", "Câu hỏi nhiều ý", ""],
  clarification: ["help", "Cần bạn chọn", "warn"], pending_approval: ["pause", "Chờ bạn duyệt", "warn"], error: ["alert", "Chưa trả lời được", "bad"],
  blocked: ["ban", "Không thể thực hiện", "bad"], info: ["info", "Thông tin", ""], rejected: ["x", "Đã hủy", ""],
};
function titleRow(r) {
  const [icn, label, cls] = HEAD[r.type] || HEAD.result;
  return h("div", { class: "answer-title-row" }, h("div", { class: "answer-mark " + cls }, ic(icn, 14)), h("span", { class: "eyebrow" }, "Sola · " + label),
    r.elapsed_ms ? h("span", { class: "answer-time" }, nf1.format(r.elapsed_ms / 1000) + " giây") : null);
}
function understood(r) {
  const p = r.plan; if (!p || !(p.metrics || []).length) return null;
  const dl = Object.fromEntries((S.cfg.dimensions || []).map(d => [d.name, d.label]));
  const span = t => t.label || ((t.start || "…") + " đến trước " + (t.end || "…"));
  const chips = p.metrics.map(m => { const mm = metricByName(m);
    return h("button", { class: "u-chip", title: "Xem định nghĩa", onclick: () => openGlossary(m) }, h("span", { class: "k" }, "Chỉ số"), mm ? mm.label : m, h("span", { class: "i" }, ic("info", 12))); });
  const kv = (k, v) => h("span", { class: "u-chip" }, h("span", { class: "k" }, k), v);
  if ((p.dimensions || []).length) chips.push(kv("Chia theo", p.dimensions.map(d => dl[d] || d).join(", ")));
  (p.filters || []).forEach(f => chips.push(kv(dl[f.dimension] || f.dimension, (f.op === "not_in" ? "khác " : f.op === "like" ? "có chứa " : "") + (f.values || []).join(", "))));
  if (p.time_range && (p.time_range.label || p.time_range.start)) chips.push(kv("Thời gian", span(p.time_range)));
  const cmp = p.compare || p.variance_compare; if (cmp) chips.push(kv("So với", span(cmp)));
  if (p.limit) chips.push(kv("Lấy", p.limit + " dòng đầu"));
  return h("div", { class: "understood" }, h("span", { class: "lb" }, "Mình hiểu là"), chips);
}
function extraNotes(r) {
  const out = [];
  if (r.llm_error) out.push(h("div", { class: "insight-note warn" }, ic("alert"), h("div", {}, h("strong", {}, "Mô hình ngôn ngữ tạm thời không dùng được. "), "Câu hỏi nêu rõ chỉ số và thời gian vẫn được trả lời; câu khác bạn thử lại sau ít phút.")));
  if ((r.assumptions || []).length) out.push(h("div", { class: "insight-note" }, ic("bulb"), h("div", {}, h("strong", {}, "Mình đã tự hiểu thêm"), h("ul", {}, r.assumptions.map(a => h("li", {}, a))))));
  const notes = r.notes || [], lint = (r.lint || []).filter(l => l.severity !== "info");
  if (notes.length || lint.length) out.push(h("div", { class: "insight-note" + (lint.length ? " warn" : "") }, ic(lint.length ? "alert" : "info"),
    h("div", {}, h("strong", {}, "Lưu ý khi đọc kết quả"), h("ul", {}, lint.map(l => h("li", {}, l.message)), notes.map(a => h("li", {}, a))))));
  return out;
}
function trustPills(r) {
  const it = [];
  const s = r.sql_source;
  if (s === "semantic" || s === "semantic+repair" || s === "fewshot") it.push(["info", "book", "Định nghĩa chuẩn của công ty"]);
  else if (s === "lookup") it.push(["info", "database", "Từ bảng danh mục"]);
  else if (s === "llm") it.push(["warn", "alert", "Trợ lý tự viết truy vấn, nên xem cách tính"]);
  if (r.validation && (r.validation.rls_applied || []).length) it.push(["", "lock", "Trong phạm vi bạn được xem"]);
  if (r.truncated) it.push(["warn", "table", "Bảng đã cắt bớt"]);
  return it.length ? h("div", { class: "trust-row" }, it.map(([c, icn, t]) => h("span", { class: "pill " + c }, ic(icn, 12), t))) : null;
}
function followUps(r) {
  if (!(r.follow_ups || []).length) return null;
  return h("div", { class: "followup-area" }, h("span", { class: "followup-label" }, "Bạn có thể hỏi tiếp"),
    h("div", { class: "followup-chips" }, r.follow_ups.map(s => h("button", { onclick: () => send(s) }, s, ic("arrowUpRight", 13)))));
}
function answerActions(r) {
  const row = h("div", { class: "answer-actions" });
  if (r.sql || r.plan || r.validation) row.append(h("button", { class: "text-action", onclick: () => openHow(r) }, ic("help", 15), "Cách tính"));
  if (r.sql) row.append(h("button", { class: "text-action", onclick: () => openSql(r) }, ic("fileCode", 15), "Xem SQL"));
  if (r.text) row.append(h("button", { class: "text-action", onclick: () => copyText(String(r.text).replace(/\*\*/g, "")) }, ic("copy", 15), "Sao chép"));
  row.append(h("span", { class: "answer-actions-spacer" }));
  if (r.audit) row.append(h("span", { class: "verified-label " + (r.audit.ok ? "" : "bad") }, ic(r.audit.ok ? "shield" : "alert", 14),
    r.audit.ok ? `Đã đối chiếu ${r.audit.facts_checked || 0} số liệu` : "Có số chưa đối chiếu được"));
  if (r.run_id && r.sql) {
    const fb = h("span", { class: "feedback" });
    const done = msg => fb.replaceChildren(h("span", { class: "thanks" }, msg));
    fb.append(
      h("button", { class: "icon-button compact", title: "Kết quả đúng", "aria-label": "Kết quả đúng", onclick: async () => {
        try { await api("/api/feedback", { body: { run_id: r.run_id, rating: 1 } }); done("Cảm ơn bạn!"); } catch (e) { toast(e.message); } } }, ic("thumbUp", 14)),
      h("button", { class: "icon-button compact", title: "Kết quả chưa đúng", "aria-label": "Kết quả chưa đúng", onclick: () => feedbackModal(r, () => done("Đã ghi nhận góp ý.")) }, ic("thumbDown", 14)));
    row.append(fb);
  }
  return row;
}
function sourceLink(r) {
  const t = (r.validation || {}).tables || [];
  return t.length ? h("button", { class: "source-link", onclick: () => openHow(r) }, ic("database", 13), t.length + " bảng dữ liệu", ic("chevronDown", 12)) : null;
}
function feedbackModal(r, done) {
  const cm = h("textarea", { rows: 3, placeholder: "Sai ở đâu? Ví dụ: số này phải tính cả đơn đang giao…" });
  modalCard("Giúp trợ lý tốt hơn", "Góp ý cho câu trả lời", [cm,
    h("div", { class: "modal-actions" }, h("button", { class: "secondary-button", onclick: closeModal }, "Hủy"),
      h("button", { class: "primary-button", onclick: async () => {
        try { await api("/api/feedback", { body: { run_id: r.run_id, rating: -1, comment: cm.value } }); closeModal(); done(); } catch (e) { toast(e.message); } } }, ic("check", 14), "Gửi góp ý"))]);
  cm.focus();
}

function resultPanel(r, spec, extraTable) {
  const hasRows = r.columns && r.rows && r.rows.length;
  if (!spec && !hasRows) return null;
  const pane = h("div", {});
  const tabsEl = h("div", { class: "result-tabs", role: "tablist", "aria-label": "Kiểu hiển thị dữ liệu" });
  const show = m => {
    [...tabsEl.children].forEach(b => { b.classList.toggle("selected", b.dataset.m === m); b.setAttribute("aria-selected", b.dataset.m === m); });
    pane.replaceChildren();
    if (m === "chart") { const a = h("div", { class: "chart-area" }); pane.append(a); renderChart(a, spec); }
    else pane.append(extraTable || dataTable(r.columns, r.rows, r.columns_meta, spec && spec.highlights));
  };
  if (spec) tabsEl.append(h("button", { "data-m": "chart", role: "tab", onclick: () => show("chart") }, ic("bars", 14), "Biểu đồ"));
  if (hasRows || extraTable) tabsEl.append(h("button", { "data-m": "table", role: "tab", onclick: () => show("table") }, ic("table", 14),
    "Bảng dữ liệu" + (hasRows && r.rows.length > 1 ? " (" + nf0.format(r.row_count || r.rows.length) + " dòng)" : "")));
  const tools = h("div", { class: "result-tools" }, hasRows ? h("button", { class: "icon-button compact", title: "Tải dữ liệu CSV", "aria-label": "Tải dữ liệu CSV", onclick: () => downloadCSV(r.columns, r.rows) }, ic("download", 15)) : null);
  const panel = h("div", { class: "result-panel" }, h("div", { class: "result-toolbar" }, tabsEl, tools), pane);
  show(spec ? "chart" : "table");
  return panel;
}
function resultVisual(r) {
  const spec = r.chart;
  const hasRows = r.columns && r.rows && r.rows.length;
  if (spec && spec.type === "kpi") {
    return [kpiTiles(spec), hasRows && !(r.rows.length === 1 && r.columns.length <= 2) ? resultPanel(r, null) : null];
  }
  return resultPanel(r, spec);
}
function varianceVisual(r) {
  const v = r.variance, f = v.format, t = v.total;
  const out = [h("div", { class: "answer-kpis" },
    h("div", { class: "answer-kpi" }, h("span", {}, v.period_now.label), h("strong", {}, fmtCompact(t.now, f))),
    h("div", { class: "answer-kpi" }, h("span", {}, v.period_prev.label), h("strong", {}, fmtCompact(t.prev, f))),
    h("div", { class: "answer-kpi" }, h("span", {}, "Chênh lệch"), h("strong", {}, (t.delta >= 0 ? "+" : "") + fmtCompact(t.delta, f)), deltaEm(t.pct)),
    v.significance ? h("div", { class: "answer-kpi", title: "So với các lần biến động trước của chính chỉ số này" }, h("span", {}, "So với dao động thường thấy"),
      h("strong", {}, v.significance.significant ? "Bất thường" : "Bình thường"), h("em", {}, "dựa trên " + v.significance.n_history + " kỳ trước")) : null)];
  const causes = (v.root_causes || []).filter(c => c.kind !== "significance");
  if (causes.length) out.push(h("div", { class: "chart-t" }, "Nguyên nhân tìm thấy"), causes.map(c => h("div", { class: "cause " + (c.severity || "") },
    h("b", {}, c.title), h("div", { class: "sec small" }, c.message), c.action ? h("div", { class: "act" }, "➜ " + c.action) : null)));
  // giả thuyết do mô hình nêu, code đã chạy truy vấn kiểm chứng (số liệu ở đây đều do code tính)
  const HS = { confirmed: ["ok", "Xác nhận"], weak: ["warn", "Yếu"], rejected: ["bad", "Bác bỏ"], untested: ["", "Chưa kiểm được"] };
  const hyps = (v.hypotheses || []).filter(x => HS[x.status]);
  if (hyps.length) out.push(h("div", { class: "chart-t" }, "Giả thuyết do AI nêu, đã kiểm bằng dữ liệu"),
    hyps.map(x => h("div", { class: "cause hyp " + x.status },
      h("div", { class: "hyp-hd" }, h("span", { class: "pill " + HS[x.status][0] }, HS[x.status][1]), h("b", {}, x.claim)),
      x.pct != null || x.now != null ? h("div", { class: "sec small" }, `${x.metric_label} ở ${x.segment_label}: ${fmtCompact(x.prev, x.format)} → ${fmtCompact(x.now, x.format)}` +
        (x.pct != null ? ` (${x.pct >= 0 ? "+" : ""}${nf1.format(x.pct)}%)` : "") + (x.share != null ? ` · giải thích ${nf1.format(x.share * 100)}% biến động` : "")) : null,
      h("div", { class: "muted small" }, x.reason))));
  const best = (v.drivers || []).find(d => d.dimension === v.best_dimension) || (v.drivers || [])[0];
  const contrib = best ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", {}, h("tr", {}, h("th", {}, best.label), h("th", { class: "num" }, v.period_now.label), h("th", { class: "num" }, v.period_prev.label), h("th", { class: "num" }, "Chênh lệch"), h("th", { class: "num" }, "Đóng góp"))),
    h("tbody", {}, best.items.map(i => h("tr", {}, h("td", {}, String(i.member)), h("td", { class: "num" }, fmtCell(i.now, f)), h("td", { class: "num" }, fmtCell(i.prev, f)),
      h("td", { class: "num" }, fmtCell(i.delta, f)), h("td", { class: "num" }, i.contribution_pct == null ? "—" : nf1.format(i.contribution_pct) + "%")))))) : null;
  if (best) out.push(h("div", { class: "chart-t" }, "Đóng góp vào biến động theo " + best.label.toLowerCase()));
  out.push(resultPanel({}, r.chart && r.chart.type !== "kpi" ? r.chart : null, contrib));
  return out;
}

// Phần thân câu trả lời (đặt trong .answer-content)
function answerBody(r) {
  const body = h("div", { class: "answer-body animate-rise" });
  const add = (...k) => body.append(...k.flat(Infinity).filter(x => x != null && x !== false));
  const T = r.type;
  add(titleRow(r));
  if (T === "multi") {          // câu nhiều ý: mỗi ý một khối trả lời đầy đủ
    add(h("div", { class: "answer-summary", html: md(r.text) }));
    (r.parts || []).forEach((p, i) => add(h("div", { class: "part" }, h("div", { class: "part-hd" }, `${i + 1}. ${p.task || p.question}`), answerBody(p))));
    add(followUps(r));
    return body;
  }
  if (T === "clarification") {
    const det = (r.clarification || {}).details || [];
    const opts = h("div", { class: "clarify-options" });
    ((r.clarification || {}).options || []).forEach((o, i) => {
      const d = det[i];
      const b = h("button", { class: "clarify-option" }, h("span", { class: "clarify-option-icon" }, ic("bars", 16)),
        h("span", {}, h("strong", {}, d ? d.label : o, d && d.owner ? h("span", { class: "pill" }, d.owner) : null),
          d && d.hint ? h("span", { class: "hint" }, d.hint) : null, d && d.description ? h("small", {}, d.description) : null), ic("arrowRight", 15));
      b.onclick = () => { [...opts.children].forEach(x => (x.disabled = true)); b.classList.add("chosen"); send(d ? d.label : o, { id: r.clarification.id, option: i }); };
      opts.append(b);
    });
    add(h("p", { class: "answer-summary" }, h("strong", {}, r.text)),
      h("p", { class: "clarify-intro" }, "Mỗi cách hiểu cho ra một con số khác nhau, nên mình hỏi lại thay vì tự đoán."), opts,
      h("div", { class: "clarify-memo" }, "Lựa chọn được ghi nhớ cho các câu sau trong cuộc hội thoại này."));
    return body;
  }
  if (T === "pending_approval") {
    const block = sqlBlock(r.sql, "Truy vấn sẽ chạy", true), ta = $("textarea", block);
    const msg = h("div", {});
    const v = r.validation || {};
    const act = async a => {
      [...btns.querySelectorAll("button")].forEach(b => (b.disabled = true));
      msg.replaceChildren(h("div", { class: "thinking", style: { marginTop: "12px" } }, h("div", { class: "spinner" }), a === "approve" ? "Đang kiểm tra lại và chạy…" : "Đang hủy…"));
      try {
        const res = await api("/api/approve", { body: { run_id: r.run_id, action: a, sql: ta.value } });
        body.replaceWith(answerBody(res));       // thay ngay trong thẻ: câu hỏi vẫn ở trên, kết quả hiện bên dưới
        if (res.conversation_id) S.conv = res.conversation_id;
        loadConvs();
      } catch (e) { msg.replaceChildren(h("div", { class: "note bad" }, ic("alert"), e.message)); [...btns.querySelectorAll("button")].forEach(b => (b.disabled = false)); }
    };
    const btns = h("div", { class: "modal-actions" },
      h("button", { class: "secondary-button", onclick: () => act("reject") }, ic("x", 14), "Không chạy"),
      h("button", { class: "primary-button", onclick: () => act("approve") }, ic("check", 14), "Duyệt & chạy truy vấn"));
    add(understood(r),
      h("p", { class: "answer-summary" }, "Câu hỏi này phải đọc khá nhiều dữ liệu" + (r.cost && r.cost.human ? " (khoảng " + r.cost.human + ")" : "") + ". Nếu phần “Mình hiểu là” đúng ý bạn, bấm ", h("strong", {}, "Duyệt & chạy"), "."),
      h("div", { class: "safety-banner" }, ic("shield"), h("span", {}, "Truy vấn ở chế độ chỉ đọc. Nếu bạn sửa câu lệnh, nó sẽ được kiểm tra lại quyền truy cập và chi phí trước khi chạy.")),
      block,
      h("div", { class: "review-details" },
        (v.tables || []).length ? h("span", {}, ic("database", 13), v.tables.join(", ")) : null,
        r.cost && r.cost.human ? h("span", {}, ic("zap", 13), "Đọc khoảng " + r.cost.human) : null,
        (v.checks || []).length ? h("span", {}, ic("shield", 13), `${v.checks.filter(c => c.passed).length}/${v.checks.length} bước kiểm tra đạt`) : null),
      extraNotes(r), btns, msg);
    return body;
  }
  if (T === "error" || T === "blocked") {
    add(h("div", { class: "error-state", style: { marginTop: "12px" } }, h("div", { class: "error-icon" + (T === "blocked" ? " blocked" : "") }, ic(T === "blocked" ? "ban" : "database", 18)),
      h("div", {}, h("strong", {}, T === "blocked" ? "Mình không thể làm yêu cầu này" : "Mình chưa trả lời được câu này"), h("p", {}, r.text || r.error),
        T === "blocked" ? h("p", {}, "Trợ lý chỉ đọc dữ liệu (không sửa, không xóa) và chỉ trong phạm vi tài khoản của bạn.") : null)),
      T === "error" && (S.cfg.samples || []).length ? h("div", { class: "followup-area" }, h("span", { class: "followup-label" }, "Thử một câu mẫu"),
        h("div", { class: "followup-chips" }, S.cfg.samples.slice(0, 3).map(s => h("button", { onclick: () => send(s) }, s, ic("arrowUpRight", 13))))) : null,
      extraNotes(r), r.sql ? h("div", { class: "answer-actions" }, h("button", { class: "text-action", onclick: () => openHow(r) }, ic("help", 15), "Xem chi tiết")) : null);
    return body;
  }
  if (T === "rejected") { add(h("p", { class: "answer-summary" }, "Đã hủy, truy vấn không được chạy.")); return body; }
  if (T === "info") {
    add(h("div", { class: "answer-summary", html: md(r.text) }));
    if (r.metric_cards) add(r.metric_cards.map(c => h("div", { class: "metric-definition" }, h("div", { class: "definition-icon" }, ic("book")),
      h("div", {}, h("span", {}, "Định nghĩa được duyệt" + (c.owner ? " · " + c.owner : "")), h("strong", {}, c.label),
        (metricByName(c.name) || {}).description ? h("p", {}, metricByName(c.name).description) : null,
        h("p", { class: "muted" }, "Chia được theo: " + (c.dimensions || []).join(", "))))));
    if (r.schema_tables) add(r.schema_tables.map(t => h("details", { class: "card fold", style: { marginTop: "8px" } }, h("summary", {}, h("b", { class: "mono" }, t.table), " · ", t.description),
      h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("tbody", {}, t.columns.map(c => h("tr", {}, h("td", { class: "mono" }, c.name), h("td", {}, c.description, c.restricted ? h("span", { class: "pill bad", style: { marginLeft: "6px" } }, ic("lock", 11), "hạn chế") : null)))))))));
    if (r.suggestions) add(h("div", { class: "followup-area" }, h("span", { class: "followup-label" }, "Bạn có thể hỏi"),
      h("div", { class: "followup-chips" }, r.suggestions.map(s => h("button", { onclick: () => send(s) }, s, ic("arrowUpRight", 13))))));
    return body;
  }
  // result / variance
  add(understood(r), h("div", { class: "answer-summary", html: md(r.text) }), extraNotes(r),
    T === "variance" ? varianceVisual(r) : resultVisual(r), trustPills(r), answerActions(r), followUps(r), sourceLink(r));
  return body;
}
// Một thẻ trả lời đầy đủ (dùng ở trang Tự viết SQL và khi không có câu hỏi đi kèm)
function renderAnswer(r) {
  return h("article", { class: "answer-card animate-rise" }, h("div", { class: "answer-content" }, answerBody(r)));
}
function userBubble(text) {
  return h("div", { class: "q-user animate-rise" }, h("div", { class: "q-text" }, text), h("span", { class: "avatar-user" }, initial(S.user)));
}
function loadingBody() {
  const steps = ["Đang đọc câu hỏi", "Đang chọn chỉ số và cách tính", "Đang viết truy vấn", "Đang kiểm tra quyền truy cập và chi phí", "Đang chạy trên kho dữ liệu", "Đang viết lời giải thích và đối chiếu số liệu"];
  const label = h("span", {}, steps[0] + "…");
  const bars = h("div", { class: "loading-steps" }, steps.map((_, i) => h("i", { class: i === 0 ? "on" : "" })));
  const el = h("div", { class: "answer-body" }, h("div", { class: "loading-card" }, h("div", { class: "loading-orb" }, ic("sparkles", 17)),
    h("div", {}, h("strong", {}, "Đang phân tích câu hỏi của bạn"), label), h("div", { class: "loading-bars" }, h("i"), h("i"), h("i"))), bars);
  let k = 0;
  el._timer = setInterval(() => { k = Math.min(k + 1, steps.length - 1); label.textContent = steps[k] + "…"; [...bars.children].forEach((b, i) => b.classList.toggle("on", i <= k)); }, S.cfg.mode === "none" ? 450 : 900);
  return el;
}

// ------------------------------------------------------------------ hội thoại
function scrollToEl(el) { const sc = $("#scroll"); if (sc && el) sc.scrollTop = Math.max(0, el.offsetTop - 16); }
function newCard() {
  const content = h("div", { class: "answer-content" });
  $("#thread").append(h("article", { class: "answer-card animate-rise" }, content));
  return content;
}
async function send(text, clarification = null) {
  if (S.busy || (!text && !clarification)) return;
  closeDrawer(); closeModal(); closePage();
  if (!$("#thread")) showConversation(text);
  const bubble = userBubble(text);
  $("#thread").append(bubble);
  const content = newCard();
  const wait = loadingBody(); content.append(wait);
  scrollToEl(bubble);
  S.busy = true; syncSend();
  try {
    if (!S.conv) { const c = await api("/api/conversations", { body: {} }); S.conv = c.id; store.set("v2_conv", S.conv); }
    const resp = await api("/api/chat", { body: { message: text, conversation_id: S.conv, clarification } });
    content.replaceChildren(answerBody(resp));
    if (resp.conversation_id) S.conv = resp.conversation_id;
    scrollToEl(bubble);
    loadConvs();
  } catch (e) {
    content.replaceChildren(h("div", { class: "answer-body" }, h("div", { class: "error-state" }, h("div", { class: "error-icon" }, ic("database", 18)),
      h("div", {}, h("strong", {}, "Chưa thể lấy dữ liệu"), h("p", {}, e.message),
        h("button", { class: "text-action", onclick: () => { content.parentElement.remove(); bubble.remove(); send(text, clarification); } }, "Thử lại", ic("arrowRight", 14))))));
  } finally { clearInterval(wait._timer); S.busy = false; syncSend(); }
}
async function loadConvs() {
  try { S.convs = await api("/api/conversations"); } catch (e) { return; }
  const list = $("#convlist"); if (!list) return;
  if (!S.convs.length) { list.replaceChildren(h("div", { class: "history-empty" }, "Chưa có cuộc hội thoại nào.")); return; }
  list.replaceChildren(...S.convs.map(c => h("div", { class: "history-item" + (c.id === S.conv && !S.page ? " history-active" : ""), role: "button", tabindex: "0",
    onclick: () => openConv(c.id), onkeydown: e => { if (e.key === "Enter") openConv(c.id); } },
    ic("message", 15), h("span", { class: "t", title: c.title }, c.title),
    h("button", { class: "x", title: "Xóa cuộc hội thoại", "aria-label": "Xóa cuộc hội thoại", onclick: async e => {
      e.stopPropagation(); if (!confirm("Xóa cuộc hội thoại “" + c.title + "”?")) return;
      await api("/api/conversations/" + c.id, { method: "DELETE" }); if (S.conv === c.id) newConv(); else loadConvs(); } }, ic("x", 13)))));
}
function destroyCharts() { S.charts.forEach(c => { try { c.destroy(); } catch (e) { /* bỏ qua */ } }); S.charts = []; }
function stopGlobe() { if (S.globe) { S.globe(); S.globe = null; } }
async function openConv(id) {
  closePage(); S.conv = id; store.set("v2_conv", id); closeMobile();
  let c; try { c = await api("/api/conversations/" + id); } catch (e) { S.conv = null; store.set("v2_conv", null); return newConv(); }
  if (!c.messages.length) return newConv();
  destroyCharts();
  showConversation(c.title);
  let content = null, lastType = null;
  for (const m of c.messages) {
    if (m.role === "user") { if (m.content.text) { $("#thread").append(userBubble(m.content.text)); content = null; lastType = null; } continue; }
    // kết quả sau khi duyệt SQL thay vào chính thẻ đang chờ duyệt
    if (!content || lastType !== "pending_approval") content = newCard();
    content.replaceChildren(answerBody(m.content));
    lastType = m.content.type;
  }
  const sc = $("#scroll"); sc.scrollTop = sc.scrollHeight;
  loadConvs();
}
function newConv() {
  closePage(); S.conv = null; store.set("v2_conv", null); destroyCharts(); closeMobile();
  showWelcome(); loadConvs();
  const ta = $("#ask"); ta && ta.focus();
}
function setTitle(t) { S.title = t || ""; const el = $("#crumb"); if (el) el.textContent = t || "Hội thoại mới"; }

function showConversation(title) {
  stopGlobe();
  const view = $("#view");
  view.className = "workspace-content conversation-content";
  view.replaceChildren(
    h("div", { class: "thread", id: "thread" }),
    h("div", { class: "conversation-footer" }, h("span", {}, ic("shield", 14), "Câu trả lời có nguồn dữ liệu và cách tính rõ ràng"),
      h("button", { onclick: showHelp }, "Tìm hiểu cách trợ lý phân tích", ic("arrowRight", 13))));
  setTitle(title);
}

// ------------------------------------------------------------------ màn chào
const STARTER_ICON = { lookup: "search", compare: "bars", why: "zap", define: "help" };
function sphere(canvas) {              // quả cầu điểm xoay, kéo để xoay (chuyển từ AnimatedSphere của bản thiết kế)
  const ctx = canvas.getContext("2d"); if (!ctx) return () => {};
  let ry = 0, rx = -0.18, drag = false, lx = 0, ly = 0, w = 0, hh = 0, raf = 0;
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const resize = () => { const r = canvas.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2); w = r.width; hh = r.height; canvas.width = Math.round(w * dpr); canvas.height = Math.round(hh * dpr); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); };
  const down = e => { drag = true; lx = e.clientX; ly = e.clientY; canvas.setPointerCapture(e.pointerId); canvas.classList.add("globe-dragging"); };
  const move = e => { if (!drag) return; ry += (e.clientX - lx) * 0.008; rx = Math.max(-0.8, Math.min(0.8, rx + (e.clientY - ly) * 0.006)); lx = e.clientX; ly = e.clientY; if (still) draw(); };
  const up = () => { drag = false; canvas.classList.remove("globe-dragging"); };
  const draw = () => {
    ctx.clearRect(0, 0, w, hh);
    const cx = w / 2, cy = hh / 2, rad = Math.min(w, hh) * 0.43, tilt = 19 * Math.PI / 180;
    ctx.save(); ctx.beginPath(); ctx.arc(cx, cy, rad, 0, Math.PI * 2); ctx.clip();
    const g = ctx.createRadialGradient(cx - rad * .3, cy - rad * .36, 0, cx, cy, rad);
    g.addColorStop(0, "rgba(111,190,255,.20)"); g.addColorStop(.65, "rgba(79,111,232,.12)"); g.addColorStop(1, "rgba(110,84,220,0)");
    ctx.fillStyle = g; ctx.fillRect(cx - rad, cy - rad, rad * 2, rad * 2);
    for (let lat = -Math.PI / 2 + 0.08; lat < Math.PI / 2; lat += 0.105) for (let lon = 0; lon < Math.PI * 2; lon += 0.105) {
      const x = Math.cos(lat) * Math.cos(lon + ry), y = Math.sin(lat), z = Math.cos(lat) * Math.sin(lon + ry);
      const y2 = y * Math.cos(rx) - z * Math.sin(rx), z2 = y * Math.sin(rx) + z * Math.cos(rx);
      if (z2 < -0.12) continue;
      const px = cx + (x * Math.cos(tilt) - y2 * Math.sin(tilt)) * rad, py = cy + (x * Math.sin(tilt) + y2 * Math.cos(tilt)) * rad;
      const s = (Math.sin(lat * 5 + lon * 2) + 1) / 2, a = 0.24 + z2 * 0.65;
      ctx.beginPath(); ctx.arc(px, py, Math.max(.3, 0.75 + z2 * 1.35 + s * 0.45), 0, Math.PI * 2);
      ctx.fillStyle = s > 0.76 ? `rgba(107,231,220,${a})` : `rgba(${Math.round(95 + s * 70)},${Math.round(150 + s * 60)},255,${a})`; ctx.fill();
    }
    ctx.restore();
  };
  const loop = () => { draw(); if (!drag) ry += 0.0028; raf = requestAnimationFrame(loop); };
  resize(); addEventListener("resize", resize);
  canvas.addEventListener("pointerdown", down); canvas.addEventListener("pointermove", move);
  canvas.addEventListener("pointerup", up); canvas.addEventListener("pointercancel", up);
  if (still) draw(); else loop();
  return () => { cancelAnimationFrame(raf); removeEventListener("resize", resize); };
}
function showWelcome() {
  stopGlobe();
  const u = S.user, p = S.cfg.profile || {};
  const name = callName(u), month = anchorMonth();
  const starters = (p.starters || []).slice(0, 4);
  const used = new Set(starters.map(g => g.questions[0]));
  const samples = [...starters.flatMap(g => g.questions.slice(1)), ...(S.cfg.samples || [])].filter(q => q && !used.has(q) && used.add(q)).slice(0, 3);
  const canvas = h("canvas", { role: "img", "aria-label": "Quả cầu dữ liệu đang xoay. Kéo để xoay." });
  const conf = (p.confusable_terms || [])[0];
  const view = $("#view");
  view.className = "workspace-content welcome-content";
  view.replaceChildren(
    h("section", { class: "welcome-hero" },
      h("div", { class: "hero-globe" }, canvas),
      h("div", { class: "welcome-overline" }, h("span", { class: "overline-line" }), assistantName(p) + " · trợ lý dữ liệu của " + (p.short_name || p.company || ""),
        h("span", { class: "overline-chip" }, ic("sparkles", 12), "AI có kiểm chứng")),
      h("h1", {}, "Hỏi dữ liệu.", h("br"), h("span", {}, "Hiểu doanh nghiệp.")),
      h("p", {}, (name === "bạn" ? "Chào bạn! " : "Chào " + name + "! ") + "Hỏi bằng tiếng Việt về dữ liệu" + (p.company ? " của " + p.company : "") +
        ". Mình trả về con số, biểu đồ, lời giải thích và cách tính minh bạch để bạn kiểm tra lại."),
      h("div", { class: "hero-annotation" }, h("span", { class: "annotation-dot" }), "Dữ liệu cập nhật đến " + fmtDate(S.cfg.anchor)),
      u.rls && Object.keys(u.rls).length ? h("div", { class: "note hero-scope" }, ic("lock"), h("div", {}, "Tài khoản của bạn chỉ xem được một phần dữ liệu (" + Object.values(u.rls).map(v => [].concat(v).join(", ")).join("; ") + "). Mọi con số đều tính trong phạm vi đó.")) : null,
      h("div", { class: "floating-data-card float-card-one" }, h("span", { class: "float-icon" }, ic("book", 14)),
        h("span", {}, h("small", {}, "Chỉ số đã định nghĩa"), h("strong", {}, String(p.n_metrics || (S.cfg.metrics || []).length), h("em", {}, "định nghĩa chung")))),
      h("div", { class: "floating-data-card float-card-two" }, h("span", { class: "float-icon float-icon-blue" }, ic("target", 14)),
        h("span", {}, h("small", {}, "Chiều phân tích"), h("strong", {}, String(p.n_dimensions || ""), h("em", {}, "cách chia số liệu")))),
      h("div", { class: "hero-grid-decoration", "aria-hidden": "true" })),
    starters.length ? h("section", { class: "question-starters", "aria-label": "Gợi ý câu hỏi" }, starters.map((g, i) =>
      h("button", { class: "starter-card", style: { animationDelay: i * 80 + "ms" }, onclick: () => send(g.questions[0]) },
        h("span", { class: "starter-icon" }, ic(STARTER_ICON[g.key] || "search", 17)), h("span", { class: "starter-number" }, "0" + (i + 1)),
        h("strong", {}, g.title), h("p", {}, g.hint), h("span", { class: "starter-q" }, "“" + g.questions[0] + "”"),
        h("span", { class: "starter-arrow" }, ic("arrowRight", 15))))) : null,
    samples.length ? h("div", { class: "sample-questions" }, h("span", { class: "sample-label" }, "THỬ HỎI"), samples.map(q => h("button", { onclick: () => send(q) }, q, ic("arrowRight", 13)))) : null,
    h("div", { class: "metric-definition-prompt" }, h("div", { class: "definition-icon" }, ic("book")),
      conf ? h("span", {}, h("strong", {}, "“" + conf.term[0].toUpperCase() + conf.term.slice(1) + "” có " + conf.options.length + " cách tính"),
        h("small", {}, "Ghi rõ tên trong câu hỏi (vd “" + conf.options[0].label.toLowerCase() + " " + (month || "tháng 10") + "”) thì mình trả lời ngay; chỉ ghi “" + conf.term + "” thì mình sẽ hỏi lại."))
        : h("span", {}, h("strong", {}, "Một chỉ số, hai cách hiểu?"), h("small", {}, "Mọi chỉ số đều có định nghĩa thống nhất, xem trước khi hỏi.")),
      h("button", { onclick: () => openGlossary() }, "Khám phá từ điển", ic("arrowRight", 14))),
    (p.out_of_scope || []).length ? h("div", { class: "welcome-foot" }, "Mình không làm: " + p.out_of_scope.join("; ") + ".") : null);
  setTitle("");
  requestAnimationFrame(() => { if (canvas.isConnected) S.globe = sphere(canvas); });
}
function showHelp() {
  const li = (b, t) => h("li", { style: { marginBottom: "8px" } }, h("b", {}, b), t);
  modalCard(assistantName(S.cfg.profile) + " · trợ lý dữ liệu AI", "Cách dùng " + assistantName(S.cfg.profile), [
    h("ol", { style: { paddingLeft: "18px", margin: 0, color: "var(--text-2)", lineHeight: 1.6 } },
      li("Hỏi rõ 3 ý: ", "số gì (chỉ số), thời gian nào, chia theo gì." + (exampleQuestion() ? " Ví dụ: “" + exampleQuestion() + "”." : "")),
      li("Đọc dòng “Mình hiểu là”: ", "trợ lý nhắc lại cách hiểu. Bấm vào tên chỉ số để xem định nghĩa."),
      li("Khi trợ lý hỏi lại: ", "một số từ (như “doanh thu”) có nhiều cách tính. Chọn một lần, trợ lý nhớ trong cả cuộc hội thoại."),
      li("Bấm “Cách tính”: ", "xem chỉ số đã dùng, các bước kiểm tra và câu lệnh SQL."),
      li("Khi trợ lý xin duyệt: ", "câu hỏi đọc nhiều dữ liệu sẽ dừng lại để bạn xác nhận trước khi chạy."),
      li("Hỏi tiếp ngắn gọn: ", "“chia theo kênh”, “so với cùng kỳ”, “còn tháng trước thì sao?”.")),
    h("p", { class: "sec small" }, "Các công cụ khác (xem dữ liệu, tự viết SQL, lịch sử câu hỏi" + (S.user.can_manage_schema ? ", chất lượng dữ liệu, đo độ chính xác, cài đặt" : "") + ") nằm ở mục Khám phá bên trái. Ctrl + K mở hội thoại mới."),
    h("div", { class: "modal-actions" }, h("button", { class: "primary-button", onclick: closeModal }, "Đã hiểu"))]);
}

// ------------------------------------------------------------------ khung ứng dụng
const PAGE_ICON = { data: "folder", schema: "list", console: "code", logs: "history", dq: "activity", lineage: "layers", eval: "target", settings: "settings" };
function syncSend() { const ta = $("#ask"), b = $("#send"); if (b && ta) b.disabled = S.busy || !ta.value.trim(); }
function closeMobile() { const s = $("#sidebar"); s && s.classList.remove("mobile-open"); const b = $(".mobile-backdrop"); b && b.remove(); }
function openMobile() {
  const s = $("#sidebar"); s.classList.add("mobile-open");
  if (!$(".mobile-backdrop")) $(".analytics-app").prepend(h("button", { class: "mobile-backdrop", "aria-label": "Đóng menu", onclick: closeMobile }));
}
function toggleSidebar() { const s = $("#sidebar"); s.classList.toggle("sidebar-collapsed"); store.set("v2_rail", s.classList.contains("sidebar-collapsed") ? "closed" : null); }
function toggleTheme() {
  const nx = isDark() ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", nx); store.set("theme", nx);
  const b = $("#theme"); b && b.replaceChildren(ic(nx === "dark" ? "sun" : "moon", 17));
  if (S.page) openPage(S.page); else if (S.conv) openConv(S.conv);        // vẽ lại biểu đồ theo màu mới
}
function layout() {
  const u = S.user, c = S.cfg, p = c.profile || {};
  const narrow = matchMedia("(max-width: 560px)").matches;
  const ta = h("textarea", { id: "ask", rows: 1, "aria-label": "Hỏi dữ liệu bằng tiếng Việt",
    placeholder: narrow || !exampleQuestion() ? "Hỏi về dữ liệu của bạn…" : "Hỏi về dữ liệu của bạn… ví dụ: " + exampleQuestion(),
    oninput: e => { const t = e.target; t.style.height = "auto"; t.style.height = Math.min(160, t.scrollHeight) + "px"; t.style.overflowY = t.scrollHeight > 160 ? "auto" : "hidden"; syncSend(); },
    onkeydown: e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); submit(); } } });
  const submit = () => { const v = ta.value.trim(); if (!v || S.busy) return; ta.value = ""; ta.style.height = "auto"; syncSend(); send(v); };
  const offline = c.mode === "none";        // chưa cấu hình khóa mô hình ngôn ngữ
  const ws = (p.short_name || p.company || "DN").replace(/[^\p{L}\p{N} ]/gu, "").split(/\s+/).filter(Boolean);
  const wsInit = (ws.length > 1 ? ws[0][0] + ws[ws.length - 1][0] : (ws[0] || "DN").slice(0, 2)).toUpperCase();
  const modelTitle = offline ? "Chưa cấu hình mô hình ngôn ngữ (OPENROUTER_API_KEY): trợ lý chỉ trả lời được câu hỏi nêu rõ chỉ số và thời gian."
    : "Trợ lý dùng mô hình " + c.model + " để hiểu câu hỏi; câu rõ ràng được trả lời nhanh không cần mô hình; truy vấn luôn được kiểm tra bằng code.";
  const app = h("div", { class: "analytics-app" },
    h("aside", { class: "app-sidebar" + (store.get("v2_rail") === "closed" ? " sidebar-collapsed" : ""), id: "sidebar" },
      h("div", { class: "brand-row" }, h("div", { class: "brand-mark" }, ic("bars", 19, 2.4)),
        brandCopy(),
        h("button", { class: "icon-button sidebar-toggle", "aria-label": "Thu gọn / mở rộng thanh bên", title: "Thu gọn / mở rộng thanh bên", onclick: toggleSidebar }, ic("panel", 16))),
      h("div", { class: "workspace-switch", title: p.company }, h("div", { class: "workspace-avatar" }, wsInit),
        h("div", { class: "workspace-copy" }, h("strong", {}, p.company || "Doanh nghiệp"), h("span", {}, "Doanh nghiệp · dữ liệu đang hỏi"))),
      h("button", { class: "new-chat-button", onclick: newConv, title: "Hội thoại mới (Ctrl + K)" }, ic("plus", 16), h("span", {}, "Hội thoại mới"), h("span", { class: "shortcut" }, "Ctrl K")),
      h("div", { class: "sidebar-scroll" },
        h("div", { class: "sidebar-section history-section" }, h("div", { class: "sidebar-section-title" }, h("span", {}, "GẦN ĐÂY")), h("div", { class: "history-list", id: "convlist" })),
        h("div", { class: "sidebar-section tools-section" }, h("div", { class: "sidebar-section-title" }, h("span", {}, "KHÁM PHÁ")),
          h("nav", { class: "tool-list", id: "toolnav" },
            h("button", { class: "tool-item", title: "Từ điển chỉ số", onclick: () => { closeMobile(); openGlossary(); } }, ic("book", 16), h("span", {}, "Từ điển chỉ số"), h("span", { class: "tool-count" }, String((c.metrics || []).length))),
            pagesFor(u).map(pg => h("button", { class: "tool-item", "data-p": pg.id, title: pg.label, onclick: () => openPage(pg.id) }, ic(PAGE_ICON[pg.id] || "list", 16), h("span", {}, pg.label),
              pg.admin ? h("span", { class: "tool-admin", title: "Chỉ dành cho quản trị" }, ic("lock", 11)) : null))))),
      h("div", { class: "sidebar-bottom" },
        h("div", { class: "plan-card", title: modelTitle }, h("div", { class: "plan-icon" + (offline ? " off" : "") }, ic(offline ? "alert" : "zap", 15)),
          h("div", {}, h("strong", {}, offline ? "Chưa có mô hình ngôn ngữ" : "Có mô hình ngôn ngữ"), h("span", {}, "Dữ liệu đến " + fmtDate(c.anchor)))),
        h("div", { class: "profile-row" }, h("div", { class: "profile-avatar" }, initial(u)),
          h("div", { class: "profile-copy" }, h("strong", {}, u.full_name || u.username), h("span", {}, (u.role_label || u.role) + (u.rls && Object.keys(u.rls).length ? " · một phần dữ liệu" : ""))),
          h("button", { class: "icon-button", title: "Đăng xuất", "aria-label": "Đăng xuất", onclick: logout }, ic("logout", 16))))),
    h("main", { class: "app-main" },
      h("header", { class: "topbar" },
        h("div", { class: "topbar-left" },
          h("button", { class: "icon-button mobile-menu-button", "aria-label": "Mở menu", onclick: openMobile }, ic("menu", 18)),
          h("span", { class: "breadcrumb-muted" }, p.short_name || "Không gian"), h("span", { class: "breadcrumb-divider" }, "/"), h("strong", { id: "crumb" }, "Hội thoại mới")),
        h("div", { class: "topbar-right" },
          h("div", { class: "connection-status", title: modelTitle }, h("span", { class: "status-dot" + (offline ? " off" : "") }),
            h("span", { class: "lbl" }, offline ? "Chưa có mô hình ngôn ngữ" : "Nguồn dữ liệu sẵn sàng"), h("span", { class: "status-date lbl" }, "· dữ liệu đến " + fmtDate(c.anchor))),
          h("button", { class: "btn", title: "Định nghĩa của mọi chỉ số", onclick: () => openGlossary() }, ic("book", 15), h("span", { class: "lbl" }, "Từ điển")),
          h("button", { class: "icon-button", title: "Cách dùng", "aria-label": "Cách dùng", onclick: showHelp }, ic("help", 17)),
          h("button", { class: "icon-button", id: "theme", title: "Đổi nền sáng/tối", "aria-label": "Đổi nền sáng/tối", onclick: toggleTheme }, ic(isDark() ? "sun" : "moon", 17)),
          h("div", { class: "topbar-avatar", title: u.full_name || u.username }, initial(u)))),
      h("div", { class: "workspace-scroll", id: "scroll" }, h("div", { id: "view" }), h("div", { class: "tool-page", id: "page", hidden: true })),
      h("div", { class: "composer", id: "composer" },
        h("div", { class: "composer-top" }, ic("sparkles", 15), ta,
          h("button", { type: "button", class: "composer-option", title: "Xem tên các chỉ số để hỏi cho rõ", onclick: () => openGlossary() }, ic("book", 14), h("span", {}, "Chỉ số"), ic("chevronDown", 12)),
          h("button", { class: "send-button", id: "send", title: "Gửi (Enter)", "aria-label": "Gửi câu hỏi", disabled: true, onclick: submit }, ic("arrowUp", 17))),
        h("div", { class: "composer-bottom" },
          h("span", { class: "keys" }, h("kbd", {}, "↵"), " gửi · ", h("kbd", {}, "Shift"), " + ", h("kbd", {}, "↵"), " xuống dòng"),
          h("span", { class: "composer-model", title: modelTitle }, h("span", { class: "status-dot" + (offline ? " off" : "") }), assistantName(p) + " · dữ liệu " + (p.short_name || p.company || ""))))));
  $("#app").replaceChildren(app);
  showWelcome();
  loadConvs();
  const last = store.get("v2_conv"), pg = store.get("page");
  if (pg && pagesFor(u).some(x => x.id === pg)) openPage(pg);
  else if (last) openConv(last); else ta.focus();
}

// ------------------------------------------------------------------ trang công cụ (tools.js)
function openPage(id) {
  const pg = pagesFor(S.user).find(p => p.id === id); if (!pg) return;
  S.page = id; store.set("page", id); closeMobile(); closeDrawer(); destroyCharts(); stopGlobe();
  $("#view").hidden = true; $("#composer").hidden = true;
  const box = $("#page"); box.hidden = false; box.replaceChildren(...[pg.fn()].flat(Infinity).filter(Boolean));
  $("#scroll").scrollTop = 0;
  const cr = $("#crumb"); if (cr) cr.textContent = pg.label;
  [...$("#toolnav").children].forEach(b => b.classList.toggle("tool-active", b.dataset.p === id));
  document.querySelectorAll(".history-active").forEach(c => c.classList.remove("history-active"));
}
// quay về khung hỏi đáp
function closePage() {
  if (!S.page) return;
  S.page = null; store.set("page", null);
  const box = $("#page"); box.hidden = true; box.replaceChildren();
  $("#view").hidden = false; $("#composer").hidden = false;
  [...$("#toolnav").children].forEach(b => b.classList.remove("tool-active"));
  setTitle(S.title);
}

// ------------------------------------------------------------------ đăng nhập
async function boot() {
  const th = store.get("theme"); if (th) document.documentElement.setAttribute("data-theme", th);
  S.token = store.get("token");
  if (!S.token) return renderLogin();
  try { S.user = await api("/api/me"); S.cfg = await api("/api/config"); } catch (e) { return renderLogin(); }
  document.title = "Sola · " + ((S.cfg.profile || {}).short_name || (S.cfg.profile || {}).company || "Trợ lý dữ liệu");
  layout();
}
function logout() { stopGlobe(); S.token = null; S.user = null; S.conv = null; S.page = null; store.set("page", null); store.set("token", null); store.set("v2_conv", null); closeDrawer(); closeModal(); renderLogin(); }
async function renderLogin() {
  let pub = { company: "", industry: "", assistant: "Trợ lý dữ liệu", demo_users: [] };
  try { pub = await (await fetch("/api/public")).json(); } catch (e) { /* dùng mặc định */ }
  document.title = assistantName(pub) + " · Đăng nhập";
  const first = (pub.demo_users || []).find(x => !x.rls && x.username !== "admin") || (pub.demo_users || [])[0] || {};
  const u = h("input", { type: "text", placeholder: "Tên đăng nhập", value: first.username || "", autocomplete: "username" });
  const pw = h("input", { type: "password", placeholder: "Nhập mật khẩu", value: first.password || "", autocomplete: "current-password" });
  const err = h("div", {});
  const go = async (un, p) => {
    try { const r = await api("/api/login", { body: { username: un, password: p } }); S.token = r.token; store.set("token", r.token); boot(); }
    catch (e) { err.replaceChildren(h("div", { class: "note bad" }, ic("alert"), e.message)); }
  };
  const form = h("form", { class: "login-form", onsubmit: e => { e.preventDefault(); go(u.value, pw.value); } },
    h("label", {}, "Tên đăng nhập", u), h("label", {}, "Mật khẩu", pw),
    h("button", { class: "primary-button full-button", type: "submit" }, "Tiếp tục", ic("arrowRight", 15)), err);
  $("#app").replaceChildren(h("div", { class: "login" }, h("div", { class: "login-card" },
    h("div", { class: "brand-row" }, h("div", { class: "brand-mark" }, ic("bars", 19, 2.4)),
      brandCopy()),
    h("h1", {}, "Đăng nhập"),
    h("p", {}, assistantName(pub) + " là trợ lý dữ liệu AI" + (pub.company ? " của " + pub.company : "") + ". Hỏi số liệu bằng tiếng Việt, nhận bảng, biểu đồ và cách tính minh bạch."),
    form,
    (pub.demo_users || []).length ? [h("div", { class: "demo-title" }, "Dữ liệu giả lập. Đăng nhập nhanh bằng tài khoản demo:"),
      h("div", { class: "demo" }, pub.demo_users.map(x => h("button", { type: "button", onclick: () => go(x.username, x.password) }, h("b", {}, x.full_name), h("span", {}, x.role_label + (x.rls ? " · một phần dữ liệu" : "")))))] : null)));
}
document.addEventListener("DOMContentLoaded", boot);     // chờ tools.js nạp xong
