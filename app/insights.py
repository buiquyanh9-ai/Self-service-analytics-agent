"""
Agent phân tích biến động ("Vì sao doanh thu tháng 9 giảm?"):
  1. Tổng biến động kỳ này vs kỳ so sánh + kiểm định ý nghĩa thống kê (z-score so với biến động lịch sử)
  2. Phân rã đóng góp theo nhiều chiều (vùng, kênh, ngành hàng, cửa hàng...) -> chọn chiều giải thích tốt nhất
  3. Drill-down 1 cấp vào thành phần đóng góp lớn nhất
  4. Phân rã sản lượng × giá trị (số đơn vs giá trị đơn trung bình)
  5. Kiểm tra nguyên nhân gốc XÁC ĐỊNH: trôi lệch đơn vị giá (KS test), cửa hàng đóng cửa, hết hàng (tồn kho),
     lịch khuyến mãi, dữ liệu kỳ chưa đầy đủ
  6. Giả thuyết do mô hình ngôn ngữ đề xuất, code kiểm chứng: mô hình đọc bức tranh phân rã và nêu tối đa 4 giả
     thuyết dạng "chỉ số X ở phân khúc Y (1–2 chiều) giảm/tăng". Code chạy truy vấn cho từng giả thuyết và chấm:
     xác nhận / yếu / bác bỏ / không kiểm được. Lời giải thích chỉ nhắc giả thuyết được xác nhận, bằng số do code
     tính; mô hình không bao giờ đưa số vào câu trả lời. Lượt gọi mô hình chạy song song với bước 4–5.
Mọi truy vấn đều đi qua governance (RBAC/RLS/chi phí) như truy vấn thường. Mọi con số trong lời giải thích
được lấy từ kết quả truy vấn và kiểm chứng lại (audit).
"""
from __future__ import annotations

import datetime as dt
import math
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from . import profile
from .catalog import get_catalog
from .semantic import get_semantic, q as sqlq
from .textutil import fmt_money, fmt_number, fmt_pct, fmt_value, norm

MAX_AUTO_DRIVERS, MAX_DRIVER_VALUES = 6, 60
MAX_HYP, MAX_SEG_DIMS, MAX_LISTED_VALUES = 3, 2, 45
HYP_MIN_TIME_S = 5.0         # lượt gọi mô hình mất ~5 s: còn ít thời gian hơn thì không gọi (đằng nào cũng không kịp)
HYP_CONFIRM_SHARE = 0.2       # chỉ số đang phân tích: phân khúc giải thích >= 20% biến động mới coi là xác nhận
HYP_CONFIRM_PCT = 5.0         # chỉ số khác: thay đổi >= 5% đúng chiều mới coi là xác nhận
HYP_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hyp")


def analysis_config(sem, metric: str) -> dict:
    """Cấu hình phân tích biến động của doanh nghiệp (semantic_layer.yml: analysis).

      driver_dimensions: các chiều đem ra phân rã, theo thứ tự ưu tiên
      coarseness:        chiều càng thô (số nhỏ) càng được ưu tiên khi mức giải thích ngang nhau
      drill:             chiều -> các chiều đào sâu tiếp
      decomposition:     chỉ số tiền -> chỉ số đếm để tách "số lượng × giá trị trung bình"
    Doanh nghiệp không khai báo thì tự chọn: các chiều không phải thời gian mà chỉ số hỗ trợ, có danh mục 2–60 giá trị.
    """
    a = sem.cfg.get("analysis") or {}
    supported = sem.dims_for_metric(metric)
    if a.get("driver_dimensions"):
        drivers = [d for d in a["driver_dimensions"] if d in supported]
    else:
        drivers = [d for d in supported if sem.dims[d].get("kind") != "time"
                   and 2 <= len(sem.dim_values(d)) <= MAX_DRIVER_VALUES][:MAX_AUTO_DRIVERS]
    return {"drivers": drivers, "coarse": a.get("coarseness") or {d: i for i, d in enumerate(drivers)},
            "drill": a.get("drill") or {}, "decomposition": (a.get("decomposition") or {}).get(metric),
            "month": next((n for n, d in sem.dims.items() if d.get("kind") == "time" and d.get("grain") == "month"), None)}


class Facts:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def add(self, fid, value, fmt, source):
        txt = fmt_value(value, fmt) if fmt != "label" else str(value)
        if fmt == "pct_abs":
            txt = fmt_pct(abs(value))
        if fmt == "pct_signed":
            txt = fmt_pct(value, signed=True)
        self.items[fid] = {"id": fid, "value": value, "text": txt, "source": source, "fmt": fmt}
        return txt


def _pct(a, b):
    return (a - b) * 100.0 / abs(b) if b else None


def analyze(plan: dict, run, anchor: dt.date, hypothesize=None, deadline: float | None = None) -> dict:
    """hypothesize: hàm (system, user) -> dict của mô hình ngôn ngữ (None = bỏ bước 6). deadline: mốc time.time() nên
    xong; quá mốc thì bỏ giả thuyết chưa về thay vì bắt người dùng chờ."""
    sem = get_semantic()
    metric = plan["metrics"][0]
    mdef = sem.metrics[metric]
    fmt = mdef.get("format", "number")
    additive = mdef.get("additive", mdef.get("type") != "derived" and fmt != "percent")
    cur, prev = plan["time_range"], plan["variance_compare"]
    filters = plan.get("filters") or []
    facts = Facts()
    queries = []
    causes = []
    cfg = analysis_config(sem, metric)

    def runq(p, purpose):
        c = sem.compile(p)
        r = run(c["sql"], purpose)
        queries.append({"purpose": purpose, "sql": r["sql"], "bytes": r["bytes"], "rows": len(r["rows"]), "ms": r["ms"]})
        return c, r

    # 1. tổng
    _, tot = runq({"metrics": [metric], "dimensions": [], "filters": filters, "time_range": cur, "compare": prev},
                  "Tổng kỳ này vs kỳ so sánh")
    cols = tot["columns"]
    row = tot["rows"][0] if tot["rows"] else [None] * len(cols)
    now = row[cols.index(f"{metric}_ky_nay")] or 0.0
    before = row[cols.index(f"{metric}_ky_truoc")] or 0.0
    delta = now - before
    pct = _pct(now, before)
    T = {"now": now, "prev": before, "delta": delta, "pct": pct}
    t_now = facts.add("now", now, fmt, ("total", 0, f"{metric}_ky_nay"))
    t_prev = facts.add("prev", before, fmt, ("total", 0, f"{metric}_ky_truoc"))
    t_pct = facts.add("pct", pct if pct is not None else 0.0, "pct_abs", ("total", 0, "tang_truong_pct"))
    direction = "giảm" if delta < 0 else "tăng"
    lines = [f"{mdef['label']} {cur.get('label')} đạt {t_now}, {direction} {t_pct} so với {prev.get('label')} ({t_prev})."]

    # 2. ý nghĩa thống kê: so với phân phối biến động theo tháng trong lịch sử
    sig = None
    try:
        if not cfg["month"] or cfg["month"] not in sem.dims_for_metric(metric):
            raise ValueError("semantic layer chưa có chiều thời gian theo tháng cho chỉ số này")
        _, hist = runq({"metrics": [metric], "dimensions": [cfg["month"]], "filters": filters,
                        "time_range": {"start": get_catalog().data_start(), "end": cur["start"]}},
                       "Chuỗi lịch sử theo tháng (kiểm định)")
        series = [r[1] for r in hist["rows"] if r[1] is not None]
        lag_m = round((dt.date.fromisoformat(cur["start"]) - dt.date.fromisoformat(prev["start"])).days / 30.4)
        lag = 12 if lag_m >= 11 else 1
        kind = "YoY (cùng kỳ năm trước)" if lag == 12 else "MoM (tháng liền trước)"
        changes = [_pct(series[i], series[i - lag]) for i in range(lag, len(series)) if series[i - lag]]
        if len(changes) >= 5 and pct is not None:
            mu = statistics.mean(changes)
            sd = statistics.pstdev(changes) or 1e-9
            z = (pct - mu) / sd
            p_value = math.erfc(abs(z) / math.sqrt(2))
            sig = {"test": f"z-score so với phân phối biến động {kind} lịch sử", "z": round(z, 2), "p_value": round(p_value, 4),
                   "n_history": len(changes), "mean_change_pct": round(mu, 2), "std_change_pct": round(sd, 2),
                   "significant": abs(z) >= 2}
            causes.append({"kind": "significance", "severity": "info" if abs(z) < 2 else "warning",
                           "title": "Kiểm định mức độ bất thường",
                           "message": ("Mức biến động này BẤT THƯỜNG so với lịch sử" if abs(z) >= 2 else
                                       "Mức biến động này nằm trong dao động thông thường") +
                                      f" (z = {sig['z']}, p = {sig['p_value']}, so với {len(changes)} lần biến động {kind} trước đó).",
                           "test": sig})
    except Exception as e:  # pragma: no cover
        causes.append({"kind": "info", "severity": "info", "title": "Không chạy được kiểm định lịch sử", "message": str(e)})

    # 3. phân rã theo chiều
    filtered_dims = {f["dimension"] for f in filters if len(f.get("values") or []) == 1}
    drivers = []
    for d in cfg["drivers"]:
        if d in filtered_dims:
            continue
        try:
            _, r = runq({"metrics": [metric], "dimensions": [d], "filters": filters, "time_range": cur, "compare": prev},
                        f"Phân rã theo {sem.dims[d]['label'].lower()}")
        except Exception:
            continue
        items = []
        for rr in r["rows"]:
            n0, p0 = rr[1] or 0.0, rr[2] or 0.0
            items.append({"member": rr[0], "now": n0, "prev": p0, "delta": n0 - p0, "pct": _pct(n0, p0)})
        if not items:
            continue
        sgn = -1 if delta < 0 else 1
        items.sort(key=lambda x: sgn * x["delta"], reverse=True)
        for it in items:
            it["contribution_pct"] = (it["delta"] * 100.0 / delta) if delta else None
        top = items[0]
        top_share = (top["delta"] / delta) if delta else 0
        prev_total = sum(i["prev"] for i in items) or 1
        base_share = top["prev"] / prev_total
        # "lift": phần đóng góp vào biến động vượt quá tỷ trọng vốn có của thành phần ở kỳ so sánh.
        # Thành phần chiếm 85% doanh thu và giải thích 85% biến động thì không mang thông tin.
        lift = min(top_share, 1.5) - base_share
        drivers.append({"dimension": d, "label": sem.dims[d]["label"], "top_share": top_share, "base_share": base_share,
                        "lift": lift, "n_members": len(items), "items": items[:12]})
    best = None
    if drivers:
        best = max(drivers, key=lambda x: (x["lift"] - 0.03 * cfg["coarse"].get(x["dimension"], 5)))
    drill = []
    if best and additive:
        top = best["items"][0]
        l_dim = best["label"].lower()
        t_m = facts.add("drv_member", top["member"], "label", ("driver", best["dimension"], 0, "member"))
        t_d = facts.add("drv_delta", top["delta"], fmt, ("driver", best["dimension"], 0, "delta"))
        t_c = facts.add("drv_share", (top["contribution_pct"] or 0), "pct_abs", ("driver", best["dimension"], 0, "contribution_pct"))
        lines.append(f"Theo {l_dim}, {t_m} đóng góp nhiều nhất vào mức {direction} ({t_d}, tương đương {t_c} tổng biến động).")
        for dd in (cfg["drill"].get(best["dimension"]) or [])[:2]:
            if dd in filtered_dims or dd not in sem.dims_for_metric(metric):
                continue
            f2 = filters + [{"dimension": best["dimension"], "op": "in", "values": [top["member"]]}]
            try:
                _, r = runq({"metrics": [metric], "dimensions": [dd], "filters": f2, "time_range": cur, "compare": prev},
                            f"Drill-down {top['member']} theo {sem.dims[dd]['label'].lower()}")
            except Exception:
                continue
            items = [{"member": rr[0], "now": rr[1] or 0.0, "prev": rr[2] or 0.0, "delta": (rr[1] or 0.0) - (rr[2] or 0.0),
                      "pct": _pct(rr[1] or 0.0, rr[2] or 0.0)} for rr in r["rows"]]
            sgn = -1 if top["delta"] < 0 else 1
            items.sort(key=lambda x: sgn * x["delta"], reverse=True)
            for it in items:
                it["contribution_pct"] = (it["delta"] * 100.0 / top["delta"]) if top["delta"] else None
            drill.append({"dimension": dd, "label": sem.dims[dd]["label"], "within": top["member"],
                          "within_dimension": best["dimension"], "items": items[:10]})
        if drill and drill[0]["items"]:
            d0 = drill[0]
            names = [it["member"] for it in d0["items"][:3] if (it["delta"] < 0) == (top["delta"] < 0)]
            if names:
                ts = [facts.add(f"drill_{i}", n, "label", ("drill", 0, i, "member")) for i, n in enumerate(names)]
                lines.append(f"Trong {t_m}, biến động tập trung ở {d0['label'].lower()}: {', '.join(ts)}.")

    # 6a. giả thuyết: gửi mô hình ngay khi có bức tranh phân rã, chạy song song với bước 4–5
    hyp_future, hyp_ctx = None, None
    if hypothesize and (deadline is None or deadline - time.time() >= HYP_MIN_TIME_S):
        try:
            hyp_ctx = _hyp_context(sem, metric, mdef, cur, prev, T, drivers, filters, cfg)
            hyp_future = HYP_POOL.submit(hypothesize, *hypothesis_prompt(hyp_ctx))
        except Exception:
            hyp_future = None

    # 4. phân rã số lượng × giá trị trung bình (chỉ số tiền có khai báo chỉ số đếm đi kèm)
    decomposition = None
    dec = cfg["decomposition"]
    if dec and best and dec.get("count") in sem.metrics:
        cnt_m, unit_m = dec["count"], dec.get("units") if dec.get("units") in sem.metrics else None
        cnt_label = dec.get("count_label") or sem.metrics[cnt_m]["label"].lower()
        val_label = dec.get("value_label") or f"giá trị trung bình mỗi {cnt_label}"
        seg = filters + ([{"dimension": best["dimension"], "op": "in", "values": [best["items"][0]["member"]]}] if additive else [])
        try:
            res = {}
            for lab, tr in (("now", cur), ("prev", prev)):
                _, r = runq({"metrics": [metric, cnt_m] + ([unit_m] if unit_m else []), "dimensions": [], "filters": seg,
                             "time_range": tr}, f"{cnt_label.capitalize()} & sản lượng ({'kỳ này' if lab == 'now' else 'kỳ so sánh'})")
                res[lab] = dict(zip(r["columns"], r["rows"][0])) if r["rows"] else {}
            rn, rp = res["now"], res["prev"]
            if rp.get(cnt_m) and rn.get(cnt_m):
                aov_n, aov_p = (rn[metric] or 0) / rn[cnt_m], (rp[metric] or 0) / rp[cnt_m]
                vol = (rn[cnt_m] - rp[cnt_m]) * aov_p
                val = rn[cnt_m] * (aov_n - aov_p)
                decomposition = {"segment": best["items"][0]["member"] if additive else "toàn bộ", "orders_now": rn[cnt_m],
                                 "orders_prev": rp[cnt_m], "orders_pct": _pct(rn[cnt_m], rp[cnt_m]),
                                 "aov_now": aov_n, "aov_prev": aov_p, "aov_pct": _pct(aov_n, aov_p),
                                 "units_now": rn.get(unit_m) if unit_m else None, "units_prev": rp.get(unit_m) if unit_m else None,
                                 "volume_effect": vol, "value_effect": val, "count_label": cnt_label, "value_label": val_label}
                to = facts.add("ord_pct", decomposition["orders_pct"] or 0, "pct_signed", ("decomp", "orders_pct"))
                ta = facts.add("aov_pct", decomposition["aov_pct"] or 0, "pct_signed", ("decomp", "aov_pct"))
                driver_word = val_label if abs(val) > abs(vol) else (dec.get("count_driver_label") or cnt_label)
                lines.append(f"Phân rã: {cnt_label} {to}, {val_label} {ta} — biến động chủ yếu do {driver_word}.")
        except Exception:
            pass

    # 5. nguyên nhân gốc: gói kiểm tra theo ngành (nếu hồ sơ doanh nghiệp bật) + kiểm tra chung
    if profile.domain_pack() == "retail_astra":
        seg_filters = list(filters)
        if best and additive and best["dimension"] in ("vung", "chi_nhanh", "kenh", "loai_ch", "nganh_hang"):
            seg_filters = filters + [{"dimension": best["dimension"], "op": "in", "values": [best["items"][0]["member"]]}]
        causes += _check_unit_drift(run, queries, cur, prev, seg_filters, sem)
        causes += _check_closures(run, queries, cur, prev, drivers, drill)
        causes += _check_stockout(run, queries, cur, prev, drivers, drill, filters, best)
        causes += _check_promotions(run, cur, prev)
    causes += _check_incomplete_period(cur, anchor)

    # 6b. kiểm chứng giả thuyết bằng truy vấn
    hypotheses = []
    if hyp_future is not None:
        wait = max(0.3, (deadline - time.time()) if deadline else 6.0)
        try:
            cands = normalize_hypotheses(hyp_future.result(timeout=wait), sem, hyp_ctx)
        except Exception as e:
            cands = []
            hypotheses.append({"claim": "", "status": "error",
                               "reason": f"Mô hình chưa đưa giả thuyết kịp ({type(e).__name__})."})
        for hyp in cands:
            if deadline and time.time() > deadline + 1.5:
                hyp.update(status="untested", reason="Hết thời gian, chưa kiểm.")
            else:
                verify_hypothesis(hyp, runq, filters, cur, prev, T, metric, additive, sem)
            hypotheses.append(hyp)
        for i, hh in enumerate([h for h in hypotheses if h.get("status") == "confirmed"][:2]):
            seg = facts.add(f"hyp{i}_seg", hh["segment_label"], "label", ("hypothesis", i, "segment"))
            tp = facts.add(f"hyp{i}_pct", hh["pct"] if hh["pct"] is not None else 0.0, "pct_abs", ("hypothesis", i, "pct"))
            word = "giảm" if hh["delta"] < 0 else "tăng"
            extra = ""
            if hh.get("share") is not None:
                ts = facts.add(f"hyp{i}_share", hh["share"] * 100, "pct_abs", ("hypothesis", i, "share"))
                extra = f", chiếm {ts} tổng biến động"
            lines.append(f"Giả thuyết đã kiểm chứng: {sem.metrics[hh['metric']]['label'].lower()} ở {seg} {word} {tp}{extra}.")

    dq = [c for c in causes if c["kind"] == "data_quality"]
    biz = [c for c in causes if c["kind"] == "business"]
    if dq:
        lines.append("⚠ Kết luận: biến động có khả năng lớn do LỖI DỮ LIỆU, không phải do kinh doanh — " + dq[0]["short"])
    elif biz:
        lines.append("Nguyên nhân kinh doanh khả dĩ: " + "; ".join(c["short"] for c in biz[:2]) + ".")
    text = " ".join(lines)
    # audit: mọi con số trong văn bản phải đến từ facts (lấy từ kết quả truy vấn) hoặc từ phần bằng chứng kiểm định
    from .explain import NUM_RE, _ntok
    allowed = set()
    for f in facts.items.values():
        allowed |= {_ntok(t) for t in NUM_RE.findall(f["text"])}
    for c in causes:
        allowed |= {_ntok(t) for t in NUM_RE.findall(c.get("short", "") + " " + c.get("message", ""))}
    for lab in (cur.get("label", ""), prev.get("label", "")):
        allowed |= {_ntok(t) for t in NUM_RE.findall(lab)}
    stray = [t for t in NUM_RE.findall(text) if _ntok(t) not in allowed]
    audit = {"ok": not stray, "facts_checked": len(facts.items), "facts_matched": len(facts.items),
             "numbers_in_text": len(NUM_RE.findall(text)), "unverified_numbers": stray,
             "details": [{"fact": f["id"], "text": f["text"], "match": True, "source": str(f["source"])} for f in facts.items.values()]}
    chart = None
    if best:
        items = best["items"][:10]
        chart = {"type": "bar", "horizontal": len(items) > 6, "format": fmt, "labels": [str(i["member"]) for i in items],
                 "datasets": [{"label": f"Chênh lệch {mdef['label'].lower()}", "data": [i["delta"] for i in items], "role": "delta"}],
                 "title": f"Đóng góp vào biến động theo {best['label'].lower()}"}
    return {"type": "variance", "metric": metric, "metric_label": mdef["label"], "format": fmt,
            "period_now": cur, "period_prev": prev, "total": T, "significance": sig, "drivers": drivers,
            "best_dimension": best["dimension"] if best else None, "drill": drill, "decomposition": decomposition,
            "root_causes": causes, "hypotheses": hypotheses, "text": text, "facts": facts.items, "audit": audit, "queries": queries, "chart": chart}


def _seg_where(filters, sem, alias_map):
    """Chuyển bộ lọc semantic sang điều kiện SQL cho các truy vấn kiểm tra (fct_ct_dh f, dim_cn cn, dim_sp sp...)."""
    conds = []
    for f in filters:
        d = sem.dims.get(f["dimension"], {})
        e = d.get("expr")
        if not e or e.split(".")[0] not in alias_map:
            continue
        vals = ", ".join(sqlq(v) for v in f.get("values") or [])
        if vals:
            conds.append(f"{e} IN ({vals})" if f.get("op", "in") == "in" else f"{e} ILIKE {sqlq('%' + f['values'][0] + '%')}")
    return conds


def _check_unit_drift(run, queries, cur, prev, filters, sem):
    """Đơn giá thực tế / giá niêm yết: nếu trung vị tụt về ~0,001 -> đơn vị bị ghi theo nghìn đồng."""
    from scipy import stats
    out = []
    joins = ("FROM dw.fct_ct_dh f JOIN dw.dim_sp sp ON sp.ma_sp = f.ma_sp JOIN dw.dim_cn cn ON cn.ma_cn = f.ma_cn "
             "JOIN dw.dim_vung vung ON vung.ma_vung = cn.ma_vung JOIN dw.dim_kenh kenh ON kenh.ma_kenh = f.ma_kenh "
             "JOIN dw.dim_nh nh ON nh.ma_nh = sp.ma_nh")
    conds = _seg_where(filters, sem, {"f", "sp", "cn", "vung", "kenh", "nh"})
    samples = {}
    for lab, tr in (("now", cur), ("prev", prev)):
        where = [f"f.ngay_dh >= DATE '{tr['start']}'", f"f.ngay_dh < DATE '{tr['end']}'", "f.trang_thai = 'HOAN_THANH'",
                 "sp.gia_niem_yet > 0", "HASH(f.ma_dh) % 10 = 0"] + conds
        sql = f"SELECT f.dg / sp.gia_niem_yet AS ty_le_gia {joins} WHERE " + " AND ".join(where) + " LIMIT 20000"
        r = run(sql, f"Mẫu tỷ lệ đơn giá/giá niêm yết ({'kỳ này' if lab == 'now' else 'kỳ so sánh'})")
        queries.append({"purpose": f"Kiểm tra trôi lệch đơn giá ({lab})", "sql": r["sql"], "bytes": r["bytes"], "rows": len(r["rows"]), "ms": r["ms"]})
        samples[lab] = [x[0] for x in r["rows"] if x[0] and x[0] > 0]
    if len(samples["now"]) < 30 or len(samples["prev"]) < 30:
        return out
    lg = lambda xs: [math.log10(x) for x in xs]
    ks = stats.ks_2samp(lg(samples["prev"]), lg(samples["now"]))
    share_tiny_now = sum(1 for x in samples["now"] if x < 0.05) / len(samples["now"])
    share_tiny_prev = sum(1 for x in samples["prev"] if x < 0.05) / len(samples["prev"])
    med_now, med_prev = statistics.median(samples["now"]), statistics.median(samples["prev"])
    if share_tiny_now - share_tiny_prev > 0.05 and ks.pvalue < 0.01:
        # cửa hàng bị ảnh hưởng
        where = [f"f.ngay_dh >= DATE '{cur['start']}'", f"f.ngay_dh < DATE '{cur['end']}'", "f.trang_thai = 'HOAN_THANH'"] + conds
        sql = (f"SELECT cn.ten_cn, MIN(f.ngay_dh) AS tu_ngay, COUNT(*) AS so_dong, MEDIAN(f.dg / sp.gia_niem_yet) AS ty_le_tv "
               f"{joins} WHERE " + " AND ".join(where) + " AND f.dg < 0.05 * sp.gia_niem_yet GROUP BY cn.ten_cn ORDER BY so_dong DESC LIMIT 15")
        r = run(sql, "Cửa hàng có đơn giá bất thường")
        queries.append({"purpose": "Cửa hàng có đơn giá bất thường", "sql": r["sql"], "bytes": r["bytes"], "rows": len(r["rows"]), "ms": r["ms"]})
        stores = [x[0] for x in r["rows"]]
        factor = round(1 / statistics.median([x[3] for x in r["rows"]]) / 1000) * 1000 if r["rows"] else None
        since = min(str(x[1]) for x in r["rows"]) if r["rows"] else None
        pct_tiny = fmt_pct(share_tiny_now * 100)
        out.append({"kind": "data_quality", "severity": "critical", "title": "Trôi lệch đơn vị đơn giá (unit drift)",
                    "short": f"{pct_tiny} dòng bán của phân khúc này trong kỳ có đơn giá chỉ bằng khoảng 1/{factor or 1000} giá "
                             f"niêm yết, tập trung ở {len(stores)} cửa hàng — nhiều khả năng hệ thống POS ghi đơn giá theo NGHÌN đồng.",
                    "message": f"Kiểm định Kolmogorov–Smirnov trên log(đơn giá/giá niêm yết) giữa hai kỳ: D = {ks.statistic:.3f}, "
                               f"p = {ks.pvalue:.2e}. Trung vị tỷ lệ kỳ trước {med_prev:.3f}, kỳ này {med_now:.3f}. "
                               f"Cửa hàng bị ảnh hưởng: {', '.join(stores[:10])}.",
                    "test": {"name": "KS 2-sample", "statistic": round(float(ks.statistic), 4), "p_value": float(ks.pvalue)},
                    "evidence": {"stores": stores, "since": since, "factor": factor, "share_rows_affected": share_tiny_now},
                    "action": "Admin vào trang Chất lượng dữ liệu để áp dụng quy tắc sửa (nhân 1000 cho dòng bị ảnh hưởng) và build lại."})
    return out


def _check_closures(run, queries, cur, prev, drivers, drill):
    out = []
    names = set()
    for d in drivers:
        if d["dimension"] == "chi_nhanh":
            names |= {i["member"] for i in d["items"][:5] if i["now"] == 0 or (i["pct"] is not None and i["pct"] < -60)}
    for d in drill:
        if d["dimension"] == "chi_nhanh":
            names |= {i["member"] for i in d["items"][:5] if i["now"] == 0 or (i["pct"] is not None and i["pct"] < -60)}
    if not names:
        return out
    vals = ", ".join(sqlq(n) for n in names)
    sql = (f"SELECT cn.ten_cn, cn.ngay_dong, cn.ngay_mo FROM dw.dim_cn cn WHERE cn.ten_cn IN ({vals}) "
           f"AND cn.ngay_dong IS NOT NULL AND cn.ngay_dong < DATE '{cur['end']}'")
    r = run(sql, "Kiểm tra cửa hàng đóng cửa")
    queries.append({"purpose": "Kiểm tra cửa hàng đóng cửa", "sql": r["sql"], "bytes": r["bytes"], "rows": len(r["rows"]), "ms": r["ms"]})
    for name, closed, _ in r["rows"]:
        out.append({"kind": "business", "severity": "warning", "title": "Cửa hàng đóng cửa",
                    "short": f"{name} đã đóng cửa từ {closed}",
                    "message": f"dw.dim_cn ghi nhận {name} ngừng hoạt động ngày {closed} — doanh thu kỳ này giảm về 0.",
                    "evidence": {"store": name, "ngay_dong": closed}})
    return out


def _check_stockout(run, queries, cur, prev, drivers, drill, filters, best):
    out = []
    cats, region = [], None
    for f in filters:
        if f["dimension"] == "vung":
            region = f["values"][0]
        if f["dimension"] == "nganh_hang":
            cats = list(f["values"])
    if best and best["dimension"] == "vung":
        region = region or best["items"][0]["member"]
    if best and best["dimension"] == "nganh_hang":
        cats = cats or [best["items"][0]["member"]]
    for d in drill:
        if d["dimension"] == "nganh_hang" and d["items"]:
            cats = cats or [d["items"][0]["member"]]
        if d["dimension"] == "vung" and d["items"]:
            region = region or d["items"][0]["member"]
    if not cats:
        return out
    cond = [f"nh.ten_nh IN ({', '.join(sqlq(c) for c in cats)})"]
    if region:
        cond.append(f"vung.ten_vung = {sqlq(region)}")
    res = {}
    for lab, tr in (("now", cur), ("prev", prev)):
        sql = ("SELECT AVG(x.ton) AS ton_tb, MIN(x.ton) AS ton_min FROM (SELECT t.ngay_chot, SUM(t.sl_ton) AS ton "
               "FROM dw.fct_tk_tuan t JOIN dw.dim_sp sp ON sp.ma_sp = t.ma_sp JOIN dw.dim_nh nh ON nh.ma_nh = sp.ma_nh "
               "JOIN dw.dim_kho kho ON kho.ma_kho = t.ma_kho JOIN dw.dim_vung vung ON vung.ma_vung = kho.ma_vung "
               f"WHERE t.ngay_chot >= DATE '{tr['start']}' AND t.ngay_chot < DATE '{tr['end']}' AND " + " AND ".join(cond) +
               " GROUP BY t.ngay_chot) x")
        r = run(sql, "Tồn kho trung bình theo tuần")
        queries.append({"purpose": f"Kiểm tra tồn kho ({lab})", "sql": r["sql"], "bytes": r["bytes"], "rows": len(r["rows"]), "ms": r["ms"]})
        res[lab] = r["rows"][0] if r["rows"] else (None, None)
    (tn, mn), (tp, _) = res["now"], res["prev"]
    if tn is not None and tp and tn < 0.35 * tp:
        where = f"{', '.join(cats)}{' tại ' + region if region else ''}"
        out.append({"kind": "business", "severity": "warning", "title": "Thiếu hàng / hết hàng (stockout)",
                    "short": f"tồn kho {where} giảm {fmt_pct((1 - tn / tp) * 100)} (trung bình {fmt_number(round(tn))} so với {fmt_number(round(tp))} sản phẩm/tuần)",
                    "message": f"Tồn kho bình quân tuần của {where}: kỳ này {fmt_number(round(tn))}, kỳ so sánh {fmt_number(round(tp))}; "
                               f"tồn thấp nhất {fmt_number(mn or 0)}. Doanh số giảm do không đủ hàng để bán.",
                    "evidence": {"categories": cats, "region": region, "avg_stock_now": tn, "avg_stock_prev": tp}})
    return out


def _check_incomplete_period(cur, anchor):
    """Kiểm tra chung cho mọi doanh nghiệp: kỳ đang phân tích đã có đủ dữ liệu chưa."""
    if dt.date.fromisoformat(cur["end"]) <= anchor + dt.timedelta(days=1):
        return []
    return [{"kind": "data_quality", "severity": "warning", "title": "Kỳ phân tích chưa đủ dữ liệu",
             "short": f"kỳ {cur['label']} mới có dữ liệu đến {anchor.strftime('%d/%m/%Y')}",
             "message": "So sánh kỳ chưa kết thúc với kỳ đầy đủ sẽ cho biến động âm giả tạo."}]


def _check_promotions(run, cur, prev):
    out = []
    sql = ("SELECT k.ten_km, k.tu_ngay, k.den_ngay FROM dw.dim_km k WHERE k.tu_ngay < DATE '{e}' AND k.den_ngay >= DATE '{s}'")
    promos = {}
    for lab, tr in (("now", cur), ("prev", prev)):
        r = run(sql.format(s=tr["start"], e=tr["end"]), "Chương trình khuyến mãi trong kỳ")
        promos[lab] = [x[0] for x in r["rows"]]
    key = lambda p: re.sub(r"[\s/]*\d{4}$", "", p).strip()   # "Siêu sale 9/9/2026" ~ "Siêu sale 9/9"
    if {key(p) for p in promos["now"]} != {key(p) for p in promos["prev"]}:
        only_prev = [p for p in promos["prev"] if key(p) not in {key(x) for x in promos["now"]}]
        only_now = [p for p in promos["now"] if key(p) not in {key(x) for x in promos["prev"]}]
        msg = []
        if only_prev:
            msg.append(f"kỳ so sánh có CTKM {', '.join(only_prev)}")
        if only_now:
            msg.append(f"kỳ này có CTKM {', '.join(only_now)}")
        if msg:
            out.append({"kind": "calendar", "severity": "info", "title": "Khác biệt lịch khuyến mãi",
                        "short": "; ".join(msg), "message": "Lịch khuyến mãi khác nhau giữa hai kỳ: " + "; ".join(msg) + ".",
                        "evidence": promos})
    return out


# ------------------------------------------------------------------ giả thuyết: mô hình đề xuất, code kiểm chứng
HYP_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["hypotheses"],
    "properties": {"hypotheses": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["claim", "metric", "segment", "expect"],
        "properties": {
            "claim": {"type": "string"},
            "metric": {"type": "string"},
            "segment": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                   "required": ["dimension", "value"],
                                                   "properties": {"dimension": {"type": "string"}, "value": {"type": "string"}}}},
            "expect": {"type": "string", "enum": ["giảm", "tăng"]}}}}}}
HYP_SHAPE = ('{"hypotheses": [{"claim": "...", "metric": "<tên chỉ số>", '
             '"segment": [{"dimension": "<tên chiều>", "value": "<giá trị>"}], "expect": "giảm"}]}')


def _hyp_metrics(sem, metric: str, cfg: dict) -> list[str]:
    """Chỉ số được dùng trong giả thuyết: chỉ số đang phân tích, các chỉ số tách số lượng × giá trị, và danh sách
    analysis.hypothesis_metrics của doanh nghiệp."""
    dec = cfg.get("decomposition") or {}
    cands = [metric, dec.get("count"), dec.get("units")] + list((sem.cfg.get("analysis") or {}).get("hypothesis_metrics") or [])
    out = []
    for m in cands:
        if m and m in sem.metrics and m not in out:
            out.append(m)
    return out


def _hyp_context(sem, metric, mdef, cur, prev, T, drivers, filters, cfg) -> dict:
    dims = []
    for d in list(cfg["drivers"]) + [x for v in (cfg["drill"] or {}).values() for x in v]:
        if d in sem.dims and d not in dims and sem.dims[d].get("kind") != "time":
            dims.append(d)
    return {
        "metric": metric, "metric_label": mdef["label"], "period_now": cur.get("label"), "period_prev": prev.get("label"),
        "total": T,
        "filters": [f"{sem.dims[f['dimension']]['label']}: {', '.join(map(str, f.get('values') or []))}"
                    for f in filters if f.get("dimension") in sem.dims],
        "drivers": [{"dimension": d["dimension"], "label": d["label"],
                     "top": [{"member": i["member"], "pct": i["pct"]} for i in d["items"][:4]]} for d in drivers],
        "metrics": {m: sem.metrics[m]["label"] for m in _hyp_metrics(sem, metric, cfg)},
        "dims": {d: {"label": sem.dims[d]["label"], "values": [str(v) for v in sem.dim_values(d)[:MAX_LISTED_VALUES]]}
                 for d in dims},
    }


def hypothesis_prompt(ctx: dict) -> tuple[str, str]:
    system = (
        "Bạn là chuyên viên phân tích kinh doanh. Nhiệm vụ: đề xuất GIẢ THUYẾT về nguyên nhân biến động của một chỉ số. "
        "Mỗi giả thuyết phải kiểm chứng được bằng một truy vấn dạng: chỉ số M ở phân khúc S (1–2 cặp chiều = giá trị) "
        f"giảm/tăng so với kỳ so sánh. Tối đa {MAX_HYP} giả thuyết. Ưu tiên giả thuyết KHÁC với việc nhắc lại thành phần "
        "đóng góp lớn nhất đã có: kết hợp hai chiều (ví dụ một kênh trong một miền), hoặc dùng chỉ số khác để giải thích "
        "cơ chế (số đơn giảm hay giá trị mỗi đơn giảm, chiết khấu tăng, trả hàng tăng...). "
        "Chỉ dùng tên chỉ số, tên chiều và giá trị có trong danh sách được cho, viết đúng như danh sách. "
        "claim: một câu tiếng Việt ngắn (dưới 15 từ) nêu giả thuyết và cơ chế, KHÔNG ghi chữ số nào. "
        "Không có giả thuyết nào hợp lý thì trả danh sách rỗng.")
    T = ctx["total"]

    def pct(x):
        return "n/a" if x is None else f"{x:+.1f}%"
    lines = [f"Chỉ số đang phân tích: {ctx['metric']} ({ctx['metric_label']})",
             f"Kỳ này: {ctx['period_now']} | kỳ so sánh: {ctx['period_prev']} | thay đổi: {pct(T.get('pct'))}"]
    if ctx["filters"]:
        lines.append("Phạm vi đang lọc: " + "; ".join(ctx["filters"]))
    lines.append("Phân rã đã có (thành phần đóng góp lớn nhất theo từng chiều, % thay đổi của thành phần):")
    for d in ctx["drivers"]:
        lines.append(f"- {d['dimension']} ({d['label']}): " + ", ".join(f"{t['member']} {pct(t['pct'])}" for t in d["top"]))
    lines.append("Chỉ số được dùng: " + "; ".join(f"{k} ({v})" for k, v in ctx["metrics"].items()))
    lines.append("Chiều và giá trị được dùng:")
    for k, v in ctx["dims"].items():
        lines.append(f"- {k} ({v['label']}): " + ", ".join(v["values"]))
    return system, "\n".join(lines)


def normalize_hypotheses(raw, sem, ctx: dict) -> list[dict]:
    """Chỉ giữ giả thuyết dùng đúng chỉ số / chiều / giá trị được phép (giá trị so khớp không phân biệt hoa thường, dấu)."""
    items = raw.get("hypotheses") if isinstance(raw, dict) else None
    out, seen = [], set()
    for h in items or []:
        if not isinstance(h, dict) or h.get("metric") not in ctx["metrics"]:
            continue
        m = h["metric"]
        seg, bad = [], False
        for x in (h.get("segment") or [])[:MAX_SEG_DIMS + 1]:
            d, v = (x or {}).get("dimension"), str((x or {}).get("value") or "").strip()
            vals = (ctx["dims"].get(d) or {}).get("values") or []
            if d in ctx["dims"] and len(vals) >= MAX_LISTED_VALUES:
                vals = [str(c) for c in sem.dim_values(d)]
            hit = next((str(c) for c in vals if norm(str(c)) == norm(v)), None)
            if not hit or d not in sem.dims_for_metric(m) or any(y["dimension"] == d for y in seg):
                bad = True
                break
            seg.append({"dimension": d, "value": hit})
        if bad or not seg or len(seg) > MAX_SEG_DIMS:
            continue
        key = (m, tuple(sorted((y["dimension"], y["value"]) for y in seg)))
        if key in seen:
            continue
        seen.add(key)
        claim = str(h.get("claim") or "").strip()
        if re.search(r"\d", claim):        # mô hình tự ghi số: bỏ câu chữ, dùng mô tả do code tạo
            claim = ""
        expect = "tăng" if h.get("expect") == "tăng" else "giảm"
        label = " · ".join(f"{sem.dims[y['dimension']]['label']} {y['value']}" for y in seg)
        out.append({"claim": claim or f"{sem.metrics[m]['label']} ở {label} {expect}", "metric": m,
                    "metric_label": sem.metrics[m]["label"], "segment": seg, "segment_label": label, "expect": expect})
        if len(out) >= MAX_HYP:
            break
    return out


def verify_hypothesis(h: dict, runq, filters, cur, prev, T, metric, additive, sem) -> dict:
    """Chạy truy vấn cho phân khúc của giả thuyết và chấm theo số liệu thật."""
    m = h["metric"]
    seg_f = [{"dimension": y["dimension"], "op": "in", "values": [y["value"]]} for y in h["segment"]]
    h["format"] = sem.metrics[m].get("format", "number")
    try:
        _, r = runq({"metrics": [m], "dimensions": [], "filters": list(filters) + seg_f, "time_range": cur, "compare": prev},
                    f"Kiểm chứng giả thuyết: {sem.metrics[m]['label'].lower()} ở {h['segment_label']}")
        row = dict(zip(r["columns"], r["rows"][0])) if r["rows"] else {}
        now, before = row.get(f"{m}_ky_nay"), row.get(f"{m}_ky_truoc")
    except Exception as e:
        h.update(status="untested", reason=f"Không chạy được truy vấn kiểm chứng: {str(e)[:120]}")
        return h
    if now is None and before is None:
        h.update(status="rejected", reason="Phân khúc này không có dữ liệu ở cả hai kỳ.")
        return h
    now, before = now or 0.0, before or 0.0
    delta, pct = now - before, _pct(now, before)
    want = -1 if h["expect"] == "giảm" else 1
    share = None
    # tỷ lệ "giải thích bao nhiêu % biến động" chỉ có nghĩa khi tổng biến động đáng kể và phân khúc không vượt xa tổng
    if m == metric and additive and T.get("delta") and abs(T.get("pct") or 0) >= 1.0:
        share = delta / T["delta"]
        if not 0 < share <= 1.5:
            share = None
    h.update(now=now, prev=before, delta=delta, pct=pct, share=share)
    if delta * want <= 0:
        h.update(status="rejected", reason="Số liệu đi ngược chiều giả thuyết (hoặc không đổi).")
    elif share is not None:
        ok = share >= HYP_CONFIRM_SHARE
        h.update(status="confirmed" if ok else "weak",
                 reason="Phân khúc giải thích phần đáng kể của biến động." if ok
                 else "Đúng chiều nhưng chỉ giải thích phần nhỏ của biến động.")
    else:
        ok = pct is None or abs(pct) >= HYP_CONFIRM_PCT
        h.update(status="confirmed" if ok else "weak",
                 reason="Thay đổi đúng chiều và đủ lớn." if ok else "Đúng chiều nhưng thay đổi nhỏ.")
    return h
