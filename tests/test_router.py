"""Router (tác tử phân loại + viết lại câu hỏi) và đường nhanh.

- Câu bộ luật hiểu trọn vẹn: trả lời không gọi mô hình nào.
- Câu còn lại: Router phân loại trước; câu lạc đề/không an toàn/xã giao không bao giờ chạy truy vấn hay kế thừa số liệu.
- Câu nối tiếp được viết lại thành câu độc lập; câu nhiều ý trả lời ý đầu và đưa các ý còn lại vào "Hỏi tiếp".
"""
from __future__ import annotations

import pytest

from app import llm
from app.agent import Agent, get_agent
from app.governance import UserCtx
from app.store import get_store

ADMIN = UserCtx("admin", "admin", {})
FIRST = "Doanh thu thuần tháng 10/2026"


def chat(*questions):
    store = get_store()
    cid = store.create_conversation("admin", "test")
    try:
        return [get_agent().ask(ADMIN, cid, q, eval_mode=True) for q in questions]
    finally:
        store.delete_conversation(cid)


def scripted_route(fake_llm, monkeypatch, **out):
    """Router giả trả đúng kết quả cho trước (mô phỏng mô hình thật phân loại/viết lại)."""
    def route(question, context):
        return llm.normalize_route(dict({"is_follow_up": False, "standalone_question": question, "tasks": [], "reply": "",
                                         "topic": ""}, **out), question)
    monkeypatch.setattr(fake_llm, "route", route)


def test_fully_understood_question_needs_no_model(fake_llm):
    r, = chat("Doanh thu thuần theo miền tháng 9/2026")
    assert r["type"] == "result" and r["path"] == "fast" and fake_llm.usage["calls"] == 0
    why, = chat("Vì sao doanh thu thuần tháng 9/2026 giảm?")            # từ hỏi nguyên nhân không chặn đường nhanh
    # không gọi Router / Planner; lượt gọi duy nhất là mô hình nêu giả thuyết nguyên nhân (code kiểm chứng)
    assert why["type"] == "variance" and why["path"] == "fast" and not fake_llm.routes and not fake_llm.contexts
    assert fake_llm.usage["calls"] == 1


def test_follow_up_is_not_answered_without_the_previous_period(fake_llm):
    _, r = chat(FIRST, "còn doanh thu gộp thì sao")
    assert r["type"] == "result" and r["path"] != "fast"
    assert r["plan"]["time_range"]["start"] == "2026-10-01"          # giữ kỳ của câu trước, không quét toàn bộ lịch sử


@pytest.mark.parametrize("intent", ["out_of_scope", "unsafe", "smalltalk", "about_assistant", "write_request", "forecast"])
def test_non_data_intents_never_query_or_inherit(fake_llm, monkeypatch, intent):
    scripted_route(fake_llm, monkeypatch, intent=intent, topic="vật lý", reply="Chào bạn!")
    _, r = chat(FIRST, "giải thích thuyết tương đối giúp mình")
    assert r["type"] == "info" and not r.get("sql") and not r.get("rows"), r.get("text")
    assert "422" not in (r.get("text") or "")                          # không lặp lại số liệu của câu trước
    if intent == "out_of_scope":
        assert "vật lý" in r["text"]


def test_router_rewrite_is_used_and_shown(fake_llm, monkeypatch):
    scripted_route(fake_llm, monkeypatch, intent="data", is_follow_up=True,
                   standalone_question="Doanh thu thuần Miền Nam tháng 10/2026")
    _, r = chat(FIRST, "thế còn phía Nam?")
    assert r["type"] == "result" and r["path"] == "fast"               # câu đã viết lại thì bộ luật hiểu trọn
    assert r["plan"]["filters"] == [{"dimension": "vung", "op": "in", "values": ["Miền Nam"]}]
    assert any("Doanh thu thuần Miền Nam tháng 10/2026" in a for a in r["assumptions"])
    assert r["question"] == "thế còn phía Nam?"                        # lịch sử hiển thị câu người dùng gõ


TASKS = ["Doanh thu thuần theo miền tháng 10/2026", "Top 3 sản phẩm có doanh thu thuần cao nhất tháng 10/2026"]


@pytest.fixture
def no_multi():
    store = get_store()
    before = store.get_settings().get("multi_task", True)
    store.set_settings({"multi_task": False})
    yield
    store.set_settings({"multi_task": before})


def test_multi_part_question_answers_every_part(fake_llm, monkeypatch):
    scripted_route(fake_llm, monkeypatch, intent="data", standalone_question=" và ".join(TASKS), tasks=TASKS)
    r, = chat("doanh thu thuần theo miền tháng 10/2026 và top 3 sản phẩm bán chạy")
    assert r["type"] == "multi" and [p["task"] for p in r["parts"]] == TASKS
    assert all(p["type"] == "result" and p["audit"]["ok"] for p in r["parts"])
    assert r["parts"][0]["plan"]["dimensions"] == ["vung"] and r["parts"][1]["plan"]["limit"] == 3
    assert "2 ý" in r["text"] and r["plan"]["dimensions"] == ["vung"]        # bộ nhớ hội thoại theo ý đầu


def test_multi_part_conversation_stores_one_message_and_follows_up(fake_llm, monkeypatch):
    scripted_route(fake_llm, monkeypatch, intent="data", standalone_question=" và ".join(TASKS), tasks=TASKS)
    store = get_store()
    cid = store.create_conversation("admin", "test")
    try:
        get_agent().ask(ADMIN, cid, "doanh thu thuần theo miền tháng 10/2026 và top 3 sản phẩm bán chạy")
        msgs = store.get_messages(cid) if hasattr(store, "get_messages") else None
        mem = store.get_conversation(cid)["memory"]
    finally:
        store.delete_conversation(cid)
    assert mem["last_plan"]["dimensions"] == ["vung"] and len(mem["history"]) == 1
    if msgs is not None:
        assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_multi_part_clarification_becomes_clickable_questions(fake_llm, monkeypatch):
    tasks = ["Doanh thu tháng 10/2026", "Top 3 cửa hàng có số đơn hàng nhiều nhất tháng 10/2026"]
    scripted_route(fake_llm, monkeypatch, intent="data", standalone_question=" và ".join(tasks), tasks=tasks)
    r, = chat("cho mình xem doanh thu tháng 10/2026, kèm luôn 3 cửa hàng chốt được nhiều đơn nhất nhé")
    amb, ok = r["parts"]
    assert amb["type"] == "info" and len(amb["suggestions"]) == 2 and ok["type"] == "result"
    assert all("Doanh thu tháng 10/2026" in x for x in amb["suggestions"])


def test_multi_part_question_answers_first_and_offers_the_rest(fake_llm, monkeypatch, no_multi):
    tasks = TASKS
    scripted_route(fake_llm, monkeypatch, intent="data", standalone_question=" và ".join(tasks), tasks=tasks)
    r, = chat("doanh thu thuần theo miền tháng 10/2026 và top 3 sản phẩm bán chạy")
    assert r["type"] == "result" and r["plan"]["dimensions"] == ["vung"]
    assert r["follow_ups"][0] == tasks[1] and r["pending_tasks"] == [tasks[1]]
    assert any("2 ý" in a for a in r["assumptions"])


def test_router_failure_is_an_error_not_a_guess(fake_llm, monkeypatch):
    def broken(*_a, **_k):
        raise llm.LLMUnavailable("mất mạng")
    monkeypatch.setattr(fake_llm, "route", broken)
    r, = chat("doanh thu thuần theo độ tuổi khách hàng năm 2025")
    assert r["type"] == "error" and "mất mạng" in r["llm_error"] and not r.get("rows")


def test_no_api_key_still_answers_fully_understood_questions(monkeypatch):
    monkeypatch.setattr(llm, "resolve_mode", lambda settings: llm.NO_LLM)
    ok, other = chat("Doanh thu thuần theo miền tháng 9/2026", "doanh thu thuần theo độ tuổi khách hàng năm 2025")
    assert ok["type"] == "result" and ok["path"] == "fast"
    assert other["type"] == "info" and other["intent"] == "llm_unavailable" and "OPENROUTER_API_KEY" in other["text"]


def test_answer_names_the_filtered_scope():
    """Câu trả lời có lọc phải nói rõ phạm vi, không để người đọc tưởng là số toàn công ty."""
    r, = chat("Doanh thu thuần Miền Nam tháng 10/2026")
    assert r["type"] == "result" and "Miền Nam" in r["text"] and r["audit"]["ok"]
    r, = chat("Doanh thu thuần không tính Miền Bắc tháng 10/2026")
    assert "trừ Miền Bắc" in r["text"] and r["audit"]["ok"]


def test_router_cannot_pick_a_definition_for_the_user(fake_llm, monkeypatch):
    """Mô hình viết lại "doanh thu" thành một định nghĩa cụ thể: code vẫn hỏi lại vì người dùng chưa chọn."""
    scripted_route(fake_llm, monkeypatch, intent="data", standalone_question="Doanh thu thuần tháng trước là bao nhiêu?")
    r, = chat("Doanh thu tháng trước là bao nhiêu?")
    assert r["type"] == "clarification" and len(r["clarification"]["options"]) == 2


def test_submit_check_compiles_plans_and_dry_runs_sql():
    agent = get_agent()
    trace = []
    check = agent._submit_check(agent._tool_impls(ADMIN), trace)
    ok = {"intent": "metric_query", "kind": "semantic", "semantic_plan": {"metrics": ["doanh_thu_thuan"], "dimensions": ["vung"]}}
    assert check(ok) is None
    bad = check({"intent": "metric_query", "kind": "semantic", "semantic_plan": {"metrics": ["khong_co_chi_so_nay"]}})
    assert bad and "CHƯA được nhận" in bad
    assert check({"intent": "metric_query", "kind": "sql", "sql": "SELECT khong_co FROM dw.khong_co_bang"})
    assert check({"intent": "metric_query", "kind": "clarify"}) is None and len(trace) == 3


def _planner_detail(r):
    return next(x["detail"] for x in r["trace"] if x["node"] == "planner")


def test_speculative_plan_is_used_when_router_keeps_the_question(fake_llm, monkeypatch):
    """Planner chạy song song với Router trên câu gốc; Router không viết lại -> dùng luôn, không gọi Planner lần hai."""
    monkeypatch.setattr(Agent, "_fast_path", lambda self, s, q: None)
    q = "Doanh thu thuần theo miền tháng 9/2026"
    scripted_route(fake_llm, monkeypatch, intent="data", standalone_question=q)
    r, = chat(q)
    assert r["type"] == "result" and "chạy đón trước" in _planner_detail(r) and len(fake_llm.contexts) == 1


def test_speculative_plan_is_discarded_when_router_rewrites(fake_llm, monkeypatch):
    monkeypatch.setattr(Agent, "_fast_path", lambda self, s, q: None)
    scripted_route(fake_llm, monkeypatch, intent="data", is_follow_up=True,
                   standalone_question="Doanh thu thuần Miền Nam tháng 10/2026")
    _, r = chat(FIRST, "thế còn phía Nam?")
    assert r["type"] == "result" and "chạy đón trước" not in _planner_detail(r)
    assert r["plan"]["filters"] == [{"dimension": "vung", "op": "in", "values": ["Miền Nam"]}]


def test_speculative_plan_survives_a_cosmetic_rewrite(fake_llm, monkeypatch):
    monkeypatch.setattr(Agent, "_fast_path", lambda self, s, q: None)
    q = "Doanh thu thuần theo miền tháng 9/2026"
    scripted_route(fake_llm, monkeypatch, intent="data", standalone_question=q + " là bao nhiêu?")
    r, = chat(q)
    assert "chạy đón trước" in _planner_detail(r) and len(fake_llm.contexts) == 1


@pytest.mark.parametrize("q", ["top 5 cửa hàng có doanh thu thuần thấp nhất tháng 10/2026, và vì sao doanh thu thuần tháng 10/2026 giảm",
                               "doanh thu thuần tháng 10/2026 theo miền; số đơn hàng theo kênh tháng 10/2026"])
def test_multi_clause_questions_skip_the_fast_path(fake_llm, q):
    r, = chat(q)
    assert r["path"] != "fast" and fake_llm.routes


def test_two_metrics_in_one_clause_stay_on_the_fast_path(fake_llm):
    r, = chat("số đơn hàng và giá trị đơn trung bình tháng 9/2026 ở Hà Nội")
    assert r["path"] == "fast" and fake_llm.usage["calls"] == 0
