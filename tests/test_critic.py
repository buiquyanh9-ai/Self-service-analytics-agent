"""Critic: tác tử kiểm tra câu trả lời có trúng câu hỏi không.

- Lớp code: kế hoạch của Planner bỏ sót phần người dùng NÊU RÕ (bộ lọc, kỳ, cách chia, top N) thì vá và báo cho người
  dùng; chỉ số được hỏi luôn đứng đầu câu trả lời. Phần người dùng không nêu thì không đụng tới lựa chọn của Planner.
- Lớp LLM: chấm "chưa đúng" thì Planner lập lại kế hoạch một lần kèm nhận xét; hết thời gian thì cảnh báo; chấm lỗi thì
  bỏ qua. Câu đi đường nhanh không bị chấm (không tốn thêm lượt gọi).
"""
from __future__ import annotations

import pytest

from app import critic
from app.agent import Agent, get_agent
from app.governance import UserCtx
from app.store import get_store

ADMIN = UserCtx("admin", "admin", {})
OCT = {"start": "2026-10-01", "end": "2026-11-01", "label": "T10/2026"}


def chat(*questions):
    store = get_store()
    cid = store.create_conversation("admin", "test")
    try:
        return [get_agent().ask(ADMIN, cid, q, eval_mode=True) for q in questions]
    finally:
        store.delete_conversation(cid)


@pytest.fixture
def planner(fake_llm, monkeypatch):
    """Tắt đường nhanh để câu nào cũng tới Planner; Planner giả nộp lần lượt các plan cho trước."""
    monkeypatch.setattr(Agent, "_fast_path", lambda self, s, q: None)
    plans = []

    def plan(question, context, tool_impl, max_turns=8, trace=None, check=None):
        fake_llm.contexts.append(context)
        p = plans.pop(0) if len(plans) > 1 else plans[0]
        return {"intent": "metric_query", "kind": "semantic", "semantic_plan": dict(p)}
    monkeypatch.setattr(fake_llm, "plan", plan)
    return plans


@pytest.fixture
def settings():
    store = get_store()
    before = store.get_settings()
    yield store.set_settings
    store.set_settings({k: before.get(k) for k in ("critic", "critic_llm", "critic_revise", "critic_revise_until_s")})


def verdicts(fake_llm, monkeypatch, *outs):
    seq = list(outs)

    def critique(prompt):
        fake_llm.critiques = getattr(fake_llm, "critiques", []) + [prompt]
        out = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(out, Exception):
            raise out
        return critic.normalize_verdict(out)
    monkeypatch.setattr(fake_llm, "critique", critique)


# ---------------------------------------------------------------------------------------------- trọng tâm câu trả lời
def test_ratio_answer_leads_with_the_asked_metric():
    """“trung bình mỗi đơn”: semantic layer thêm doanh thu và số đơn làm cột phụ, nhưng câu đầu phải là giá trị đơn TB."""
    r, = chat("Giá trị đơn trung bình ở Hà Nội tháng 10/2026")
    assert r["type"] == "result" and r["text"].startswith("Giá trị đơn trung bình Hà Nội T10/2026:"), r["text"]
    assert "Thành phần:" in r["text"] and r["audit"]["ok"]


def test_focus_check_on_narration():
    plan = {"metrics": ["gia_tri_don_tb"]}
    assert critic.answer_focus_ok("Giá trị đơn trung bình T10/2026 là 21 triệu. Doanh thu đạt 119 tỷ.", plan)
    assert not critic.answer_focus_ok("Doanh thu thuần đạt 119 tỷ. Giá trị đơn trung bình 21 triệu.", plan)


# ---------------------------------------------------------------------------------------------- lớp code
def test_missing_stated_filter_and_period_are_patched(planner):
    planner.append({"metrics": ["doanh_thu_thuan"], "dimensions": ["kenh"]})       # quên Hà Nội và quên kỳ
    r, = chat("doanh thu thuần ở Hà Nội tháng 10/2026 theo kênh bán")
    assert r["type"] == "result"
    assert {"dimension": "tinh_tp", "op": "in", "values": ["Hà Nội"]} in r["plan"]["filters"]
    assert r["plan"]["time_range"]["start"] == "2026-10-01"
    assert any(a.startswith("Tác tử kiểm tra đã sửa kế hoạch") for a in r["assumptions"])
    assert r["critic"]["fixes"] and "Hà Nội" in r["text"]


def test_missing_stated_breakdown_and_top_n_are_patched(planner):
    planner.append({"metrics": ["doanh_thu_thuan"], "time_range": OCT, "dimensions": ["vung"], "limit": 10,
                    "order_by": [{"field": "doanh_thu_thuan", "direction": "desc"}]})
    r, = chat("top 3 miền có doanh thu thuần cao nhất tháng 10/2026 theo kênh bán")
    assert r["plan"]["limit"] == 3 and "kenh" in r["plan"]["dimensions"]


def test_asked_metric_is_moved_first(planner):
    planner.append({"metrics": ["doanh_thu_thuan", "gia_tri_don_tb"], "time_range": OCT})
    r, = chat("giá trị đơn trung bình tháng 10/2026 so với doanh thu thuần")
    assert r["plan"]["metrics"][0] == "gia_tri_don_tb"
    assert r["text"].startswith("Giá trị đơn trung bình"), r["text"]


def test_unstated_parts_keep_the_planners_choice(planner):
    """Người dùng không nêu kỳ: Planner chọn tháng 10 là lựa chọn của Planner, code không ép về toàn bộ lịch sử."""
    planner.append({"metrics": ["doanh_thu_thuan"], "dimensions": ["vung"], "time_range": OCT})
    r, = chat("doanh thu thuần theo miền")
    assert r["plan"]["time_range"] == OCT and not r["critic"]["fixes"]


def test_conflicting_filter_is_left_to_the_judge(planner, fake_llm):
    """Lọc cùng chiều nhưng khác giá trị: code không tự quyết, chuyển nghi vấn vào prompt của LLM chấm."""
    planner.append({"metrics": ["doanh_thu_thuan"], "time_range": OCT,
                    "filters": [{"dimension": "tinh_tp", "op": "in", "values": ["Đà Nẵng"]}]})
    r, = chat("doanh thu thuần ở Hà Nội tháng 10/2026")
    assert r["critic"]["issues"] and r["plan"]["filters"][0]["values"] == ["Đà Nẵng"]
    assert "Kiểm tra bằng code nghi ngờ" in fake_llm.critiques[0][1] and "Hà Nội" in fake_llm.critiques[0][1]


# ---------------------------------------------------------------------------------------------- lớp LLM
def test_wrong_verdict_replans_once_with_the_feedback(planner, fake_llm, monkeypatch):
    planner += [{"metrics": ["doanh_thu_thuan"], "time_range": OCT},
                {"metrics": ["so_don"], "time_range": OCT}]
    verdicts(fake_llm, monkeypatch, {"verdict": "wrong", "problem": "Người dùng hỏi số đơn, kế hoạch tính doanh thu.",
                                     "fix": "Dùng chỉ số số đơn hàng."})
    r, = chat("tháng 10/2026 bán được bao nhiêu đơn")
    assert r["type"] == "result" and r["plan"]["metrics"] == ["so_don"]
    assert len(fake_llm.critiques) == 1                                  # chỉ chấm một lần, không lặp vô hạn
    assert "số đơn" in fake_llm.contexts[-1]["hints"][0] and "CHƯA" in fake_llm.contexts[-1]["hints"][0]
    assert r["critic"]["revised"] and any("lập lại kế hoạch" in n for n in r["notes"])
    assert sum(a.startswith("Mình hiểu") for a in r["assumptions"]) <= 1   # giả định của lần đầu không bị lặp


def test_no_time_left_skips_judging(planner, fake_llm, monkeypatch, settings):
    """Không còn kịp lập lại thì không chấm: kết quả chấm không đổi được gì, chỉ làm người dùng chờ thêm."""
    settings({"critic_revise_until_s": 0})
    planner.append({"metrics": ["doanh_thu_thuan"], "time_range": OCT})
    verdicts(fake_llm, monkeypatch, {"verdict": "wrong", "problem": "Hỏi số đơn nhưng tính doanh thu.", "fix": "x"})
    r, = chat("tháng 10/2026 bán được bao nhiêu đơn")
    assert r["type"] == "result" and r["critic"]["verdict"] == "skipped" and not getattr(fake_llm, "critiques", [])


def test_wrong_verdict_without_revise_warns(planner, fake_llm, monkeypatch, settings):
    settings({"critic_revise": False})
    planner.append({"metrics": ["doanh_thu_thuan"], "time_range": OCT})
    verdicts(fake_llm, monkeypatch, {"verdict": "wrong", "problem": "Hỏi số đơn nhưng tính doanh thu.", "fix": "x"})
    r, = chat("tháng 10/2026 bán được bao nhiêu đơn")
    assert r["type"] == "result" and r["critic"]["warned"] and len(fake_llm.contexts) == 1
    assert any(n.startswith("⚠ Tác tử kiểm tra") for n in r["notes"])


def test_judge_sees_the_company_glossary():
    """Chỉ số đã lọc sẵn đơn hoàn thành: LLM chấm phải thấy định nghĩa, không chê nhầm "chưa lọc trạng thái"."""
    g = critic.glossary({"metrics": ["so_don"], "filters": [{"dimension": "vung", "values": ["Miền Nam"]}]})
    assert "Số đơn" in g and "điều kiện bắt buộc" in g and "còn gọi: miền" in g


def test_judge_failure_keeps_the_verified_answer(planner, fake_llm, monkeypatch):
    planner.append({"metrics": ["doanh_thu_thuan"], "time_range": OCT})
    verdicts(fake_llm, monkeypatch, RuntimeError("mất mạng"))
    r, = chat("doanh thu thuần tháng 10/2026 của cả công ty")
    assert r["type"] == "result" and r["audit"]["ok"] and r["critic"]["verdict"] == "error"


def test_wrong_without_reason_is_not_trusted():
    assert critic.normalize_verdict({"verdict": "wrong", "problem": ""})["verdict"] == "ok"
    assert critic.normalize_verdict({"verdict": "maybe"})["verdict"] == "ok"


def test_fast_path_answers_are_not_judged(fake_llm):
    r, = chat("Doanh thu thuần theo miền tháng 9/2026")
    assert r["path"] == "fast" and not getattr(fake_llm, "critiques", []) and fake_llm.usage["calls"] == 0


def test_critic_can_be_switched_off_for_ablation(planner, fake_llm, settings):
    settings({"critic": False})
    planner.append({"metrics": ["doanh_thu_thuan"], "dimensions": ["kenh"]})
    r, = chat("doanh thu thuần ở Hà Nội tháng 10/2026 theo kênh bán")
    assert not r["plan"].get("filters") and not getattr(fake_llm, "critiques", [])
