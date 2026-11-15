"""
Eval hội thoại: đo bot có trả lời ĐÚNG Ý không, qua nhiều lượt, chứ không chỉ đúng số ở từng câu đơn lẻ.

Mỗi kịch bản (eval/conversations.json) là một cuộc hội thoại nhiều lượt chạy qua đường đi thật của người dùng (có
viết lời giải thích, có dừng chờ duyệt). Mỗi lượt được chấm hai cách, độc lập nhau:
  1) Kiểm tra bằng code (xác định): loại câu trả lời, số ý, chỉ số / chiều / bộ lọc / kỳ trong kế hoạch, chữ có hoặc
     không được có trong câu trả lời, số dòng, thời gian. Viết trong trường "expect" của lượt.
  2) Mô hình ngôn ngữ chấm (LLM-judge): đọc lịch sử hội thoại + câu trả lời, chấm "ok" / "wrong" theo tiêu chí trúng
     trọng tâm, đủ ý, đúng ngữ cảnh câu nối tiếp, không lạc đề. Mô hình chấm có thể khác mô hình của bot.
Một lượt đạt khi qua mọi kiểm tra code VÀ mô hình chấm không nói "wrong". Báo cáo tách riêng hai nguồn, kèm số lượt
hai nguồn bất đồng (dấu hiệu kiểm tra code còn hở, hoặc mô hình chấm sai).

ablation(): chạy cùng bộ kịch bản với từng tính năng bị tắt (Critic, đa nhiệm, đón trước, đường nhanh, giả thuyết)
để thấy mỗi tính năng đóng góp gì vào độ đúng ý và thời gian.
"""
from __future__ import annotations

import json
import statistics
import unicodedata
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import config, critic, llm
from .governance import UserCtx
from .store import get_store
from .textutil import norm

CONV_FILE = config.EVAL_DIR / "conversations.json"
JUDGE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="conv-judge")

ABLATIONS = [
    ("Đầy đủ", {}),
    ("Tắt Critic", {"critic": False}),
    ("Tắt đa nhiệm", {"multi_task": False}),
    ("Tắt lập kế hoạch đón trước", {"speculative_plan": False}),
    ("Tắt giả thuyết biến động", {"variance_hypotheses": False}),
    ("Tắt đường nhanh", {"fast_path": False}),
]


def load_scenarios() -> list[dict]:
    return json.loads(CONV_FILE.read_text(encoding="utf-8")) if CONV_FILE.is_file() else []


# ------------------------------------------------------------------ kiểm tra bằng code
def _parts(r: dict) -> list[dict]:
    return r.get("parts") or [r]


def _plans(r: dict) -> list[dict]:
    return [p["plan"] for p in _parts(r) if isinstance(p.get("plan"), dict)]


def _all_text(r: dict) -> str:
    return " ".join([r.get("text") or ""] + [p.get("text") or "" for p in r.get("parts") or []])


def _fold(t: str) -> str:
    return " ".join(unicodedata.normalize("NFC", t or "").lower().replace("*", "").split())


def check_turn(r: dict, exp: dict, elapsed_s: float) -> list[str]:
    """Trả danh sách điều kiện KHÔNG đạt (rỗng = đạt hết)."""
    fails = []
    typ = r.get("type")
    plans = _plans(r)
    if "type" in exp:
        want = exp["type"] if isinstance(exp["type"], list) else [exp["type"]]
        if typ not in want:
            fails.append(f"loại câu trả lời là {typ}, cần {'/'.join(want)}")
    parts = [p.get("type") for p in r.get("parts") or []]
    if "parts" in exp and parts != exp["parts"]:
        fails.append(f"các ý là {parts}, cần {exp['parts']}")
    if "n_parts" in exp and len(parts) != exp["n_parts"]:
        fails.append(f"có {len(parts)} ý, cần {exp['n_parts']}")
    if "part_types_in" in exp and any(t not in exp["part_types_in"] for t in parts or [typ]):
        fails.append(f"có ý thuộc loại ngoài {exp['part_types_in']}: {parts or [typ]}")
    if "types_include" in exp and not set(exp["types_include"]) <= set(parts or [typ]):
        fails.append(f"thiếu loại ý {sorted(set(exp['types_include']) - set(parts or [typ]))}")
    metrics = [m for p in plans for m in p.get("metrics") or []]
    for m in exp.get("metrics") or []:
        if m not in metrics:
            fails.append(f"không có chỉ số {m} (kế hoạch: {metrics})")
    if "first_metric" in exp and (not plans or (plans[0].get("metrics") or [None])[0] != exp["first_metric"]):
        fails.append(f"chỉ số đầu tiên là {(plans[0].get('metrics') or [None])[0] if plans else None}, cần {exp['first_metric']}")
    dims = [d for p in plans for d in p.get("dimensions") or []]
    for d in exp.get("dimensions") or []:
        if d not in dims:
            fails.append(f"không chia theo {d} (kế hoạch: {dims})")
    for dim, vals in (exp.get("filters") or {}).items():
        fl = [f for p in plans for f in p.get("filters") or [] if f.get("dimension") == dim]
        if not fl:
            fails.append(f"không lọc theo {dim}")
        elif vals and not any({norm(str(v)) for v in vals} <= {norm(str(x)) for x in f.get("values") or []} for f in fl):
            fails.append(f"lọc {dim} không gồm {vals}")
    if "time_start" in exp:
        starts = [(p.get("time_range") or {}).get("start") for p in plans if p.get("time_range")]
        if not starts or any(s_ != exp["time_start"] for s_ in starts):
            fails.append(f"kỳ bắt đầu {starts}, cần {exp['time_start']}")
    if exp.get("compare") and not any(p.get("compare") or p.get("variance_compare") for p in plans):
        fails.append("không có kỳ so sánh")
    if "limit" in exp and not any(p.get("limit") == exp["limit"] for p in plans):
        fails.append(f"không lấy {exp['limit']} dòng đầu")
    # so khớp chữ: không phân biệt hoa thường nhưng GIỮ dấu (bỏ dấu thì "tỷ ₫" thành "ty", khớp nhầm "Công ty")
    text = _fold(_all_text(r))
    if "text_starts" in exp and not _fold(r.get("text") or "").startswith(_fold(exp["text_starts"])):
        fails.append(f"câu trả lời không mở đầu bằng “{exp['text_starts']}”")
    for t in exp.get("text_has") or []:
        if _fold(t) not in text:
            fails.append(f"câu trả lời thiếu “{t}”")
    for t in exp.get("text_not") or []:
        if _fold(t) in text:
            fails.append(f"câu trả lời không được có “{t}”")
    rows = [x for p in _parts(r) for x in p.get("rows") or []]
    if exp.get("no_rows") and rows:
        fails.append(f"không được trả bảng số liệu (có {len(rows)} dòng)")
    if "rows_max" in exp and len(rows) > exp["rows_max"]:
        fails.append(f"có {len(rows)} dòng, tối đa {exp['rows_max']}")
    if "max_s" in exp and elapsed_s > exp["max_s"]:
        fails.append(f"mất {elapsed_s:.1f} s, tối đa {exp['max_s']} s")
    return fails


# ------------------------------------------------------------------ mô hình chấm
JUDGE_SYSTEM = (
    "Bạn chấm câu trả lời của một trợ lý dữ liệu doanh nghiệp (Text-to-SQL) trong một cuộc hội thoại. Chỉ chấm lượt "
    "CUỐI. Chấm \"ok\" khi câu trả lời: trúng trọng tâm câu hỏi (đúng chỉ số, đúng kỳ, đúng bộ lọc, đúng cách chia mà "
    "người dùng nêu hoặc ngụ ý qua ngữ cảnh các lượt trước); trả lời ĐỦ mọi ý nếu câu có nhiều ý; câu nối tiếp ngắn được "
    "hiểu đúng theo ngữ cảnh. Các cách xử lý sau cũng là \"ok\": hỏi lại khi từ ngữ thật sự mơ hồ; dừng chờ người dùng "
    "duyệt truy vấn đọc nhiều dữ liệu; từ chối hoặc giải thích lịch sự khi câu hỏi ngoài phạm vi dữ liệu, đòi sửa xóa "
    "dữ liệu, hoặc đòi dự báo; trả lời chỉ trong phạm vi dữ liệu người dùng được xem. Chấm \"wrong\" khi: bỏ sót ý, "
    "hiểu sai chỉ số / kỳ / bộ lọc, quên ngữ cảnh của câu nối tiếp, lạc đề, đưa số liệu cho câu không hỏi số liệu, hoặc "
    "câu trả lời rỗng / lỗi. KHÔNG chấm văn phong, KHÔNG tự tính lại số (số đã được code đối chiếu). Định nghĩa chỉ số "
    "của doanh nghiệp (nếu có) là chuẩn, không chê theo hiểu biết chung. problem: nêu cụ thể sai ở đâu (rỗng nếu ok); "
    "fix: một câu gợi ý sửa.")


def _describe_answer(r: dict) -> str:
    out = []
    for i, p in enumerate(_parts(r), 1):
        head = f"[Ý {i}: {p.get('task') or ''}] " if r.get("parts") else ""
        out.append(f"{head}loại: {p.get('type')}")
        if isinstance(p.get("plan"), dict):
            out.append(critic.describe_plan(p["plan"], None))
        txt = p.get("text") or ""
        out.append("Lời trả lời: " + (txt if len(txt) <= 2500 else txt[:2500] + " […phần sau bị người chấm lược bớt, không phải lỗi của trợ lý]"))
        if p.get("columns") and p.get("rows"):
            out.append("Bảng (" + str(p.get("row_count") or len(p["rows"])) + " dòng), cột: " + ", ".join(map(str, p["columns"]))
                       + "; vài dòng đầu: " + json.dumps(p["rows"][:3], ensure_ascii=False, default=str)[:400])
        if p.get("clarification"):
            out.append("Lựa chọn đưa ra: " + "; ".join(map(str, p["clarification"].get("options") or [])))
        notes = list(p.get("assumptions") or []) + list(p.get("notes") or [])
        if notes:
            out.append("Ghi chú hiện kèm câu trả lời: " + " | ".join(map(str, notes))[:800])
    if r.get("parts"):
        out.insert(0, "Tổng: " + (r.get("text") or "")[:600])
    return "\n".join(out)


def judge_prompt(history: list[tuple[str, str]], question: str, r: dict, goal: str | None,
                 user: UserCtx | None = None) -> tuple[str, str]:
    lines = []
    if user is not None and user.rls:
        lines.append("Phạm vi tài khoản người hỏi (phân quyền theo dòng, trợ lý KHÔNG được trả dữ liệu ngoài phạm vi này): "
                     + "; ".join(f"{k} = {', '.join(map(str, v if isinstance(v, list) else [v]))}" for k, v in user.rls.items()))
    if history:
        lines.append("Các lượt trước:")
        for q, a in history[-4:]:
            lines.append(f"- Người dùng: {q}\n  Trợ lý: {a[:300]}")
    lines.append(f"\nLượt cuối, người dùng hỏi: {question}")
    if goal:
        lines.append(f"Ý định của người dùng (do người viết kịch bản ghi): {goal}")
    lines.append("\nCâu trả lời của trợ lý:\n" + _describe_answer(r))
    gl = "\n".join(filter(None, (critic.glossary(p) for p in _plans(r))))
    if gl:
        lines.append("\nĐịnh nghĩa của doanh nghiệp:\n" + gl)
    return JUDGE_SYSTEM, "\n".join(lines)


# ------------------------------------------------------------------ chạy
def _user(name: str | None) -> UserCtx:
    if not name:
        return UserCtx("eval_bot", "admin", {})
    u = (config.policies().get("users") or {}).get(name)
    if not u:
        raise ValueError(f"Kịch bản dùng tài khoản không có trong policies.yml: {name}")
    return UserCtx(name, u["role"], u.get("rls") or {}, full_name=u.get("full_name", ""))


def _pct(rows, key):
    return round(100.0 * sum(1 for x in rows if x[key]) / len(rows), 1) if rows else None


def run_conversations(judge: bool = True, overrides: dict | None = None, label: str | None = None,
                      ids: list[str] | None = None, judge_model: str | None = None, save: bool = True,
                      scenarios: list[dict] | None = None) -> dict:
    from .agent import get_agent
    store = get_store()
    scs = [s_ for s_ in (scenarios or load_scenarios()) if not ids or s_["id"] in ids]
    old = store.get_settings()
    if overrides:
        store.set_settings(overrides)
    try:
        st = store.get_settings()
        mode = llm.resolve_mode(st)
        jclient = None
        if judge and mode in llm.LLM_MODES:
            jst = dict(st, router_model=judge_model) if judge_model else st
            jclient = llm.get_client(jst, mode, role="router")
        t_all = time.time()
        details = []
        for sc in scs:
            user = _user(sc.get("user"))
            cid = store.create_conversation(user.username, "eval hội thoại")
            history, turns, last = [], [], None
            try:
                for t in sc["turns"]:
                    t0 = time.time()
                    try:
                        if "choose" in t:          # bấm một lựa chọn của câu hỏi lại ở lượt trước
                            c = (last or {}).get("clarification") or {}
                            opts = c.get("options") or []
                            idx = next((i for i, o in enumerate(opts) if norm(t["choose"]) in norm(str(o))), None)
                            if idx is None:
                                raise ValueError(f"không có lựa chọn chứa “{t['choose']}” trong {opts}")
                            q = f"(chọn) {opts[idx]}"
                            r = get_agent().ask(user, cid, opts[idx], clarification={"id": c.get("id"), "option": idx})
                        else:
                            q = t["q"]
                            r = get_agent().ask(user, cid, q)
                    except Exception as e:
                        q = t.get("q") or f"(chọn) {t.get('choose')}"
                        r = {"type": "error", "text": f"{type(e).__name__}: {e}"}
                    el = time.time() - t0
                    fails = check_turn(r, t.get("expect") or {}, el)
                    fut = JUDGE_POOL.submit(jclient.critique, judge_prompt(history, q, r, t.get("goal"), user)) if jclient else None
                    turns.append({"q": q, "type": r.get("type"), "parts": [p.get("type") for p in r.get("parts") or []],
                                  "elapsed_s": round(el, 2), "fails": fails, "text": (r.get("text") or "")[:400],
                                  "path": r.get("path"), "_fut": fut})
                    history.append((q, _all_text(r)))
                    last = r
            finally:
                store.delete_conversation(cid)
            details.append({"id": sc["id"], "name": sc.get("name", sc["id"]), "tags": sc.get("tags", []), "turns": turns})
        # thu kết quả chấm (chạy song song trong lúc các kịch bản sau đang chạy)
        for d in details:
            for t in d["turns"]:
                fut = t.pop("_fut")
                if fut is None:
                    t["judge"] = None
                else:
                    try:
                        v = fut.result(timeout=90)
                        t["judge"] = {"verdict": v.get("verdict"), "problem": v.get("problem") or ""}
                    except Exception as e:
                        t["judge"] = {"verdict": "error", "problem": f"{type(e).__name__}: {str(e)[:120]}"}
                t["checks_ok"] = not t["fails"]
                t["judge_ok"] = None if not t["judge"] or t["judge"]["verdict"] == "error" else t["judge"]["verdict"] == "ok"
                t["ok"] = t["checks_ok"] and t["judge_ok"] is not False
            d["ok"] = all(t["ok"] for t in d["turns"])
        if jclient is not None:
            judge_usage = dict(getattr(jclient, "usage", {}) or {})
        else:
            judge_usage = None
    finally:
        if overrides:
            store.set_settings({k: old.get(k, config.DEFAULT_SETTINGS.get(k)) for k in overrides})

    turns = [t for d in details for t in d["turns"]]
    judged = [t for t in turns if t["judge_ok"] is not None]
    lat = sorted(t["elapsed_s"] for t in turns)
    by_tag = defaultdict(list)
    for d in details:
        for tg in d["tags"]:
            by_tag[tg].append(d)
    summary = {
        "kind": "conversation", "label": label or "Hội thoại" + (f" · {', '.join(f'{k}={v}' for k, v in overrides.items())}" if overrides else ""),
        "overrides": overrides or {}, "n": len(details), "n_turns": len(turns),
        "scenario_pass": _pct(details, "ok"), "turn_pass": _pct(turns, "ok"), "checks_pass": _pct(turns, "checks_ok"),
        "judge_ok": _pct(judged, "judge_ok") if judged else None, "judged": len(judged),
        "disagree": sum(1 for t in judged if t["judge_ok"] != t["checks_ok"]),
        "latency_median_s": round(statistics.median(lat), 2) if lat else None,
        "latency_p90_s": round(lat[min(len(lat) - 1, int(round(0.9 * (len(lat) - 1))))], 2) if lat else None,
        "latency_max_s": round(lat[-1], 2) if lat else None,
        "by_tag": {k: {"n": len(v), "pass": _pct(v, "ok")} for k, v in sorted(by_tag.items())},
        "model": st.get("llm_model"), "judge_model": getattr(jclient, "model", None) if jclient else None,
        "judge_usage": judge_usage, "elapsed_s": round(time.time() - t_all, 1),
        # cùng tên trường với eval câu đơn để trang lịch sử hiện được cột "Đúng"
        "execution_accuracy": _pct(details, "ok"), "fewshots_in_store": len(store.list_fewshots()),
    }
    eid = store.save_eval(summary["label"], summary, details) if save else None
    return {"id": eid, "summary": summary, "details": details}


def ablation(variants: list[tuple[str, dict]] | None = None, judge: bool = True, ids: list[str] | None = None) -> dict:
    """Chạy bộ kịch bản với từng tính năng bị tắt; trả bảng so sánh."""
    rows = []
    for name, ov in variants or ABLATIONS:
        r = run_conversations(judge=judge, overrides=ov, label=f"Hội thoại · {name}", ids=ids)
        rows.append({"variant": name, "overrides": ov, "id": r["id"], **{k: r["summary"][k] for k in (
            "scenario_pass", "turn_pass", "checks_pass", "judge_ok", "disagree", "latency_median_s", "latency_p90_s",
            "latency_max_s")}})
    return {"rows": rows}


if __name__ == "__main__":       # python -m app.conv_eval [--no-judge] [--ablation] [--ids c01,c02] [--judge-model ...]
    import argparse
    ap = argparse.ArgumentParser(description="Eval hội thoại: kiểm tra bằng code + mô hình chấm")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--ablation", action="store_true")
    ap.add_argument("--ids", default="")
    ap.add_argument("--judge-model", default=None)
    a = ap.parse_args()
    ids = [x for x in a.ids.split(",") if x] or None
    if a.ablation:
        for row in ablation(judge=not a.no_judge, ids=ids)["rows"]:
            print(json.dumps(row, ensure_ascii=False))
    else:
        res = run_conversations(judge=not a.no_judge, ids=ids, judge_model=a.judge_model)
        for d in res["details"]:
            print(("✓" if d["ok"] else "✗"), d["id"], d["name"], " | ".join(f"{t['elapsed_s']}s {t['type']}" for t in d["turns"]))
            for t in d["turns"]:
                if not t["ok"]:
                    print("    Q:", t["q"])
                    for f in t["fails"]:
                        print("      code:", f)
                    if t["judge"] and t["judge"]["verdict"] != "ok":
                        print("      judge:", t["judge"]["verdict"], t["judge"]["problem"])
        print(json.dumps(res["summary"], ensure_ascii=False, indent=1))
