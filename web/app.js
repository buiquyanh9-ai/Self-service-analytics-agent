/* Trợ lý dữ liệu (vanilla JS + Chart.js)
 * Một cột hỏi đáp: kết quả trước, cách tính để trong ngăn bên, từ điển chỉ số luôn mở được.
 * Các trang công cụ và quản trị (dữ liệu, SQL, nhật ký, chất lượng dữ liệu, nguồn gốc, đo độ chính xác, cài đặt) ở tools.js. */
"use strict";

const S = { token: null, user: null, cfg: null, conv: null, convs: [], charts: [], busy: false, page: null };
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
const ICON = {
  menu: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>',
  send: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  book: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/></svg>',
  moon: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
  sun: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  help: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3M12 17h.01"/></svg>',
  logo: '<svg width="18" height="18" viewBox="0 0 24 24"><rect x="3" y="12" width="4" height="9" rx="1" fill="#2a78d6"/><rect x="10" y="7" width="4" height="14" rx="1" fill="#eb6834"/><rect x="17" y="3" width="4" height="18" rx="1" fill="#1baf7a"/></svg>',
};
const icon = (name, cls = "") => h("span", { class: cls, html: ICON[name], "aria-hidden": "true", style: { display: "inline-grid" } });

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
const metricByName = n => (S.cfg.metrics || []).find(m => m.name === n);

// ------------------------------------------------------------------ biểu đồ & bảng
function renderChart(container, spec) {
  if (!spec || spec.type === "kpi") return;
  if (typeof Chart === "undefined") { container.append(h("div", { class: "note warn" }, "Không tải được thư viện biểu đồ, hãy xem dạng bảng.")); return; }
  const colors = [1, 2, 3, 4, 5, 6, 7, 8].map(i => cssVar("--series-" + i));
  const n = spec.labels.length;
  const box = h("div", { class: "chart-box", style: { height: (spec.horizontal ? Math.max(240, Math.min(900, n * 28 + 70)) : 300) + "px" } });
  const cv = h("canvas", { role: "img", "aria-label": spec.title || "Biểu đồ" });
  box.append(cv);
  if (spec.title) container.append(h("div", { class: "chart-t" }, spec.title));
  container.append(box);
  const surface = cssVar("--surface-1"), grid = cssVar("--grid"), muted = cssVar("--text-muted"), sec = cssVar("--text-secondary");
  const isLine = spec.type === "line", isPie = spec.type === "doughnut";
  const datasets = spec.datasets.map((d, i) => {
    const base = { label: d.label, data: d.data };
    if (isPie) return Object.assign(base, { backgroundColor: spec.labels.map((_, k) => colors[k % 8]), borderColor: surface, borderWidth: 2 });
    let col = colors[i % 8];
    if (d.role === "previous") col = cssVar("--series-muted");
    if (d.role === "current") col = colors[0];
    if (isLine) return Object.assign(base, { borderColor: col, backgroundColor: col, borderWidth: 2, pointRadius: n <= 24 ? 3 : 0, pointHoverRadius: 5, pointBorderColor: surface, pointBorderWidth: 2, tension: 0, spanGaps: true });
    const bg = d.role === "delta" ? d.data.map(v => (v >= 0 ? cssVar("--pos") : cssVar("--neg"))) : col;
    return Object.assign(base, { backgroundColor: bg, borderRadius: 4, borderSkipped: "start", maxBarThickness: 28, categoryPercentage: 0.72, barPercentage: 0.9, borderColor: surface, borderWidth: spec.stacked ? { top: 2 } : 0 });
  });
  const valAxis = { grid: { color: grid, drawTicks: false }, border: { display: false }, ticks: { color: muted, padding: 6, callback: v => fmtAxis(v, spec.format) }, stacked: !!spec.stacked, beginAtZero: !isLine };
  const catAxis = { grid: { display: false }, border: { color: grid }, ticks: { color: muted, autoSkip: true, maxRotation: 0 }, stacked: !!spec.stacked };
  S.charts.push(new Chart(cv, {
    type: isPie ? "doughnut" : (isLine ? "line" : "bar"),
    data: { labels: spec.labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
      indexAxis: spec.horizontal ? "y" : "x", cutout: isPie ? "62%" : undefined,
      interaction: isPie ? { mode: "nearest", intersect: true } : { mode: "index", intersect: false },
      plugins: {
        legend: { display: isPie || datasets.length > 1, position: isPie ? "right" : "top", align: "start", labels: { color: sec, usePointStyle: true, pointStyle: "rectRounded", boxWidth: 10, boxHeight: 10 } },
        tooltip: { backgroundColor: surface, titleColor: cssVar("--text-primary"), bodyColor: sec, borderColor: cssVar("--border"), borderWidth: 1, padding: 10, usePointStyle: true,
          callbacks: { label: c => ` ${c.dataset.label}: ${fmtCompact(isPie ? c.parsed : (spec.horizontal ? c.parsed.x : c.parsed.y), spec.format)}` } },
      },
      scales: isPie ? {} : (spec.horizontal ? { x: valAxis, y: catAxis } : { x: catAxis, y: valAxis }),
    },
  }));
}
function kpiTiles(spec) {
  return h("div", { class: "kpis" }, spec.items.map(it => h("div", { class: "kpi" },
    h("div", { class: "l" }, it.label), h("div", { class: "v" }, fmtCompact(it.value, it.format)),
    it.delta_pct != null ? h("div", { class: "d " + (it.delta_pct >= 0 ? "up" : "down") },
      (it.delta_pct >= 0 ? "▲ +" : "▼ ") + nf1.format(it.delta_pct) + "% so với " + (it.prev_label || "kỳ trước") + " (" + fmtCompact(it.prev, it.format) + ")") : null)));
}
function dataTable(columns, rows, meta, highlights) {
  const mm = {}; (meta || []).forEach(m => (mm[m.name] = m));
  const hl = {}; (highlights || []).forEach(x => (hl[x.row] = x.type === "down" ? "hl-down" : "hl-up"));
  return h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
    h("thead", {}, h("tr", {}, columns.map(c => h("th", { title: c, class: (mm[c] || {}).kind === "metric" ? "num" : "" }, (mm[c] && mm[c].label) || c)))),
    h("tbody", {}, rows.slice(0, 500).map((r, i) => h("tr", { class: hl[i] || "" }, r.map((v, j) => h("td", { class: typeof v === "number" ? "num" : "" }, fmtCell(v, (mm[columns[j]] || {}).format))))))));
}
function downloadCSV(columns, rows, name = "ket_qua.csv") {
  const q = v => (v == null ? "" : /[",\n;]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v));
  const a = h("a", { href: URL.createObjectURL(new Blob(["﻿" + [columns.map(q).join(","), ...rows.map(r => r.map(q).join(","))].join("\n")], { type: "text/csv" })), download: name });
  document.body.append(a); a.click(); a.remove();
}
function codeBlock(text) {
  return h("div", { class: "code" }, h("button", { class: "btn sm copy", onclick: () => { navigator.clipboard && navigator.clipboard.writeText(text); toast("Đã sao chép"); } }, "Sao chép"), h("pre", {}, text));
}

// ------------------------------------------------------------------ ngăn bên
function drawer(title, body) {
  closeDrawer();
  const bg = h("div", { class: "drawer-bg", id: "drawer-bg", onclick: closeDrawer });
  const d = h("aside", { class: "drawer", id: "drawer", role: "dialog", "aria-label": title },
    h("div", { class: "drawer-hd" }, h("b", {}, title), h("button", { class: "btn sm", onclick: closeDrawer }, "Đóng")),
    h("div", { class: "drawer-bd" }, body));
  document.body.append(bg, d);
  return d;
}
function closeDrawer() { ["#drawer", "#drawer-bg"].forEach(s => { const e = $(s); e && e.remove(); }); }
document.addEventListener("keydown", e => { if (e.key === "Escape") { closeDrawer(); const m = $(".modal-bg"); m && m.remove(); } });

// Từ điển chỉ số: các từ dễ nhầm lên đầu, sau đó toàn bộ chỉ số, có ô tìm
function openGlossary(focus) {
  const p = S.cfg.profile || {}, month = anchorMonth();
  const list = h("div", {});
  const tryChip = m => h("button", { class: "chip", onclick: () => { closeDrawer(); send(m.label + (month ? " " + month : "")); } }, "Hỏi thử: " + m.label + (month ? " " + month : ""));
  const termRow = m => h("div", { class: "term" + (m.name === focus ? " hit" : ""), id: "term-" + m.name },
    h("div", { class: "hd" }, h("b", {}, m.label), m.owner ? h("span", { class: "pill" }, m.owner) : null),
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
  const d = drawer("Từ điển chỉ số", [h("div", { class: "sec small", style: { marginBottom: "10px" } },
    "Mỗi chỉ số có một định nghĩa thống nhất trong công ty. Gõ đúng tên chỉ số trong câu hỏi thì trợ lý trả lời ngay, không phải hỏi lại."), search, h("div", { style: { marginTop: "6px" } }, list)]);
  draw("");
  if (focus) { const t = $("#term-" + focus, d); t && t.scrollIntoView({ block: "center" }); } else search.focus();
}
function norm(s) { return String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/g, "d"); }

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
function openHow(r) {
  const v = r.validation || {};
  const defs = (r.definitions || []).length ? r.definitions : (r.plan && r.plan.metrics || []).map(metricByName).filter(Boolean);
  const src = { semantic: "Theo định nghĩa chỉ số chung", "semantic+repair": "Theo định nghĩa chỉ số chung (đã tự sửa)", fewshot: "Theo truy vấn mẫu đã được xác nhận", lookup: "Đọc thẳng từ bảng danh mục", llm: "Trợ lý tự viết truy vấn", user: "Câu lệnh do người dùng viết", user_edit: "Câu lệnh đã được người dùng sửa" }[r.sql_source];
  drawer("Cách tính câu trả lời này", [
    defs.length ? [h("h4", {}, "Chỉ số đã dùng"), defs.map(d => h("div", { class: "term" }, h("div", { class: "hd" }, h("b", {}, d.label), d.owner ? h("span", { class: "pill" }, d.owner) : null), h("div", { class: "desc" }, d.description || "Chưa có mô tả.")))] : null,
    src ? [h("h4", {}, "Nguồn của truy vấn"), h("div", { class: "sec" }, src)] : null,
    r.audit ? [h("h4", {}, "Đối chiếu số liệu"), h("div", { class: "note " + (r.audit.ok ? "ok" : "bad") }, r.audit.ok
      ? `Đã tính lại ${r.audit.facts_checked} con số trong lời giải thích từ bảng kết quả, tất cả đều khớp.`
      : "Có số chưa đối chiếu được: " + (r.audit.unverified_numbers || []).join(", ") + ". Hãy dựa vào bảng kết quả.")] : null,
    (v.checks || []).length ? [h("h4", {}, "Các bước kiểm tra trước khi chạy"), h("ul", { class: "checks" }, v.checks.map(c => h("li", {}, c.passed ? "✅" : "❌", h("span", {}, c.name), h("span", { class: "muted small" }, c.detail || ""))))] : null,
    r.cost && r.cost.human ? [h("h4", {}, "Dung lượng dữ liệu đã đọc"), h("div", { class: "sec" }, "Khoảng " + r.cost.human)] : null,
    r.sql ? [h("h4", {}, "Câu lệnh SQL"), codeBlock(r.sql)] : null,
    r.variance && (r.variance.queries || []).length ? [h("h4", {}, `Các truy vấn đã chạy (${r.variance.queries.length})`), r.variance.queries.map(q => h("details", { style: { marginTop: "6px" } }, h("summary", {}, q.purpose, h("span", { class: "muted small" }, " · " + fmtBytes(q.bytes) + " · " + q.rows + " dòng")), codeBlock(q.sql)))] : null,
    h("h4", {}, "Chi tiết kỹ thuật"), h("div", { class: "tech" }, techDetails(r)),
  ]);
}

// ------------------------------------------------------------------ câu trả lời
function understood(r) {
  const p = r.plan; if (!p || !(p.metrics || []).length) return null;
  const dl = Object.fromEntries((S.cfg.dimensions || []).map(d => [d.name, d.label]));
  const span = t => t.label || ((t.start || "…") + " đến trước " + (t.end || "…"));
  const chips = p.metrics.map(m => { const mm = metricByName(m);
    return h("button", { class: "u-chip", title: "Xem định nghĩa", onclick: () => openGlossary(m) }, h("span", { class: "k" }, "Chỉ số"), mm ? mm.label : m, h("span", { class: "i" }, "ⓘ")); });
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
  if (r.llm_error) out.push(h("div", { class: "note warn" }, "⚠️", h("div", {}, h("b", {}, "Mô hình ngôn ngữ tạm thời không dùng được. "), "Câu hỏi nêu rõ chỉ số và thời gian vẫn được trả lời; câu khác bạn thử lại sau ít phút.")));
  if ((r.assumptions || []).length) out.push(h("div", { class: "note" }, "ℹ️", h("div", {}, h("b", {}, "Mình đã tự hiểu thêm:"), h("ul", {}, r.assumptions.map(a => h("li", {}, a))))));
  const notes = r.notes || [], lint = (r.lint || []).filter(l => l.severity !== "info");
  if (notes.length || lint.length) out.push(h("div", { class: "note " + (lint.length ? "warn" : "") }, lint.length ? "⚠️" : "📝",
    h("div", {}, h("b", {}, "Lưu ý khi đọc kết quả:"), h("ul", {}, lint.map(l => h("li", {}, l.message)), notes.map(a => h("li", {}, a))))));
  return out;
}
function trust(r) {
  const it = [];
  if (r.audit) it.push(r.audit.ok ? ["ok", "✓ Số liệu đã đối chiếu"] : ["bad", "✗ Có số chưa đối chiếu được"]);
  const s = r.sql_source;
  if (s === "semantic" || s === "semantic+repair" || s === "fewshot") it.push(["info", "📐 Định nghĩa chuẩn của công ty"]);
  else if (s === "lookup") it.push(["info", "📇 Từ bảng danh mục"]);
  else if (s === "llm") it.push(["warn", "✍️ Trợ lý tự viết truy vấn, nên xem cách tính"]);
  if (r.validation && (r.validation.rls_applied || []).length) it.push(["", "🔒 Trong phạm vi bạn được xem"]);
  if (r.truncated) it.push(["warn", "Bảng đã cắt bớt"]);
  it.push(["", "⏱ " + nf1.format((r.elapsed_ms || 0) / 1000) + " giây"]);
  return h("div", { class: "trust" }, it.map(([c, t]) => h("span", { class: "pill " + c }, t)));
}
function followUps(r) {
  if (!(r.follow_ups || []).length) return null;
  return h("div", { class: "follow" }, h("span", { class: "lb" }, "Hỏi tiếp:"), r.follow_ups.map(s => h("button", { class: "chip", onclick: () => send(s) }, s)));
}
function footer(r) {
  const ft = h("div", { class: "ans-ft" });
  ft.append(h("button", { class: "btn ghost sm", onclick: () => openHow(r) }, "📐 Cách tính"));
  if (r.columns && r.rows && r.rows.length) ft.append(h("button", { class: "btn ghost sm", onclick: () => downloadCSV(r.columns, r.rows) }, "⬇ Tải CSV"));
  ft.append(h("span", { class: "sp" }));
  if (r.run_id && r.sql) {
    const thanks = h("span", { class: "thanks" });
    const up = h("button", { class: "btn ghost sm", title: "Kết quả đúng", onclick: async () => {
      try { await api("/api/feedback", { body: { run_id: r.run_id, rating: 1 } }); thanks.textContent = "Cảm ơn bạn!"; up.remove(); down.remove(); } catch (e) { toast(e.message); } } }, "👍");
    const down = h("button", { class: "btn ghost sm", title: "Kết quả chưa đúng", onclick: () => feedbackModal(r, () => { thanks.textContent = "Đã ghi nhận góp ý."; up.remove(); down.remove(); }) }, "👎");
    ft.append(thanks, up, down);
  }
  return ft;
}
function feedbackModal(r, done) {
  const cm = h("textarea", { rows: 3, placeholder: "Sai ở đâu? Ví dụ: số này phải tính cả đơn đang giao…" });
  const bg = h("div", { class: "modal-bg", onclick: e => { if (e.target === bg) bg.remove(); } },
    h("div", { class: "modal" }, h("h3", {}, "Góp ý cho câu trả lời"), cm,
      h("div", { style: { display: "flex", gap: "8px", justifyContent: "flex-end", marginTop: "12px" } },
        h("button", { class: "btn", onclick: () => bg.remove() }, "Hủy"),
        h("button", { class: "btn primary", onclick: async () => {
          try { await api("/api/feedback", { body: { run_id: r.run_id, rating: -1, comment: cm.value } }); bg.remove(); done(); } catch (e) { toast(e.message); } } }, "Gửi góp ý"))));
  document.body.append(bg); cm.focus();
}

function resultVisual(r) {
  const wrap = h("div", {});
  const spec = r.chart;
  const hasRows = r.columns && r.rows && r.rows.length;
  if (spec && spec.type === "kpi") {
    wrap.append(kpiTiles(spec));
    if (hasRows && !(r.rows.length === 1 && r.columns.length <= 2)) wrap.append(dataTable(r.columns, r.rows, r.columns_meta));
    return wrap;
  }
  if (!spec) { if (hasRows) wrap.append(dataTable(r.columns, r.rows, r.columns_meta)); return wrap; }
  // có biểu đồ: cho chuyển qua lại Biểu đồ / Bảng thay vì xếp chồng cả hai
  const pane = h("div", {});
  let mode = "chart";
  const seg = h("div", { class: "seg", role: "tablist" });
  const show = m => {
    mode = m; [...seg.children].forEach(b => b.classList.toggle("on", b.dataset.m === m)); pane.replaceChildren();
    if (m === "chart") renderChart(pane, spec); else pane.append(dataTable(r.columns, r.rows, r.columns_meta, spec.highlights));
  };
  seg.append(h("button", { "data-m": "chart", onclick: () => show("chart") }, "Biểu đồ"));
  if (hasRows) seg.append(h("button", { "data-m": "table", onclick: () => show("table") }, "Bảng" + (r.rows.length > 1 ? " (" + nf0.format(r.row_count || r.rows.length) + " dòng)" : "")));
  wrap.append(h("div", { class: "viewbar" }, seg), pane);
  show(mode);
  return wrap;
}
function varianceVisual(r) {
  const v = r.variance, f = v.format, t = v.total;
  const wrap = h("div", {}, h("div", { class: "kpis" },
    h("div", { class: "kpi" }, h("div", { class: "l" }, v.period_now.label), h("div", { class: "v" }, fmtCompact(t.now, f))),
    h("div", { class: "kpi" }, h("div", { class: "l" }, v.period_prev.label), h("div", { class: "v" }, fmtCompact(t.prev, f))),
    h("div", { class: "kpi" }, h("div", { class: "l" }, "Chênh lệch"), h("div", { class: "v" }, (t.delta >= 0 ? "+" : "") + fmtCompact(t.delta, f)),
      t.pct == null ? null : h("div", { class: "d " + (t.pct >= 0 ? "up" : "down") }, (t.pct >= 0 ? "▲ +" : "▼ ") + nf1.format(t.pct) + "%")),
    v.significance ? h("div", { class: "kpi", title: "So với các lần biến động trước của chính chỉ số này" }, h("div", { class: "l" }, "So với dao động thường thấy"),
      h("div", { class: "v" }, v.significance.significant ? "Bất thường" : "Bình thường"), h("div", { class: "d" }, "dựa trên " + v.significance.n_history + " kỳ trước")) : null));
  const causes = (v.root_causes || []).filter(c => c.kind !== "significance");
  if (causes.length) wrap.append(h("div", { class: "chart-t" }, "Nguyên nhân tìm thấy"), causes.map(c => h("div", { class: "cause " + (c.severity || "") },
    h("b", {}, c.title), h("div", { class: "sec small" }, c.message), c.action ? h("div", { class: "act" }, "➜ " + c.action) : null)));
  // giả thuyết do mô hình nêu, code đã chạy truy vấn kiểm chứng (số liệu ở đây đều do code tính)
  const HS = { confirmed: ["ok", "Xác nhận"], weak: ["warn", "Yếu"], rejected: ["bad", "Bác bỏ"], untested: ["", "Chưa kiểm được"] };
  const hyps = (v.hypotheses || []).filter(x => HS[x.status]);
  if (hyps.length) wrap.append(h("div", { class: "chart-t" }, "Giả thuyết do AI nêu, đã kiểm bằng dữ liệu"),
    hyps.map(x => h("div", { class: "cause hyp " + x.status },
      h("div", { class: "hyp-hd" }, h("span", { class: "pill " + HS[x.status][0] }, HS[x.status][1]), h("b", {}, x.claim)),
      x.pct != null || x.now != null ? h("div", { class: "sec small" }, `${x.metric_label} ở ${x.segment_label}: ${fmtCompact(x.prev, x.format)} → ${fmtCompact(x.now, x.format)}` +
        (x.pct != null ? ` (${x.pct >= 0 ? "+" : ""}${nf1.format(x.pct)}%)` : "") + (x.share != null ? ` · giải thích ${nf1.format(x.share * 100)}% biến động` : "")) : null,
      h("div", { class: "muted small" }, x.reason))));
  const viz = h("div", {}); renderChart(viz, r.chart); wrap.append(viz);
  const best = (v.drivers || []).find(d => d.dimension === v.best_dimension) || (v.drivers || [])[0];
  if (best) wrap.append(h("div", { class: "chart-t" }, "Đóng góp vào biến động theo " + best.label.toLowerCase()),
    h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, h("th", {}, best.label), h("th", { class: "num" }, v.period_now.label), h("th", { class: "num" }, v.period_prev.label), h("th", { class: "num" }, "Chênh lệch"), h("th", { class: "num" }, "Đóng góp"))),
      h("tbody", {}, best.items.map(i => h("tr", {}, h("td", {}, String(i.member)), h("td", { class: "num" }, fmtCell(i.now, f)), h("td", { class: "num" }, fmtCell(i.prev, f)),
        h("td", { class: "num" }, fmtCell(i.delta, f)), h("td", { class: "num" }, i.contribution_pct == null ? "—" : nf1.format(i.contribution_pct) + "%")))))));
  return wrap;
}

function renderAnswer(r) {
  const card = h("div", { class: "ans" });
  const add = (...k) => card.append(...k.flat(Infinity).filter(x => x != null && x !== false));
  const T = r.type;
  if (T === "multi") {          // câu nhiều ý: mỗi ý một khối trả lời đầy đủ
    add(h("div", { class: "txt", html: md(r.text) }));
    (r.parts || []).forEach((p, i) => add(h("div", { class: "part" },
      h("div", { class: "part-hd" }, `${i + 1}. ${p.task || p.question}`), renderAnswer(p))));
    add(followUps(r));
    return card;
  }
  if (T === "clarification") {
    const det = (r.clarification || {}).details || [];
    const opts = h("div", { class: "opts" });
    ((r.clarification || {}).options || []).forEach((o, i) => {
      const d = det[i];
      const b = d ? h("button", { class: "opt" }, h("div", { class: "t" }, d.label, d.owner ? h("span", { class: "pill" }, d.owner) : null), d.hint ? h("div", { class: "h" }, d.hint) : null, h("div", { class: "d" }, d.description))
        : h("button", { class: "opt simple" }, h("div", { class: "t" }, o));
      b.onclick = () => { [...opts.children].forEach(x => (x.disabled = true)); b.classList.add("chosen"); send(o, { id: r.clarification.id, option: i }); };
      opts.append(b);
    });
    add(h("div", { class: "ask-q" }, "🤔 ", r.text), h("div", { class: "ask-why" }, "Mỗi cách hiểu cho ra một con số khác nhau, nên mình hỏi lại thay vì tự đoán."), opts,
      h("div", { class: "muted small", style: { marginTop: "8px" } }, "Lựa chọn được ghi nhớ cho các câu sau trong cuộc hội thoại này."));
    return card;
  }
  if (T === "pending_approval") {
    const ta = h("textarea", { rows: Math.min(16, (r.sql || "").split("\n").length + 1), class: "mono" }, r.sql || "");
    const msg = h("div", {});
    const act = async a => {
      [...btns.querySelectorAll("button")].forEach(b => (b.disabled = true));
      msg.replaceChildren(h("div", { class: "thinking", style: { marginTop: "10px" } }, h("div", { class: "spinner" }), a === "approve" ? "Đang kiểm tra lại và chạy…" : "Đang hủy…"));
      try {
        const res = await api("/api/approve", { body: { run_id: r.run_id, action: a, sql: ta.value } });
        if (card.closest("#thread")) { msg.replaceChildren(); appendAnswer(res); }
        else msg.replaceChildren(renderAnswer(res));          // trang Tự viết SQL: hiện kết quả ngay dưới thẻ
      }
      catch (e) { msg.replaceChildren(h("div", { class: "note bad" }, e.message)); }
    };
    const btns = h("div", { style: { display: "flex", gap: "8px", marginTop: "12px", flexWrap: "wrap" } },
      h("button", { class: "btn primary", onclick: () => act("approve") }, "✓ Đồng ý chạy"), h("button", { class: "btn danger", onclick: () => act("reject") }, "✕ Không chạy"));
    add(understood(r), h("div", { class: "ask-q" }, "⏸ Mình cần bạn đồng ý trước khi chạy"),
      h("div", { class: "ask-why" }, "Câu hỏi này phải đọc khá nhiều dữ liệu" + (r.cost && r.cost.human ? " (khoảng " + r.cost.human + ")" : "") + ". Nếu phần “Mình hiểu là” đúng ý bạn, bấm “Đồng ý chạy”."),
      extraNotes(r), btns, msg,
      h("details", { style: { marginTop: "10px" } }, h("summary", { class: "sec small" }, "Xem hoặc sửa câu lệnh SQL sẽ chạy"), ta,
        h("div", { class: "muted small", style: { marginTop: "4px" } }, "Nếu bạn sửa, câu lệnh sẽ được kiểm tra lại quyền truy cập và chi phí trước khi chạy.")));
    return card;
  }
  if (T === "error" || T === "blocked") {
    add(h("div", { class: "note bad", style: { marginTop: 0 } }, T === "blocked" ? "⛔" : "⚠️", h("div", {}, h("b", {}, T === "blocked" ? "Mình không thể làm yêu cầu này. " : "Mình chưa trả lời được. "), r.text || r.error)),
      T === "blocked" ? h("div", { class: "sec small", style: { marginTop: "8px" } }, "Trợ lý chỉ đọc dữ liệu (không sửa, không xóa) và chỉ trong phạm vi tài khoản của bạn.")
        : h("div", { class: "follow" }, h("span", { class: "lb" }, "Thử một câu mẫu:"), (S.cfg.samples || []).slice(0, 3).map(s => h("button", { class: "chip", onclick: () => send(s) }, s))),
      extraNotes(r));
    if (r.sql) add(h("div", { class: "ans-ft" }, h("button", { class: "btn ghost sm", onclick: () => openHow(r) }, "📐 Xem chi tiết")));
    return card;
  }
  if (T === "rejected") { add(h("div", { class: "muted" }, "Đã hủy, truy vấn không được chạy.")); return card; }
  if (T === "info") {
    add(h("div", { class: "txt", html: md(r.text) }));
    if (r.metric_cards) add(h("div", { style: { marginTop: "8px" } }, r.metric_cards.map(c => h("div", { class: "term" },
      h("div", { class: "hd" }, h("b", {}, c.label), c.owner ? h("span", { class: "pill" }, c.owner) : null),
      (metricByName(c.name) || {}).description ? h("div", { class: "desc" }, metricByName(c.name).description) : null,
      h("div", { class: "muted small", style: { marginTop: "4px" } }, "Chia được theo: " + (c.dimensions || []).join(", "))))));
    if (r.schema_tables) add(r.schema_tables.map(t => h("details", { style: { marginTop: "8px" } }, h("summary", {}, h("b", { class: "mono" }, t.table), " · ", t.description),
      h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("tbody", {}, t.columns.map(c => h("tr", {}, h("td", { class: "mono" }, c.name), h("td", {}, c.description, c.restricted ? h("span", { class: "pill bad", style: { marginLeft: "6px" } }, "🔒 hạn chế") : null)))))))));
    if (r.suggestions) add(h("div", { class: "follow" }, r.suggestions.map(s => h("button", { class: "chip", onclick: () => send(s) }, s))));
    return card;
  }
  // result / variance
  add(understood(r), h("div", { class: "txt", html: md(r.text) }), extraNotes(r), T === "variance" ? varianceVisual(r) : resultVisual(r), trust(r), followUps(r), footer(r));
  return card;
}

// ------------------------------------------------------------------ hội thoại
function scrollToEl(el) { const sc = $("#scroll"); if (sc && el) sc.scrollTop = Math.max(0, el.offsetTop - 12); }
function appendAnswer(resp) {
  const th = $("#thread"); if (!th) return;
  const card = renderAnswer(resp);
  th.append(card);
  const prev = card.previousElementSibling;
  scrollToEl(prev && prev.classList.contains("q-user") ? prev : card);   // đưa câu hỏi + đầu câu trả lời lên đầu khung nhìn
  if (resp.conversation_id) S.conv = resp.conversation_id;
  loadConvs();
}
async function send(text, clarification = null) {
  const th = $("#thread"); if (!th || S.busy) return;
  if (!text && !clarification) return;
  closeDrawer(); closePage();
  const hero = $(".hero-wrap", th); if (hero) { th.replaceChildren(); setTitle(text); }
  th.append(h("div", { class: "q-user" }, text));
  const steps = ["Đang đọc câu hỏi", "Đang chọn chỉ số và cách tính", "Đang viết truy vấn", "Đang kiểm tra quyền truy cập và chi phí", "Đang chạy trên kho dữ liệu", "Đang viết lời giải thích và đối chiếu số liệu"];
  const label = h("span", {}, steps[0] + "…");
  const bars = h("div", { class: "steps-mini" }, steps.map((_, i) => h("i", { class: i === 0 ? "on" : "" })));
  const wait = h("div", { class: "ans" }, h("div", { class: "thinking" }, h("div", { class: "spinner" }), label), bars);
  th.append(wait); scrollToEl(wait);
  S.busy = true; syncSend();
  let k = 0;
  const timer = setInterval(() => { k = Math.min(k + 1, steps.length - 1); label.textContent = steps[k] + "…"; [...bars.children].forEach((b, i) => b.classList.toggle("on", i <= k)); }, S.cfg.mode === "none" ? 450 : 900);
  try {
    if (!S.conv) { const c = await api("/api/conversations", { body: {} }); S.conv = c.id; }
    const resp = await api("/api/chat", { body: { message: text, conversation_id: S.conv, clarification } });
    wait.remove(); appendAnswer(resp);
  } catch (e) { wait.replaceChildren(h("div", { class: "note bad", style: { marginTop: 0 } }, "⚠️", e.message)); }
  finally { clearInterval(timer); S.busy = false; syncSend(); }
}
async function loadConvs() {
  try { S.convs = await api("/api/conversations"); } catch (e) { return; }
  const list = $("#convlist"); if (!list) return;
  if (!S.convs.length) { list.replaceChildren(h("div", { class: "muted small", style: { padding: "6px 10px" } }, "Chưa có cuộc hội thoại nào.")); return; }
  list.replaceChildren(...S.convs.map(c => h("div", { class: "conv" + (c.id === S.conv && !S.page ? " active" : ""), onclick: () => openConv(c.id) },
    h("span", { class: "t", title: c.title }, c.title),
    h("button", { class: "x", title: "Xóa cuộc hội thoại", "aria-label": "Xóa", onclick: async e => {
      e.stopPropagation(); if (!confirm("Xóa cuộc hội thoại “" + c.title + "”?")) return;
      await api("/api/conversations/" + c.id, { method: "DELETE" }); if (S.conv === c.id) newConv(); else loadConvs(); } }, "✕"))));
}
function destroyCharts() { S.charts.forEach(c => { try { c.destroy(); } catch (e) { /* bỏ qua */ } }); S.charts = []; }
async function openConv(id) {
  closePage(); S.conv = id; store.set("v2_conv", id); closeRailSm();
  let c; try { c = await api("/api/conversations/" + id); } catch (e) { S.conv = null; store.set("v2_conv", null); return newConv(); }
  destroyCharts();
  const th = $("#thread"); th.replaceChildren();
  setTitle(c.title);
  for (const m of c.messages) {
    if (m.role === "user") { if (m.content.text) th.append(h("div", { class: "q-user" }, m.content.text)); }
    else th.append(renderAnswer(m.content));
  }
  if (!c.messages.length) th.append(welcome());
  const sc = $("#scroll"); sc.scrollTop = sc.scrollHeight;
  loadConvs();
}
function newConv() {
  closePage(); S.conv = null; store.set("v2_conv", null); destroyCharts(); closeRailSm();
  const th = $("#thread"); if (th) th.replaceChildren(welcome());
  setTitle(""); loadConvs();
  const ta = $("#ask"); ta && ta.focus();
}
function setTitle(t) { const el = $("#title"); if (el) el.textContent = t || "Cuộc hội thoại mới"; }

// ------------------------------------------------------------------ màn hình chào
const TASK_ICON = { lookup: "🔎", compare: "📊", why: "🧭", define: "📖" };
function welcome() {
  const u = S.user, p = S.cfg.profile || {};
  const name = callName(u);
  return h("div", { class: "hero-wrap" },
    h("div", { class: "hero" },
      h("h1", {}, name === "bạn" ? "Chào bạn, hôm nay bạn muốn xem số liệu gì?" : "Chào " + name + ", hôm nay bạn muốn xem số liệu gì?"),
      h("div", { class: "lead" }, "Hỏi bằng tiếng Việt như hỏi một đồng nghiệp" + (p.company ? ", về dữ liệu của " + p.company : "") +
        ". Mình trả về con số, biểu đồ, lời giải thích và cách tính để bạn kiểm tra lại."),
      u.rls && Object.keys(u.rls).length ? h("div", { class: "note scope" }, "🔒", "Tài khoản của bạn chỉ xem được một phần dữ liệu (" + Object.values(u.rls).map(v => [].concat(v).join(", ")).join("; ") + "). Mọi con số đều tính trong phạm vi đó.") : null),
    (p.starters || []).length ? h("div", { class: "tasks" }, p.starters.map(g => h("div", { class: "task" },
      h("span", { class: "ic" }, TASK_ICON[g.key] || "•"), h("b", {}, g.title), h("span", { class: "hint" }, g.hint),
      g.questions.map(q => h("button", { class: "q", onclick: () => send(q) }, q))))) : null,
    (p.confusable_terms || []).map(t => [
      h("div", { class: "section-t" }, "📖 Trước khi hỏi: “" + t.term + "” có " + t.options.length + " cách tính"),
      compareCards(t, true),
      h("div", { class: "tip" }, "Ghi rõ tên trong câu hỏi (vd “" + t.options[0].label.toLowerCase() + " " + (anchorMonth() || "tháng 10") + "”) thì mình trả lời ngay. Chỉ ghi “" + t.term + "” thì mình sẽ hỏi lại bạn chọn loại nào."),
    ]),
    h("div", { class: "section-t" }, "💡 Mẹo"),
    h("div", { class: "sec small" }, "Sau mỗi câu trả lời, bạn có thể hỏi tiếp ngắn gọn: “chia theo kênh”, “so với cùng kỳ”, “còn tháng trước thì sao?”. ",
      "Bấm ", h("b", {}, "Từ điển"), " ở góc trên để xem định nghĩa mọi chỉ số."),
    (p.out_of_scope || []).length ? h("div", { class: "muted small", style: { marginTop: "6px" } }, "Mình không làm: " + p.out_of_scope.join("; ") + ".") : null);
}
function showHelp() {
  const bg = h("div", { class: "modal-bg", onclick: e => { if (e.target === bg) bg.remove(); } });
  const li = (b, t) => h("li", { style: { marginBottom: "6px" } }, h("b", {}, b), t);
  bg.append(h("div", { class: "modal" }, h("h3", {}, "Cách dùng trợ lý"),
    h("ol", { style: { paddingLeft: "18px", margin: 0 } },
      li("Hỏi rõ 3 ý: ", "số gì (chỉ số), thời gian nào, chia theo gì. Ví dụ: “Doanh thu thuần theo kênh " + (anchorMonth() || "tháng 10/2026") + "”."),
      li("Đọc dòng “Mình hiểu là”: ", "trợ lý nhắc lại cách hiểu. Bấm vào tên chỉ số để xem định nghĩa."),
      li("Khi trợ lý hỏi lại: ", "một số từ (như “doanh thu”) có nhiều cách tính. Chọn một lần, trợ lý nhớ trong cả cuộc hội thoại."),
      li("Bấm “📐 Cách tính”: ", "xem chỉ số đã dùng, các bước kiểm tra và câu lệnh SQL."),
      li("Khi trợ lý xin duyệt: ", "câu hỏi đọc nhiều dữ liệu sẽ dừng lại để bạn xác nhận trước khi chạy.")),
    h("div", { class: "sec small", style: { marginTop: "10px" } }, "Các công cụ khác (xem dữ liệu, tự viết SQL, lịch sử câu hỏi" + (S.user.can_manage_schema ? ", chất lượng dữ liệu, đo độ chính xác, cài đặt" : "") + ") nằm ở mục ", h("b", {}, "Công cụ"), " bên trái."),
    h("div", { style: { textAlign: "right", marginTop: "12px" } }, h("button", { class: "btn primary", onclick: () => bg.remove() }, "Đã hiểu"))));
  document.body.append(bg);
}

// ------------------------------------------------------------------ khung ứng dụng
function syncSend() { const ta = $("#ask"), b = $("#send"); if (b && ta) b.disabled = S.busy || !ta.value.trim(); }
function closeRailSm() { const a = $(".app"); a && a.classList.remove("rail-open"); }
function toggleRail() {
  const a = $(".app");
  if (matchMedia("(max-width: 820px)").matches) a.classList.toggle("rail-open");
  else { a.classList.toggle("rail-closed"); store.set("v2_rail", a.classList.contains("rail-closed") ? "closed" : null); }
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const nx = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", nx); store.set("theme", nx);
  const b = $("#theme"); b && b.replaceChildren(icon(nx === "dark" ? "sun" : "moon"));
  if (S.page) openPage(S.page); else if (S.conv) openConv(S.conv);        // vẽ lại biểu đồ theo màu mới
}
function layout() {
  const u = S.user, c = S.cfg, p = c.profile || {};
  const ta = h("textarea", { id: "ask", rows: 1, "aria-label": "Câu hỏi", placeholder: "Hỏi về số liệu, ví dụ: Doanh thu thuần theo kênh " + (anchorMonth() || "tháng này"),
    oninput: e => { e.target.style.height = "auto"; e.target.style.height = Math.min(180, e.target.scrollHeight) + "px"; syncSend(); },
    onkeydown: e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } } });
  const submit = () => { const v = ta.value.trim(); if (!v || S.busy) return; ta.value = ""; ta.style.height = "auto"; syncSend(); send(v); };
  const dark = (document.documentElement.getAttribute("data-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")) === "dark";
  const offline = c.mode === "none";        // chưa cấu hình khóa mô hình ngôn ngữ
  const app = h("div", { class: "app" + (store.get("v2_rail") === "closed" ? " rail-closed" : "") },
    h("aside", { class: "rail" },
      h("div", { class: "rail-hd" }, h("div", { class: "logo" }, icon("logo")), h("div", { class: "brand" }, h("b", {}, p.assistant || "Trợ lý dữ liệu"), h("span", {}, p.company || ""))),
      h("button", { class: "btn primary new", onclick: newConv }, "＋ Câu hỏi mới"),
      h("div", { class: "rail-sect" }, "Gần đây"),
      h("div", { class: "convlist", id: "convlist" }),
      h("div", { class: "rail-sect" }, "Công cụ"),
      h("nav", { class: "toolnav", id: "toolnav" }, pagesFor(u).map(pg => h("button", { "data-p": pg.id, onclick: () => openPage(pg.id) }, h("span", { class: "ic" }, pg.icon), pg.label))),
      h("div", { class: "rail-ft" },
        h("div", { class: "me" }, h("div", { class: "avatar" }, (callName(u) === "bạn" ? (u.full_name || u.username || "?") : callName(u)).trim().charAt(0).toUpperCase()),
          h("div", { class: "who" }, h("b", {}, u.full_name || u.username), h("span", {}, (u.role_label || u.role) + (u.rls && Object.keys(u.rls).length ? " · 🔒 một phần dữ liệu" : "")))),
        h("div", { class: "rail-links" },
          h("button", { class: "btn ghost", onclick: logout }, "Đăng xuất")))),
    h("main", { class: "main" },
      h("div", { class: "topbar" },
        h("button", { class: "icon-btn", title: "Ẩn/hiện danh sách hội thoại", "aria-label": "Menu", onclick: toggleRail }, icon("menu")),
        h("div", { class: "title", id: "title" }, "Cuộc hội thoại mới"),
        h("span", { class: "status hide-sm", title: offline ? "Chưa cấu hình mô hình ngôn ngữ (OPENROUTER_API_KEY): trợ lý chỉ trả lời được câu hỏi nêu rõ chỉ số và thời gian." : "Trợ lý dùng mô hình " + c.model + " để hiểu câu hỏi; câu rõ ràng được trả lời nhanh không cần mô hình; truy vấn luôn được kiểm tra bằng code." },
          h("span", { class: "dot" + (offline ? " off" : "") }), offline ? "Chưa có mô hình ngôn ngữ" : "Có mô hình ngôn ngữ", " · dữ liệu đến " + (c.anchor || "")),
        h("button", { class: "btn sm", title: "Định nghĩa của mọi chỉ số", onclick: () => openGlossary() }, icon("book"), h("span", { class: "hide-sm" }, "Từ điển")),
        h("button", { class: "icon-btn", title: "Cách dùng", "aria-label": "Cách dùng", onclick: showHelp }, icon("help")),
        h("button", { class: "icon-btn", id: "theme", title: "Đổi nền sáng/tối", "aria-label": "Đổi nền sáng/tối", onclick: toggleTheme }, icon(dark ? "sun" : "moon"))),
      h("div", { class: "scroll", id: "scroll" }, h("div", { class: "thread", id: "thread" }, welcome()), h("div", { class: "page", id: "page", hidden: true })),
      h("div", { class: "composer-wrap", id: "composer" },
        h("div", { class: "composer" }, ta, h("div", { class: "bar" },
          h("button", { class: "btn ghost sm", onclick: () => openGlossary(), title: "Xem tên các chỉ số để hỏi cho rõ" }, icon("book"), "Chỉ số"),
          h("span", { class: "sp" }),
          h("button", { class: "send", id: "send", title: "Gửi (Enter)", "aria-label": "Gửi", disabled: true, onclick: submit }, icon("send")))),
        )));
  $("#app").replaceChildren(app);
  loadConvs();
  const last = store.get("v2_conv"), pg = store.get("page");
  if (pg && pagesFor(u).some(p => p.id === pg)) openPage(pg);
  else if (last) openConv(last); else ta.focus();
}

// ------------------------------------------------------------------ trang công cụ (tools.js)
function openPage(id) {
  const pg = pagesFor(S.user).find(p => p.id === id); if (!pg) return;
  S.page = id; store.set("page", id); closeRailSm(); closeDrawer(); destroyCharts();
  $("#thread").hidden = true; $("#composer").hidden = true;
  const box = $("#page"); box.hidden = false; box.replaceChildren(...[pg.fn()].flat(Infinity).filter(Boolean));
  $("#scroll").scrollTop = 0;
  setTitle(pg.icon + " " + pg.label);
  [...$("#toolnav").children].forEach(b => b.classList.toggle("active", b.dataset.p === id));
  document.querySelectorAll(".conv.active").forEach(c => c.classList.remove("active"));
}
// quay về khung hỏi đáp
function closePage() {
  if (!S.page) return;
  S.page = null; store.set("page", null);
  const box = $("#page"); box.hidden = true; box.replaceChildren();
  $("#thread").hidden = false; $("#composer").hidden = false;
  [...$("#toolnav").children].forEach(b => b.classList.remove("active"));
}

// ------------------------------------------------------------------ đăng nhập
async function boot() {
  const th = store.get("theme"); if (th) document.documentElement.setAttribute("data-theme", th);
  S.token = store.get("token");
  if (!S.token) return renderLogin();
  try { S.user = await api("/api/me"); S.cfg = await api("/api/config"); } catch (e) { return renderLogin(); }
  document.title = ((S.cfg.profile || {}).assistant || "Trợ lý dữ liệu");
  layout();
}
function logout() { S.token = null; S.user = null; S.conv = null; S.page = null; store.set("page", null); store.set("token", null); store.set("v2_conv", null); closeDrawer(); renderLogin(); }
async function renderLogin() {
  let pub = { company: "", industry: "", assistant: "Trợ lý dữ liệu", demo_users: [] };
  try { pub = await (await fetch("/api/public")).json(); } catch (e) { /* dùng mặc định */ }
  const first = (pub.demo_users || []).find(x => !x.rls && x.username !== "admin") || (pub.demo_users || [])[0] || {};
  const u = h("input", { type: "text", placeholder: "Tên đăng nhập", value: first.username || "", autocomplete: "username", "aria-label": "Tên đăng nhập" });
  const pw = h("input", { type: "password", placeholder: "Mật khẩu", value: first.password || "", autocomplete: "current-password", "aria-label": "Mật khẩu" });
  const err = h("div", {});
  const go = async (un, p) => {
    try { const r = await api("/api/login", { body: { username: un, password: p } }); S.token = r.token; store.set("token", r.token); boot(); }
    catch (e) { err.replaceChildren(h("div", { class: "note bad" }, e.message)); }
  };
  pw.onkeydown = e => { if (e.key === "Enter") go(u.value, pw.value); };
  $("#app").replaceChildren(h("div", { class: "login" }, h("div", { class: "login-card" },
    h("div", { class: "logo" }, icon("logo")),
    h("h1", {}, pub.assistant || "Trợ lý dữ liệu"),
    h("div", { class: "sec" }, "Hỏi số liệu bằng tiếng Việt" + (pub.company ? " trên dữ liệu của " + pub.company : "") + "."),
    h("div", { class: "field" }, u), h("div", { class: "field" }, pw),
    h("button", { class: "btn primary", style: { width: "100%", justifyContent: "center", marginTop: "12px" }, onclick: () => go(u.value, pw.value) }, "Đăng nhập"), err,
    (pub.demo_users || []).length ? [h("div", { class: "muted small", style: { marginTop: "16px" } }, "Dữ liệu giả lập. Đăng nhập nhanh bằng tài khoản demo:"),
      h("div", { class: "demo" }, pub.demo_users.map(x => h("button", { onclick: () => go(x.username, x.password) }, h("b", {}, x.full_name), h("span", {}, x.role_label + (x.rls ? " · một phần dữ liệu" : "")))))] : null,
    )));
}
document.addEventListener("DOMContentLoaded", boot);     // chờ tools.js nạp xong
