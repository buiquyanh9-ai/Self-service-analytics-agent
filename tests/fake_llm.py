"""Client LLM giả cho test: không bao giờ gọi mạng.

Router và Planner giả mô phỏng một mô hình làm đúng việc, dựa trên hồ sơ doanh nghiệp và bộ luật, để test kiểm tra phần
nối dây của agent (định tuyến, chốt chặn, bộ nhớ hội thoại, hỏi lại, phân quyền) chứ không đo chất lượng mô hình thật.
Chất lượng mô hình thật đo bằng bộ eval (app/evals.py) với khóa OpenRouter.
"""
from __future__ import annotations

from app import llm, lookup, profile
from app.nlu import get_nlu


class FakeLLM:
    model, effort = "fake/llm", "test"

    def __init__(self, *_, **__):
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "calls": 0}
        self.routes, self.contexts = [], []

    # ------------------------------------------------------------------ Router
    def route(self, question: str, context: dict) -> dict:
        self.usage["calls"] += 1
        q = question

        def out(intent, follow=False, reply="", topic=""):
            r = llm.normalize_route({"intent": intent, "standalone_question": q, "is_follow_up": follow, "tasks": [],
                                     "reply": reply, "topic": topic}, q)
            self.routes.append(r)
            return r
        if profile.blocked_topic(q):
            return out("unsafe")
        if profile.write_request(q):
            return out("write_request")
        if profile.is_intro_question(q):
            return out("about_assistant")
        if profile.smalltalk_reply(q):
            return out("smalltalk", reply="Rất vui được giúp bạn!")
        if profile.forecast_request(q):
            return out("forecast")
        r = get_nlu().parse(q, {"last_plan": context.get("last_plan"), "prefs": context.get("prefs") or {}})
        topic = profile.off_topic(q)
        if topic and not r.plan.get("metrics"):
            return out("out_of_scope", topic=topic["label"])
        intent = {"variance": "variance", "metric_info": "metric_definition", "schema_info": "schema_info",
                  "dq": "data_quality", "help": "about_assistant"}.get(r.intent)
        if intent:
            return out(intent, follow=r.follow_up)
        p = r.plan
        signal = p.get("metrics") or p.get("time_range") or p.get("filters") or p.get("dimensions") or p.get("limit") \
            or r.follow_up or lookup.get_lookups().find_codes(q)[0]
        if r.intent == "unknown" and not signal:
            return out("out_of_scope")
        return out("data", follow=r.follow_up)

    # ------------------------------------------------------------------ Planner
    def plan(self, question, context, tool_impl, max_turns=8, trace=None, check=None):
        self.usage["calls"] += 1
        self.contexts.append(context)
        r = get_nlu().parse(question, context.get("memory") or {})
        p = dict(r.plan)
        if r.clarification:
            c = r.clarification
            return {"intent": "metric_query", "kind": "clarify",
                    "clarification": {"question": c["question"], "options": c["options"]}}
        if r.intent == "variance" and p.get("metrics"):
            p["compare"] = p.pop("variance_compare", None)
            return {"intent": "variance", "kind": "semantic", "semantic_plan": p, "assumptions": r.assumptions}
        if p.get("metrics") and r.intent == "metric_query":
            return {"intent": "metric_query", "kind": "semantic", "semantic_plan": p, "assumptions": r.assumptions,
                    "chart_hint": r.chart_hint}
        return {"intent": "metric_query", "kind": "answer", "answer_text": None}

    def repair_sql(self, sql, errors, schema_text, question):
        raise llm.LLMError("fake: không sửa SQL")

    def narrate(self, question, facts, template):
        return template

    # ------------------------------------------------------------------ Critic: mặc định chấm "đúng"
    def critique(self, prompt):
        self.usage["calls"] += 1
        self.critiques = getattr(self, "critiques", []) + [prompt]
        return {"verdict": "ok", "problem": "", "fix": ""}

    # ------------------------------------------------------------------ giả thuyết biến động: mặc định không có
    hypotheses: list = []

    def hypothesize(self, system, user):
        self.usage["calls"] += 1
        return {"hypotheses": list(self.hypotheses)}
