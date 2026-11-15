/* Trợ lý dữ liệu · các trang công cụ (mở từ mục "Công cụ" ở cột trái)
 * Dữ liệu đầu vào, tự viết SQL, chỉ số & bảng, lịch sử câu hỏi; quản trị: chất lượng dữ liệu, nguồn gốc dữ liệu, đo độ chính xác, cài đặt.
 * Dùng chung tiện ích của app.js (h, api, S, dataTable, codeBlock, renderAnswer…). */
"use strict";

// ------------------------------------------------------------------ tiện ích riêng của các trang
// thay nội dung một phần tử; khác replaceChildren ở chỗ trải mảng lồng nhau và bỏ qua null (replaceChildren sẽ in ra "[object HTMLDivElement]")
function fill(el, ...kids) { el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false)); return el; }
function modal(title, body, actions = []) {
  const bg = h("div", { class: "modal-bg", onclick: e => { if (e.target === bg) bg.remove(); } });
  const close = () => bg.remove();
  bg.append(h("div", { class: "modal wide", role: "dialog", "aria-label": title },
    h("div", { class: "modal-hd" }, h("b", {}, title), h("button", { class: "btn sm", onclick: close }, "Đóng")),
    h("div", { class: "modal-bd" }, body),
    actions.length ? h("div", { class: "modal-ft" }, actions.map(a => a(close))) : null));
  document.body.append(bg);
  return close;
}
const fmtTime = ts => new Date(ts * 1000).toLocaleString("vi-VN");
function sevPill(sev) {
  const m = { critical: ["bad", "Nghiêm trọng"], error: ["bad", "Lỗi"], warning: ["warn", "Cảnh báo"], info: ["info", "Thông tin"], ok: ["ok", "Ổn"] };
  const [c, t] = m[sev] || m.info;
  return h("span", { class: "pill " + c }, t);
}
function tabs(defs) {
  const items = defs.filter(Boolean);
  const bar = h("div", { class: "tabs", role: "tablist" }), pane = h("div", { class: "tabpane" });
  const show = i => { [...bar.children].forEach((b, k) => b.classList.toggle("on", k === i)); fill(pane, items[i].render()); };
  items.forEach((d, i) => bar.append(h("button", { onclick: () => show(i) }, d.label)));
  if (items.length) show(0);
  return h("div", {}, bar, pane);
}
function costView(c) {
  return h("div", {},
    h("div", { class: "row" }, h("span", { class: "pill" }, "Tổng: " + c.human),
      h("span", { class: "pill " + (c.decision === "approval" ? "warn" : c.decision === "blocked" ? "bad" : "ok") }, { auto: "Tự động chạy", approval: "Cần duyệt trước khi chạy", blocked: "Bị chặn" }[c.decision] || c.decision),
      c.thresholds ? h("span", { class: "muted small" }, `Ngưỡng của vai trò: cảnh báo ${fmtBytes(c.thresholds.warn_bytes)} · duyệt ${fmtBytes(c.thresholds.approval_bytes)} · chặn ${fmtBytes(c.thresholds.max_bytes)}`) : null),
    (c.tables || []).length ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, ["Bảng", "Cột được đọc", "Dòng quét / tổng", "Cắt phân vùng", "Dung lượng"].map(x => h("th", {}, x)))),
      h("tbody", {}, c.tables.map(t => h("tr", {},
        h("td", { class: "mono" }, t.table, t.large ? h("span", { class: "pill warn", style: { marginLeft: "4px" } }, "bảng lớn") : null),
        h("td", { class: "mono" }, t.columns.join(", ")),
        h("td", { class: "num" }, nf0.format(t.rows_scanned) + " / " + nf0.format(t.rows_total)),
        h("td", {}, t.partition_filter ? `${t.partition_column} ∈ [${t.partition_filter[0] || "…"}, ${t.partition_filter[1] || "…"}) · bỏ ${nf1.format(t.pruned_pct)}%` : (t.partition_column ? "không (quét toàn bảng)" : "—")),
        h("td", { class: "num" }, fmtBytes(t.bytes))))))) : null,
    (c.reasons || []).length ? h("div", { class: "note warn" }, c.reasons.join(" ")) : null);
}
function loading(text) { return h("div", { class: "thinking", style: { padding: "8px 0" } }, h("div", { class: "spinner" }), text); }
function errNote(e) { return h("div", { class: "note bad" }, "⚠️", h("div", {}, e.message || String(e))); }
function pageHead(title, sub) { return [h("h2", {}, title), sub ? h("p", { class: "sub" }, sub) : null]; }

// Chi tiết kỹ thuật của một câu trả lời (dùng trong ngăn "Cách tính")
function techDetails(r) {
  const v = r.validation || {};
  return [
    r.plan ? h("details", {}, h("summary", {}, "Kế hoạch truy vấn"), codeBlock(JSON.stringify(r.plan, null, 2))) : null,
    v.sql_exec && v.sql_exec !== r.sql ? h("details", {}, h("summary", {}, "SQL thực thi (đã thêm phân quyền dòng / giới hạn dòng)"), codeBlock(v.sql_exec)) : null,
    r.cost && (r.cost.tables || []).length ? h("details", {}, h("summary", {}, "Chi phí quét theo bảng"), costView(r.cost)) : null,
    (r.lineage || []).length ? h("details", {}, h("summary", {}, "Nguồn gốc từng cột kết quả"), h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, h("th", {}, "Cột"), h("th", {}, "Lấy từ cột trong kho"), h("th", {}, "Dữ liệu gốc"))),
      h("tbody", {}, r.lineage.map(l => h("tr", {}, h("td", {}, h("b", {}, l.column)), h("td", { class: "mono" }, l.sources.join("\n")), h("td", { class: "mono", style: { whiteSpace: "pre-line" } }, (l.raw_sources || []).join("\n")))))))) : null,
    r.audit && (r.audit.details || []).length ? h("details", {}, h("summary", {}, "Đối chiếu từng con số"), h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, ["Dữ kiện", "Hiển thị", "Tính lại", "Khớp"].map(x => h("th", {}, x)))),
      h("tbody", {}, r.audit.details.map(d => h("tr", {}, h("td", { class: "mono" }, d.fact), h("td", {}, d.text), h("td", { class: "mono" }, d.recomputed == null ? (d.source || "ngữ cảnh") : String(d.recomputed)), h("td", {}, d.match ? "✅" : "❌"))))))) : null,
    r.critic ? h("details", {}, h("summary", {}, "Tác tử kiểm tra"), codeBlock(JSON.stringify(r.critic, null, 2))) : null,
    (r.repairs || []).length ? h("details", {}, h("summary", {}, `Tự sửa truy vấn (${r.repairs.length} lần)`), r.repairs.map(x => h("div", { class: "sec small", style: { marginTop: "6px" } },
      h("b", {}, "Lần " + x.attempt + ": "), x.fix, h("div", { class: "muted" }, ((x.error || {}).code || "") + " " + ((x.error || {}).message || ""))))) : null,
    (r.trace || []).length ? h("details", {}, h("summary", {}, "Các bước xử lý"), h("ul", { class: "checks" }, r.trace.map(t => h("li", {}, h("b", {}, t.node), h("span", { class: "sec" }, t.detail || ""), h("span", { class: "muted" }, t.ms + " ms"))))) : null,
  ];
}

// ------------------------------------------------------------------ tự viết SQL
function pageConsole() {
  const ta = h("textarea", { rows: 12, class: "mono", spellcheck: "false" }, store.get("console_sql") || (S.cfg.profile || {}).console_example || "SELECT 1");
  const out = h("div", { style: { marginTop: "14px" } });
  const save = () => store.set("console_sql", ta.value);
  const check = async () => {
    save(); fill(out, loading("Đang kiểm tra…"));
    try {
      const v = await api("/api/sql/validate", { body: { sql: ta.value } });
      fill(out, h("div", { class: "card" },
        h("div", { class: "card-hd" }, h("b", {}, "Kết quả kiểm tra (chưa chạy)"), h("span", { class: "pill " + (v.ok ? "ok" : "bad") }, v.ok ? "Hợp lệ" : "Không hợp lệ")),
        h("ul", { class: "checks" }, (v.checks || []).map(c => h("li", {}, c.passed ? "✅" : "❌", h("b", {}, c.name), h("span", { class: "muted" }, c.detail || "")))),
        (v.errors || []).map(e => h("div", { class: "note bad" }, h("div", {}, h("code", {}, e.code), " ", e.message, e.hint ? h("div", { class: "muted" }, e.hint) : null))),
        (v.lint || []).map(l => h("div", { class: "note " + (l.severity === "info" ? "" : "warn") }, h("div", {}, h("code", {}, l.code), " ", l.message, l.hint ? h("span", { class: "muted" }, " — " + l.hint) : null))),
        v.cost ? h("div", { style: { marginTop: "10px" } }, costView(v.cost)) : null,
        v.sql_exec ? h("details", { style: { marginTop: "8px" } }, h("summary", {}, "SQL sẽ thực thi (sau phân quyền dòng / giới hạn dòng)"), codeBlock(v.sql_exec)) : null));
    } catch (e) { fill(out, errNote(e)); }
  };
  const run = async () => {
    save(); fill(out, loading("Đang kiểm tra và chạy…"));
    try {
      const r = await api("/api/sql/run", { body: { sql: ta.value, conversation_id: S.consoleConv || null } });
      S.consoleConv = r.conversation_id;
      fill(out, renderAnswer(r));
    } catch (e) { fill(out, errNote(e)); }
  };
  ta.onkeydown = e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); } };
  return [pageHead("Tự viết SQL", "Dành cho người biết SQL. Truy vấn đi qua cùng các chốt như câu hỏi bằng lời: chỉ đọc, phân quyền theo vai trò và theo dòng, ước lượng chi phí, tự sửa lỗi tên cột. Ctrl+Enter để chạy."),
    ta, h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "btn", onclick: check }, "🔍 Kiểm tra"), h("button", { class: "btn primary", onclick: run }, "▶ Chạy"),
      ((S.cfg.profile || {}).console_tries || []).length ? h("span", { class: "muted small" }, "Thử xem hệ thống chặn thế nào: " + S.cfg.profile.console_tries.join(" · ")) : null), out];
}

// ------------------------------------------------------------------ chỉ số & bảng
function pageSchema() {
  const q = h("input", { type: "search", placeholder: "Lọc bảng, cột, chỉ số…", style: { maxWidth: "360px" } });
  const content = h("div", { style: { marginTop: "12px" } }, loading("Đang tải danh mục…"));
  api("/api/schema").then(data => {
    const draw = () => {
      const f = norm(q.value);
      const match = s => !f || norm(s).includes(f);
      const ms = data.metrics.filter(m => match(m.label + " " + m.name + " " + (m.description || "")));
      const ts = data.tables.filter(t => match(t.table + " " + (t.description || "") + " " + t.columns.map(c => c.name + " " + (c.description || "")).join(" ")));
      fill(content, 
        h("div", { class: "card" }, h("div", { class: "card-hd" }, h("b", {}, `Chỉ số (${ms.length})`)),
          h("div", { class: "tbl-wrap", style: { maxHeight: "380px" } }, h("table", { class: "tbl" },
            h("thead", {}, h("tr", {}, ["Chỉ số", "Bộ phận", "Định nghĩa", "Công thức", "Điều kiện"].map(x => h("th", {}, x)))),
            h("tbody", {}, ms.map(m => h("tr", {}, h("td", {}, h("b", {}, m.label), h("div", { class: "muted mono" }, m.name)), h("td", {}, m.owner || ""),
              h("td", { style: { maxWidth: "380px" } }, m.description || ""), h("td", { class: "mono" }, m.sql), h("td", { class: "mono" }, m.filter || "—"))))))),
        h("div", { class: "section-t" }, `Bảng dữ liệu (${ts.length})`),
        ts.map(t => h("details", { class: "card fold" },
          h("summary", {}, h("b", { class: "mono" }, t.table), h("span", { class: "muted small" }, " · " + nf0.format(t.rows) + " dòng · " + fmtBytes(t.bytes) + (t.partition_column ? " · phân vùng " + t.partition_column : "")),
            h("div", { class: "sec small" }, t.description || "")),
          h("div", { class: "row", style: { margin: "8px 0" } },
            h("button", { class: "btn sm", onclick: () => { store.set("data_table", t.table); openPage("data"); } }, "👁 Xem dữ liệu"),
            S.user.can_manage_schema ? h("button", { class: "btn sm", onclick: () => editCatalog(t.table, t.description, t.synonyms, draw) }, "✎ Sửa mô tả bảng") : null),
          h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["Cột", "Kiểu", "Mô tả", "Giá trị mẫu", ""].map(x => h("th", {}, x)))),
            h("tbody", {}, t.columns.map(c => h("tr", {}, h("td", { class: "mono" }, c.name), h("td", { class: "muted" }, c.type),
              h("td", {}, c.description || h("span", { class: "muted" }, "(chưa có mô tả)"), c.sensitivity ? h("span", { class: "pill " + (c.restricted ? "bad" : "warn"), style: { marginLeft: "6px" } }, (c.restricted ? "🔒 " : "") + c.sensitivity) : null),
              h("td", { class: "small", style: { maxWidth: "320px" } }, c.sample_values ? c.sample_values.slice(0, 8).join(", ") : (c.range ? c.range.join(" → ") : "")),
              h("td", {}, S.user.can_manage_schema ? h("button", { class: "btn sm", title: "Sửa mô tả cột", onclick: () => editCatalog(t.table + "." + c.name, c.description, [], draw) }, "✎") : null))))))))
      );
    };
    q.oninput = draw; draw();
  }).catch(e => fill(content, errNote(e)));
  return [pageHead("Chỉ số & bảng", "Mọi chỉ số (cách tính, bộ phận chịu trách nhiệm) và các bảng dữ liệu phía sau. Cột có 🔒 bị ẩn với tài khoản của bạn."), q, content];
}
function editCatalog(key, desc, syn, redraw) {
  const d = h("textarea", { rows: 3 }, desc || "");
  const s = h("input", { type: "text", value: (syn || []).join(", ") });
  modal("Sửa mô tả: " + key, h("div", {}, h("div", { class: "muted small" }, "Mô tả nghiệp vụ (dùng khi tìm bảng liên quan và trong lời nhắc cho mô hình)"), d,
    h("div", { class: "muted small", style: { marginTop: "8px" } }, "Từ đồng nghĩa, cách nhau bằng dấu phẩy"), s),
  [close => h("button", { class: "btn primary", onclick: async () => {
    try { await api("/api/admin/catalog", { method: "PUT", body: { key, description: d.value, synonyms: s.value.split(",").map(x => x.trim()).filter(Boolean) } }); close(); toast("Đã cập nhật mô tả và chỉ mục tìm kiếm"); openPage(S.page); }
    catch (e) { toast(e.message); } } }, "Lưu")]);
}

// ------------------------------------------------------------------ dữ liệu đầu vào
function pageData() {
  const filter = h("input", { type: "search", placeholder: "Tìm bảng…" });
  const list = h("div", { class: "data-list" });
  const pane = h("div", { class: "data-pane" }, loading("Đang tải danh sách bảng…"));
  const st = { table: store.get("data_table"), offset: 0, limit: 50, q: "" };
  let tables = [];
  const pick = name => { st.table = name; st.offset = 0; st.q = ""; store.set("data_table", name); drawList(); load(); };
  const drawList = () => {
    const f = norm(filter.value.trim());
    const groups = new Map();
    tables.filter(t => !f || norm(t.name + " " + (t.description || "")).includes(f))
      .forEach(t => { if (!groups.has(t.layer)) groups.set(t.layer, []); groups.get(t.layer).push(t); });
    fill(list, ...[...groups].map(([layer, ts]) => h("div", { class: "data-grp" }, h("div", { class: "data-grp-t" }, layer),
      ts.map(t => h("button", { class: "data-item" + (t.name === st.table ? " active" : ""), title: t.description || t.name, onclick: () => pick(t.name) },
        h("span", { class: "mono" }, t.name), h("span", { class: "muted" }, nf0.format(t.rows)))))));
    if (!groups.size) list.append(h("div", { class: "muted small", style: { padding: "8px" } }, "Không có bảng phù hợp."));
  };
  const load = async () => {
    const t = tables.find(x => x.name === st.table); if (!t) return;
    fill(pane, loading("Đang đọc " + t.name + "…"));
    try {
      const d = await api("/api/data/" + encodeURIComponent(t.name) + "?limit=" + st.limit + "&offset=" + st.offset + (st.q ? "&q=" + encodeURIComponent(st.q) : ""));
      const search = h("input", { type: "search", placeholder: "Tìm trong bảng (mọi cột)…", value: st.q, style: { width: "240px", maxWidth: "100%" },
        onkeydown: e => { if (e.key === "Enter") { st.q = search.value.trim(); st.offset = 0; load(); } } });
      const size = h("select", { onchange: () => { st.limit = +size.value; st.offset = 0; load(); } },
        [25, 50, 100, 200].map(n => h("option", { value: n, selected: n === st.limit }, n + " dòng/trang")));
      const total = d.total == null ? d.rows.length : d.total;
      const from = d.rows.length ? d.offset + 1 : 0, to = d.offset + d.rows.length;
      fill(pane, 
        h("div", { class: "card" },
          h("div", { class: "row" }, h("b", { class: "mono" }, d.table), h("span", { class: "pill" }, t.layer)),
          d.description ? h("div", { class: "sec", style: { marginTop: "4px" } }, d.description) : null,
          h("div", { class: "muted small", style: { marginTop: "4px" } }, nf0.format(t.rows) + " dòng · " + t.n_columns + " cột · " + fmtBytes(t.bytes) + (d.grain ? " · mỗi dòng: " + d.grain : "")),
          d.rls_applied ? h("div", { class: "note" }, "🔒", h("div", {}, "Chỉ hiện các dòng thuộc phạm vi tài khoản của bạn.")) : null,
          (d.hidden_columns || []).length ? h("div", { class: "note warn" }, "🙈", h("div", {}, "Ẩn " + d.hidden_columns.length + " cột bị hạn chế với vai trò của bạn: ", h("span", { class: "mono" }, d.hidden_columns.join(", ")))) : null),
        h("div", { class: "row", style: { margin: "10px 0" } }, search,
          h("button", { class: "btn sm", onclick: () => { st.q = search.value.trim(); st.offset = 0; load(); } }, "Tìm"),
          st.q ? h("button", { class: "btn sm", onclick: () => { st.q = ""; st.offset = 0; load(); } }, "Bỏ lọc") : null,
          size, h("span", { class: "grow" }),
          h("button", { class: "btn sm", disabled: d.offset === 0, onclick: () => { st.offset = Math.max(0, st.offset - st.limit); load(); } }, "‹ Trước"),
          h("span", { class: "muted small" }, nf0.format(from) + "–" + nf0.format(to) + " / " + nf0.format(total) + (st.q ? " dòng khớp" : " dòng")),
          h("button", { class: "btn sm", disabled: to >= total, onclick: () => { st.offset += st.limit; load(); } }, "Sau ›"),
          h("button", { class: "btn sm", title: "Tải các dòng đang hiển thị", onclick: () => downloadCSV(d.columns.map(c => c.name), d.rows, t.name + ".csv") }, "⬇ CSV")),
        d.rows.length ? h("div", { class: "tbl-wrap data-tbl" }, h("table", { class: "tbl" },
          h("thead", {}, h("tr", {}, d.columns.map(c => h("th", { title: (c.description ? c.description + "\n" : "") + c.type }, h("span", { class: "mono" }, c.name), h("div", { class: "th-type" }, c.type))))),
          h("tbody", {}, d.rows.map(r => h("tr", {}, r.map(v => v == null ? h("td", { class: "muted" }, "null") : h("td", { class: typeof v === "number" ? "num" : "" }, String(v))))))))
          : h("div", { class: "note" }, "Không có dòng nào" + (st.q ? " khớp “" + st.q + "”" : "") + "."));
    } catch (e) { fill(pane, errNote(e)); }
  };
  filter.oninput = drawList;
  api("/api/data").then(d => {
    tables = d.tables;
    if (!tables.find(t => t.name === st.table)) st.table = (tables.find(t => t.schema === d.serving_schema && /fct/.test(t.name)) || tables[0] || {}).name;
    drawList();
    if (st.table) load(); else fill(pane, h("div", { class: "note" }, "Tài khoản này chưa được xem bảng dữ liệu nào."));
  }).catch(e => fill(pane, errNote(e)));
  return [pageHead("Dữ liệu đầu vào", "Xem trực tiếp từng dòng của các bảng mà trợ lý dùng để trả lời. Cột chứa thông tin cá nhân bị ẩn, và bạn chỉ thấy phần dữ liệu thuộc phạm vi tài khoản của mình."),
    h("div", { class: "data-grid" }, h("div", { class: "data-side" }, filter, list), pane)];
}

// ------------------------------------------------------------------ lịch sử câu hỏi
function pageLogs() {
  const box = h("div", {}, loading("Đang tải…"));
  const STATUS = { success: ["ok", "Thành công"], error: ["bad", "Lỗi"], blocked: ["bad", "Bị chặn"], pending_approval: ["warn", "Chờ duyệt"], rejected: ["warn", "Từ chối"] };
  api("/api/logs").then(d => {
    const st = d.stats;
    fill(box, 
      st ? h("div", { class: "kpis", style: { marginTop: 0, marginBottom: "12px" } }, [["Tổng truy vấn", nf0.format(st.n || 0)], ["Thành công", nf0.format(st.ok || 0)], ["Lỗi", nf0.format(st.err || 0)], ["Bị chặn", nf0.format(st.blocked || 0)],
        ["Chờ / từ chối duyệt", nf0.format((st.pending || 0) + (st.rejected || 0))], ["Lần tự sửa", nf0.format(st.retries || 0)], ["Thời gian TB", nf1.format((st.lat || 0) / 1000) + " giây"], ["Tổng dữ liệu quét", fmtBytes(st.bytes || 0)]]
        .map(([l, v]) => h("div", { class: "kpi" }, h("div", { class: "l" }, l), h("div", { class: "v sm" }, v)))) : null,
      h("div", { class: "row", style: { marginBottom: "8px" } },
        h("button", { class: "btn sm", onclick: async () => {
          const res = await fetch("/api/logs.csv", { headers: { Authorization: "Bearer " + S.token } }); const b = await res.blob();
          const a = h("a", { href: URL.createObjectURL(b), download: "query_log.csv" }); document.body.append(a); a.click(); a.remove();
        } }, "⬇ Xuất CSV"),
        d.feedback ? h("span", { class: "muted small" }, `Góp ý: ${d.feedback.up || 0} 👍 · ${d.feedback.down || 0} 👎`) : null),
      d.logs.length ? h("div", { class: "tbl-wrap", style: { maxHeight: "68vh" } }, h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, ["Thời gian", "Người dùng", "Câu hỏi", "Trạng thái", "Nguồn truy vấn", "Dữ liệu quét", "Dòng", "Thời gian xử lý"].map(x => h("th", {}, x)))),
        h("tbody", {}, d.logs.map(l => { const [c, t] = STATUS[l.status] || ["", l.status];
          return h("tr", { class: "click", onclick: () => modal("Truy vấn " + l.run_id, h("div", {}, h("p", {}, h("b", {}, l.question)), l.error ? h("div", { class: "note bad" }, l.error) : null,
            l.sql ? codeBlock(l.sql) : h("div", { class: "muted" }, "(không có SQL)"), l.approved_by ? h("div", { class: "muted small", style: { marginTop: "6px" } }, "Duyệt bởi " + l.approved_by) : null)) },
            h("td", { style: { whiteSpace: "nowrap" } }, fmtTime(l.ts)), h("td", {}, l.username, h("div", { class: "muted small" }, l.role)), h("td", { style: { maxWidth: "340px" } }, l.question),
            h("td", {}, h("span", { class: "pill " + c }, t)), h("td", {}, l.source || "—"), h("td", { class: "num" }, l.est_bytes ? fmtBytes(l.est_bytes) : "—"),
            h("td", { class: "num" }, l.rows ?? "—"), h("td", { class: "num" }, nf1.format((l.latency_ms || 0) / 1000) + " s")); }))))
        : h("div", { class: "note" }, "Chưa có câu hỏi nào."));
  }).catch(e => fill(box, errNote(e)));
  return [pageHead("Lịch sử câu hỏi", S.user.can_manage_schema ? "Mọi câu hỏi và truy vấn của tất cả người dùng, kèm trạng thái, thời gian và dữ liệu đã quét. Bấm một dòng để xem câu lệnh." : "Các câu bạn đã hỏi và truy vấn đã chạy. Bấm một dòng để xem câu lệnh."), box];
}

// ------------------------------------------------------------------ chất lượng dữ liệu
function pageDQ() {
  const box = h("div", {}, loading("Đang tải kết quả kiểm định…"));
  const draw = rep => {
    fill(box, 
      h("div", { class: "row", style: { marginBottom: "12px" } },
        h("span", { class: "pill bad" }, rep.summary.critical + " nghiêm trọng"), h("span", { class: "pill warn" }, rep.summary.warning + " cảnh báo"), h("span", { class: "pill info" }, rep.summary.info + " thông tin"),
        h("span", { class: "muted small" }, "Cập nhật " + rep.generated_at + " · " + rep.elapsed_s + " s"), h("span", { class: "grow" }),
        h("button", { class: "btn", onclick: async e => { e.target.disabled = true; e.target.textContent = "Đang chạy…"; try { draw(await api("/api/dq/run", { body: {} })); } catch (er) { toast(er.message); e.target.disabled = false; } } }, "↻ Chạy lại kiểm định")),
      (rep.active_fixes || []).length ? h("div", { class: "card", style: { marginBottom: "12px" } }, h("div", { class: "card-hd" }, h("b", {}, "Quy tắc sửa đang áp dụng")),
        rep.active_fixes.map(f => h("div", { class: "row", style: { marginBottom: "6px" } }, h("span", { class: "pill ok" }, "đang bật"), h("b", {}, f.title || f.id), h("span", { class: "muted small" }, "bởi " + f.applied_by + " lúc " + f.applied_at), h("span", { class: "grow" }),
          h("button", { class: "btn sm danger", onclick: async e => { if (!confirm("Hoàn tác quy tắc và dựng lại kho?")) return; e.target.disabled = true; e.target.textContent = "Đang dựng lại…"; try { const r = await api("/api/dq/revert/" + f.id, { body: {} }); toast("Đã hoàn tác"); draw(r.report); } catch (er) { toast(er.message); } } }, "Hoàn tác")))) : null,
      rep.findings.length ? rep.findings.map(f => findingCard(f, draw)) : h("div", { class: "note ok" }, "✓ Không phát hiện vấn đề nào."));
  };
  api("/api/dq").then(draw).catch(e => fill(box, errNote(e)));
  return [pageHead("Chất lượng dữ liệu", "Hệ thống tự kiểm tra dữ liệu: nạp thiếu, lệch bất thường, trùng lặp, mã chưa chuẩn hóa, khóa mồ côi, sai đơn vị. Vấn đề chưa xử lý được nhắc ngay trong câu trả lời có liên quan. Quy tắc sửa được kiểm duyệt rồi mới dựng lại kho."), box];
}
function findingCard(f, redraw) {
  const cls = f.status === "open" ? f.severity : "done";
  const ev = Array.isArray(f.evidence) && f.evidence.length ? f.evidence : null;
  return h("div", { class: "finding " + cls },
    h("div", { class: "row" }, sevPill(f.severity), h("b", {}, f.title), h("span", { class: "pill" }, f.check), f.table ? h("span", { class: "pill mono" }, f.table) : null,
      h("span", { class: "pill " + ({ open: "warn", handled: "ok", fixed: "ok", ok: "ok" }[f.status] || "") }, { open: "đang mở", handled: "đã xử lý ở bước làm sạch", fixed: "đã sửa", ok: "ổn" }[f.status] || f.status)),
    h("div", { style: { marginTop: "6px" } }, f.message),
    f.recommendation ? h("div", { class: "sec small", style: { marginTop: "4px" } }, "➜ " + f.recommendation) : null,
    f.test ? h("div", { class: "muted mono", style: { marginTop: "4px" } }, "Kiểm định: " + JSON.stringify(f.test)) : null,
    ev ? h("details", { style: { marginTop: "6px" } }, h("summary", {}, "Bằng chứng (" + ev.length + ")"), dataTable(Object.keys(ev[0]), ev.slice(0, 50).map(o => Object.values(o).map(v => typeof v === "object" && v !== null ? JSON.stringify(v) : v)), [])) : null,
    f.fix && f.status === "open" ? h("div", { class: "note", style: { alignItems: "center" } }, h("div", { class: "grow" }, h("b", {}, "Quy tắc sửa đề xuất: "), f.fix.description),
      h("button", { class: "btn primary sm", onclick: () => confirmFix(f, redraw) }, "Áp dụng…")) : null);
}
function confirmFix(f, redraw) {
  const fx = f.fix, res = h("div", {});
  const body = h("div", {}, h("p", { style: { marginTop: 0 } }, fx.description),
    fx.slots ? h("div", {}, h("div", { class: "muted small" }, "Biểu thức sẽ cài vào model " + fx.model + ":"), Object.entries(fx.slots).map(([k, v]) => h("div", { style: { marginTop: "6px" } }, h("b", { class: "mono" }, k), codeBlock(v)))) : null,
    fx.seed_rows ? h("div", {}, h("div", { class: "muted small" }, "Dòng ánh xạ sẽ thêm:"), codeBlock(JSON.stringify(fx.seed_rows, null, 2))) : null,
    h("div", { class: "note warn" }, "Quy tắc được kiểm tra cú pháp (không truy vấn con, không hàm bị cấm), sau đó dựng lại toàn bộ kho và chạy lại kiểm định."), res);
  modal("Áp dụng quy tắc sửa dữ liệu", body, [close => h("button", { class: "btn primary", onclick: async e => {
    e.target.disabled = true; fill(res, loading("Đang kiểm duyệt quy tắc và dựng lại kho…"));
    try {
      const r = await api("/api/dq/fix/" + f.id, { body: {} });
      if (!r.ok) { fill(res, h("div", { class: "note bad" }, (r.errors || []).join("; "))); e.target.disabled = false; return; }
      const b = r.impact.before.rows, a = r.impact.after.rows;
      fill(res, h("div", { class: "note ok" }, `✓ Đã dựng lại ${r.build.models.length} model trong ${r.build.seconds} s.`),
        h("div", { class: "muted small", style: { marginTop: "6px" } }, "Tác động lên " + r.impact.before.metric + ":"),
        dataTable(["Tháng", "Trước khi sửa", "Sau khi sửa"], b.map((x, i) => [x.thang, x.value, a[i] ? a[i].value : null]), [{ name: "Trước khi sửa", format: "currency" }, { name: "Sau khi sửa", format: "currency" }]));
      redraw(r.report);
    } catch (err) { fill(res, errNote(err)); e.target.disabled = false; }
  } }, "Xác nhận áp dụng")]);
}

// ------------------------------------------------------------------ nguồn gốc dữ liệu
function pageLineage() {
  const wrap = h("div", {}, loading("Đang dựng sơ đồ…"));
  api("/api/lineage").then(data => {
    const g = data.graph, serving = (S.cfg.profile || {}).schema || "dw";
    if (!g.nodes.length) {
      fill(wrap, h("div", { class: "note" }, "ℹ️", h("div", {}, h("b", {}, "Doanh nghiệp này chưa khai báo các bước biến đổi dữ liệu, nên chưa có sơ đồ nguồn gốc. "),
        "Hệ thống đọc thẳng các bảng đã dựng sẵn trong kho ", h("code", {}, serving), ". Xem danh sách bảng và cột ở trang ", h("button", { class: "linklike", onclick: () => openPage("schema") }, "Chỉ số & bảng"), ".")));
      return;
    }
    const layers = { raw: 0, ref: 0, stg: 1, staging: 1 }; layers[serving] = 2;
    const cols = [[], [], []];
    g.nodes.forEach(n => cols[layers[n.layer] ?? 0].push(n));
    cols.forEach(c => c.sort((a, b) => a.id.localeCompare(b.id)));
    const W = 230, X0 = 14, Y0 = 30, DY = 30, pos = {};
    cols.forEach((c, i) => c.forEach((n, j) => (pos[n.id] = { x: X0 + i * (W + 70), y: Y0 + j * DY })));
    const height = Math.max(...cols.map(c => c.length)) * DY + Y0 + 10, width = X0 + 3 * (W + 70);
    const NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
    svg.setAttribute("width", width); svg.setAttribute("height", height); Object.assign(svg.style, { position: "absolute", left: 0, top: 0 });
    const paths = [];
    g.edges.forEach(e => {
      const a = pos[e.from], b = pos[e.to]; if (!a || !b) return;
      const p = document.createElementNS(NS, "path"), x1 = a.x + 200, y1 = a.y + 12, x2 = b.x, y2 = b.y + 12, mx = (x1 + x2) / 2;
      p.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`); p.setAttribute("fill", "none"); p.setAttribute("stroke", cssVar("--border-strong")); p.setAttribute("stroke-width", "1.2");
      svg.append(p); paths.push({ e, p });
    });
    const inner = h("div", { style: { position: "relative", width: width + "px", height: height + "px" } }, svg);
    ["Dữ liệu gốc", "Đã làm sạch", "Kho để phân tích (" + serving + ")"].forEach((t, i) => inner.append(h("div", { class: "lin-head", style: { left: X0 + i * (W + 70) + "px" } }, t)));
    const nodes = {};
    g.nodes.forEach(n => { nodes[n.id] = h("div", { class: "lin-node", style: { left: pos[n.id].x + "px", top: pos[n.id].y + "px" }, title: n.desc || n.id, onclick: () => select(n.id) }, n.id, n.rows != null ? h("span", { class: "muted" }, " · " + nf0.format(n.rows)) : null); inner.append(nodes[n.id]); });
    const side = h("div", { class: "card", style: { marginTop: "14px" } }, h("div", { class: "muted" }, "Chọn một bảng ở sơ đồ."));
    fill(wrap, h("div", { class: "lineage" }, inner), side);
    const reach = (id, from, to) => { const s = new Set([id]); let ch = true; while (ch) { ch = false; g.edges.forEach(e => { if (s.has(e[to]) && !s.has(e[from])) { s.add(e[from]); ch = true; } }); } return s; };
    function select(id) {
      const rel = new Set([...reach(id, "from", "to"), ...reach(id, "to", "from")]);
      Object.entries(nodes).forEach(([k, el]) => { el.classList.toggle("sel", k === id); el.classList.toggle("dim", !rel.has(k)); });
      paths.forEach(({ e, p }) => { const on = rel.has(e.from) && rel.has(e.to); p.setAttribute("stroke", on ? cssVar("--accent") : cssVar("--border-strong")); p.setAttribute("stroke-width", on ? "2" : "1.2"); });
      const m = data.models[id];
      fill(side, h("b", { class: "mono" }, id), h("div", { class: "sec" }, (m && m.desc) || (g.nodes.find(n => n.id === id) || {}).desc || ""),
        m ? [h("div", { class: "muted small", style: { marginTop: "6px" } }, "Nguồn trực tiếp: " + m.sources.join(", ")),
          h("div", { class: "tbl-wrap", style: { maxHeight: "460px" } }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, h("th", {}, "Cột"), h("th", {}, "Lấy từ cột"))),
            h("tbody", {}, Object.entries(m.columns).map(([c, srcs]) => h("tr", { class: "click", title: "Bấm để lần ngược về dữ liệu gốc", onclick: () => colTrace(id + "." + c) }, h("td", { class: "mono" }, c), h("td", { class: "mono" }, srcs.join(", ") || "(hằng số / biểu thức)"))))))]
          : h("div", { class: "muted small", style: { marginTop: "6px" } }, "Bảng nguồn (không do model nào sinh ra)."));
    }
    async function colTrace(col) {
      try {
        const r = await api("/api/lineage/column?col=" + encodeURIComponent(col));
        const tree = n => h("li", {}, h("code", {}, n.column), n.sources.length ? h("ul", {}, n.sources.map(tree)) : null);
        modal("Lần ngược cột " + col, h("div", {}, h("div", {}, h("b", {}, "Cột gốc: "), r.raw_sources.join(", ") || "—"), h("ul", { style: { marginTop: "8px" } }, tree(r.upstream)),
          r.downstream.length ? h("div", {}, h("b", {}, "Được dùng bởi: "), r.downstream.join(", ")) : null));
      } catch (e) { toast(e.message); }
    }
    const fanIn = id => g.edges.filter(e => e.to === id).length;
    select(([...cols[2]].sort((a, b) => fanIn(b.id) - fanIn(a.id))[0] || g.nodes[0]).id);
  }).catch(e => fill(wrap, errNote(e)));
  return [pageHead("Nguồn gốc dữ liệu", "Mỗi bảng, mỗi cột được tạo ra từ nguồn nào (phân tích cú pháp SQL của các model). Chọn một bảng để xem cột; bấm một cột để lần ngược về dữ liệu gốc."), wrap];
}

// ------------------------------------------------------------------ đo độ chính xác
function pageEval() {
  const split = h("select", {}, [["all", "Toàn bộ"], ["train", "Tập học (train)"], ["dev", "Tập kiểm (dev)"]].map(([v, t]) => h("option", { value: v }, t)));
  const out = h("div", { style: { marginTop: "12px" } }), hist = h("div", {}), fsBox = h("div", {});
  const runBtn = h("button", { class: "btn primary", onclick: async () => {
    runBtn.disabled = true; fill(out, loading("Đang chạy bộ câu hỏi… (có thể mất vài phút)"));
    try { const r = await api("/api/eval/run", { body: { split: split.value, mode: null } }); showRun(r.summary, r.details); loadHist(); } catch (e) { fill(out, errNote(e)); }
    runBtn.disabled = false;
  } }, "▶ Chạy đánh giá");
  const optBtn = h("button", { class: "btn", onclick: async () => {
    optBtn.disabled = true; fill(out, loading("Đang tối ưu: đo tập kiểm → học từ lỗi trên tập học → đo lại tập kiểm…"));
    try {
      const r = await api("/api/eval/optimize", { body: { mode: null } });
      const k = (l, v, d) => h("div", { class: "kpi" }, h("div", { class: "l" }, l), h("div", { class: "v" }, v), d ? h("div", { class: "d" }, d) : null);
      const gain = r.after.execution_accuracy - r.before.execution_accuracy;
      fill(out, h("div", { class: "card" }, h("div", { class: "card-hd" }, h("b", {}, "Kết quả tự học từ ví dụ")),
        h("div", { class: "kpis", style: { marginTop: 0 } }, k("Tập kiểm, trước", r.before.execution_accuracy + "%"), k("Tập kiểm, sau", r.after.execution_accuracy + "%", (gain >= 0 ? "+" : "") + nf1.format(gain) + " điểm"),
          k("Tập học", r.train.execution_accuracy + "%"), k("Ví dụ thêm mới", r.added.length)),
        r.added.length ? h("ul", {}, r.added.map(q => h("li", {}, q))) : null));
      loadHist(); loadFs();
    } catch (e) { fill(out, errNote(e)); }
    optBtn.disabled = false;
  } }, "🧠 Tự học từ lỗi (train → dev)");
  function showRun(sm, details) {
    const k = (l, v) => h("div", { class: "kpi" }, h("div", { class: "l" }, l), h("div", { class: "v" }, v));
    const DIFF = { easy: "Dễ", medium: "Vừa", hard: "Khó", extra: "Rất khó" };
    fill(out, h("div", { class: "card" }, h("div", { class: "card-hd" }, h("b", {}, sm.label), h("span", { class: "muted small" }, sm.n + " câu · " + sm.elapsed_s + " s")),
      h("div", { class: "kpis", style: { marginTop: 0 } }, k("Trả lời đúng", sm.execution_accuracy + "%"), k("Ra được kết quả", sm.valid_sql_rate + "%"), k("Lời giải thích khớp số", sm.audit_pass_rate + "%"),
        k("Thời gian TB", nf1.format(sm.avg_latency_ms / 1000) + " s"), k("Tự sửa TB", sm.avg_retries), k("Dữ liệu quét", fmtBytes(sm.total_bytes))),
      h("div", { class: "grid2" },
        dataTable(["Độ khó", "Số câu", "Đúng (%)"], Object.entries(sm.by_difficulty).map(([a, b]) => [DIFF[a] || a, b.n, b.acc]), []),
        dataTable(["Nhóm", "Số câu", "Đúng (%)"], Object.entries(sm.by_tag).map(([a, b]) => [a, b.n, b.acc]), [])),
      details ? h("div", { class: "tbl-wrap", style: { maxHeight: "60vh" } }, h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, ["#", "Câu hỏi", "Độ khó", "Kết quả", "Lý do", "Nguồn", ""].map(x => h("th", {}, x)))),
        h("tbody", {}, details.map(d => h("tr", {}, h("td", { class: "mono" }, d.id), h("td", { style: { maxWidth: "360px" } }, d.question), h("td", {}, DIFF[d.difficulty] || d.difficulty),
          h("td", {}, d.correct ? "✅" : "❌"), h("td", { class: "small", style: { maxWidth: "280px" } }, d.reason), h("td", {}, d.pred_source || d.pred_type),
          h("td", {}, h("button", { class: "btn sm", onclick: () => modal("Câu " + d.id, h("div", {}, h("p", { style: { marginTop: 0 } }, h("b", {}, d.question)),
            h("div", { class: "muted small" }, "SQL của trợ lý"), d.pred_sql ? codeBlock(d.pred_sql) : h("div", { class: "muted" }, "—"),
            h("div", { class: "muted small", style: { marginTop: "8px" } }, "SQL đáp án"), d.gold_sql ? codeBlock(d.gold_sql) : h("div", { class: "muted" }, "—"))) }, "SQL"))))))) : null));
  }
  async function loadHist() {
    try {
      const d = await api("/api/eval");
      convInfo.textContent = (d.conversations || []).length ? `${d.conversations.length} kịch bản, ${d.conversations.reduce((a, c) => a + c.turns, 0)} lượt hỏi` : "Chưa có bộ kịch bản (eval/conversations.json).";
      fill(hist, h("div", { class: "card", style: { marginTop: "14px" } }, h("div", { class: "card-hd" }, h("b", {}, "Lịch sử đánh giá"), h("span", { class: "muted small" }, d.questions.length + " câu hỏi đơn · " + (d.conversations || []).length + " kịch bản hội thoại")),
        d.runs.length ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["Thời gian", "Nhãn", "Số câu", "Đúng", "Ví dụ trong kho", ""].map(x => h("th", {}, x)))),
          h("tbody", {}, d.runs.map(r => h("tr", {}, h("td", {}, fmtTime(r.ts)), h("td", {}, r.label), h("td", { class: "num" }, r.summary.n), h("td", { class: "num" }, r.summary.execution_accuracy + "%"), h("td", { class: "num" }, r.summary.fewshots_in_store),
            h("td", {}, h("button", { class: "btn sm", onclick: async () => { const x = await api("/api/eval/" + r.id); (x.summary.kind === "conversation" ? showConv : showRun)(x.summary, x.details); $("#scroll").scrollTop = 0; } }, "Xem")))))))
          : h("div", { class: "muted" }, "Chưa có lượt nào.")));
    } catch (e) { fill(hist, errNote(e)); }
  }
  async function loadFs() {
    try {
      const f = await api("/api/fewshots");
      fill(fsBox, h("div", { class: "card", style: { marginTop: "14px" } }, h("div", { class: "card-hd" }, h("b", { class: "grow" }, "Kho ví dụ đã xác nhận (" + f.length + ")"),
        h("button", { class: "btn sm danger", onclick: async () => { if (!confirm("Xóa mọi ví dụ do bộ tự học thêm vào?")) return; await api("/api/fewshots?source=optimizer", { method: "DELETE" }); loadFs(); } }, "Xóa ví dụ do bộ tự học thêm")),
        f.length ? h("div", { class: "tbl-wrap" }, h("table", { class: "tbl" }, h("thead", {}, h("tr", {}, ["#", "Câu hỏi", "Nguồn", "Lượt dùng", ""].map(x => h("th", {}, x)))),
          h("tbody", {}, f.map(x => h("tr", {}, h("td", {}, x.id), h("td", {}, x.question, h("details", {}, h("summary", { class: "small" }, "SQL"), codeBlock(x.sql))), h("td", {}, h("span", { class: "pill" }, x.source)), h("td", { class: "num" }, x.uses),
            h("td", {}, h("button", { class: "btn sm danger", onclick: async () => { await api("/api/fewshots/" + x.id, { method: "DELETE" }); loadFs(); } }, "Xóa")))))))
          : h("div", { class: "muted" }, "Kho trống.")));
    } catch (e) { fill(fsBox, errNote(e)); }
  }
  // ---- eval hội thoại: mỗi lượt chấm bằng code (kỳ vọng viết sẵn) và bằng mô hình (đúng ý, đủ ý, không lạc đề)
  const convInfo = h("span", { class: "muted small" });
  const judgeBox = h("input", { type: "checkbox", checked: true, id: "conv-judge" });
  const convBtn = h("button", { class: "btn primary", onclick: async () => {
    convBtn.disabled = ablBtn.disabled = true; fill(out, loading("Đang chạy các kịch bản hội thoại… (khoảng 2–3 phút)"));
    try { const r = await api("/api/eval/conversations", { body: { judge: judgeBox.checked } }); showConv(r.summary, r.details); loadHist(); } catch (e) { fill(out, errNote(e)); }
    convBtn.disabled = ablBtn.disabled = false;
  } }, "▶ Chạy eval hội thoại");
  const ablBtn = h("button", { class: "btn", title: "Chạy lại bộ kịch bản với từng tính năng bị tắt", onclick: async () => {
    if (!confirm("So sánh chạy bộ kịch bản 6 lần (mỗi lần tắt một tính năng), mất khoảng 15 phút và tốn lượt gọi mô hình. Tiếp tục?")) return;
    convBtn.disabled = ablBtn.disabled = true; fill(out, loading("Đang so sánh bật/tắt từng tính năng… (khoảng 15 phút)"));
    try { showAblation(await api("/api/eval/ablation", { body: { judge: judgeBox.checked } })); loadHist(); } catch (e) { fill(out, errNote(e)); }
    convBtn.disabled = ablBtn.disabled = false;
  } }, "⚖ So sánh bật/tắt tính năng");
  const pctTxt = v => v == null ? "—" : nf1.format(v) + "%";
  function showConv(sm, details) {
    const k = (l, v, d) => h("div", { class: "kpi" }, h("div", { class: "l" }, l), h("div", { class: "v" }, v), d ? h("div", { class: "d" }, d) : null);
    fill(out, h("div", { class: "card" }, h("div", { class: "card-hd" }, h("b", {}, sm.label), h("span", { class: "muted small" }, `${sm.n} kịch bản · ${sm.n_turns} lượt · ${sm.elapsed_s} s` + (sm.judge_model ? " · chấm bằng " + sm.judge_model : ""))),
      h("div", { class: "kpis", style: { marginTop: 0 } },
        k("Kịch bản đạt", pctTxt(sm.scenario_pass)), k("Lượt đạt", pctTxt(sm.turn_pass)),
        k("Qua kiểm tra code", pctTxt(sm.checks_pass)), k("AI chấm đúng ý", pctTxt(sm.judge_ok), sm.judged ? `${sm.disagree} lượt hai bên chấm khác nhau` : "không chấm"),
        k("Thời gian mỗi lượt", nf1.format(sm.latency_median_s || 0) + " s", `p90 ${nf1.format(sm.latency_p90_s || 0)} s · chậm nhất ${nf1.format(sm.latency_max_s || 0)} s`)),
      dataTable(["Nhóm", "Số kịch bản", "Đạt (%)"], Object.entries(sm.by_tag || {}).map(([a, b]) => [a, b.n, b.pass]), []),
      (details || []).map(d => h("details", { class: "conv-sc" + (d.ok ? "" : " bad"), open: !d.ok },
        h("summary", {}, h("span", { class: "pill " + (d.ok ? "ok" : "bad") }, d.ok ? "Đạt" : "Chưa đạt"), " ", h("b", {}, d.name), h("span", { class: "muted small" }, " · " + d.id + " · " + (d.tags || []).join(", "))),
        d.turns.map((t, i) => h("div", { class: "conv-turn" },
          h("div", {}, h("span", { class: "muted small" }, `Lượt ${i + 1} · ${nf1.format(t.elapsed_s)} s · ${t.type}${(t.parts || []).length ? " [" + t.parts.join(", ") + "]" : ""} `),
            t.checks_ok ? h("span", { class: "pill ok" }, "code ✓") : h("span", { class: "pill bad" }, "code ✗"), " ",
            t.judge ? h("span", { class: "pill " + (t.judge.verdict === "ok" ? "ok" : t.judge.verdict === "wrong" ? "bad" : "") }, "AI " + (t.judge.verdict === "ok" ? "✓" : t.judge.verdict === "wrong" ? "✗" : "?")) : null),
          h("div", {}, h("b", {}, t.q)),
          t.text ? h("div", { class: "sec small" }, t.text) : null,
          (t.fails || []).length ? h("ul", { class: "small bad-list" }, t.fails.map(f => h("li", {}, f))) : null,
          t.judge && t.judge.verdict !== "ok" && t.judge.problem ? h("div", { class: "note warn" }, "🤖", h("div", {}, t.judge.problem)) : null))))));
  }
  function showAblation(r) {
    const rows = r.rows || [];
    fill(out, h("div", { class: "card" }, h("div", { class: "card-hd" }, h("b", {}, "So sánh bật/tắt tính năng"), h("span", { class: "muted small" }, "mỗi dòng là một lượt chạy toàn bộ kịch bản; xem chi tiết ở Lịch sử")),
      dataTable(["Cấu hình", "Kịch bản đạt (%)", "Lượt đạt (%)", "Code ✓ (%)", "AI ✓ (%)", "Trung vị (s)", "p90 (s)", "Chậm nhất (s)"],
        rows.map(x => [x.variant, x.scenario_pass, x.turn_pass, x.checks_pass, x.judge_ok, x.latency_median_s, x.latency_p90_s, x.latency_max_s]), [])));
  }
  loadHist(); loadFs();
  return [pageHead("Đo độ chính xác", "Chạy bộ câu hỏi có đáp án chuẩn để biết trợ lý trả lời đúng bao nhiêu phần trăm. Một câu đúng khi bảng kết quả trùng với bảng của đáp án (mọi cột của đáp án phải có trong kết quả)."),
    h("div", { class: "row" }, h("span", { class: "sec small" }, "Câu hỏi đơn:"), split, runBtn, optBtn),
    h("div", { class: "row", style: { marginTop: "8px" } }, h("span", { class: "sec small" }, "Hội thoại nhiều lượt:"), convBtn, ablBtn,
      h("label", { for: "conv-judge", class: "row small", style: { gap: "4px" } }, judgeBox, "AI chấm đúng ý"), convInfo),
    out, hist, fsBox];
}

// ------------------------------------------------------------------ cài đặt
const SETTING_GROUPS = [
  ["Mô hình ngôn ngữ", [
    ["llm_mode", "Nhà cung cấp", "auto = OpenRouter nếu có OPENROUTER_API_KEY", { select: ["auto", "openrouter", "claude"] }],
    ["llm_model", "Model lập kế hoạch (Planner)", "Dạng nhà-cung-cấp/tên khi dùng OpenRouter", { text: true }],
    ["router_model", "Model nhanh (Router, lời giải thích, chấm điểm)", "Để trống = model mặc định", { text: true, placeholder: "anthropic/claude-haiku-5.5" }],
    ["llm_effort", "Độ sâu suy luận", "", { select: ["low", "medium", "high", "xhigh", "max"] }],
  ]],
  ["Tốc độ và đa nhiệm", [
    ["fast_path", "Đường nhanh", "Câu bộ luật hiểu trọn vẹn thì trả lời ngay, không gọi mô hình", { check: true }],
    ["speculative_plan", "Lập kế hoạch đón trước", "Planner chạy song song với Router; dùng luôn nếu Router không đổi nội dung câu", { check: true }],
    ["multi_task", "Trả lời câu nhiều ý", "Tách và trả lời song song tối đa 4 ý trong một câu", { check: true }],
    ["latency_budget_s", "Ngân sách thời gian mỗi câu (giây)", "Quá ngân sách thì bỏ bớt các bước không bắt buộc", { num: [4, 60, 1] }],
  ]],
  ["Tác tử kiểm tra", [
    ["critic", "Kiểm tra bằng code", "Đối chiếu kế hoạch với những gì câu hỏi nêu rõ và vá phần bị bỏ sót", { check: true }],
    ["critic_llm", "Mô hình chấm câu trả lời", "Chạy song song với lượt viết lời giải thích", { check: true }],
    ["critic_revise", "Lập lại kế hoạch khi bị chấm sai", "Tối đa một lần mỗi câu", { check: true }],
    ["critic_revise_until_s", "Chỉ chấm khi có kết quả trước mốc (giây)", "Để tổng thời gian không vượt ngân sách", { num: [0, 60, 1] }],
    ["variance_hypotheses", "Giả thuyết nguyên nhân cho câu “vì sao”", "Mô hình nêu tối đa 3 giả thuyết, code chạy truy vấn kiểm chứng từng cái; chỉ giả thuyết được xác nhận vào lời giải thích", { check: true }],
  ]],
  ["Truy vấn và an toàn", [
    ["always_review", "Luôn cần duyệt SQL trước khi chạy", "", { check: true }],
    ["max_repair_attempts", "Số lần tự sửa SQL tối đa", "", { num: [0, 6, 1] }],
    ["query_timeout_s", "Thời gian chờ tối đa của truy vấn (giây)", "", { num: [1, 120, 1] }],
    ["fewshot_k", "Số ví dụ đưa vào lời nhắc", "", { num: [0, 10, 1] }],
    ["fewshot_reuse_threshold", "Ngưỡng dùng lại SQL đã xác nhận", "Độ giống câu hỏi (0,5–1)", { num: [0.5, 1, 0.01] }],
    ["anchor_date", "Ngày neo cho “tháng này”", "YYYY-MM-DD; để trống = ngày dữ liệu mới nhất", { text: true, placeholder: "YYYY-MM-DD" }],
  ]],
];
function pageSettings() {
  const box = h("div", {}, loading("Đang tải cài đặt…"));
  api("/api/admin/settings").then(d => {
    const s = d.settings, els = {};
    const field = ([k, label, hint, t]) => {
      let el;
      if (t.select) el = h("select", {}, t.select.map(x => h("option", { value: x, selected: s[k] === x }, x)));
      else if (t.check) el = h("input", { type: "checkbox", checked: s[k] !== false && s[k] != null });
      else if (t.num) el = h("input", { type: "number", value: s[k] ?? "", min: t.num[0], max: t.num[1], step: t.num[2] });
      else el = h("input", { type: "text", value: s[k] || "", placeholder: t.placeholder || "" });
      els[k] = el;
      const id = "set-" + k; el.id = id;
      return h("div", { class: "set-row" + (t.check ? " check" : "") },
        t.check ? el : null,
        h("label", { for: id }, h("b", {}, label), hint ? h("span", { class: "muted small" }, hint) : null),
        t.check ? null : el);
    };
    const save = h("button", { class: "btn primary", onclick: async () => {
      const values = {};
      for (const [k, el] of Object.entries(els)) values[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : (el.value.trim() || (k === "anchor_date" ? null : ""));
      save.disabled = true;
      try { await api("/api/admin/settings", { method: "PUT", body: { values } }); S.cfg = await api("/api/config"); toast("Đã lưu cài đặt"); }
      catch (e) { toast(e.message); }
      save.disabled = false;
    } }, "Lưu cài đặt");
    fill(box, 
      h("div", { class: "note " + (d.credentials ? "ok" : "warn"), style: { marginTop: 0, marginBottom: "12px" } }, d.credentials ? "✓" : "⚠️",
        h("div", {}, d.credentials ? "Đã có khóa API của mô hình ngôn ngữ." : "Chưa có khóa API: trợ lý chỉ trả lời được câu nêu rõ chỉ số và thời gian. Đặt OPENROUTER_API_KEY trong file .env rồi khởi động lại.")),
      SETTING_GROUPS.map(([title, rows]) => h("div", { class: "card", style: { marginBottom: "12px" } }, h("div", { class: "card-hd" }, h("b", {}, title)), rows.map(field))),
      h("div", { class: "row", style: { position: "sticky", bottom: 0, padding: "10px 0", background: "var(--bg)" } }, save),
      h("details", { class: "card fold", style: { marginTop: "12px" } }, h("summary", {}, h("b", {}, "Chính sách vai trò"), h("span", { class: "muted small" }, " (config/policies.yml, chỉ xem)")), codeBlock(JSON.stringify(d.policies, null, 2))));
  }).catch(e => fill(box, errNote(e)));
  return [pageHead("Cài đặt", "Mô hình ngôn ngữ, tốc độ, tác tử kiểm tra và các ngưỡng an toàn. Thay đổi có hiệu lực ngay từ câu hỏi tiếp theo."), box];
}

// ------------------------------------------------------------------ danh sách trang
const PAGES = [
  { id: "data", icon: "🗂️", label: "Dữ liệu đầu vào", fn: pageData },
  { id: "schema", icon: "📚", label: "Chỉ số & bảng", fn: pageSchema },
  { id: "console", icon: "⌨️", label: "Tự viết SQL", fn: pageConsole },
  { id: "logs", icon: "🧾", label: "Lịch sử câu hỏi", fn: pageLogs },
  { id: "dq", icon: "🩺", label: "Chất lượng dữ liệu", fn: pageDQ, admin: true },
  { id: "lineage", icon: "🔀", label: "Nguồn gốc dữ liệu", fn: pageLineage, admin: true },
  { id: "eval", icon: "🎯", label: "Đo độ chính xác", fn: pageEval, admin: true },
  { id: "settings", icon: "⚙️", label: "Cài đặt", fn: pageSettings, admin: true },
];
const pagesFor = u => PAGES.filter(p => !p.admin || u.can_manage_schema);
