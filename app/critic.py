"""
Critic: tác tử kiểm tra câu trả lời có trúng câu hỏi không, gồm hai lớp.

1) Kiểm tra bằng code (xác định, ~0 ms), chạy TRƯỚC khi truy vấn: đối chiếu kế hoạch của Planner với những gì người
   dùng NÊU RÕ trong câu (bộ luật nhận ra: chỉ số được gọi tên, giá trị lọc, cách chia, kỳ, top N).
   - Kế hoạch bỏ sót phần người dùng nêu rõ (quên lọc "Hà Nội", quên kỳ nên quét toàn bộ lịch sử, quên "theo kênh",
     sai "top 5"): vá thẳng vào kế hoạch và ghi rõ cho người dùng biết.
   - Chỉ số được hỏi không đứng đầu: đưa lên đầu để câu trả lời nói đúng trọng tâm.
   - Mâu thuẫn mà code không tự quyết được (chỉ số khác hẳn, lọc cùng chiều nhưng khác giá trị): chuyển cho lớp 2.
2) LLM chấm (model nhanh), chạy SAU khi có kết quả, song song với lượt viết lời nên gần như không thêm thời gian chờ:
   đọc câu hỏi + kế hoạch + vài dòng kết quả, trả "ok" hoặc "wrong" kèm vấn đề và hướng sửa. "wrong" thì Planner lập
   lại kế hoạch một lần với nhận xét đó (nếu còn thời gian), không thì câu trả lời mang cảnh báo.

Lớp code không bao giờ dựa vào phần bộ luật tự suy (chỉ số mặc định, kỳ mặc định, top 10 mặc định), chỉ dựa vào chữ
người dùng gõ, nên không ép kế hoạch đúng của Planner về cách hiểu sai của bộ luật.
"""
from __future__ import annotations

import copy
import json
import re

from .nlu import get_nlu
from .semantic import get_semantic
from .textutil import norm

TIME_DIMS = ("ngay", "tuan", "thang", "quy", "nam")


def _label(name: str, kind: str = "metric") -> str:
    sem = get_semantic()
    src = sem.metrics if kind == "metric" else sem.dims
    return (src.get(name) or {}).get("label", name)


def check_plan(question: str, plan: dict, prefs: dict | None = None) -> dict:
    """Đối chiếu semantic plan với phần người dùng nêu rõ. Trả {plan (đã vá), fixes (đã vá, để báo người dùng),
    issues (mâu thuẫn cần LLM chấm xem xét)}."""
    sem, nlu = get_semantic(), get_nlu()
    r = nlu.parse(question, {"prefs": prefs or {}})
    m, exp = r.matched, r.plan
    p = copy.deepcopy(plan or {})
    fixes, issues = [], []
    metrics = list(p.get("metrics") or [])
    supported = lambda d: all(d in sem.dims_for_metric(x) for x in metrics if x in sem.metrics)

    # --- chỉ số: được gọi tên trong câu thì phải có trong kế hoạch, và đứng đầu
    named = [x for x in m.get("metrics") or [] if x in sem.metrics and x in (exp.get("metrics") or [])]
    if named and metrics:
        hit = [x for x in named if x in metrics]
        if not hit:
            issues.append(f"Câu hỏi gọi tên chỉ số {', '.join(_label(x) for x in named)} nhưng kế hoạch tính "
                          f"{', '.join(_label(x) for x in metrics)}.")
        elif metrics[0] != hit[0]:          # chỉ số người dùng nêu trước phải đứng đầu
            p["metrics"] = hit + [x for x in metrics if x not in hit]
            for o in p.get("order_by") or []:
                if o.get("field") == metrics[0]:
                    o["field"] = p["metrics"][0]
            fixes.append(f"đưa {_label(hit[0]).lower()} (chỉ số bạn hỏi) lên đầu câu trả lời")

    # --- giá trị lọc người dùng nêu rõ
    pf = {f.get("dimension"): f for f in p.get("filters") or []}
    pvals = {norm(str(v)) for f in p.get("filters") or [] for v in f.get("values") or []}
    for f in exp.get("filters") or []:
        d = f["dimension"]
        if not any(x.get("dimension") == d for x in m.get("values") or []):
            continue        # bộ lọc kế thừa/suy ra, không phải chữ người dùng gõ
        vals = [norm(str(v)) for v in f.get("values") or []]
        if all(v in pvals for v in vals):
            continue
        shown = ", ".join(map(str, f.get("values") or []))
        if d in pf:
            issues.append(f"Câu hỏi lọc {_label(d, 'dim').lower()} {shown} nhưng kế hoạch lọc "
                          f"{', '.join(map(str, pf[d].get('values') or []))}.")
        elif metrics and supported(d):
            p.setdefault("filters", []).append(copy.deepcopy(f))
            fixes.append(f"thêm bộ lọc {_label(d, 'dim').lower()} {'khác ' if f.get('op') == 'not_in' else ''}{shown}")
        else:
            issues.append(f"Câu hỏi nhắc {shown} nhưng kế hoạch không lọc theo.")

    # --- cách chia người dùng nêu rõ ("theo kênh", "từng cửa hàng")
    pd = list(p.get("dimensions") or [])
    if m.get("dimensions"):
        for d in exp.get("dimensions") or []:
            if d in TIME_DIMS or d in pd or d in pf:
                continue
            if metrics and supported(d):
                p["dimensions"] = pd = pd + [d]
                fixes.append(f"chia theo {_label(d, 'dim').lower()}")

    # --- kỳ người dùng nêu rõ: kế hoạch bỏ kỳ (quét toàn bộ lịch sử) hoặc lệch hẳn cả đầu lẫn cuối
    et, pt = exp.get("time_range"), p.get("time_range")
    if m.get("time") and et and not r.follow_up:
        if not pt or not pt.get("start"):
            p["time_range"] = dict(et)
            fixes.append(f"giới hạn trong {et.get('label') or 'kỳ bạn hỏi'}")
        elif str(pt.get("start")) != et["start"] and str(pt.get("end")) != et["end"]:
            p["time_range"] = dict(et)
            fixes.append(f"dùng đúng kỳ {et.get('label')} (kế hoạch trước tính {pt.get('label') or pt.get('start')})")

    # --- top N người dùng nêu rõ
    n = m.get("limit")
    if n and re.search(r"\btop\s?\d+\b|\b\d+\s+\S+.*\bnhat\b", norm(question)) and p.get("limit") not in (None, n) \
            and pd:
        p["limit"] = n
        fixes.append(f"lấy đúng top {n}")
    return {"plan": p, "fixes": fixes, "issues": issues}


def check_sql(question: str, sql: str, prefs: dict | None = None) -> list[str]:
    """SQL do mô hình tự viết: code không vá được, chỉ ghi nhận mâu thuẫn rõ ràng để LLM chấm xem xét."""
    r = get_nlu().parse(question, {"prefs": prefs or {}})
    low = norm(sql or "")
    issues = []
    for v in r.matched.get("values") or []:
        if norm(str(v["value"])) not in low:
            issues.append(f"Câu hỏi nhắc “{v['value']}” nhưng SQL không lọc theo giá trị này.")
    return issues


def answer_focus_ok(text: str, plan: dict) -> bool:
    """Câu đầu của lời diễn giải phải nói tới chỉ số người dùng hỏi (theo nhãn trong semantic layer)."""
    sem = get_semantic()
    mets = [x for x in (plan or {}).get("metrics") or [] if x in sem.metrics]
    if not mets or not text:
        return True
    first = norm(re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0])
    lab = norm(re.sub(r"\s*\(.*?\)", "", sem.metrics[mets[0]]["label"]))
    return lab in first


# ====================================================================== LLM chấm
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string", "enum": ["ok", "wrong"]},
                   "problem": {"type": "string"}, "fix": {"type": "string"}},
    "required": ["verdict", "problem", "fix"], "additionalProperties": False,
}

JUDGE_RULES = """You are the CRITIC of a Text-to-SQL analytics assistant. You check whether the query that was run answers
the user's question. You do not see the final wording; numbers are verified by code separately.

Answer "wrong" ONLY when the result clearly does not answer what was asked, for example:
- a different metric than the one asked (asked average order value, got revenue; asked count of orders, got revenue);
- a filter, entity or period the user stated is missing or different (asked Hà Nội, no Hà Nội filter; asked October,
  computed September or all history);
- a breakdown, ranking, top N or direction the user asked for is missing or reversed (asked "theo kênh", got one total;
  asked lowest, got highest);
- the result is obviously implausible for the question (e.g. empty because of a wrong filter value, a share above 100%).
Answer "ok" when the result answers the question, including when the assistant made a reasonable, stated assumption for
something the user did not specify (default period, default metric definition), added helper columns, or chose a
sensible format. Do not nitpick. When unsure, answer "ok".
The glossary is the company's own definition: a metric already includes its mandatory filters (e.g. only completed
orders), and a breakdown means what its synonyms say. Never call a plan wrong for a filter that the metric definition
already applies, or for a breakdown whose synonyms match the user's words.
problem: one short Vietnamese sentence for the user ("" if ok). fix: one short instruction for the planner on how to
correct the plan ("" if ok). Return only the JSON object."""


def describe_plan(plan: dict | None, sql: str | None) -> str:
    if not plan:
        return "SQL do mô hình viết:\n" + (sql or "")[:1500]
    parts = [f"chỉ số: {', '.join(_label(x) for x in plan.get('metrics') or []) or '(không)'}"]
    if plan.get("dimensions"):
        parts.append("chia theo: " + ", ".join(_label(d, "dim") for d in plan["dimensions"]))
    for f in plan.get("filters") or []:
        parts.append(f"lọc {_label(f.get('dimension'), 'dim')} {'KHÔNG thuộc' if f.get('op') == 'not_in' else 'thuộc'} "
                     f"{', '.join(map(str, f.get('values') or []))}")
    tr = plan.get("time_range") or {}
    parts.append(f"kỳ: {tr.get('label') or (str(tr.get('start')) + '..' + str(tr.get('end')) if tr else 'toàn bộ dữ liệu')}")
    if plan.get("compare"):
        parts.append(f"so với: {plan['compare'].get('label')}")
    for o in plan.get("order_by") or []:
        if not isinstance(o, dict):         # một số kế hoạch ghi order_by dạng chuỗi tên trường
            o = {"field": str(o), "direction": "desc"}
        parts.append(f"sắp {'tăng dần' if str(o.get('direction')).lower().startswith('asc') else 'giảm dần'} theo "
                     f"{_label(o.get('field'))}")
    if plan.get("limit"):
        parts.append(f"lấy {plan['limit']} dòng đầu")
    for h in plan.get("having") or []:
        parts.append(f"điều kiện {_label(h.get('metric'))} {h.get('op')} {h.get('value')}")
    if plan.get("per"):
        parts.append(f"bình quân mỗi {plan['per']}")
    return "Kế hoạch: " + "; ".join(parts)


def glossary(plan: dict | None) -> str:
    """Định nghĩa chỉ số và từ đồng nghĩa của chiều mà kế hoạch dùng: để LLM chấm không chê nhầm "chưa lọc đơn hoàn
    thành" (chỉ số đã lọc sẵn) hay "miền phát là nơi gửi" (doanh nghiệp định nghĩa là nơi giao tới)."""
    if not plan:
        return ""
    sem = get_semantic()
    lines = []
    for x in plan.get("metrics") or []:
        if x in sem.metrics:
            m = sem.metrics[x]
            lines.append(f"- {m['label']}: {(m.get('description') or '').strip()[:220]}"
                         + (f" [điều kiện bắt buộc: {m['filter']}]" if m.get("filter") else ""))
    dims = list(plan.get("dimensions") or []) + [f.get("dimension") for f in plan.get("filters") or []]
    for d in dict.fromkeys(dims):
        if d in sem.dims:
            syn = [x for x in sem.dims[d].get("synonyms") or [] if x][:8]
            lines.append(f"- {sem.dims[d]['label']}" + (f" (còn gọi: {', '.join(syn)})" if syn else "")
                         + (f": {sem.dims[d]['description'][:160]}" if sem.dims[d].get("description") else ""))
    return ("Từ điển của doanh nghiệp:\n" + "\n".join(lines) + "\n") if lines else ""


def judge_prompt(question: str, shown_question: str | None, plan: dict | None, sql: str | None, result: dict,
                 assumptions: list, issues: list) -> tuple[str, str]:
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    sample = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows[:8]) or "(không có dòng nào)"
    user = ((f"Người dùng gõ: {shown_question}\n" if shown_question and shown_question != question else "")
            + f"Câu hỏi (đã viết lại thành câu độc lập): {question}\n\n{describe_plan(plan, sql)}\n" + glossary(plan)
            + (f"Giả định đã nêu cho người dùng: {' | '.join(assumptions[:6])}\n" if assumptions else "")
            + (f"Kiểm tra bằng code nghi ngờ: {' | '.join(issues)}\n" if issues else "")
            + f"\nKết quả: {result.get('row_count', len(rows))} dòng, cột {cols}\n{sample}")
    return JUDGE_RULES, user


def normalize_verdict(out: dict) -> dict:
    out = dict(out or {})
    v = out.get("verdict")
    out["verdict"] = v if v in ("ok", "wrong") else "ok"
    for k in ("problem", "fix"):
        out[k] = out.get(k).strip() if isinstance(out.get(k), str) else ""
    if out["verdict"] == "wrong" and not out["problem"]:
        out["verdict"] = "ok"           # chê mà không nói được sai ở đâu thì không đủ căn cứ để làm lại
    return out
