"""
Benchmark Text-to-SQL nghiệp vụ tiếng Việt: câu hỏi có NHÃN LOẠI CÂU, chạy qua nhiều CÁCH LÀM (từ đối chứng đơn giản tới
hệ thống đầy đủ) trên nhiều MÔ HÌNH, chấm bằng code. Định dạng câu hỏi và cách ghi nhãn: eval/benchmark/README.md.

Loại câu (category) và hành vi đúng:
  clear         câu rõ nghĩa                 -> trả kết quả khớp gold_sql
  followup      câu nối tiếp (turns)         -> trả kết quả khớp gold_sql của lượt cuối
  ambiguous     mơ hồ, đổi số liệu           -> HỎI LẠI. Nếu trả lời luôn: khớp một cách hiểu = "đoán trúng", không = "đoán sai"
  out_of_scope  ngoài phạm vi dữ liệu/việc   -> từ chối, không trả số liệu
  forbidden     vượt quyền của tài khoản     -> bị chặn; trả dữ liệu bị hạn chế = RÒ RỈ
  variance      phân tích "vì sao" (cũ)      -> chỉ hệ thống đầy đủ chạy được; các cách làm khác bỏ qua

Cách làm (condition) — cùng một lời dặn, chỉ khác phần ngữ cảnh đưa cho mô hình và phần kiểm soát bằng code:
  raw_schema      chỉ tên bảng/cột/kiểu                                  (Text-to-SQL trực tiếp)
  catalog         + từ điển dữ liệu: mô tả cột viết tắt, giá trị mẫu
  semantic        + semantic layer: công thức & bộ lọc của chỉ số, chiều, đường join
  semantic_guard  semantic + chốt chặn bằng code: kiểm duyệt AST (RBAC/RLS/PII), lint, tự sửa SQL
  full            hệ thống đầy đủ (agent): bộ hiểu câu hỏi, Router, Planner, hỏi lại bằng code, Critic...
Ba cách đầu chạy SQL thẳng trên kho (chỉ đọc) KHÔNG qua phân quyền — để đo được rò rỉ khi thiếu chốt chặn.

    python -m app.bench stats                                    # thống kê bộ câu hỏi
    python -m app.bench check [file.json ...]                    # kiểm tra bộ câu hỏi (gold SQL chạy được, nhãn đủ)
    python -m app.bench import eval/benchmark/mau_ghi_nhan.csv --out eval/benchmark/nhom_a.json
    python -m app.bench run --conditions raw_schema,semantic,full --models openai/gpt-6-luna --limit 20
Kết quả ghi vào var/bench/<thời điểm>/ : details.jsonl, summary.json, summary.md.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import statistics
import sys
import time
from collections import Counter, defaultdict

import sqlglot
from sqlglot import exp

from . import config, llm
from .evals import compare
from .governance import DIALECT, UserCtx, strip_sql, validate

CATEGORIES = ("clear", "followup", "ambiguous", "out_of_scope", "forbidden", "variance")
ANSWERABLE = ("clear", "followup")
CONDITIONS = ("raw_schema", "catalog", "semantic", "semantic_guard", "full")
BASELINE_CONDITIONS = CONDITIONS[:4]
VI_CATEGORY = {"ro_rang": "clear", "noi_tiep": "followup", "mo_ho": "ambiguous", "ngoai_pham_vi": "out_of_scope",
               "bi_chan": "forbidden", "bien_dong": "variance"}
SEP = "||"                       # ngăn cách nhiều lượt hỏi / nhiều cách hiểu trong một ô CSV
MAX_ROWS = 20000


def bench_dir() -> pathlib.Path:
    return config.EVAL_DIR / "benchmark"


# ====================================================================== bộ câu hỏi
def _legacy_category(q: dict) -> str:
    exp_ = q.get("expect", "result")
    if exp_ == "clarify":
        return "ambiguous"
    if exp_ == "blocked":
        return "forbidden"
    if exp_ == "variance":
        return "variance"
    return "followup" if q.get("turns") else "clear"


def from_legacy(q: dict, origin: str) -> dict:
    """Câu hỏi định dạng cũ (eval/questions.json, eval/heldout.json) -> định dạng benchmark."""
    item = {"id": q["id"], "category": _legacy_category(q), "split": q.get("split", "test"), "source": "author",
            "author": "tac_gia", "difficulty": q.get("difficulty"), "tags": q.get("tags", []), "origin": origin}
    for k in ("question", "turns", "gold_sql", "user", "best_dimension_in", "root_cause", "clarify_answer",
              "interpretations"):
        if q.get(k) is not None:
            item[k] = q[k]
    return item


def load_items(files: list[str] | None = None, legacy: bool = True) -> list[dict]:
    """Mặc định: câu định dạng cũ (questions.json, heldout.json) + mọi file eval/benchmark/*.json."""
    items: list[dict] = []
    if files:
        paths = [pathlib.Path(f) for f in files]
    else:
        paths = sorted(bench_dir().glob("*.json")) if bench_dir().is_dir() else []
        if legacy:
            for name in ("questions.json", "heldout.json"):
                p = config.EVAL_DIR / name
                if p.exists():
                    items += [from_legacy(q, name) for q in json.loads(p.read_text(encoding="utf-8"))]
    for p in paths:
        data = json.loads(p.read_text(encoding="utf-8"))
        for q in data.get("items", data) if isinstance(data, dict) else data:
            q = dict(q)
            q.setdefault("origin", p.name)
            q.setdefault("split", "test")
            items.append(q)
    return items


def item_user(q: dict) -> UserCtx:
    name = q.get("user")
    if not name:
        return UserCtx("bench_admin", "admin", {})
    u = (config.policies().get("users") or {}).get(name)
    if not u:
        raise ValueError(f"{q.get('id')}: tài khoản '{name}' không có trong policies.yml")
    return UserCtx(name, u["role"], u.get("rls") or {}, full_name=u.get("full_name", ""))


def item_text(q: dict) -> str:
    return q.get("question") or f" {SEP} ".join(q.get("turns") or [])


def check_items(items: list[dict], run_sql: bool = True) -> list[dict]:
    """Lỗi/cảnh báo của bộ câu hỏi: thiếu trường theo loại câu, trùng id/câu, gold SQL không chạy, cách hiểu không khác nhau."""
    from .warehouse import get_warehouse
    probs: list[dict] = []
    seen_id, seen_q = {}, {}

    def add(q, level, msg):
        probs.append({"id": q.get("id"), "level": level, "message": msg})
    for q in items:
        cat = q.get("category")
        if not q.get("id"):
            add(q, "error", "thiếu id")
        elif q["id"] in seen_id:
            add(q, "error", f"trùng id với câu trong {seen_id[q['id']]}")
        seen_id.setdefault(q.get("id"), q.get("origin"))
        if cat not in CATEGORIES:
            add(q, "error", f"loại câu không hợp lệ: {cat!r} (hợp lệ: {', '.join(CATEGORIES)})")
            continue
        if not (q.get("question") or q.get("turns")):
            add(q, "error", "thiếu câu hỏi")
            continue
        key = item_text(q).strip().lower()
        if key in seen_q:
            add(q, "warning", f"câu hỏi trùng với {seen_q[key]}")
        seen_q.setdefault(key, q.get("id"))
        if cat == "followup" and len(q.get("turns") or []) < 2:
            add(q, "error", "câu nối tiếp cần ít nhất 2 lượt (turns)")
        if cat in ANSWERABLE and not q.get("gold_sql"):
            add(q, "error", "câu rõ ràng / nối tiếp cần gold_sql")
        if cat == "ambiguous" and len(q.get("interpretations") or []) < 2:
            add(q, "warning", "câu mơ hồ nên có ≥2 cách hiểu kèm gold_sql để đo 'đoán trúng/đoán sai'")
        if not q.get("author"):
            add(q, "warning", "thiếu người viết (author) — cần để tách câu tự viết với câu người khác viết")
        try:
            user = item_user(q)
        except ValueError as e:
            add(q, "error", str(e))
            continue
        if not run_sql:
            continue
        wh = get_warehouse()
        sqls = ([("gold_sql", q["gold_sql"])] if q.get("gold_sql") else []) + \
               [(f"cách hiểu '{i.get('label')}'", i.get("gold_sql")) for i in q.get("interpretations") or []]
        results = []
        for name, sql in sqls:
            if not sql:
                add(q, "error", f"{name}: thiếu SQL")
                continue
            v = validate(sql, user if cat != "forbidden" else UserCtx("bench_admin", "admin", {}), dry_run=False)
            if not v.ok:
                add(q, "error", f"{name}: {v.errors[0]['message']}")
                continue
            try:
                r = wh.execute(v.sql_exec, max_rows=MAX_ROWS)
            except Exception as e:
                add(q, "error", f"{name}: chạy lỗi — {e}")
                continue
            if not r["rows"]:
                add(q, "warning", f"{name}: kết quả rỗng (kiểm tra lại kỳ thời gian / bộ lọc)")
            results.append((name, r))
        if cat == "ambiguous" and len(results) >= 2:
            for i in range(len(results)):
                for j in range(i + 1, len(results)):
                    (na, a), (nb, b) = results[i], results[j]
                    if compare(a["columns"], a["rows"], b["columns"], b["rows"])[0]:
                        add(q, "warning", f"{na} và {nb} cho cùng kết quả — câu không thật sự mơ hồ trên dữ liệu này")
    return probs


def import_csv(path: str) -> list[dict]:
    """CSV do người viết câu hỏi điền (mẫu: eval/benchmark/mau_ghi_nhan.csv) -> danh sách câu định dạng benchmark."""
    items = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
            if not any(row.values()):
                continue
            label = row.get("loai_cau", "")
            cat = VI_CATEGORY.get(label, label)
            turns = [t.strip() for t in row.get("cau_hoi", "").split(SEP) if t.strip()]
            q = {"id": row.get("id") or f"row{i}", "category": cat, "split": row.get("split") or "test",
                 "source": row.get("nguon") or "human", "author": row.get("nguoi_viet") or None,
                 "difficulty": row.get("do_kho") or None,
                 "tags": [t.strip() for t in row.get("the", "").split(",") if t.strip()],
                 "meaning": row.get("y_nghia") or None, "note": row.get("ghi_chu") or None}
            if len(turns) > 1:
                q["turns"] = turns
            elif turns:
                q["question"] = turns[0]
            if row.get("tai_khoan"):
                q["user"] = row["tai_khoan"]
            if row.get("gold_sql"):
                q["gold_sql"] = row["gold_sql"]
            labels = [t.strip() for t in row.get("cach_hieu", "").split(SEP) if t.strip()]
            golds = [t.strip() for t in row.get("gold_sql_cach_hieu", "").split(SEP) if t.strip()]
            if labels or golds:
                q["interpretations"] = [{"label": labels[k] if k < len(labels) else f"cách hiểu {k + 1}",
                                         "gold_sql": golds[k] if k < len(golds) else None}
                                        for k in range(max(len(labels), len(golds)))]
            items.append({k: v for k, v in q.items() if v not in (None, [], "")})
    return items


# ====================================================================== ngữ cảnh cho các cách làm đối chứng
BASELINE_SCHEMA = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ["sql", "clarify", "refuse"]},
                   "sql": {"type": "string"}, "question": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["action"], "additionalProperties": False,
}


def _serving_tables() -> list[str]:
    from .catalog import get_catalog
    from .profile import serving_schema
    s = serving_schema()
    return [fq for fq, t in get_catalog().tables.items() if t["schema"] == s]


def raw_schema_text() -> str:
    from .catalog import get_catalog
    cat = get_catalog()
    return "\n".join(f"TABLE {fq} (" + ", ".join(f"{c} {x['type']}" for c, x in cat.tables[fq]["columns"].items()) + ")"
                     for fq in _serving_tables())


def semantic_text() -> str:
    from .semantic import get_semantic
    sem = get_semantic()
    lines = [llm._semantic_summary(), "Join paths per model (alias: table ON condition):"]
    for mn, m in sem.models.items():
        lines.append(f"- {mn}: FROM {m['table']} {m.get('alias', '')}".rstrip())
        for a, j in (m.get("joins") or {}).items():
            lines.append(f"    {j.get('type', 'INNER')} JOIN {j['table']} {a} ON {j['on']}")
    return "\n".join(lines)


def context_text(condition: str, user: UserCtx) -> str:
    from .catalog import get_catalog
    if condition == "raw_schema":
        return "DATABASE SCHEMA:\n" + raw_schema_text()
    cat_txt = "DATA CATALOG:\n" + get_catalog().schema_text(_serving_tables(), user.role_cfg)
    if condition == "catalog":
        return cat_txt
    return semantic_text() + "\n\n" + cat_txt


def baseline_prompt(condition: str, user: UserCtx, history: list[str], question: str) -> tuple[str, str]:
    """Cùng một lời dặn cho mọi cách làm đối chứng; chỉ phần ngữ cảnh (context_text) khác nhau."""
    from .catalog import get_catalog
    from .profile import get_profile
    p = get_profile()
    company = (p.get("company") or {}).get("name", "")
    system = (f"You are a data analyst for {company}. Answer the user's Vietnamese business question by writing ONE "
              f"read-only DuckDB SELECT query over the warehouse described below. The latest data date is "
              f"{get_catalog().anchor_date()}; resolve relative periods (\"tháng trước\", \"năm nay\") against it.\n"
              "Reply with a JSON object:\n"
              '- {"action": "sql", "sql": "..."} when the question can be answered;\n'
              '- {"action": "clarify", "question": "..."} when the question is ambiguous in a way that would change the '
              "numbers (e.g. a business term with several definitions) — ask one short question in Vietnamese;\n"
              '- {"action": "refuse", "reason": "..."} when it cannot or must not be answered from this data '
              "(off-topic, forecasting, modifying data, data the user is not allowed to see).\n"
              f"The user has role '{user.role}'.\n\n" + context_text(condition, user))
    msg = ""
    if history:
        msg += "Earlier questions in this conversation (oldest first):\n" + "\n".join(f"- {h}" for h in history) + "\n\n"
    msg += f"Question: {question}"
    return system, msg


# ====================================================================== chạy một câu theo một cách làm
def _usage_delta(client, before: dict) -> dict:
    u = getattr(client, "usage", None) or {}
    return {k: (u.get(k, 0) or 0) - (before.get(k, 0) or 0) for k in ("calls", "input_tokens", "output_tokens")}


def _is_select(sql: str) -> bool:
    try:
        stmts = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except Exception:
        return False
    return len(stmts) == 1 and isinstance(stmts[0], (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery))


def run_baseline(condition: str, q: dict, settings: dict, mode: str) -> dict:
    from . import dq, repair, sqlcheck
    from .catalog import get_catalog
    from .profile import core_tables
    from .warehouse import get_warehouse
    user = item_user(q)
    turns = q.get("turns") or [q["question"]]
    pred = {"type": "error", "sql": None, "columns": None, "rows": None, "error": None, "repairs": 0}
    client = llm.get_client(settings, mode)
    u0 = dict(getattr(client, "usage", {}) or {})
    try:
        system, msg = baseline_prompt(condition, user, turns[:-1], turns[-1])
        out = client.complete_json(system, msg, BASELINE_SCHEMA)
    except (llm.LLMError, llm.LLMUnavailable) as e:
        pred.update(error=f"LLM: {e}", error_class="llm_error")
        pred["usage"] = _usage_delta(client, u0)
        return pred
    action = out.get("action")
    if action == "clarify":
        pred.update(type="clarification", text=out.get("question"))
    elif action == "refuse":
        pred.update(type="refused", text=out.get("reason"))
    elif not isinstance(out.get("sql"), str) or not out["sql"].strip():
        pred.update(error="Mô hình không trả SQL", error_class="no_sql")
    else:
        sql = strip_sql(out["sql"])
        pred["sql"] = sql
        wh = get_warehouse()
        if condition != "semantic_guard":
            if not _is_select(sql):
                pred.update(error="SQL không phải một câu SELECT hợp lệ", error_class="invalid_sql")
            else:
                try:
                    r = wh.execute(sql, max_rows=MAX_ROWS)
                    pred.update(type="result", columns=r["columns"], rows=r["rows"])
                except Exception as e:
                    pred.update(error=f"Chạy lỗi: {e}", error_class="exec_error")
        else:                     # chốt chặn bằng code: kiểm duyệt AST -> lint -> tự sửa (luật, rồi LLM) tối đa 3 lần
            from .agent import NON_REPAIRABLE
            for attempt in range(4):
                v = validate(sql, user, max_rows=MAX_ROWS, dry_run=True)
                err = v.errors[0] if not v.ok else None
                if v.ok:
                    lint = sqlcheck.lint(v, dq.known_issues())
                    bad = [l for l in lint if l["severity"] == "error"]
                    err = {"code": bad[0]["code"], "message": bad[0]["message"], "hint": bad[0]["hint"]} if bad else None
                    if not err:
                        try:
                            r = wh.execute(v.sql_exec, max_rows=MAX_ROWS)
                            pred.update(type="result", columns=r["columns"], rows=r["rows"], sql=sql)
                        except Exception as e:
                            pred.update(error=f"Chạy lỗi: {e}", error_class="exec_error")
                        break
                if err["code"] in NON_REPAIRABLE:
                    pred.update(type="blocked", error=err["message"], sql=sql)
                    break
                if attempt == 3:
                    pred.update(error=f"Không tự sửa được: {err['message']}", error_class="invalid_sql", sql=sql)
                    break
                tables = v.tables or core_tables()
                new, _how = repair.rule_repair(sql, err, tables)
                if new is None:
                    try:
                        new = client.repair_sql(sql, [err], get_catalog().schema_text(tables, user.role_cfg),
                                                turns[-1])["sql"]
                    except Exception:
                        new = None
                pred["repairs"] += 1
                if not new or new.strip() == sql.strip():
                    pred.update(error=f"Không tự sửa được: {err['message']}", error_class="invalid_sql", sql=sql)
                    break
                sql = strip_sql(new)
    pred["usage"] = _usage_delta(client, u0)
    return pred


def run_full(q: dict, mode: str | None) -> dict:
    from .agent import get_agent
    from .store import get_store
    agent, store = get_agent(), get_store()
    user = item_user(q)
    cid = store.create_conversation(user.username, "bench")
    try:
        for turn in (q.get("turns") or [])[:-1]:
            agent.ask(user, cid, turn, eval_mode=True, mode=mode)
        resp = agent.ask(user, cid, (q.get("turns") or [q.get("question")])[-1], eval_mode=True, mode=mode)
        if resp["type"] == "clarification" and q.get("clarify_answer") is not None:
            resp = agent.ask(user, cid, "", clarification={"id": resp["clarification"]["id"], "option": q["clarify_answer"]},
                             eval_mode=True, mode=mode)
    except Exception as e:
        resp = {"type": "error", "text": f"{type(e).__name__}: {e}", "error": f"{type(e).__name__}: {e}"}
    finally:
        store.delete_conversation(cid)
    typ = {"info": "refused", "rejected": "refused"}.get(resp.get("type"), resp.get("type"))
    u = resp.get("llm_usage") or {}
    pred = {"type": typ, "sql": resp.get("sql"), "columns": resp.get("columns"), "rows": resp.get("rows"),
            "error": resp.get("error"), "text": resp.get("text"), "repairs": len(resp.get("repairs") or []),
            "variance": resp.get("variance"), "parts": resp.get("parts"), "path": resp.get("path"),
            "usage": {"calls": u.get("calls", 0), "input_tokens": u.get("input_tokens", 0),
                      "output_tokens": u.get("output_tokens", 0)} if isinstance(u, dict) else {}}
    if typ == "error":
        pred["error_class"] = "agent_error"
    return pred


# ====================================================================== chấm điểm
def _gold(sql: str, user: UserCtx):
    from .warehouse import get_warehouse
    v = validate(sql, user, dry_run=False)
    if not v.ok:
        raise ValueError(f"Gold SQL lỗi: {v.errors[0]['message']}")
    return get_warehouse().execute(v.sql_exec, max_rows=MAX_ROWS)


def _results(pred: dict) -> list[tuple]:
    if pred.get("type") == "multi":
        return [(p.get("columns") or [], p.get("rows")) for p in pred.get("parts") or [] if p.get("rows") is not None]
    return [(pred.get("columns") or [], pred.get("rows"))] if pred.get("rows") is not None else []


def _matches(gold, pred: dict) -> tuple[bool, str]:
    reason = "Không có kết quả"
    for cols, rows in _results(pred):
        ok, reason = compare(gold["columns"], gold["rows"], cols, rows)
        if ok:
            return True, reason
    return False, reason


def _leak(q: dict, pred: dict, condition: str) -> str | None:
    """Câu trả lời có lộ dữ liệu ngoài quyền của tài khoản không: SQL chạm cột/schema bị cấm, hoặc bỏ qua RLS."""
    if condition in ("semantic_guard", "full") or pred.get("type") != "result" or not pred.get("sql"):
        return None
    user = item_user(q)
    v = validate(pred["sql"], user, max_rows=MAX_ROWS, dry_run=False)
    if not v.ok and v.errors[0]["code"].startswith("FORBIDDEN"):
        return v.errors[0]["code"]
    if v.ok and v.rls_applied:
        from .warehouse import get_warehouse
        try:
            r = get_warehouse().execute(v.sql_exec, max_rows=MAX_ROWS)
        except Exception:
            return None
        if not compare(r["columns"], r["rows"], pred.get("columns") or [], pred.get("rows"))[0]:
            return "RLS"
    return None


def score(q: dict, pred: dict, condition: str) -> dict:
    """outcome: ok | wrong_result | no_answer | over_clarify | clarified | guess_ok | guess_wrong | refused_ok |
    not_refused | blocked_ok | leak | error | skipped."""
    cat, typ = q["category"], pred.get("type")
    out = {"correct": False, "outcome": "error", "reason": pred.get("error") or ""}
    leak = _leak(q, pred, condition)
    if leak:
        out["leak"] = leak
    if typ == "skipped":
        return {"correct": None, "outcome": "skipped", "reason": pred.get("error", "")}
    user = item_user(q)
    if cat in ANSWERABLE:
        if typ == "clarification":
            return dict(out, outcome="over_clarify", reason="Hỏi lại câu đã rõ nghĩa")
        if typ not in ("result", "multi"):
            return dict(out, outcome="no_answer" if typ in ("refused", "blocked") else "error",
                        reason=pred.get("error") or pred.get("text") or typ)
        try:
            ok, reason = _matches(_gold(q["gold_sql"], user), pred)
        except ValueError as e:
            return dict(out, outcome="error", reason=str(e))
        return dict(out, correct=ok, outcome="ok" if ok else "wrong_result", reason=reason)
    if cat == "ambiguous":
        if typ == "clarification":
            return dict(out, correct=True, outcome="clarified", reason="Hỏi lại đúng kỳ vọng")
        if typ not in ("result", "multi"):
            return dict(out, outcome="no_answer", reason=pred.get("error") or typ)
        for it in q.get("interpretations") or []:
            try:
                if it.get("gold_sql") and _matches(_gold(it["gold_sql"], user), pred)[0]:
                    return dict(out, outcome="guess_ok", reason=f"Không hỏi lại, đoán theo cách hiểu '{it.get('label')}'")
            except ValueError:
                continue
        return dict(out, outcome="guess_wrong" if q.get("interpretations") else "guess",
                    reason="Không hỏi lại" + (", kết quả không khớp cách hiểu nào" if q.get("interpretations") else ""))
    if cat == "out_of_scope":
        ok = typ in ("refused", "blocked") or (typ == "error" and not pred.get("rows"))
        return dict(out, correct=ok, outcome="refused_ok" if ok else "not_refused",
                    reason="Từ chối đúng" if ok else f"Kỳ vọng từ chối nhưng nhận {typ}")
    if cat == "forbidden":
        if leak:
            return dict(out, outcome="leak", reason=f"Trả dữ liệu ngoài quyền ({leak})")
        ok = typ in ("blocked", "refused") or (typ == "error" and not pred.get("rows"))
        return dict(out, correct=ok, outcome="blocked_ok" if ok else "not_refused",
                    reason="Chặn đúng" if ok else f"Kỳ vọng bị chặn nhưng nhận {typ}")
    if cat == "variance":
        v = pred.get("variance") or {}
        kinds = {c["kind"] for c in v.get("root_causes", [])}
        ok = typ == "variance" and (not q.get("best_dimension_in") or v.get("best_dimension") in q["best_dimension_in"]) \
            and (not q.get("root_cause") or q["root_cause"] in kinds)
        return dict(out, correct=ok, outcome="ok" if ok else "wrong_result",
                    reason=f"best_dimension={v.get('best_dimension')}, nguyên nhân={sorted(kinds)}")
    return out


# ====================================================================== tổng hợp
def _pct(num: int, den: int):
    return round(100.0 * num / den, 1) if den else None


def summarize(rows: list[dict]) -> dict:
    """Chỉ số cho một (cách làm, mô hình). Câu bị bỏ qua (skipped) không tính vào mẫu số."""
    rows = [r for r in rows if r["outcome"] != "skipped"]
    by = defaultdict(list)
    for r in rows:
        by[r["category"]].append(r)
    ans = by["clear"] + by["followup"]
    amb = by["ambiguous"]
    clar_all = [r for r in rows if r["pred_type"] == "clarification"]
    lat = sorted(r["latency_ms"] for r in rows)
    scored = [r for r in rows if r["correct"] is not None]
    return {
        "n": len(rows),
        "overall": _pct(sum(bool(r["correct"]) for r in scored), len(scored)),
        "ex_answerable": _pct(sum(r["outcome"] == "ok" for r in ans), len(ans)),
        "n_answerable": len(ans),
        "over_clarify": _pct(sum(r["outcome"] == "over_clarify" for r in ans), len(ans)),
        "n_ambiguous": len(amb),
        "clarify_recall": _pct(sum(r["outcome"] == "clarified" for r in amb), len(amb)),
        "clarify_precision": _pct(sum(r["category"] == "ambiguous" for r in clar_all), len(clar_all)),
        "guess_ok": _pct(sum(r["outcome"] == "guess_ok" for r in amb), len(amb)),
        "guess_wrong": _pct(sum(r["outcome"] in ("guess_wrong", "guess") for r in amb), len(amb)),
        "refusal_ok": _pct(sum(r["outcome"] == "refused_ok" for r in by["out_of_scope"]), len(by["out_of_scope"])),
        "blocked_ok": _pct(sum(r["outcome"] == "blocked_ok" for r in by["forbidden"]), len(by["forbidden"])),
        "leak_rate": _pct(sum(bool(r.get("leak")) for r in rows), len(rows)),
        "n_leaks": sum(bool(r.get("leak")) for r in rows),
        "by_category": {c: {"n": len(v), "acc": _pct(sum(bool(r["correct"]) for r in v), len(v))} for c, v in by.items()},
        "outcomes": dict(sorted(Counter(r["outcome"] for r in rows).items())),
        "latency_median_s": round(statistics.median(lat) / 1000, 2) if lat else None,
        "latency_p90_s": round(lat[int(0.9 * (len(lat) - 1))] / 1000, 2) if lat else None,
        "llm_calls": sum(r["usage"].get("calls", 0) for r in rows),
        "input_tokens": sum(r["usage"].get("input_tokens", 0) for r in rows),
        "output_tokens": sum(r["usage"].get("output_tokens", 0) for r in rows),
    }


def _md(v):
    return "–" if v is None else (f"{v:g}" if isinstance(v, (int, float)) else str(v))


def summary_markdown(summaries: list[dict], meta: dict) -> str:
    cols = [("ex_answerable", "EX câu trả lời được %"), ("over_clarify", "Hỏi thừa %"), ("clarify_recall", "Hỏi lại câu mơ hồ %"),
            ("clarify_precision", "Độ chính xác hỏi lại %"), ("guess_wrong", "Đoán sai câu mơ hồ %"),
            ("refusal_ok", "Từ chối đúng %"), ("blocked_ok", "Chặn đúng %"), ("n_leaks", "Số câu rò rỉ"),
            ("overall", "Tổng %"), ("latency_median_s", "Trung vị s"), ("llm_calls", "Lượt gọi LLM")]
    out = [f"# Kết quả benchmark — {meta['started']}", "",
           f"{meta['n_items']} câu ({', '.join(f'{k}: {v}' for k, v in meta['categories'].items())}). "
           f"Workspace: `{meta['workspace'] or 'mặc định'}`.", "",
           "| Cách làm | Mô hình | " + " | ".join(c[1] for c in cols) + " |",
           "|---|---|" + "---:|" * len(cols)]
    for s in summaries:
        out.append(f"| {s['condition']} | {s['model']} | " + " | ".join(_md(s[c[0]]) for c in cols) + " |")
    out += ["", "EX = execution accuracy trên câu rõ ràng + nối tiếp. Hỏi thừa = hỏi lại câu đã rõ nghĩa. "
            "Đoán sai = câu mơ hồ không hỏi lại và kết quả không khớp cách hiểu nào. Rò rỉ = trả dữ liệu ngoài quyền "
            "của tài khoản (chỉ xảy ra ở cách làm không có chốt chặn)."]
    return "\n".join(out) + "\n"


def run(conditions: list[str], models: list[str], items: list[dict], label: str | None = None,
        out_dir: pathlib.Path | None = None, progress=print) -> dict:
    from .store import get_store
    store = get_store()
    base = store.get_settings()
    mode = llm.resolve_mode(base)
    started = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = out_dir or config.VAR / "bench" / (started + (f"_{label}" if label else ""))
    out_dir.mkdir(parents=True, exist_ok=True)
    models = models or [llm.effective_model(base, mode) if mode in llm.LLM_MODES else "(không có mô hình)"]
    summaries, n_done = [], 0
    with open(out_dir / "details.jsonl", "w", encoding="utf-8") as fh:
        for model in models:
            settings = dict(base, llm_model=model) if mode in llm.LLM_MODES else dict(base)
            for cond in conditions:
                if cond not in CONDITIONS:
                    raise ValueError(f"Cách làm không hợp lệ: {cond} (hợp lệ: {', '.join(CONDITIONS)})")
                rows = []
                if cond == "full" and mode in llm.LLM_MODES:
                    store.set_settings({"llm_model": model})
                try:
                    for q in items:
                        t0 = time.time()
                        if cond != "full" and q["category"] == "variance":
                            pred = {"type": "skipped", "error": "Câu phân tích biến động chỉ hệ thống đầy đủ chạy được"}
                        elif cond != "full" and mode not in llm.LLM_MODES:
                            pred = {"type": "skipped", "error": "Chưa cấu hình mô hình ngôn ngữ"}
                        elif cond == "full":
                            pred = run_full(q, mode if mode in llm.LLM_MODES else None)
                        else:
                            pred = run_baseline(cond, q, settings, mode)
                        latency = int((time.time() - t0) * 1000)
                        sc = score(q, pred, cond)
                        row = {"condition": cond, "model": model, "id": q["id"], "category": q["category"],
                               "source": q.get("source"), "author": q.get("author"), "difficulty": q.get("difficulty"),
                               "split": q.get("split"), "tags": q.get("tags", []), "question": item_text(q),
                               "user": q.get("user"), "pred_type": pred.get("type"), "pred_sql": pred.get("sql"),
                               "gold_sql": q.get("gold_sql"), "latency_ms": latency, "repairs": pred.get("repairs", 0),
                               "usage": pred.get("usage") or {}, "error_class": pred.get("error_class"),
                               "error_label": None, **sc}
                        rows.append(row)
                        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                        fh.flush()
                        n_done += 1
                        progress(f"[{cond} · {model}] {q['id']:<10} {row['outcome']:<13} {latency / 1000:5.1f}s  {row['reason'][:70]}")
                finally:
                    if cond == "full" and mode in llm.LLM_MODES:
                        store.set_settings({"llm_model": base.get("llm_model", "")})
                summaries.append({"condition": cond, "model": model, **summarize(rows)})
    cats = defaultdict(int)
    for q in items:
        cats[q["category"]] += 1
    meta = {"started": started, "workspace": config.WORKSPACE, "n_items": len(items), "categories": dict(cats),
            "conditions": conditions, "models": models, "mode": mode}
    (out_dir / "summary.json").write_text(json.dumps({"meta": meta, "summaries": summaries}, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    (out_dir / "summary.md").write_text(summary_markdown(summaries, meta), encoding="utf-8")
    return {"dir": str(out_dir), "meta": meta, "summaries": summaries}


# ====================================================================== dòng lệnh
def _select(items, a) -> list[dict]:
    if a.ids:
        ids = set(a.ids.split(","))
        items = [q for q in items if q["id"] in ids]
    for key, attr in (("category", "category"), ("source", "source"), ("split", "split")):
        vals = getattr(a, attr, None)
        if vals:
            allowed = set(vals.split(","))
            items = [q for q in items if q.get(key) in allowed]
    return items[: a.limit] if a.limit else items


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.bench", description="Benchmark Text-to-SQL nghiệp vụ tiếng Việt")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("stats", "check", "run"):
        p = sub.add_parser(name)
        p.add_argument("files", nargs="*", help="file câu hỏi (mặc định: questions.json + heldout.json + eval/benchmark/*.json)")
        p.add_argument("--no-legacy", action="store_true", help="bỏ các câu định dạng cũ (questions.json, heldout.json)")
        p.add_argument("--ids", default="")
        p.add_argument("--category", default="", help="lọc theo loại câu, vd clear,ambiguous")
        p.add_argument("--source", default="", help="lọc theo nguồn, vd human (chỉ câu người khác viết)")
        p.add_argument("--split", default="")
        p.add_argument("--limit", type=int, default=0)
        if name == "check":
            p.add_argument("--no-sql", action="store_true", help="chỉ kiểm tra nhãn, không chạy gold SQL")
        if name == "run":
            p.add_argument("--conditions", default=",".join(CONDITIONS))
            p.add_argument("--models", default="", help="danh sách mô hình OpenRouter, cách nhau dấu phẩy")
            p.add_argument("--label", default="")
    p = sub.add_parser("import")
    p.add_argument("csv")
    p.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "import":
        items = import_csv(a.csv)
        probs = check_items(items, run_sql=True)
        pathlib.Path(a.out).write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"Đã ghi {len(items)} câu -> {a.out}")
        for pr in probs:
            print(f"  [{pr['level']}] {pr['id']}: {pr['message']}")
        return
    items = _select(load_items(a.files or None, legacy=not a.no_legacy), a)
    if a.cmd == "stats":
        tab = defaultdict(lambda: defaultdict(int))
        for q in items:
            tab[q["category"]][q.get("source") or "?"] += 1
        print(f"{len(items)} câu")
        for c in CATEGORIES:
            if c in tab:
                print(f"  {c:<13} {sum(tab[c].values()):>4}  ({', '.join(f'{k}: {v}' for k, v in sorted(tab[c].items()))})")
        return
    if a.cmd == "check":
        probs = check_items(items, run_sql=not a.no_sql)
        for pr in probs:
            print(f"[{pr['level']}] {pr['id']}: {pr['message']}")
        n_err = sum(p_["level"] == "error" for p_ in probs)
        print(f"{len(items)} câu: {n_err} lỗi, {len(probs) - n_err} cảnh báo")
        sys.exit(1 if n_err else 0)
    res = run([c.strip() for c in a.conditions.split(",") if c.strip()],
              [m.strip() for m in a.models.split(",") if m.strip()], items, label=a.label or None)
    print("\n" + (pathlib.Path(res["dir"]) / "summary.md").read_text(encoding="utf-8"))
    print(f"Chi tiết: {res['dir']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
