"""
Agent Text-to-SQL đa tác tử, điều phối bằng đồ thị trạng thái (tương đương LangGraph StateGraph):

  planner = chốt chặn bằng code ─► đường nhanh (bộ luật hiểu trọn câu) ─► Router (LLM: ý định, viết lại câu hỏi
            độc lập, tách câu nhiều ý) ─► đường nhanh trên câu đã viết lại ─► Planner (LLM gọi tool)

  load_context ─► planner ─┬─► clarify ─────────────────────────────────────────────┐
                           ├─► responder (định nghĩa chỉ số / schema / DQ / trợ giúp) ┤
                           ├─► variance_analyst (vì sao tăng/giảm) ──────────────────┤
                           └─► sql_generator ─► optimizer ─► validator ◄──┐            │
                                                              │    │      │            │
                                                              │    └─► repairer        │
                                                              ▼           ▲            │
                                                         cost_guard ──(HITL: chờ duyệt)┤
                                                              ▼           │            │
                                                          executor ───────┘(lỗi)       │
                                                              ▼                        │
                                                    analyst ─► explainer ─► finalize ◄─┘
Mỗi node ghi trace (thời gian, chi tiết) để hiển thị trên giao diện.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
import time
import uuid

from concurrent.futures import ThreadPoolExecutor

from . import config, cost, critic, dq, explain, llm, lookup, profile, repair, sqlcheck
from .catalog import get_catalog
from .governance import UserCtx, restricted_request, validate
from .nlu import get_nlu, is_why_question
from .retriever import get_retriever
from .semantic import SemanticError, get_semantic
from .store import get_store
from .textutil import fmt_money, human_bytes, norm
from .warehouse import QueryError, get_warehouse

END = "__end__"
NARRATE_RESERVE_S = 3.5     # thời gian dành cho lượt gọi viết lời diễn giải (model nhanh) trong ngân sách mỗi câu
# từ hỏi nguyên nhân/chiều biến động mà bộ luật không đánh dấu là đã dùng, nhưng không đổi nghĩa câu "vì sao"
WHY_WORDS = {"vi", "sao", "tai", "giam", "tang", "bien", "dong", "nguyen", "nhan", "ly", "do", "lai", "vay", "the", "nao",
             "a", "nhi", "sut", "manh", "di", "len", "xuong", "nhu", "the"}
# câu nối tiếp chỉ đòi chia theo một chiều ("chia theo kênh bán") mà không hỏi nguyên nhân: trả bảng chia, không phân tích
# "vì sao" lại dù Router viết lại câu thành câu "vì sao ... chia theo ..." do lượt trước là câu vì sao
CAUSE_RE = re.compile(r"\b(vi sao|tai sao|nguyen nhan|ly do|do dau|sao lai|giai thich|vi dau)\b")
BREAKDOWN_RE = re.compile(r"^(chia|phan|tach|xem|cho xem|nhom)?\s*theo\b|\b(chia|phan|tach|nhom) theo\b")


def _breakdown_only(msg: str) -> bool:
    m = norm(msg or "")
    return bool(BREAKDOWN_RE.search(m)) and not CAUSE_RE.search(m)


CRITIC_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="critic")   # LLM chấm chạy song song với lượt viết lời
MULTI_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="task")       # các ý của câu nhiều ý chạy song song
MAX_TASKS = 4
SPEC_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="spec")       # Planner chạy đón trước song song Router
CRITIC_WAIT_S = 6.0         # chờ LLM chấm tối đa từng này giây sau khi đã có lời diễn giải
# trạng thái của một lần lập kế hoạch + chạy truy vấn; Critic yêu cầu làm lại thì xóa, làm lại lỗi thì khôi phục
PLAN_KEYS = ("plan", "sql", "sql_source", "columns_meta", "result", "facts", "template", "text", "highlights", "chart",
             "audit", "validation", "_v", "lint", "cost", "metrics_matched", "lineage", "attempts", "repairs", "executed",
             "empty_result", "narrator", "intent", "chart_hint", "status", "error", "last_error", "assumptions", "notes",
             "critic_issues", "variance")
MULTI_CLAUSE_RE = re.compile(r";|,\s*(va|roi|con|them|kem|dong thoi|ngoai ra|sau do)\b|\b(them nua|ngoai ra|dong thoi|sau do|"
                             r"kem theo|kem luon)\b|\b(va|roi) (vi sao|tai sao|top|cho (minh|toi|em))\b")
NON_REPAIRABLE = {"FORBIDDEN_STATEMENT", "FORBIDDEN_FUNCTION", "FORBIDDEN_SCHEMA", "FORBIDDEN_COLUMN", "MULTI_STATEMENT", "EMPTY"}


class Graph:
    """Đồ thị trạng thái tối giản: node là hàm (state) -> tên node kế tiếp."""

    def __init__(self):
        self.nodes = {}

    def add(self, name, fn):
        self.nodes[name] = fn

    def run(self, state: dict, start: str, max_steps: int = 40) -> dict:
        name = start
        steps = 0
        while name != END and steps < max_steps:
            steps += 1
            t0 = time.time()
            state["_detail"] = ""
            try:
                nxt = self.nodes[name](state)
            except Exception as e:  # lỗi không lường trước -> kết thúc an toàn
                state["status"] = "error"
                state["error"] = f"Lỗi nội bộ ở bước {name}: {type(e).__name__}: {e}"
                nxt = "finalize" if name != "finalize" else END
            state["trace"].append({"node": name, "ms": int((time.time() - t0) * 1000), "detail": state.pop("_detail", "")})
            name = nxt
        return state


SAMPLE_QUESTIONS = profile.sample_questions()      # câu hỏi mẫu của doanh nghiệp đang nạp (config/profile.yml)


class Agent:
    def __init__(self):
        self.store = get_store()
        self.graph = Graph()
        for n in ("load_context", "planner", "clarify", "responder", "variance_analyst", "sql_generator", "optimizer",
                  "validator", "repairer", "cost_guard", "executor", "analyst", "explainer", "critic", "multi", "finalize"):
            self.graph.add(n, getattr(self, "n_" + n))
        self._seed_fewshots()

    def _seed_fewshots(self):
        import yaml
        if any(f["source"] == "seed" for f in self.store.list_fewshots()):
            return
        path = config.CONFIG / "fewshots.yml"
        if path.exists():
            for item in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
                self.store.add_fewshot(item["question"], item["sql"].strip(), source="seed", verified=True)
        get_retriever().reload_fewshots()

    # ================================================================== API công khai
    def new_state(self, user: UserCtx, conversation_id: str, question: str, **kw) -> dict:
        s = {"run_id": uuid.uuid4().hex[:12], "user": user, "conversation_id": conversation_id, "question": question,
             "t0": time.time(), "trace": [], "assumptions": [], "notes": [], "repairs": [], "attempts": 0,
             "status": None, "plan": None, "sql": None, "sql_source": None, "columns_meta": None, "intent": None,
             "approved": False, "approved_by": None, "chart_hint": None, "llm_trace": [], "executed": [],
             "clarification_answer": kw.get("clarification"), "eval_mode": kw.get("eval_mode", False),
             "force_mode": kw.get("mode")}
        return s

    def ask(self, user: UserCtx, conversation_id: str, question: str, clarification: dict | None = None,
            eval_mode: bool = False, mode: str | None = None) -> dict:
        s = self.new_state(user, conversation_id, question, clarification=clarification, eval_mode=eval_mode, mode=mode)
        self.graph.run(s, "load_context")
        return s["response"]

    def run_sql(self, user: UserCtx, conversation_id: str, sql: str, question: str | None = None) -> dict:
        s = self.new_state(user, conversation_id, question or "[SQL Console]")
        s["sql"], s["sql_source"], s["intent"] = sql, "user", "sql"
        s["console"] = True
        self.n_load_context(s, skip_clarify=True)
        s["trace"].append({"node": "load_context", "ms": 0, "detail": "SQL do người dùng nhập"})
        self.graph.run(s, "validator")
        return s["response"]

    def approve(self, user: UserCtx, run_id: str, action: str, edited_sql: str | None = None) -> dict:
        p = self.store.pop_pending(run_id)
        if not p:
            return {"type": "error", "text": "Không tìm thấy truy vấn đang chờ duyệt (có thể đã được xử lý)."}
        st = p["state"]
        if p["username"] != user.username and user.role != "admin":
            return {"type": "error", "text": "Chỉ người tạo truy vấn (hoặc Admin) được duyệt."}
        s = self.new_state(user, p["conversation_id"], st["question"])
        for k in ("plan", "sql", "sql_source", "columns_meta", "intent", "assumptions", "notes", "chart_hint", "repairs",
                  "attempts", "mode", "console", "lookup", "lookup_built"):
            if k in st:
                s[k] = st[k]
        s["run_id"] = run_id
        s["trace"] = st.get("trace", [])
        s["_from_approval_msg"] = True
        self.n_load_context(s, skip_clarify=True)
        if action == "reject":
            s["status"] = "rejected"
            s["trace"].append({"node": "hitl", "ms": 0, "detail": f"{user.username} từ chối chạy SQL"})
            self.graph.run(s, "finalize")
            return s["response"]
        s["approved"], s["approved_by"] = True, user.username
        detail = f"{user.username} đã duyệt"
        if edited_sql and edited_sql.strip() != (s["sql"] or "").strip():
            s["sql"], s["sql_source"] = edited_sql, "user_edit"
            s["columns_meta"] = None
            detail += " (đã chỉnh sửa SQL — kiểm duyệt lại từ đầu)"
        s["trace"].append({"node": "hitl", "ms": 0, "detail": detail})
        self.graph.run(s, "validator")
        return s["response"]

    # ================================================================== NODES
    def n_load_context(self, s, skip_clarify=False):
        conv = self.store.get_conversation(s["conversation_id"]) or {"memory": {}}
        s["memory"] = conv.get("memory") or {}
        s["settings"] = self.store.get_settings()
        forced = s.get("force_mode") if s.get("force_mode") in llm.LLM_MODES else None
        s["mode"] = forced or (s.get("mode") if s.get("mode") in llm.LLM_MODES else None) or llm.resolve_mode(s["settings"])
        s["anchor"] = dt.date.fromisoformat(get_catalog().anchor_date())
        if skip_clarify:
            return "planner"
        pc = s["memory"].get("pending_clarification")
        ans = s.get("clarification_answer")
        chosen = None
        if pc:
            if ans and ans.get("id") == pc["id"] and ans.get("option") is not None and 0 <= int(ans["option"]) < len(pc["options"]):
                chosen = pc["options"][int(ans["option"])]
            else:
                qn = norm(s["question"])
                for o in pc["options"]:
                    if norm(o["label"]).split(" —")[0] and (norm(o["label"]).startswith(qn) or qn in norm(o["label"])) and len(qn) >= 4:
                        chosen = o
                        break
            s["memory"].pop("pending_clarification", None)
            if chosen:
                prefs = s["memory"].setdefault("prefs", {})
                if chosen.get("metric") and pc.get("pref_key"):
                    prefs[pc["pref_key"]] = chosen["metric"]
                s["question_display"] = s["question"]
                s["question"] = chosen.get("rewrite") or pc["question"]
                s["_detail"] = f"Người dùng chọn: {chosen['label']}"
                s["clarified"] = chosen
        mem = s["memory"]
        s["_detail"] = s.get("_detail") or (f"Bộ nhớ: {len(mem.get('history', []))} lượt trước, ưu tiên {mem.get('prefs', {})}"
                                            if mem else "Hội thoại mới")
        return "planner"

    def n_planner(self, s):
        """Thứ tự: chốt chặn bằng code -> đường nhanh (bộ luật hiểu TRỌN VẸN câu hỏi, không gọi LLM) -> Router (LLM phân
        loại ý định, viết lại câu hỏi độc lập, tách câu nhiều ý) -> chốt chặn + đường nhanh trên câu đã viết lại -> SQL đã
        xác nhận -> Planner (LLM gọi tool). Không còn chế độ offline: câu bộ luật chỉ hiểu một phần luôn đi qua LLM."""
        q = s["question"]
        clarified = bool(s.get("clarified"))
        # -1) chủ đề cấm: từ chối bằng code, trước mọi bước khác (không gọi LLM, không kế thừa ngữ cảnh)
        hit = None if clarified else profile.blocked_topic(q)
        if hit:
            s["intent"], s["refused"] = "refused", True
            s["info_text"] = profile.refusal_text()
            s["suggestions"] = profile.sample_questions()[:4]
            s["_detail"] = f"Từ chối: câu hỏi chạm chủ đề cấm (“{hit}”), không truy vấn dữ liệu"
            return "responder"
        if not clarified:
            # 0) giới thiệu / xã giao khớp mẫu chắc chắn: trả lời ngay từ hồ sơ doanh nghiệp
            if profile.is_intro_question(q):
                s["intent"] = "help"
                s["_detail"] = "Câu hỏi giới thiệu: trả lời từ hồ sơ doanh nghiệp, không gọi LLM"
                return "responder"
            reply = profile.smalltalk_reply(q)
            if reply:
                s["intent"], s["info_text"] = "smalltalk", reply
                s["_detail"] = "Câu xã giao: đáp ngắn, không truy vấn dữ liệu"
                return "responder"
            # 1) chốt chặn bằng code: sửa/xóa dữ liệu, cột bị hạn chế, dự báo, mốc thời gian sai, mã không có, tra cứu mã
            nxt = self._code_guards(s, q)
            if nxt:
                return nxt
        # 2) đường nhanh trên câu gốc
        nxt = self._fast_path(s, q)
        if nxt:
            return nxt
        if s["mode"] not in llm.LLM_MODES:
            return self._no_llm(s)
        # 3) Router (ý con của câu nhiều ý đã được Router của câu cha phân loại và viết thành câu độc lập)
        if not clarified and not s.get("subtask"):
            # câu có thuật ngữ mơ hồ sẽ được hỏi lại bằng code: không chạy đón trước (tốn lượt gọi vô ích)
            if s["settings"].get("speculative_plan", True) and not get_nlu().detect_ambiguity(q, s["memory"]):
                snap = dict(s, route_why=False, llm_trace=[])        # ảnh chụp trạng thái: luồng nền chỉ đọc
                s["_spec"] = (q, SPEC_POOL.submit(self._call_planner, snap))
            nxt = self._route(s)
            if nxt:
                return nxt
            if s["question"] != q:          # câu đã được viết lại: chạy lại chốt chặn và đường nhanh trên câu mới
                q = s["question"]
                nxt = self._code_guards(s, q) or self._fast_path(s, q)
                if nxt:
                    return nxt
        # 4) tái sử dụng SQL đã được xác nhận (học từ phản hồi) cho câu hỏi độc lập
        if not clarified:
            ret = get_retriever()
            fs = ret.reusable(q, threshold=float(s["settings"].get("fewshot_reuse_threshold", 0.8)))
            if fs and fs.get("sql"):
                s["sql"], s["sql_source"], s["intent"] = fs["sql"], "fewshot", "metric_query"
                s["fewshot_hit"] = {"id": fs["id"], "question": fs["question"], "similarity": fs["similarity"], "source": fs["source"]}
                self.store.touch_fewshot(fs["id"])
                s["notes"].append(f"Tái sử dụng SQL đã được xác nhận cho câu hỏi tương tự: “{fs['question']}” "
                                  f"(độ tương đồng {fs['similarity']:.2f}).")
                s["_detail"] = f"Few-shot hit #{fs['id']} ({fs['source']})"
                return "optimizer"
            # câu đã rõ thời gian/bộ lọc/cách chia, bộ luật hiểu hết mọi từ, chỉ thiếu chỉ số ("so sánh miền Bắc với miền
            # Nam quý 3"): hỏi lại chỉ số bằng code thay vì để mô hình đoán
            r = get_nlu().parse(q, {"prefs": s["memory"].get("prefs") or {}})
            follow = (s.get("route") or {}).get("is_follow_up") or get_nlu().parse(q, s["memory"]).follow_up
            if r.intent == "unknown" and not r.plan.get("metrics") and not get_nlu().unknown_terms(r)[0] and not follow \
                    and (r.plan.get("time_range") or r.plan.get("filters") or r.plan.get("dimensions")):
                c = self._metric_question(q, r)
                if c:
                    s["clarification"] = c
                    s["_detail"] = "Câu hỏi có thời gian/bộ lọc/cách chia nhưng không nêu chỉ số: hỏi lại chỉ số"
                    return "clarify"
            # thuật ngữ mơ hồ do doanh nghiệp khai báo thì luôn hỏi lại bằng code, không phụ thuộc mô hình
            amb = get_nlu().detect_ambiguity(q, s["memory"])
            if amb:
                s["clarification"] = dict(amb, question_text=amb["question"])
                s["_detail"] = f"Thuật ngữ mơ hồ theo semantic layer ({amb['id']}): hỏi lại trước khi gọi Planner"
                return "clarify"
        # 5) Planner: LLM function calling
        try:
            return self._plan_with_claude(s)
        except (llm.LLMUnavailable, llm.LLMError) as e:
            return self._llm_failed(s, e)
        except Exception as e:  # pragma: no cover
            return self._llm_failed(s, f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------ đường nhanh: bộ luật hiểu trọn vẹn câu hỏi
    def _fast_path(self, s, q):
        """Trả lời không cần LLM khi bộ luật dùng hết mọi từ có nghĩa trong câu, chỉ số được nêu ngay trong câu (không kế
        thừa từ câu trước) và không có mã đối tượng nào bộ luật không lọc được. Câu hiểu một phần luôn đi qua LLM.
        Tắt được bằng cài đặt fast_path=False (đo riêng chất lượng của các tác tử LLM)."""
        if not s["settings"].get("fast_path", True):
            return None
        nlu = get_nlu()
        r = nlu.parse(q, {"prefs": (s["memory"].get("prefs") or {})})
        if r.intent not in ("metric_query", "variance") or not r.plan.get("metrics") or r.follow_up:
            return None
        # câu nối tiếp ("còn doanh thu gộp thì sao") đọc riêng thì có vẻ trọn vẹn nhưng thiếu kỳ/bộ lọc của câu trước
        if s["memory"].get("last_plan") and nlu.parse(q, s["memory"]).follow_up:
            return None
        # câu nối nhiều mệnh đề ("top 5 cửa hàng ..., và vì sao ... giảm"): bộ luật chỉ dựng được MỘT kế hoạch, sẽ bỏ mất một
        # ý -> để Router tách ý. "doanh thu thuần và số đơn" (hai chỉ số trong một mệnh đề) vẫn đi đường nhanh.
        if MULTI_CLAUSE_RE.search(_fold(q)):
            return None
        left = [w for w in r.matched.get("unparsed") or []
                if not (r.intent == "variance" and norm(w) in WHY_WORDS)]     # "Vì sao ... giảm": từ hỏi nguyên nhân
        if left or r.matched.get("invalid_time"):
            return None
        lk = lookup.get_lookups()
        hits, unknown = lk.find_codes(q)
        if unknown or any(not e.dimension for e, _, _ in hits):
            return None
        # "Top 10 khách hàng ..." mà khách hàng không phải chiều phân tích: bộ luật chỉ ra được con số tổng -> để LLM viết SQL
        tn = norm(q)
        for e in lk.entities.values():
            words = "|".join(map(re.escape, e.words))
            if not e.dimension and words and re.search(rf"\b(top \d+|\d+|theo|tung|moi|cac|nhung)\s+({words})\b|\b({words})\s+nao\b", tn):
                return None
        if r.clarification:
            s["clarification"] = dict(r.clarification, question_text=r.clarification["question"])
            s["_detail"] = "Đường nhanh: thuật ngữ mơ hồ theo semantic layer, hỏi lại"
            return "clarify"
        s["intent"], s["plan"], s["chart_hint"] = r.intent, r.plan, r.chart_hint
        s["assumptions"] += r.assumptions
        s["path"] = "fast"
        p = r.plan
        s["_detail"] = (f"Đường nhanh (bộ luật hiểu trọn câu hỏi, không gọi LLM): ý định={r.intent}, chỉ số={p.get('metrics')}, "
                        f"chiều={p.get('dimensions')}, lọc={[(f['dimension'], f['values']) for f in p.get('filters', [])]}, "
                        f"thời gian={(p.get('time_range') or {}).get('label')}")
        return "variance_analyst" if r.intent == "variance" else "sql_generator"

    @staticmethod
    def _metric_question(q, r) -> dict | None:
        """Câu hỏi đã có thời gian, bộ lọc hoặc cách chia mà thiếu chỉ số ("so sánh miền Bắc với miền Nam quý 3"):
        hỏi lại nên xem chỉ số nào, mỗi lựa chọn viết lại câu hỏi kèm tên chỉ số."""
        sem, nlu = get_semantic(), get_nlu()
        ranking = bool(r.matched.get("direction"))
        names = [o["metric"] for a in nlu.rank_amb for o in a["options"] if o.get("metric") in sem.metrics]
        dims = set(r.plan.get("dimensions") or []) | {f["dimension"] for f in r.plan.get("filters") or []}
        # chỉ đưa chỉ số phân tích được theo đúng các chiều câu hỏi nhắc tới
        names = [m for m in dict.fromkeys(names + list(sem.metrics)) if dims <= set(sem.dims_for_metric(m))][:4]
        if not names:
            return None
        return {"id": "metric_" + uuid.uuid4().hex[:6], "pref_key": None, "question": q,
                "question_text": "Bạn muốn xếp hạng theo tiêu chí nào?" if ranking else "Bạn muốn xem chỉ số nào?",
                "options": [{"label": sem.metrics[m]["label"], "metric": m, "rewrite": f"{sem.metrics[m]['label']} {q}"}
                            for m in names]}

    # ------------------------------------------------------------------ Router: phân loại + viết lại câu hỏi
    def _route(self, s):
        q, mem = s["question"], s["memory"]
        ctx = {"anchor": s["anchor"].isoformat(), "history": mem.get("history") or [],
               "last_question": mem.get("last_question"), "prefs": mem.get("prefs"), "last_plan": mem.get("last_plan")}
        t0 = time.time()
        try:
            client = llm.get_client(s["settings"], s["mode"], role="router")
            out = client.route(q, ctx)
        except (llm.LLMUnavailable, llm.LLMError) as e:
            return self._llm_failed(s, e)
        except Exception as e:  # pragma: no cover
            return self._llm_failed(s, f"{type(e).__name__}: {e}")
        ms = int((time.time() - t0) * 1000)
        self._add_usage(s, client)
        s["llm_trace"].append({"tool": "router", "input": {"question": q}, "ms": ms, "error": False,
                               "output": {k: out[k] for k in ("intent", "is_follow_up", "standalone_question", "tasks")}})
        s["route"] = out
        intent = out["intent"]
        s["_detail"] = (f"Router ({client.model}, {ms} ms): ý định={intent}" + (", câu nối tiếp" if out["is_follow_up"] else "")
                        + (f", viết lại: “{out['standalone_question']}”" if out["standalone_question"] != q else "")
                        + (f", {len(out['tasks'])} việc" if len(out["tasks"]) > 1 else ""))
        sug = profile.sample_questions()[:4]
        if intent == "unsafe":
            s.update(intent="refused", refused=True, info_text=profile.refusal_text(), suggestions=sug)
            return "responder"
        if intent == "write_request":
            s.update(intent="refused", refused=True, info_text=profile.write_refusal_text(), suggestions=sug)
            return "responder"
        if intent == "about_assistant":
            s["intent"] = "help"
            return "responder"
        if intent == "smalltalk":
            reply = out["reply"] if out["reply"] and not re.search(r"\d", out["reply"]) else ""
            s.update(intent="smalltalk", info_text=reply or "Mình luôn sẵn sàng! Bạn muốn xem số liệu nào?")
            return "responder"
        if intent == "out_of_scope":
            topic = profile.off_topic(q) or ({"label": out["topic"].strip()} if out["topic"].strip() else None)
            s.update(intent="off_topic", off_topic=(topic or {}).get("label"), info_text=profile.off_topic_text(topic),
                     suggestions=sug)
            return "responder"
        if intent == "forecast":
            s.update(intent="out_of_scope", info_text=profile.forecast_text(s["anchor"].isoformat()), suggestions=sug)
            return "responder"
        if intent in ("metric_definition", "schema_info", "data_quality"):
            if intent == "metric_definition":
                r = get_nlu().parse(out["standalone_question"], {})
                topic = r.metric_topic or next(iter(r.plan.get("metrics") or []), None)
                if not topic:
                    return None         # không nhận ra chỉ số: để Planner tra semantic layer và trả lời
                s["metric_topic"] = topic
            s["intent"] = {"metric_definition": "metric_info", "schema_info": "schema_info", "data_quality": "dq"}[intent]
            return "responder"
        # câu GỐC có thuật ngữ mơ hồ mà người dùng chưa chọn định nghĩa: hỏi lại bằng code, không để câu viết lại của
        # Router (mô hình có thể tự chọn "cước ghi nhận" thay người dùng) lách qua bước hỏi lại
        amb = get_nlu().detect_ambiguity(q, mem)
        tasks = out["tasks"]
        first = tasks[0]
        multi = len(tasks) > 1 and s["settings"].get("multi_task", True)
        # câu nhiều ý: ý nào còn thuật ngữ mơ hồ thì ý đó tự hỏi lại; chỉ khi Router đã "chọn hộ" định nghĩa (câu gốc mơ hồ
        # mà không ý nào còn mơ hồ) mới hỏi lại cả câu
        if amb and not (multi and any(get_nlu().detect_ambiguity(x, mem) for x in tasks)):
            s["clarification"] = dict(amb, question_text=amb["question"])
            s["_detail"] += f"; câu gốc có thuật ngữ mơ hồ ({amb['id']}): hỏi lại"
            return "clarify"
        # data / variance / lookup: tiếp tục với câu hỏi độc lập (mọi ý chạy song song nếu câu có nhiều ý)
        if multi:
            s["multi_tasks"] = tasks[:MAX_TASKS]
            s["question_display"] = s.get("question_display") or q
            if len(tasks) > MAX_TASKS:
                s["pending_tasks"] = tasks[MAX_TASKS:]
            s["_detail"] += f" → trả lời song song {min(len(tasks), MAX_TASKS)} ý"
            return "multi"
        if len(tasks) > 1:
            s["pending_tasks"] = tasks[1:]
            s["assumptions"].append(f"Câu hỏi có {len(tasks)} ý. Mình trả lời ý thứ nhất trước; các ý còn lại nằm trong phần "
                                    "“Hỏi tiếp” bên dưới để bạn bấm xem tiếp.")
        if norm(first) != norm(q):
            s["question_display"] = s.get("question_display") or q
            s["question"] = first
            s["assumptions"].append(f"Mình hiểu câu hỏi là: “{first}”.")
        s["breakdown_only"] = _breakdown_only(q)
        s["route_why"] = intent == "variance" and not s["breakdown_only"]
        return None

    @staticmethod
    def _add_usage(s, client):
        u = s.setdefault("llm_usage", {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "calls": 0})
        for k, v in (getattr(client, "usage", None) or {}).items():
            u[k] = u.get(k, 0) + (v or 0)

    def _no_llm(self, s):
        s["intent"] = "llm_unavailable"
        s["info_text"] = ("Mình chưa hiểu trọn câu này bằng bộ luật nhanh, và hệ thống chưa được cấu hình mô hình ngôn ngữ để "
                          "hiểu câu hỏi tự do (quản trị viên cần đặt OPENROUTER_API_KEY). Bạn thử nêu rõ chỉ số và thời "
                          "gian, ví dụ một trong các câu bên dưới.")
        s["suggestions"] = profile.sample_questions()[:4]
        s["_detail"] = "Không có mô hình ngôn ngữ: chỉ trả lời được câu bộ luật hiểu trọn vẹn"
        return "responder"

    def _llm_failed(self, s, e):
        name = llm.MODE_LABEL.get(s["mode"], s["mode"])
        s["status"] = "error"
        s["llm_error"] = f"{name}: {e}"
        s["error"] = (f"Mình tạm thời không gọi được mô hình ngôn ngữ ({name}) nên chưa trả lời được câu này. "
                      f"Bạn thử lại sau ít phút. Chi tiết: {str(e)[:200]}")
        s["_detail"] = f"Lỗi mô hình ngôn ngữ: {str(e)[:160]}"
        return "finalize"

    # ------------------------------------------------------------------ chốt chặn bằng code trước khi hiểu câu hỏi
    def _code_guards(self, s, q):
        """Những câu hệ thống phải trả lời giống nhau dù có LLM hay không: lệnh sửa/xóa dữ liệu, hỏi dữ liệu cá nhân/mật
        mà vai trò không được xem, hỏi số liệu tương lai, mốc thời gian không tồn tại, mã đối tượng không có trong kho,
        và câu tra cứu danh mục ("thông tin về SP0010")."""
        info = lambda intent, text, detail, refused=False: (
            s.update(intent=intent, info_text=text, suggestions=profile.sample_questions()[:4], _detail=detail,
                     refused=refused) or "responder")
        if profile.write_request(q):
            return info("refused", profile.write_refusal_text(), "Từ chối: yêu cầu sửa/xóa dữ liệu (trợ lý chỉ đọc)", True)
        hidden = restricted_request(q, s["user"])
        if hidden:
            return info("refused",
                        f"Tài khoản của bạn không được xem **{', '.join(hidden)}** vì đây là dữ liệu cá nhân hoặc dữ liệu mật "
                        "theo phân quyền của công ty. Mình vẫn giúp được với số liệu tổng hợp, hoặc tra cứu theo mã mà "
                        "không kèm các thông tin này.",
                        f"Từ chối: câu hỏi cần cột bị hạn chế với vai trò {s['user'].role} ({', '.join(hidden)})", True)
        r = get_nlu().parse(q, s["memory"])
        if profile.forecast_request(q):
            m = next((x for x in r.plan.get("metrics") or [] if x in get_semantic().metrics), None)
            sug = f"{get_semantic().metrics[m]['label']} theo tháng năm {s['anchor'].year}" if m else None
            nxt = info("out_of_scope", profile.forecast_text(s["anchor"].isoformat(), sug),
                       "Ngoài phạm vi: hỏi số liệu tương lai (dự báo)")
            if sug:
                s["suggestions"] = [sug] + s["suggestions"][:3]
            return nxt
        bad = r.matched.get("invalid_time")
        if bad:
            shown = re.sub(r"\bthang\b", "tháng", re.sub(r"\bquy\b", "quý", re.sub(r"\bnam\b", "năm", re.sub(r"\bngay\b", "ngày", bad[0]))))
            return info("invalid_time", f"Mốc thời gian “{shown}” không tồn tại (tháng từ 1 đến 12, quý từ 1 đến 4, ngày phải có "
                        "thật). Bạn kiểm tra lại giúp mình, ví dụ “tháng 10/2026” hoặc “quý 3 năm 2026”.",
                        f"Mốc thời gian không hợp lệ: {bad}")
        lk = lookup.get_lookups()
        if r.plan.get("metrics"):
            _, unknown = lk.find_codes(q)
            unknown = [c for c in unknown if c.lower() not in get_nlu().vocab]
            if unknown:
                return info("not_found", f"Không tìm thấy mã **{', '.join(unknown)}** trong dữ liệu, nên mình chưa tính số liệu "
                            "cho mã này. Bạn kiểm tra lại mã, hoặc hỏi theo tên (ví dụ tên cửa hàng, tên sản phẩm).",
                            f"Mã không có trong danh mục: {unknown}")
        spec = lk.detect(q, r)
        if spec:
            return self._start_lookup(s, spec)
        return None

    def _start_lookup(self, s, spec):
        lk = lookup.get_lookups()
        e = lk.entities[spec["entity"]]
        s["intent"] = "lookup"
        if spec["mode"] == "missing":
            s["info_text"] = (f"Không tìm thấy {e.label} có mã **{', '.join(spec['codes'])}** trong dữ liệu. "
                              "Bạn kiểm tra lại mã, hoặc tìm theo tên.")
            s["_detail"] = f"Tra cứu {e.label}: mã không có trong danh mục"
            return "responder"
        built = lk.build(spec, s["user"].role_cfg)
        s["lookup"], s["lookup_built"] = spec, built
        s["sql"], s["sql_source"], s["columns_meta"] = built["sql"], "lookup", None
        s["_detail"] = f"Tra cứu danh mục {e.label} ({spec['mode']}) trên {e.table}"
        return "validator"

    def _plan_with_claude(self, s):
        if not s.get("critic_rounds"):      # để Critic làm lại kế hoạch mà không giữ giả định của lần lập trước
            s["_assumptions_before_plan"], s["_notes_before_plan"] = list(s["assumptions"]), list(s["notes"])
        why = (is_why_question(s["question"]) or bool(s.get("route_why"))) and not s.get("breakdown_only")
        spec = s.pop("_spec", None)
        out = client = None
        # Planner đã chạy đón trước song song với Router trên đúng câu này, cùng ngữ cảnh -> dùng luôn, đỡ ~1,5 s chờ
        if spec and self._same_question(s, spec[0]) and not s.get("clarified") and not s.get("critic_feedback") \
                and why == is_why_question(spec[0]):
            try:
                out, client, trace = spec[1].result(timeout=60)
                s["llm_trace"] += trace
                s["speculative"] = True
            except Exception:
                out = None          # chạy đón trước lỗi: gọi lại bình thường (lỗi thật sẽ lộ ra ở lần gọi này)
        if out is None:
            out, client, trace = self._call_planner(s)
            s["llm_trace"] += trace
        return self._use_plan(s, out, client, why)

    @staticmethod
    def _same_question(s, original: str) -> bool:
        """Câu Router viết lại vẫn cùng nội dung với câu gốc (chỉ thêm “là bao nhiêu?”, sửa dấu câu...): không phải câu nối
        tiếp và bộ luật đọc ra cùng chỉ số, bộ lọc, kỳ, cách chia, top N. Sửa chính tả làm đổi bộ lọc thì khác."""
        q = s["question"]
        if norm(q) == norm(original):
            return True
        if (s.get("route") or {}).get("is_follow_up"):
            return False
        nlu, prefs = get_nlu(), {"prefs": s["memory"].get("prefs") or {}}
        keys = ("metrics", "dimensions", "filters", "time_range", "compare", "limit", "order_by", "having", "per")
        a, b = nlu.parse(original, prefs), nlu.parse(q, prefs)
        return a.intent == b.intent and all(a.plan.get(k) == b.plan.get(k) for k in keys) and bool(a.plan.get("metrics"))

    def _call_planner(self, s):
        """Dựng ngữ cảnh và gọi Planner. Chỉ ĐỌC trạng thái, nên chạy được ở luồng nền (chạy đón trước song song Router)."""
        user = s["user"]
        trace = []
        ret = get_retriever()
        shots = ret.search_fewshots(s["question"], k=int(s["settings"].get("fewshot_k", 4)))
        fs_txt = "\n".join(f"- Q: {f['question']}\n  SQL: {re.sub(r'\s+', ' ', f['sql'])}" for f in shots) or "(chưa có)"
        mem = {k: s["memory"].get(k) for k in ("prefs", "last_plan", "last_question", "last_sql") if s["memory"].get(k)}
        hints = []
        why = (is_why_question(s["question"]) or bool(s.get("route_why"))) and not s.get("breakdown_only")
        if s.get("clarified"):
            c = s["clarified"]
            hints.append(f"Người dùng vừa làm rõ câu hỏi bằng lựa chọn: “{c['label']}”"
                         + (f" (chỉ số {c['metric']})" if c.get("metric") else "") + ". Dùng đúng lựa chọn này, không hỏi lại.")
        fb = s.get("critic_feedback")
        if fb:
            hints.append(f"Lần trước bạn nộp {fb['plan']}. Tác tử kiểm tra đánh giá là CHƯA trả lời đúng câu hỏi: "
                         f"{fb['problem']} Hướng sửa: {fb['fix'] or 'lập lại kế hoạch cho đúng ý câu hỏi'}. Hãy nộp kế hoạch "
                         "đã sửa (nếu bạn chắc chắn kế hoạch trước đã đúng thì nộp lại y như cũ).")
        if why:
            hints.append("Đây là câu hỏi tìm nguyên nhân biến động: nộp intent=variance, kind=semantic với semantic_plan gồm "
                         "metrics (một chỉ số), filters, time_range (kỳ đang hỏi) và compare (kỳ so sánh; nếu người dùng "
                         "không nêu thì dùng kỳ liền trước có cùng độ dài).")
        draft = None if why else get_nlu().draft_for_llm(s["question"], s["memory"])
        if draft:       # bộ luật đề xuất, LLM kiểm tra: câu hỏi thông thường đi đúng định nghĩa chỉ số, đỡ lượt gọi
            plan_txt = json.dumps(draft["plan"], ensure_ascii=False)
            if draft["unparsed"]:
                hints.append(
                    f"Bộ phân tích bằng luật chỉ hiểu MỘT PHẦN câu hỏi. Phần nó hiểu được: {plan_txt}. "
                    f"Những từ nó chưa hiểu: {', '.join(draft['unparsed'])}. Trước khi dùng plan này hãy xét các từ chưa hiểu: "
                    "nếu chúng nói tới một đối tượng người dùng muốn xem từng dòng (như “nhân viên” trong “top 5 nhân viên”), "
                    "một điều kiện lọc, hay một phép tính (trung bình theo nhóm, tỷ trọng, theo thứ trong tuần) mà danh sách "
                    "dimension không có, thì plan này KHÔNG trả lời đúng câu hỏi: hãy viết SQL, chỉ tái dùng công thức và bộ "
                    "lọc của chỉ số. Nếu chúng chỉ là từ đệm thì biên dịch rồi nộp plan (kind=semantic).")
            else:
                hints.append(
                    f"Bộ phân tích bằng luật đã dùng hết các từ có nghĩa trong câu hỏi và đề xuất plan: {plan_txt}. "
                    "Biên dịch rồi nộp đúng plan đó (kind=semantic), trừ khi bạn thấy nó sai ý câu hỏi.")
        # schema linking: mô tả cột của vài bảng liên quan nhất, tra sẵn bằng code -> mô hình viết SQL không cần tra bảng
        cat = get_catalog()
        linked = [t for t in ret.search_schema(s["question"], k=3)["tables"]
                  if cat.get(t) and cat.get(t)["schema"] in user.role_cfg["schemas"]][:3]
        ctx = {"anchor": s["anchor"].isoformat(), "role": user.role, "rls": user.rls, "memory": mem, "fewshots": fs_txt,
               "hints": hints, "schema": cat.schema_text(linked, user.role_cfg) if linked else ""}
        client = llm.get_client(s["settings"], s["mode"])
        tools = self._tool_impls(user, draft)
        out = client.plan(s["question"], ctx, tools, trace=trace, check=self._submit_check(tools, trace))
        return out, client, trace

    def _use_plan(self, s, out, client, why):
        self._add_usage(s, client)
        s["claude"] = client
        s["assumptions"] += out.get("assumptions") or []
        s["chart_hint"] = out.get("chart_hint") if out.get("chart_hint") not in (None, "auto", "table") else None
        s["intent"] = out.get("intent") or "metric_query"
        kind = out.get("kind")
        s["_detail"] = f"{llm.MODE_LABEL[s['mode']]} ({client.model}, effort={client.effort}): kind={kind}, intent={s['intent']}, " \
                       f"{len(s['llm_trace'])} lần gọi tool, {client.usage['calls']} lượt gọi API" \
                       + (" (chạy đón trước song song với Router)" if s.get("speculative") else "")
        # Phân tích nguyên nhân chỉ dành cho câu hỏi "vì sao" (nhận diện bằng code), không theo nhãn intent của mô hình:
        # - câu "vì sao" mà mô hình không lập được plan phân tích (viết truy vấn so sánh, hoặc hỏi lại dù các thuật ngữ mơ hồ
        #   đã được code kiểm ở bước trước) -> dùng plan của bộ luật nếu bộ luật hiểu trọn câu hỏi;
        # - câu so sánh thường ("so với cùng kỳ") mà mô hình gắn nhãn variance -> trả về bảng so sánh như người dùng hỏi.
        if why and not (s["intent"] == "variance" and kind != "clarify" and (out.get("semantic_plan") or {}).get("metrics")):
            r = get_nlu().parse(s["question"], s["memory"])
            if r.intent == "variance" and r.plan.get("metrics") and not r.clarification:
                s["intent"], s["plan"] = "variance", r.plan
                s["assumptions"] += r.assumptions
                s["notes"].append("Mô hình không lập kế hoạch phân tích nguyên nhân; dùng kế hoạch của bộ phân tích bằng luật.")
                return "variance_analyst"
        if not why and s["intent"] == "variance":
            s["intent"] = "metric_query"
        if kind == "clarify" and out.get("clarification"):
            c = out["clarification"]
            s["clarification"] = {"id": "llm_" + uuid.uuid4().hex[:6], "question": s["question"], "question_text": c.get("question"),
                                  "pref_key": get_nlu().pref_key_for([o.get("metric") for o in c.get("options", [])]),
                                  "options": c.get("options", [])}
            return "clarify"
        if kind == "answer" or s["intent"] in ("help", "schema_info", "dq") and not out.get("sql") and not out.get("semantic_plan"):
            # lời giới thiệu luôn do code dựng từ hồ sơ doanh nghiệp, không dùng văn bản của LLM
            s["info_text"] = None if s["intent"] == "help" else out.get("answer_text")
            return "responder"
        if s["intent"] == "variance" and out.get("semantic_plan"):
            p = out["semantic_plan"]
            p["variance_compare"] = p.pop("compare", None)
            if not p.get("variance_compare") and p.get("time_range"):
                from .nlu import previous_period
                st, en = dt.date.fromisoformat(p["time_range"]["start"]), dt.date.fromisoformat(p["time_range"]["end"])
                ps, pe, lab = previous_period(st, en)
                p["variance_compare"] = {"start": ps.isoformat(), "end": pe.isoformat(), "label": lab}
            s["plan"] = p
            return "variance_analyst"
        if kind == "sql" and out.get("sql"):
            s["sql"], s["sql_source"] = out["sql"], "llm"
            if s["settings"].get("critic", True):
                s["critic_issues"] = critic.check_sql(s["question"], out["sql"], s["memory"].get("prefs"))
            return "optimizer"
        if out.get("semantic_plan"):
            s["plan"] = out["semantic_plan"]
            return self._coverage_check(s, s["plan"]) or "sql_generator"
        s["info_text"] = out.get("answer_text") or "Mình chưa hiểu câu hỏi, bạn có thể diễn đạt lại?"
        s["intent"] = "unknown"
        return "responder"

    @staticmethod
    def _submit_check(tools: dict, trace: list):
        """Kiểm tra bài Planner nộp ngay lúc nộp (biên dịch semantic plan / chạy thử SQL). Có lỗi thì trả nội dung lỗi để
        mô hình sửa trong cùng hội thoại, thay vì mất một lượt gọi riêng để "biên dịch thử" trước khi nộp."""
        def check(ans: dict) -> str | None:
            kind, t0 = ans.get("kind"), time.time()
            try:
                if kind == "semantic" and ans.get("intent") != "variance" and ans.get("semantic_plan"):
                    r = tools["compile_semantic_plan"](plan=ans["semantic_plan"])
                    bad = r["errors"] or r.get("warnings")
                elif kind == "sql" and ans.get("sql"):
                    r = tools["dry_run_sql"](ans["sql"])
                    bad = r["errors"] or [x for x in r.get("lint") or [] if x["severity"] == "error"]
                else:
                    return None
            except Exception as e:      # lỗi biên dịch (vd tên chỉ số/chiều sai): mô hình sửa được
                r, bad = {"error": str(e)}, True
            trace.append({"tool": f"submit_check:{kind}", "input": {}, "ms": int((time.time() - t0) * 1000), "error": bool(bad)})
            if not bad:
                return None
            return ("Bài nộp CHƯA được nhận vì kiểm tra tự động báo lỗi. Sửa rồi gọi lại submit_answer:\n"
                    + json.dumps({k: r.get(k) for k in ("error", "errors", "warnings", "lint") if r.get(k)},
                                 ensure_ascii=False, default=str)[:4000])
        return check

    def _coverage_check(self, s, plan):
        """Kiểm tra bằng code sau Planner: semantic plan không được âm thầm bỏ qua một phần câu hỏi rồi trả số liệu chung.
        - mã đối tượng không phải chiều phân tích (vd mã khách hàng ở kho không có chiều khách hàng): plan không lọc được;
        - từ lạ gần giống một giá trị trong kho ("Aple" ~ "Apple") mà plan không lọc theo: hỏi lại;
        - từ lạ còn lại mà plan không phản ánh: nói rõ là kết quả CHƯA lọc theo phần đó."""
        q, nlu, lk = s["question"], get_nlu(), lookup.get_lookups()
        vals = norm(" ".join(str(v) for f in plan.get("filters") or [] for v in f.get("values") or []))
        hits, _ = lk.find_codes(q)
        nofilter = [(e, k) for e, k, _ in hits if not e.dimension and norm(k) not in vals]
        if nofilter:
            e, k = nofilter[0]
            s["intent"], s["info_text"] = "unsupported", (
                f"Mình chưa tính được số liệu riêng cho từng {e.label} (mã {k}) theo danh mục chỉ số, nên không trả con số "
                f"chung để tránh hiểu nhầm. Bạn có thể xem thông tin {e.label} này (hỏi “thông tin {k}”).")
            s["_detail"] = f"Kiểm tra độ bao phủ: plan không lọc được theo {e.label} {k}"
            return "responder"
        if (s.get("clarified") or {}).get("typo"):
            return None
        r = nlu.parse(q, {"prefs": s["memory"].get("prefs") or {}})
        unknown, sugg = nlu.unknown_terms(r)
        unknown = [w for w in unknown if norm(w) not in vals]
        if sugg and norm(str(sugg["value"])) not in vals:
            dim_label = get_semantic().dims[sugg["dimension"]]["label"] if sugg["dimension"] in get_semantic().dims else ""
            words = r"\s+".join(map(re.escape, sugg["text"].split()))
            fixed = re.sub(words, str(sugg["value"]), q, count=1)
            dropped = re.sub(r"\s{2,}", " ", re.sub(words, " ", q, count=1)).strip()
            s["clarification"] = {
                "id": "typo_" + uuid.uuid4().hex[:6], "pref_key": None,
                "question": q, "question_text": f"Mình không thấy “{sugg['text']}” trong dữ liệu. Có phải ý bạn là "
                                                f"**{sugg['value']}**" + (f" ({dim_label.lower()})" if dim_label else "") + "?",
                "options": [{"label": f"Đúng, {sugg['value']}", "rewrite": fixed, "typo": True},
                            {"label": f"Không, bỏ qua “{sugg['text']}” và tính chung", "rewrite": dropped, "typo": True}]}
            s["_detail"] = f"Kiểm tra độ bao phủ: từ lạ “{sugg['text']}” gần giống “{sugg['value']}”, plan không lọc theo -> hỏi lại"
            return "clarify"
        if unknown:
            s["assumptions"].append(f"Mình chưa hiểu “{' '.join(unknown)}” nên kết quả CHƯA lọc theo phần này — đây là số liệu "
                                    "chung. Nếu đó là tên sản phẩm/cửa hàng, hãy kiểm tra lại chính tả hoặc hỏi “danh sách sản phẩm”.")
        # Critic lớp 1 (code): kế hoạch không được bỏ sót phần người dùng nêu rõ; vá thẳng và báo cho người dùng
        if s["settings"].get("critic", True):
            c = critic.check_plan(q, plan, s["memory"].get("prefs"))
            if c["fixes"]:
                s["plan"] = c["plan"]
                s["assumptions"].append("Tác tử kiểm tra đã sửa kế hoạch cho khớp câu hỏi: " + "; ".join(c["fixes"]) + ".")
            s["critic_issues"] = c["issues"]
            log = s.setdefault("critic", {"fixes": [], "issues": [], "verdict": None})
            log["fixes"] += c["fixes"]
            log["issues"] += c["issues"]
        return None

    def _tool_impls(self, user: UserCtx, draft: dict | None = None) -> dict:
        """Các tool cho LLM. draft = bản nháp plan của bộ luật (nếu có), dùng để cảnh báo khi plan bỏ sót bộ lọc."""
        cat, ret, sem = get_catalog(), get_retriever(), get_semantic()
        role = user.role_cfg

        def search_schema(query: str):
            r = ret.search_schema(query)
            tables = [t for t in r["tables"] if cat.get(t) and cat.get(t)["schema"] in role["schemas"]]
            return {"schema": cat.schema_text(tables[:6], role),
                    "metrics": [sem.describe_metric(m) for m in r["metrics"][:5]]}

        def describe_table(table: str):
            fq = cat.resolve(table)
            if not fq or cat.get(fq)["schema"] not in role["schemas"]:
                raise ValueError(f"Bảng {table} không tồn tại hoặc không được phép.")
            return cat.describe(fq, role, max_values=25)

        def get_column_values(table: str = "", column: str = "", contains: str | None = None):
            fq = cat.resolve(table or "")
            if column in sem.dims and not (fq and column in cat.get(fq)["columns"]):
                loc = sem.dim_column(column)        # mô hình hay truyền tên chiều phân tích thay cho tên cột
                if not loc:
                    raise ValueError(f"'{column}' là chiều thời gian hoặc biểu thức, không có danh mục giá trị; "
                                     "khoảng thời gian đặt ở time_range.")
                fq, column = loc
            if not fq or cat.get(fq)["schema"] not in role["schemas"]:
                raise ValueError(f"Bảng '{table}' không tồn tại hoặc không được phép. Tìm bảng bằng search_schema.")
            c = cat.get(fq)["columns"].get(column)
            if not c:
                raise ValueError(f"Cột '{column}' không có trong {fq}. Các cột của bảng: {', '.join(cat.get(fq)['columns'])}. "
                                 "Cũng có thể truyền tên một chiều phân tích của semantic layer vào column.")
            if c.get("sensitivity") in role.get("deny_sensitivity", []):
                raise ValueError("Cột bị hạn chế với vai trò hiện tại.")
            vals = c.get("values")
            if vals:
                if contains:
                    vals = [v for v in vals if norm(contains) in norm(str(v))]
                return {"values": vals[:60]}
            sql = f"SELECT DISTINCT {column} FROM {fq}" + (f" WHERE {column} ILIKE '%{contains.replace(chr(39), '')}%'" if contains else "") + " LIMIT 40"
            v = validate(sql, user)
            if not v.ok:
                raise ValueError(v.errors[0]["message"])
            return {"values": [r[0] for r in get_warehouse().execute(v.sql_exec)["rows"]]}

        def dry_run_sql(sql: str):
            sql2, notes = repair.optimize_partition_filters(sql)
            v = validate(sql2, user)
            out = {"ok": v.ok, "errors": v.errors, "optimizations": notes}
            if v.ok:
                est = cost.estimate(v.qualified, v.rls_applied)
                out["estimated_bytes"] = est["human"]
                out["decision"] = cost.decide(est, role)["decision"]
                out["lint"] = sqlcheck.lint(v, dq.known_issues())
                out["rls_applied"] = v.rls_applied
                must_fix = [l["code"] for l in out["lint"] if l["severity"] == "error"]
                out["next"] = (f"Sửa các mục lint mức error ({', '.join(must_fix)}) rồi dry_run lại." if must_fix else
                               "SQL hợp lệ. Lint mức warning/info chỉ để tham khảo, không cần sửa hay chạy lại. "
                               "Nộp ngay bằng submit_answer (kind=sql).")
            else:
                out["next"] = "Sửa lỗi rồi gọi lại dry_run_sql."
            return out

        def compile_semantic_plan(plan: dict | None = None, **fields):
            # mô hình nhỏ hay gửi thẳng {"metrics": [...], ...} thay vì bọc trong {"plan": {...}} -> nhận cả hai dạng
            plan = plan if isinstance(plan, dict) else fields
            c = sem.compile(plan)
            v = validate(c["sql"], user)
            keep = {k: x for k, x in c["plan"].items() if x not in (None, [], {})}
            out = {"ok": v.ok, "errors": v.errors, "notes": c["notes"], "plan": keep,
                   "next": ("Plan hợp lệ. Nộp ngay bằng submit_answer với kind=semantic và semantic_plan là đúng plan này; "
                            "không cần dry_run_sql.") if v.ok else "Sửa plan theo lỗi rồi biên dịch lại."}
            # câu hỏi có nhắc đúng tên một giá trị trong kho mà plan không lọc theo nó -> nhắc mô hình xem lại
            used = {f["dimension"] for f in c["plan"].get("filters") or []} | set(c["plan"].get("dimensions") or [])
            dropped = [f for f in ((draft or {}).get("plan") or {}).get("filters") or [] if f["dimension"] not in used]
            warnings = [f"Câu hỏi có nhắc “{', '.join(map(str, f['values']))}” ({sem.dims[f['dimension']]['label']}) "
                        "nhưng plan không lọc theo giá trị này. Nếu người dùng muốn giới hạn theo giá trị đó thì thêm filter."
                        for f in dropped]
            # xếp hạng (limit/order_by) mà không nhóm theo chiều nào thì kết quả chỉ có một dòng tổng: vô nghĩa
            if (c["plan"].get("limit") or c["plan"].get("order_by")) and not c["plan"].get("dimensions"):
                warnings.append("Plan có limit/order_by nhưng không có dimension nào nên chỉ trả về MỘT dòng tổng, không phải bảng "
                                "xếp hạng. Nếu đối tượng cần xếp hạng không nằm trong danh sách dimension của semantic layer thì "
                                "phải viết SQL: GROUP BY mã của đối tượng đó, tái dùng đúng công thức và bộ lọc của chỉ số.")
            if v.ok and warnings:
                out["warnings"] = warnings
                out["next"] = "Plan biên dịch được nhưng CHƯA nên nộp: xử lý các cảnh báo trước (sửa plan hoặc chuyển sang SQL)."
            return out

        return {"search_schema": search_schema, "describe_table": describe_table, "get_column_values": get_column_values,
                "dry_run_sql": dry_run_sql, "compile_semantic_plan": compile_semantic_plan}

    def n_multi(self, s):
        """Câu nhiều ý: mỗi ý chạy song song như một câu hỏi độc lập (đường nhanh hoặc Planner, đủ các chốt kiểm tra,
        Critic), rồi gộp thành một câu trả lời nhiều phần. Thời gian chờ bằng ý chậm nhất, không phải tổng các ý."""
        tasks = s["multi_tasks"]

        def run(task):
            sub = self.new_state(s["user"], s["conversation_id"], task, eval_mode=s["eval_mode"], mode=s.get("force_mode"))
            sub["subtask"], sub["t0"] = True, s["t0"]      # chung ngân sách thời gian với câu cha
            self.graph.run(sub, "load_context")
            return sub
        subs = list(MULTI_POOL.map(run, tasks))
        parts = [x["response"] for x in subs]
        for sub in subs:
            for k, v in (sub.get("llm_usage") or {}).items():
                u = s.setdefault("llm_usage", {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "calls": 0})
                u[k] = u.get(k, 0) + v
        s["parts"] = parts
        ok = [x for x in subs if x["status"] == "success"]
        main = ok[0] if ok else subs[0]
        # bộ nhớ hội thoại theo ý trả lời được đầu tiên (câu hỏi tiếp “còn tháng trước thì sao” bám theo ý đó)
        for k in ("plan", "sql", "intent", "mode"):
            s[k] = main.get(k)
        s["status"] = "success" if ok else main["status"]
        n_ok = len(ok)
        s["text"] = (f"Bạn hỏi {len(tasks)} ý — mình trả lời từng ý bên dưới"
                     + ("." if n_ok == len(tasks) else f" ({n_ok}/{len(tasks)} ý có số liệu, các ý còn lại cần bạn xem lại)."))
        s["_detail"] = f"{len(tasks)} ý chạy song song: " + ", ".join(f"{p['type']} ({p['elapsed_ms']} ms)" for p in parts)
        return "finalize"

    def n_clarify(self, s):
        c = s["clarification"]
        s["memory"]["pending_clarification"] = {"id": c["id"], "question": s["question"], "pref_key": c.get("pref_key"),
                                                "options": c["options"]}
        s["status"] = "clarification"
        s["_detail"] = f"Hỏi lại: {c.get('question_text')}"
        return "finalize"

    def n_responder(self, s):
        sem, cat = get_semantic(), get_catalog()
        intent = s["intent"]
        s["status"] = "info"
        if s.get("info_text"):
            s["text"] = s["info_text"]
            return "finalize"
        if intent == "metric_info":
            topic = s.get("metric_topic")
            names = [topic] if topic in sem.metrics else get_nlu().ambiguity_metrics(topic)
            cards = [sem.describe_metric(n) for n in names if n in sem.metrics]
            s["metric_cards"] = cards
            parts = []
            for c in cards:
                parts.append(f"**{c['label']}** (`{c['name']}`, chủ sở hữu: {c['owner']}): {c['description']} "
                             f"Công thức: `{c['sql']}`" + (f" với điều kiện `{c['filter']}`." if c.get("filter") else "."))
            s["text"] = "\n\n".join(parts) or "Chưa có định nghĩa cho chỉ số này trong semantic layer."
        elif intent == "schema_info":
            r = get_retriever().search_schema(s["question"])
            tabs = [t for t in r["tables"] if cat.get(t)["schema"] in s["user"].role_cfg["schemas"]][:5]
            s["schema_tables"] = [cat.describe(t, s["user"].role_cfg) for t in tabs]
            s["text"] = "Các bảng liên quan nhất: " + "; ".join(f"`{t}` — {cat.get(t)['description']}" for t in tabs)
        elif intent == "dq":
            rep = dq.latest_report()
            open_f = [f for f in rep["findings"] if f["status"] == "open" and f["severity"] in ("critical", "warning")]
            s["text"] = (f"Báo cáo chất lượng dữ liệu ({rep['generated_at']}): {rep['summary']['critical']} vấn đề nghiêm trọng, "
                         f"{rep['summary']['warning']} cảnh báo.\n\n" + "\n".join(f"- **{f['title']}**: {f['message']}" for f in open_f[:6]))
        elif intent == "help":
            s["text"] = profile.intro_text()
            s["suggestions"] = profile.sample_questions()[:6]
        else:
            labels = [m["label"].lower() for m in sem.metrics.values()]
            s["text"] = ("Mình chưa hiểu câu này muốn xem số liệu nào. Mình trả lời được các câu hỏi về: " +
                         ", ".join(labels[:12]) + ("…" if len(labels) > 12 else "") +
                         ". Bạn thử nêu rõ chỉ số và thời gian, hoặc bắt đầu từ một câu mẫu bên dưới.")
            s["suggestions"] = SAMPLE_QUESTIONS[:5]
        s["_detail"] = f"Trả lời thông tin ({intent})"
        return "finalize"

    def n_sql_generator(self, s):
        plan = s.get("plan") or {}
        tr = plan.get("time_range") or {}
        win = get_semantic().data_window(plan.get("metrics") or [])
        if win and tr.get("start") and tr.get("end") and (str(tr["end"]) <= win[0] or str(tr["start"]) > win[1]):
            d = lambda x: f"{x[8:10]}/{x[5:7]}/{x[:4]}"
            future = str(tr["start"]) > s["anchor"].isoformat()
            s["status"], s["intent"] = "info", "out_of_range"
            s["text"] = (f"Kho chỉ có số liệu này từ ngày {d(win[0])} đến ngày {d(win[1])}, nên không có số liệu cho "
                         f"{tr.get('label') or 'khoảng thời gian bạn hỏi'}."
                         + (" Mình không dự báo số liệu tương lai." if future else ""))
            s["suggestions"] = profile.sample_questions()[:4]
            s["_detail"] = f"Kỳ hỏi {tr.get('start')}..{tr.get('end')} nằm ngoài dữ liệu {win[0]}..{win[1]}"
            return "finalize"
        try:
            c = get_semantic().compile(s["plan"])
        except SemanticError as e:
            s["status"], s["error"] = "error", str(e)
            return "finalize"
        # giữ plan ĐÃ CHUẨN HÓA (vd order_by mô hình trả dạng object -> danh sách): các bước sau đọc plan theo đúng kiểu
        s["plan"] = c.get("plan") or s["plan"]
        s["sql"], s["sql_source"], s["columns_meta"] = c["sql"], "semantic", c["columns"]
        s["notes"] += c["notes"]
        s["_detail"] = f"Biên dịch semantic plan → SQL (models: {', '.join(c['models'])})"
        return "optimizer"

    def n_optimizer(self, s):
        if s["sql_source"] in ("llm", "fewshot"):
            sql2, notes = repair.optimize_partition_filters(s["sql"])
            if notes:
                s["sql"] = sql2
                s["notes"] += notes
                s["_detail"] = "; ".join(notes)
                return "validator"
        s["_detail"] = "Không cần tối ưu"
        return "validator"

    def n_validator(self, s):
        user = s["user"]
        v = validate(s["sql"], user)
        s["validation"] = v.to_dict()
        s["_v"] = v
        if not v.ok:
            e = v.errors[0]
            s["_detail"] = f"✗ {e['code']}: {e['message'][:160]}"
            if e["code"] in NON_REPAIRABLE:
                s["status"], s["error"] = "blocked", e["message"]
                return "finalize"
            s["last_error"] = e
            return "repairer"
        tr = (s.get("plan") or {}).get("time_range") or {}
        s["lint"] = sqlcheck.lint(v, dq.known_issues(), (tr.get("start"), tr.get("end")) if tr else None)
        s["metrics_matched"] = sqlcheck.matched_metrics(v)
        try:
            s["lineage"] = sqlcheck.query_lineage(v)
        except Exception:
            s["lineage"] = []
        errs = [l for l in s["lint"] if l["severity"] == "error"]
        s["_detail"] = f"✓ {len(v.checks)} kiểm tra đạt; lint: {len(s['lint'])} ({', '.join(l['code'] for l in s['lint']) or 'sạch'})" + \
                       (f"; RLS: {', '.join(v.rls_applied)}" if v.rls_applied else "")
        if errs and s["sql_source"] != "semantic":
            s["last_error"] = {"code": errs[0]["code"], "message": errs[0]["message"], "hint": errs[0]["hint"]}
            return "repairer"
        return "cost_guard"

    def n_repairer(self, s):
        e = s.get("last_error") or {}
        maxa = int(s["settings"].get("max_repair_attempts", 3))
        if s["attempts"] >= maxa:
            s["status"], s["error"] = "error", f"Không tự sửa được sau {s['attempts']} lần thử. Lỗi cuối: {e.get('message')}"
            return "finalize"
        s["attempts"] += 1
        before = s["sql"]
        tables = (s.get("validation") or {}).get("tables") or []
        new, how = repair.rule_repair(before, e, tables or profile.core_tables())
        agent = "rule"
        if new is None and s["mode"] in llm.LLM_MODES:
            try:
                client = s.get("claude") or llm.get_client(s["settings"], s["mode"])
                cat = get_catalog()
                schema_txt = cat.schema_text(tables or profile.core_tables(), s["user"].role_cfg)
                out = client.repair_sql(before, [e], schema_txt, s["question"])
                new, how, agent = out["sql"], out["explanation"], s["mode"]
            except Exception as ex:
                how = f"{llm.MODE_LABEL[s['mode']]} không sửa được: {ex}"
        s["repairs"].append({"attempt": s["attempts"], "error": e, "agent": agent, "fix": how, "sql_before": before, "sql_after": new})
        s["_detail"] = f"Lần {s['attempts']}: {e.get('code')} → {how}"
        if new is None or new.strip() == before.strip():
            s["status"], s["error"] = "error", f"{e.get('message')} — {how}" + (f" Gợi ý: {e['hint']}" if e.get("hint") else "")
            return "finalize"
        s["sql"] = new
        s["columns_meta"] = None if s["sql_source"] != "semantic" else s["columns_meta"]
        if s["sql_source"] == "semantic":
            s["sql_source"] = "semantic+repair"
        return "validator"

    def n_cost_guard(self, s):
        v = s["_v"]
        est = cost.estimate(v.qualified, v.rls_applied)
        dec = cost.decide(est, s["user"].role_cfg, bool(s["settings"].get("always_review")) and not s["eval_mode"])
        s["cost"] = dict(est, **dec)
        s["_detail"] = f"Ước tính quét {est['human']} → {dec['decision']}" + (f" ({'; '.join(dec['reasons'])})" if dec["reasons"] else "")
        if dec["decision"] == "blocked":
            s["status"], s["error"] = "blocked", " ".join(dec["reasons"])
            return "finalize"
        if dec["decision"] == "approval" and not s["approved"] and not s["eval_mode"]:
            s["status"] = "pending_approval"
            keep = {k: s.get(k) for k in ("question", "plan", "sql", "sql_source", "columns_meta", "intent", "assumptions",
                                          "notes", "chart_hint", "repairs", "attempts", "mode", "trace", "console",
                                          "lookup", "lookup_built")}
            self.store.save_pending(s["run_id"], s["conversation_id"], s["user"].username, copy.deepcopy(keep))
            return "finalize"
        return "executor"

    def n_executor(self, s):
        v = s["_v"]
        st = s["settings"]
        try:
            r = get_warehouse().execute(v.sql_exec, timeout_s=float(st.get("query_timeout_s", 20)),
                                        max_rows=int(s["user"].role_cfg.get("max_rows", 5000)))
        except QueryError as e:
            s["_detail"] = f"✗ {e.kind}: {str(e)[:160]}"
            if e.kind == "timeout":
                s["status"], s["error"] = "error", str(e)
                return "finalize"
            s["last_error"] = {"code": "BINDER", "message": str(e).split("\n")[0], "raw": str(e)}
            return "repairer"
        s["result"] = r
        s["executed"].append({"sql": v.sql_exec, "bytes": s["cost"]["total_bytes"], "rows": r["row_count"], "ms": r["elapsed_ms"]})
        s["_detail"] = f"✓ {r['row_count']} dòng trong {r['elapsed_ms']} ms" + (" (đã cắt bớt)" if r["truncated"] else "")
        return "analyst"

    def n_analyst(self, s):
        r = s["result"]
        if s.get("lookup"):
            out = lookup.get_lookups().render(s["lookup"], s["lookup_built"], r["columns"], r["rows"], s["user"])
            s["result"] = dict(r, columns=out["columns"], rows=out["rows"], row_count=len(out["rows"]))
            s["columns_meta"] = out["meta"]
            s["facts"], s["template"], s["text"], s["highlights"] = out["facts"], out["template"], out["text"], []
            s["chart"] = None
            s["notes"] += out["notes"]
            s["lookup_follow_ups"] = out["follow_ups"]
            s["_detail"] = f"Tra cứu danh mục: {len(out['rows'])} dòng hiển thị"
            return "explainer"
        meta = s.get("columns_meta")
        if not meta or [m["name"] for m in meta] != r["columns"]:
            meta = infer_meta(r["columns"], r["types"], r["rows"])
            s["columns_meta"] = meta
        if _is_listing(s.get("sql")):
            # liệt kê từng đối tượng (không SUM/COUNT, không GROUP BY): cột số là thuộc tính (giá vốn của từng sản phẩm),
            # không phải chỉ số kinh doanh -> không xếp hạng "dẫn đầu", không vẽ biểu đồ
            meta = [dict(m, kind="dimension") if m.get("kind") == "metric" else m for m in meta]
            s["columns_meta"] = meta
        e = explain.explain(meta, r["columns"], r["rows"], s.get("plan") or {}, r["truncated"])
        s["facts"], s["template"], s["text"], s["highlights"] = e["facts"], e["template"], e["text"], e["highlights"]
        s["chart"] = explain.recommend_chart(meta, r["columns"], r["rows"], s.get("chart_hint"), e["highlights"])
        s["_detail"] = f"{len(e['facts'])} dữ kiện; biểu đồ: {(s['chart'] or {}).get('type', 'không')}"
        if _no_numbers(meta, r):
            self._explain_empty(s)
        return "explainer"

    def _explain_empty(self, s):
        """Kết quả rỗng ("—"): nói rõ là không có số liệu và vì sao có thể như vậy, thay vì hiện một ô trống."""
        sem, plan = get_semantic(), s.get("plan") or {}
        facts = {}

        def ctx(fid, text):
            facts[fid] = {"id": fid, "value": text, "text": text, "recipe": {"op": "context"}, "kind": "context"}
            return text
        mets = [sem.metrics[m]["label"].lower() for m in plan.get("metrics") or [] if m in sem.metrics]
        conds = [ctx(f"c{i}", f"{sem.dims[f['dimension']]['label'].lower() if f['dimension'] in sem.dims else f['dimension']} "
                              f"{'khác ' if f.get('op') == 'not_in' else ''}{', '.join(map(str, f.get('values') or []))}")
                 for i, f in enumerate(plan.get("filters") or [])]
        tr = (plan.get("time_range") or {}).get("label")
        hv = [h for h in plan.get("having") or [] if h.get("metric") in sem.metrics]
        gdims = [d for d in plan.get("dimensions") or [] if d in sem.dims and sem.dims[d].get("kind") != "time"]
        if hv and gdims:    # có điều kiện lọc trên chỉ số: không đối tượng nào thỏa, khác với "không có số liệu"
            ops = {"<": "dưới", "<=": "không quá", ">": "trên", ">=": "từ", "=": "bằng"}
            cond = " và ".join(f"{sem.metrics[h['metric']]['label'].lower()} {ops.get(h.get('op'), h.get('op'))} "
                               f"{h['value']:g}{'%' if sem.metrics[h['metric']].get('format') == 'percent' else ''}" for h in hv)
            text = (f"Không có {sem.dims[gdims[0]]['label'].lower()} nào có {cond}" + (f" (lọc {'; '.join(conds)})" if conds else "")
                    + (f" trong {ctx('period', tr)}" if tr else "") + ".")
            s["text"] = s["template"] = text
            s["facts"], s["chart"], s["highlights"], s["empty_result"] = facts, None, [], True
            return
        text = "Không có số liệu" + (f" {', '.join(mets)}" if mets else "") +                (f" cho {'; '.join(conds)}" if conds else "") + (f" trong {ctx('period', tr)}" if tr else "") + "."
        if (s.get("validation") or {}).get("rls_applied"):
            text += (" Tài khoản của bạn chỉ được xem một phần dữ liệu (phân quyền theo dòng), nên điều kiện này có thể nằm "
                     "ngoài phạm vi bạn được xem.")
        elif conds:
            text += " Có thể đối tượng này không phát sinh giao dịch trong kỳ (ví dụ đã đóng cửa, ngừng kinh doanh hoặc chưa mở)."
        s["text"] = s["template"] = text
        s["facts"], s["chart"], s["highlights"], s["empty_result"] = facts, None, [], True

    def _start_judge(self, s):
        """Critic lớp 2: LLM chấm kết quả có trả lời đúng câu hỏi không. Chỉ cho câu do Planner (LLM) lập kế hoạch: câu đi
        đường nhanh do bộ luật hiểu trọn vẹn, không có gì để chấm. Chạy nền, song song với lượt viết lời."""
        st = s["settings"]
        if not (st.get("critic", True) and st.get("critic_llm", True)) or s["mode"] not in llm.LLM_MODES \
                or not s.get("claude") or s.get("lookup") or s.get("critic_rounds"):
            return
        # chỉ chấm khi còn kịp làm lại: hết thời gian thì kết quả chấm không đổi được gì, chỉ làm người dùng chờ thêm
        if st.get("critic_revise", True) and time.time() - s["t0"] > float(st.get("critic_revise_until_s", 6)):
            s.setdefault("critic", {"fixes": [], "issues": [], "verdict": None})["verdict"] = "skipped"
            return
        try:        # chấm là lớp kiểm tra phụ: hỏng thì bỏ qua, không làm hỏng câu trả lời đã kiểm chứng số liệu
            client = llm.get_client(st, s["mode"], role="router")
            semantic = (s.get("sql_source") or "").startswith("semantic")
            prompt = critic.judge_prompt(s["question"], s.get("question_display"), s.get("plan") if semantic else None,
                                         s.get("sql"), s["result"], s.get("assumptions") or [], s.get("critic_issues") or [])
            s["_judge"] = (client, CRITIC_POOL.submit(client.critique, prompt), time.time())
        except Exception as e:
            s.setdefault("critic", {"fixes": [], "issues": [], "verdict": None})["verdict"] = f"error: {type(e).__name__}"

    def n_explainer(self, s):
        r = s["result"]
        self._start_judge(s)
        # hỏi "theo miền" nhưng tài khoản chỉ xem được một miền: nói rõ vì sao bảng chia chỉ có một dòng
        if (s.get("validation") or {}).get("rls_applied") and len(r.get("rows") or []) == 1 \
                and (s.get("plan") or {}).get("dimensions"):
            s["notes"].append("Tài khoản của bạn chỉ được xem một phần dữ liệu (phân quyền theo dòng), nên bảng chỉ có phần "
                              "thuộc phạm vi đó.")
        # ngân sách thời gian: câu đã tốn gần hết ngân sách (thường do Planner) thì dùng bản diễn giải tự động, cũng đã
        # kiểm chứng số liệu, thay vì bắt người dùng chờ thêm một lượt gọi mô hình chỉ để câu văn mượt hơn
        budget = float(s["settings"].get("latency_budget_s", 10)) - NARRATE_RESERVE_S
        over = time.time() - s["t0"] > budget
        if over and s["mode"] in llm.LLM_MODES and s["facts"] and not s["eval_mode"]:
            s["notes"].append("Dùng bản diễn giải tự động để trả lời trong ngân sách thời gian.")
        single = len(r.get("rows") or []) <= 1      # một dòng kết quả: bản tự động đã đủ rõ, không cần thêm lượt gọi
        if s["mode"] in llm.LLM_MODES and s["facts"] and not s["eval_mode"] and not s.get("lookup") and not s.get("empty_result") \
                and not over and not single:
            try:
                client = llm.get_client(s["settings"], s["mode"], role="router")     # model nhanh
                txt = client.narrate(s["question"], s["facts"], s["template"])
                self._add_usage(s, client)
                if txt and not critic.answer_focus_ok(_render(txt, s["facts"]), s.get("plan")) \
                        and critic.answer_focus_ok(s["text"], s.get("plan")):
                    s["notes"].append("Lời diễn giải của LLM không mở đầu bằng chỉ số bạn hỏi — dùng bản diễn giải tự động.")
                elif txt:
                    s["text"] = _render(txt, s["facts"])
                    s["narrator"] = s["mode"]
                else:
                    s["notes"].append("Lời diễn giải của LLM chứa số không kiểm chứng được — dùng bản diễn giải tự động.")
            except Exception:
                pass
        s["audit"] = explain.audit(s["text"], s["facts"], r["columns"], r["rows"])
        if not s["audit"]["ok"]:
            s["text"] = _render(s["template"], s["facts"])
            s["audit"] = explain.audit(s["text"], s["facts"], r["columns"], r["rows"])
            s["notes"].append("Đã thay lời diễn giải bằng bản tự động vì phát hiện số liệu không khớp.")
        s["status"] = "success"
        a = s["audit"]
        s["_detail"] = f"Kiểm chứng số liệu: {a['facts_matched']}/{a['facts_checked']} dữ kiện khớp, {len(a['unverified_numbers'])} số lạ"
        return "critic"

    def n_critic(self, s):
        """Nhận kết quả LLM chấm. "wrong": nếu còn thời gian thì Planner lập lại kế hoạch MỘT lần kèm nhận xét, không thì
        giữ câu trả lời và cảnh báo rõ cho người dùng. LLM chấm lỗi/quá lâu thì bỏ qua (số liệu vẫn đã được code kiểm)."""
        log = s.setdefault("critic", {"fixes": [], "issues": [], "verdict": None})
        job = s.pop("_judge", None)
        if not job:
            s["_detail"] = "Không cần LLM chấm (" + ("đường nhanh: bộ luật hiểu trọn câu" if s.get("path") == "fast" else
                                                     "đã chấm ở lượt trước" if s.get("critic_rounds") else
                                                     "tắt hoặc không áp dụng") + ")"
            return "finalize"
        client, fut, t0 = job
        try:
            v = fut.result(timeout=CRITIC_WAIT_S)
        except Exception as e:
            log["verdict"] = "error"
            s["_detail"] = f"LLM chấm không phản hồi kịp ({type(e).__name__}): giữ câu trả lời đã kiểm chứng số liệu"
            return "finalize"
        self._add_usage(s, client)
        ms = int((time.time() - t0) * 1000)
        s["llm_trace"].append({"tool": "critic", "input": {"question": s["question"]}, "ms": ms, "error": False, "output": v})
        log.update(verdict=v["verdict"], problem=v["problem"], judge_model=client.model)
        if v["verdict"] == "ok":
            s["_detail"] = f"LLM chấm ({client.model}, {ms} ms): câu trả lời trúng câu hỏi"
            return "finalize"
        st = s["settings"]
        elapsed = time.time() - s["t0"]
        if st.get("critic_revise", True) and elapsed < float(st.get("critic_revise_until_s", 6)) + CRITIC_WAIT_S:
            keep = {k: (s[k] if k == "_v" else copy.deepcopy(s[k])) for k in PLAN_KEYS if k in s}
            semantic = (s.get("sql_source") or "").startswith("semantic")
            s["critic_rounds"] = 1
            s["critic_feedback"] = {"plan": critic.describe_plan(s.get("plan") if semantic else None, s.get("sql")),
                                    "problem": v["problem"], "fix": v["fix"]}
            base_a, base_n = s.get("_assumptions_before_plan") or [], s.get("_notes_before_plan") or []
            for k in PLAN_KEYS:
                s.pop(k, None)
            s.update(assumptions=list(base_a), notes=list(base_n), attempts=0, repairs=[], executed=[], status=None,
                     plan=None, sql=None, sql_source=None, columns_meta=None, intent=None, chart_hint=None)
            s["notes"].append(f"Tác tử kiểm tra thấy lần trả lời đầu chưa đúng ý ({v['problem']}) nên đã lập lại kế hoạch.")
            log["revised"] = True
            s["_detail"] = f"LLM chấm ({client.model}, {ms} ms): CHƯA đúng — {v['problem']} → Planner lập lại kế hoạch"
            try:
                return self._plan_with_claude(s)
            except Exception as e:      # làm lại thất bại: trả câu trả lời cũ kèm cảnh báo
                s.update(keep)
                s["notes"].append(f"⚠ Tác tử kiểm tra nghi câu trả lời chưa đúng ý: {v['problem']} "
                                  f"(chưa lập lại được: {str(e)[:80]}).")
                return "finalize"
        s["notes"].append(f"⚠ Tác tử kiểm tra nghi câu trả lời chưa đúng ý: {v['problem']} Bạn có thể hỏi lại cụ thể hơn.")
        log["warned"] = True
        s["_detail"] = f"LLM chấm ({client.model}, {ms} ms): CHƯA đúng — {v['problem']} (hết thời gian làm lại, cảnh báo)"
        return "finalize"

    def n_variance_analyst(self, s):
        plan = s["plan"]
        if not plan.get("metrics"):
            s["status"], s["error"] = "error", "Chưa xác định được chỉ số cần phân tích biến động."
            return "finalize"
        user = s["user"]
        role = user.role_cfg
        budget = {"bytes": 0}

        def run(sql, purpose):
            v = validate(sql, user)
            if not v.ok:
                raise ValueError(f"{purpose}: {v.errors[0]['message']}")
            est = cost.estimate(v.qualified, v.rls_applied)
            budget["bytes"] += est["total_bytes"]
            if budget["bytes"] > role["cost"]["max_bytes"]:
                raise ValueError("Phân tích vượt hạn mức chi phí quét của vai trò.")
            res = get_warehouse().execute(v.sql_exec, timeout_s=30, max_rows=20000)
            return {"columns": res["columns"], "rows": res["rows"], "sql": v.sql_exec, "bytes": est["total_bytes"], "ms": res["elapsed_ms"]}

        # giả thuyết nguyên nhân: mô hình nhanh đề xuất, insights tự chạy truy vấn kiểm chứng; phải xong trong ngân sách
        st, hyp_client, hypothesize, deadline = s["settings"], None, None, None
        if st.get("variance_hypotheses", True) and s["mode"] in llm.LLM_MODES:
            try:
                hyp_client = llm.get_client(st, s["mode"], role="router")
                hypothesize = hyp_client.hypothesize
                deadline = s["t0"] + float(st.get("latency_budget_s", 10)) - 0.5
            except Exception:
                hyp_client = hypothesize = None
        try:
            out = insights.analyze(plan, run, s["anchor"], hypothesize=hypothesize, deadline=deadline)
        except Exception as e:
            s["status"], s["error"] = "error", f"Không phân tích được biến động: {e}"
            return "finalize"
        s["variance"] = out
        if hyp_client is not None:
            self._add_usage(s, hyp_client)
        s["text"], s["facts"], s["audit"], s["chart"] = out["text"], out["facts"], out["audit"], out["chart"]
        s["sql"] = out["queries"][0]["sql"] if out["queries"] else None
        s["executed"] = [{"sql": q["sql"], "bytes": q["bytes"], "rows": q["rows"], "ms": q["ms"]} for q in out["queries"]]
        s["cost"] = {"total_bytes": budget["bytes"], "human": human_bytes(budget["bytes"]), "decision": "auto", "reasons": [],
                     "tables": [], "large_tables": []}
        s["status"] = "success"
        hyps = [h for h in out.get("hypotheses") or [] if h.get("status") != "error"]
        s["_detail"] = f"{len(out['queries'])} truy vấn phân tích; chiều giải thích tốt nhất: {out['best_dimension']}; " \
                       f"{len(out['root_causes'])} kiểm tra nguyên nhân; " \
                       f"{sum(h.get('status') == 'confirmed' for h in hyps)}/{len(hyps)} giả thuyết được xác nhận"
        return "finalize"

    def n_finalize(self, s):
        user = s["user"]
        resp = self._response(s)
        s["response"] = resp
        if s.get("subtask"):
            return self._finalize_subtask(s, resp)
        mem = s["memory"]
        if s["status"] == "success" and s.get("plan") and s["intent"] in ("metric_query", "variance"):
            mem["last_plan"] = s["plan"]
        if s["status"] in ("success", "pending_approval"):
            mem["last_sql"] = s.get("sql")
            mem["last_question"] = s["question"]
        hist = mem.setdefault("history", [])
        hist.append({"q": s.get("question_display") or s["question"], "intent": s.get("intent"), "status": s["status"],
                     "summary": (s.get("text") or s.get("error") or "")[:200]})
        mem["history"] = hist[-8:]
        self.store.update_conversation(s["conversation_id"], memory=mem)
        if s["eval_mode"]:
            return END
        conv = self.store.get_conversation(s["conversation_id"])
        if conv and conv["title"] == "Hội thoại mới" and not s.get("console"):
            self.store.update_conversation(s["conversation_id"], title=(s.get("question_display") or s["question"])[:60])
        if not s.get("_from_approval_msg"):
            self.store.add_message(s["conversation_id"], "user", {"text": s.get("question_display") or s["question"],
                                                                  "clarification": s.get("clarification_answer")})
        self.store.add_message(s["conversation_id"], "assistant", resp)
        if (s["status"] not in ("clarification", "info") or s.get("sql") or s.get("refused")) and not s.get("parts"):
            self.store.log_query(run_id=s["run_id"], username=user.username, role=user.role, conversation_id=s["conversation_id"],
                                 question=s.get("question_display") or s["question"], sql=s.get("sql"),
                                 status="refused" if s.get("refused") else s["status"],
                                 error=s.get("error"), est_bytes=(s.get("cost") or {}).get("total_bytes"),
                                 rows=(s.get("result") or {}).get("row_count") or (len(s.get("executed", [])) if s.get("variance") else None),
                                 latency_ms=int((time.time() - s["t0"]) * 1000), retries=s["attempts"], llm_mode=s["mode"],
                                 source=s.get("sql_source") or ("variance" if s.get("variance") else s.get("intent")),
                                 plan=s.get("plan"), lint=s.get("lint"), approved_by=s.get("approved_by"))
        return END

    def _finalize_subtask(self, s, resp):
        """Ý con của câu nhiều ý: không ghi bộ nhớ/tin nhắn (câu cha ghi một lần). Ý cần hỏi lại thì đưa các lựa chọn thành
        câu hỏi bấm được, vì trạng thái “đang chờ chọn” của hội thoại chỉ giữ được một câu hỏi lại."""
        if s["status"] == "clarification":
            c, sem = s["clarification"], get_semantic()
            opts = []
            for o in c.get("options") or []:
                if o.get("rewrite"):
                    opts.append(o["rewrite"])
                elif o.get("metric") in sem.metrics:
                    opts.append(f"{s['question'].rstrip(' ?')} (tính theo {sem.metrics[o['metric']]['label'].lower()})")
                elif o.get("label"):
                    opts.append(f"{s['question'].rstrip(' ?')} ({o['label']})")
            resp.update(type="info", text="🤔 " + (c.get("question_text") or "Ý này cần bạn nói rõ hơn:"),
                        suggestions=opts[:4], clarification=None)
        resp["task"] = s["question"]
        if not s["eval_mode"] and (s["status"] not in ("clarification", "info") or s.get("sql")):
            u = s["user"]
            self.store.log_query(run_id=s["run_id"], username=u.username, role=u.role, conversation_id=s["conversation_id"],
                                 question=s["question"], sql=s.get("sql"), status=s["status"], error=s.get("error"),
                                 est_bytes=(s.get("cost") or {}).get("total_bytes"),
                                 rows=(s.get("result") or {}).get("row_count"),
                                 latency_ms=int((time.time() - s["t0"]) * 1000), retries=s["attempts"], llm_mode=s["mode"],
                                 source=s.get("sql_source") or s.get("intent"), plan=s.get("plan"), lint=s.get("lint"),
                                 approved_by=s.get("approved_by"))
        return END

    def _response(self, s) -> dict:
        r = s.get("result") or {}
        v = s.get("validation") or {}
        typ = {"success": "variance" if s.get("variance") else "result", "clarification": "clarification",
               "pending_approval": "pending_approval", "error": "error", "blocked": "blocked", "info": "info",
               "rejected": "rejected"}.get(s["status"], "error")
        resp = {
            "type": typ, "run_id": s["run_id"], "question": s.get("question_display") or s["question"],
            "text": s.get("text") or s.get("error"), "error": s.get("error"), "mode": s["mode"], "intent": s.get("intent"),
            "sql": s.get("sql"), "sql_exec": v.get("sql_exec"), "sql_source": s.get("sql_source"), "plan": s.get("plan"),
            "assumptions": s.get("assumptions", []), "notes": s.get("notes", []),
            "columns": r.get("columns"), "columns_meta": s.get("columns_meta"), "rows": r.get("rows"),
            "row_count": r.get("row_count"), "truncated": r.get("truncated"), "chart": s.get("chart"),
            "facts": s.get("facts"), "audit": s.get("audit"), "validation": v or None, "lint": s.get("lint"),
            "cost": _cost_public(s.get("cost")), "lineage": s.get("lineage"), "metrics_matched": s.get("metrics_matched"),
            "repairs": s.get("repairs"), "trace": s["trace"], "llm_trace": s.get("llm_trace"), "llm_usage": s.get("llm_usage"),
            "executed": s.get("executed"), "fewshot_hit": s.get("fewshot_hit"), "elapsed_ms": int((time.time() - s["t0"]) * 1000),
            "approved_by": s.get("approved_by"), "narrator": s.get("narrator", "template"),
        }
        if s["status"] == "clarification":
            c = s["clarification"]
            resp["clarification"] = {"id": c["id"], "question": c.get("question_text"), "options": [o["label"] for o in c["options"]],
                                     "details": [profile.metric_brief(o.get("metric"), profile._option_hint(o.get("metric")))
                                                 for o in c["options"]]}
            resp["text"] = c.get("question_text")
        if s["status"] == "pending_approval":
            resp["text"] = ("Truy vấn cần bạn xem lại SQL và xác nhận trước khi chạy: " + " ".join(s["cost"].get("reasons", [])))
        if s.get("variance"):
            resp["variance"] = {k: s["variance"][k] for k in ("metric_label", "format", "period_now", "period_prev", "total",
                                                                  "significance", "drivers", "best_dimension", "drill",
                                                                  "decomposition", "root_causes", "hypotheses", "queries")
                                if k in s["variance"]}
        for k in ("metric_cards", "schema_tables", "suggestions"):
            if s.get(k):
                resp[k] = s[k]
        # phần dành cho giao diện thân thiện: câu này có phải do bộ luật trả lời thay LLM không, chỉ số được tính theo
        # định nghĩa nào, và có thể hỏi tiếp điều gì (đều dựng bằng code từ kế hoạch vừa chạy)
        resp["fallback"] = None
        resp["llm_error"] = s.get("llm_error")
        resp["path"] = s.get("path") or ("llm" if s.get("route") or s.get("claude") else "code")
        resp["route"] = s.get("route")
        resp["critic"] = s.get("critic")
        if s.get("parts"):          # câu nhiều ý: mỗi phần là một câu trả lời đầy đủ (bảng, biểu đồ, kiểm chứng riêng)
            resp.update(type="multi", parts=s["parts"], rows=None, columns=None, chart=None, facts=None, sql=None)
        if s["status"] == "success":
            resp["definitions"] = [] if s.get("lookup") else _definitions(s)
            resp["follow_ups"] = s.get("lookup_follow_ups") or [] if s.get("lookup") else _follow_ups(s)
        pending = s.get("pending_tasks") or []
        if pending:     # câu nhiều ý: các ý chưa trả lời đứng đầu phần "Hỏi tiếp" để người dùng bấm xem tiếp
            resp["follow_ups"] = list(dict.fromkeys(pending + (resp.get("follow_ups") or [])))[:6]
            resp["pending_tasks"] = pending
            if s["status"] != "success":
                resp["suggestions"] = list(dict.fromkeys(pending + (resp.get("suggestions") or [])))[:6]
        return resp


def _fold(text: str) -> str:
    """Bỏ dấu tiếng Việt, chữ thường, nhưng GIỮ dấu câu (norm() bỏ cả dấu phẩy/chấm phẩy, cần để nhận câu nhiều mệnh đề)."""
    import unicodedata
    t = unicodedata.normalize("NFD", (text or "").lower().replace("đ", "d"))
    return re.sub(r"\s+", " ", "".join(c for c in t if not unicodedata.combining(c)))


def _definitions(s) -> list[dict]:
    """Định nghĩa các chỉ số có trong câu trả lời (theo semantic layer), để người dùng biết con số được tính thế nào."""
    sem = get_semantic()
    names = list((s.get("plan") or {}).get("metrics") or []) or [m["metric"] for m in s.get("metrics_matched") or []]
    return [{"name": n, "label": sem.metrics[n]["label"], "description": sem.metrics[n].get("description") or "",
             "owner": sem.metrics[n].get("owner") or ""} for n in dict.fromkeys(names) if n in sem.metrics]


def _follow_ups(s, limit: int = 4) -> list[str]:
    """Gợi ý câu hỏi tiếp theo cho kết quả vừa trả. Chỉ gợi ý điều hệ thống chắc chắn làm được: chia theo một chiều mà chỉ số
    hỗ trợ, so với cùng kỳ, tìm nguyên nhân biến động. Câu gợi ý viết theo dạng bộ luật hiểu được khi nối tiếp hội thoại."""
    from .nlu import _period_label
    sem, plan = get_semantic(), s.get("plan") or {}
    metric = next((m for m in plan.get("metrics") or [] if m in sem.metrics), None)
    if not metric:
        return []
    mdef = sem.metrics[metric]
    label = mdef["label"].lower()
    cfg = insights.analysis_config(sem, metric)
    used = set(plan.get("dimensions") or []) | {f["dimension"] for f in plan.get("filters") or []}
    tr = plan.get("time_range") or {}
    period = None
    if tr.get("start") and tr.get("end"):
        p = _period_label(dt.date.fromisoformat(tr["start"]), dt.date.fromisoformat(tr["end"]))
        period = p if re.fullmatch(r"T\d{1,2}/\d{4}|Q[1-4]/\d{4}|năm \d{4}", p) else None
    out = []
    if s.get("variance"):
        best = s["variance"].get("best_dimension")
        if best in sem.dims and period:
            out.append(f"{mdef['label']} theo {sem.dims[best]['label'].lower()} {period}")
        out += [f"Chia theo {sem.dims[d]['label'].lower()}" for d in cfg["drivers"] if d not in used and d != best][:1]
        return out[:limit]
    out += [f"Chia theo {sem.dims[d]['label'].lower()}" for d in cfg["drivers"] if d not in used][:2]
    if tr and not plan.get("compare"):
        out.append("So với cùng kỳ năm trước")
    additive = mdef.get("additive", mdef.get("type") != "derived" and mdef.get("format") != "percent")
    if period and additive:
        out.append(f"Tìm nguyên nhân biến động {label} {period}")
    return out[:limit]


def _no_numbers(meta, r) -> bool:
    """Kết quả không có con số nào: không có dòng, hoặc mọi ô chỉ số đều NULL."""
    if not r["rows"]:
        return True
    idx = [i for i, m in enumerate(meta or []) if m.get("kind") == "metric" and i < len(r["columns"])]
    return bool(idx) and all(row[i] is None for row in r["rows"] for i in idx)


def _cost_public(c):
    if not c:
        return None
    return {k: c.get(k) for k in ("total_bytes", "human", "decision", "level", "reasons", "tables", "large_tables", "thresholds")}


def _render(template: str, facts: dict) -> str:
    return re.sub(r"\{([a-z0-9_]+)\}", lambda m: facts[m.group(1)]["text"] if m.group(1) in facts else m.group(0), template)


def _is_listing(sql: str | None) -> bool:
    """SQL chỉ liệt kê dòng của bảng danh mục (không hàm tổng hợp, không GROUP BY, không hàm cửa sổ, chỉ đọc bảng chiều /
    bảng tra cứu) — vd danh sách sản phẩm kèm giá. Bảng sự kiện đã tính sẵn (kế hoạch theo cửa hàng) không tính là liệt kê."""
    if not sql:
        return False
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return False
    if tree.find(exp.AggFunc) or tree.find(exp.Group) or tree.find(exp.Window):
        return False
    lookup_tables = {e.table.split(".")[-1] for e in lookup.get_lookups().entities.values()}
    tables = [t.name for t in tree.find_all(exp.Table)]
    return bool(tables) and all(t.startswith("dim_") or t in lookup_tables for t in tables)


def infer_meta(columns, types, rows) -> list[dict]:
    sem = get_semantic()
    out = []
    for i, c in enumerate(columns):
        t = (types[i] if i < len(types) else "").upper()
        if c in sem.metrics:
            m = sem.metrics[c]
            out.append({"name": c, "label": m["label"], "kind": "metric", "format": m.get("format", "number"),
                        "additive": m.get("additive", m.get("format") != "percent")})
            continue
        if c in sem.dims:
            d = sem.dims[c]
            out.append({"name": c, "label": d["label"], "kind": "dimension", "time": d.get("kind") == "time", "format": "text"})
            continue
        numeric = any(x in t for x in ("INT", "DOUBLE", "DECIMAL", "FLOAT", "NUMERIC", "REAL"))
        is_id = c.startswith("ma_") or c in ("dong", "nam", "thang", "quy", "tuan", "thu")
        label = _catalog_label(c)
        if "DATE" in t or "TIMESTAMP" in t or c in ("thang", "nam", "quy", "tuan", "ngay"):
            out.append({"name": c, "label": label, "kind": "dimension", "time": True, "format": "text"})
        elif numeric and not is_id:
            nc = norm(c.replace("_", " "))
            fmt = "percent" if re.search(r"pct|ty le|ti le|bien|ty trong", nc) else \
                ("currency" if re.search(r"doanh thu|tien|^tt|gia|chi phi|loi nhuan|von|^ck|dtt|thanh tien", nc) else "number")
            out.append({"name": c, "label": label, "kind": "metric", "format": fmt, "additive": fmt != "percent"})
        else:
            out.append({"name": c, "label": label, "kind": "dimension", "time": False, "format": "text"})
    return out


def _catalog_label(col: str) -> str:
    """Nhãn thân thiện cho cột của SQL tự do: lấy phần đầu mô tả nghiệp vụ trong catalog (vd ma_kh -> Mã khách hàng)."""
    for t in get_catalog().tables.values():
        if t["schema"] == profile.serving_schema() and col in t["columns"] and t["columns"][col].get("description"):
            d = re.split(r"\s*(?:->|\(|—|:|,)", t["columns"][col]["description"])[0].strip()
            if d and len(d) <= 40:
                return d
    return col


from . import insights  # noqa: E402  (tránh vòng import)

_agent: Agent | None = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent
