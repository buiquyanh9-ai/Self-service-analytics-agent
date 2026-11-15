"""Eval hội thoại: kiểm tra bằng code + mô hình chấm (ở đây là client giả, không gọi mạng).

Kèm các lỗi mà eval hội thoại đã phát hiện trên mô hình thật, để không tái phát:
- "chia theo kênh bán" sau câu "vì sao" phải trả bảng chia, không chạy lại phân tích nguyên nhân;
- hỏi "theo miền" khi tài khoản chỉ xem được một miền: không nói "dẫn đầu", có ghi chú phạm vi.
"""
from __future__ import annotations

from app import conv_eval, llm
from app.agent import get_agent
from app.governance import UserCtx
from app.store import get_store

ADMIN = UserCtx("admin", "admin", {})


def test_scenario_file_is_well_formed():
    scs = conv_eval.load_scenarios()
    assert len(scs) >= 15 and len({s["id"] for s in scs}) == len(scs)
    known = {"type", "parts", "n_parts", "part_types_in", "types_include", "metrics", "first_metric", "dimensions", "filters",
             "time_start", "compare", "limit", "text_starts", "text_has", "text_not", "no_rows", "rows_max", "max_s"}
    for s in scs:
        assert s["turns"]
        for t in s["turns"]:
            assert ("q" in t) != ("choose" in t)
            assert set(t.get("expect") or {}) <= known, (s["id"], set(t["expect"]) - known)


def test_check_turn_reports_each_failure():
    r = {"type": "multi", "text": "Bạn hỏi 2 ý", "parts": [
        {"type": "result", "text": "Doanh thu thuần T10/2026 ...", "plan": {"metrics": ["doanh_thu_thuan"], "dimensions": ["vung"],
                                                                          "filters": [], "time_range": {"start": "2026-10-01"}},
         "rows": [["Miền Bắc", 1.0]]},
        {"type": "result", "text": "Top 3 ...", "plan": {"metrics": ["doanh_thu_thuan"], "dimensions": ["san_pham"], "limit": 3,
                                                         "filters": [{"dimension": "vung", "values": ["Miền Bắc"]}],
                                                         "time_range": {"start": "2026-09-01"}}, "rows": []}]}
    assert conv_eval.check_turn(r, {"type": "multi", "parts": ["result", "result"], "limit": 3, "dimensions": ["vung"],
                                    "filters": {"vung": ["miền bắc"]}}, 1.0) == []
    fails = conv_eval.check_turn(r, {"type": "result", "time_start": "2026-10-01", "metrics": ["so_don"], "no_rows": True,
                                     "text_has": ["lợi nhuận"], "max_s": 0.5}, 1.0)
    assert len(fails) == 6


def test_run_conversations_with_fake_judge(fake_llm):
    scs = [{"id": "t1", "name": "nối tiếp", "tags": ["ngữ cảnh"], "turns": [
        {"q": "Doanh thu thuần tháng 10/2026 theo kênh bán", "expect": {"type": "result", "dimensions": ["kenh"]}},
        {"q": "còn Miền Bắc thì sao", "expect": {"type": "result", "filters": {"vung": None}}}]},
           {"id": "t2", "name": "sai kỳ vọng", "tags": ["x"], "turns": [
        {"q": "Doanh thu thuần tháng 10/2026", "expect": {"type": "info"}}]}]
    res = conv_eval.run_conversations(scenarios=scs, save=False)
    sm, d = res["summary"], {x["id"]: x for x in res["details"]}
    assert d["t1"]["ok"] and not d["t2"]["ok"]
    assert sm["n"] == 2 and sm["n_turns"] == 3 and sm["scenario_pass"] == 50.0 and sm["judged"] == 3
    assert sm["disagree"] == 1            # lượt t2: code nói sai, mô hình chấm (giả) nói đúng
    judge_calls = [p for p in fake_llm.critiques if p[0] == conv_eval.JUDGE_SYSTEM]    # ngoài lượt chấm của Critic trong app
    assert len(judge_calls) == 3


def test_overrides_are_restored(fake_llm):
    store = get_store()
    before = store.get_settings()["critic"]
    conv_eval.run_conversations(scenarios=[{"id": "t", "turns": [{"q": "Doanh thu thuần tháng 10/2026"}]}], judge=False,
                                overrides={"critic": not before}, save=False)
    assert store.get_settings()["critic"] == before


def test_breakdown_after_why_returns_breakdown(fake_llm, monkeypatch):
    real_route = fake_llm.route

    def route(question, context):          # mô hình thật viết lại "chia theo kênh bán" thành câu "vì sao ... chia theo ..."
        if question == "chia theo kênh bán":
            return llm.normalize_route({"intent": "variance", "is_follow_up": True, "tasks": [], "reply": "", "topic": "",
                                        "standalone_question": "Vì sao doanh thu thuần Miền Trung tháng 6/2026 giảm, chia theo kênh bán"},
                                       question)
        return real_route(question, context)
    monkeypatch.setattr(fake_llm, "route", route)
    cid = get_store().create_conversation("admin", "t")
    try:
        a = get_agent().ask(ADMIN, cid, "Vì sao doanh thu thuần Miền Trung tháng 6/2026 giảm?")
        b = get_agent().ask(ADMIN, cid, "chia theo kênh bán")
    finally:
        get_store().delete_conversation(cid)
    assert a["type"] == "variance"
    assert b["type"] == "result" and "kenh" in b["plan"]["dimensions"]


def test_single_row_under_rls_is_not_called_leader():
    user = UserCtx("analyst_bac", "analyst", {"vung": ["B"]})
    cid = get_store().create_conversation("analyst_bac", "t")
    try:
        r = get_agent().ask(user, cid, "Doanh thu thuần theo miền tháng 10/2026")
    finally:
        get_store().delete_conversation(cid)
    assert r["type"] == "result" and len(r["rows"]) == 1
    assert "Dẫn đầu" not in r["text"] and any("phân quyền theo dòng" in n for n in r.get("notes") or [])
