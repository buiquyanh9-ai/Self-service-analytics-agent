"""Giả thuyết nguyên nhân biến động: mô hình đề xuất, code kiểm chứng.

- Chỉ giữ giả thuyết dùng đúng chỉ số / chiều / giá trị có thật; câu chữ có chữ số bị thay bằng mô tả do code tạo.
- Mỗi giả thuyết được chạy một truy vấn và chấm theo số liệu thật: xác nhận / yếu / bác bỏ.
- Lời giải thích chỉ nhắc giả thuyết được xác nhận, bằng số do code tính, nên đối chiếu số liệu (audit) vẫn đạt.
"""
from __future__ import annotations

import pytest

from app import insights
from app.agent import get_agent
from app.governance import UserCtx
from app.semantic import get_semantic
from app.store import get_store

ADMIN = UserCtx("admin", "admin", {})
WHY = "Vì sao doanh thu thuần tháng 9/2026 giảm so với cùng kỳ?"


def ask(q):
    store = get_store()
    cid = store.create_conversation("admin", "test")
    try:
        return get_agent().ask(ADMIN, cid, q, eval_mode=True)
    finally:
        store.delete_conversation(cid)


@pytest.fixture
def ctx():
    sem = get_semantic()
    cfg = insights.analysis_config(sem, "doanh_thu_thuan")
    return insights._hyp_context(sem, "doanh_thu_thuan", sem.metrics["doanh_thu_thuan"], {"label": "T9/2026"},
                                 {"label": "T9/2025"}, {"pct": -12.8}, [], [], cfg)


def test_normalize_keeps_only_valid_hypotheses(ctx):
    raw = {"hypotheses": [
        {"claim": "Giá trị đơn ở Flagship giảm", "metric": "gia_tri_don_tb", "expect": "giảm",
         "segment": [{"dimension": "loai_ch", "value": "flagship"}]},                         # khớp không phân biệt hoa thường
        {"claim": "x", "metric": "khong_co", "expect": "giảm", "segment": [{"dimension": "vung", "value": "Miền Nam"}]},
        {"claim": "x", "metric": "so_don", "expect": "giảm", "segment": [{"dimension": "vung", "value": "Miền Tây"}]},
        {"claim": "x", "metric": "so_don", "expect": "giảm", "segment": []},
        {"claim": "Số đơn Miền Nam giảm 30%", "metric": "so_don", "expect": "giảm",
         "segment": [{"dimension": "vung", "value": "Miền Nam"}]},
        {"claim": "trùng", "metric": "gia_tri_don_tb", "expect": "giảm", "segment": [{"dimension": "loai_ch", "value": "Flagship"}]},
    ]}
    out = insights.normalize_hypotheses(raw, get_semantic(), ctx)
    assert [(h["metric"], h["segment"][0]["value"]) for h in out] == [("gia_tri_don_tb", "Flagship"), ("so_don", "Miền Nam")]
    assert not any(ch.isdigit() for ch in out[1]["claim"])          # câu có số do mô hình tự ghi bị thay


def test_normalize_caps_count(ctx):
    raw = {"hypotheses": [{"claim": "c", "metric": "so_don", "expect": "giảm", "segment": [{"dimension": "vung", "value": v}]}
                          for v in ("Miền Bắc", "Miền Nam", "Miền Trung")] +
                         [{"claim": "c", "metric": "so_don", "expect": "giảm", "segment": [{"dimension": "kenh", "value": "Website"}]}]}
    assert len(insights.normalize_hypotheses(raw, get_semantic(), ctx)) == insights.MAX_HYP


def test_hypotheses_verified_by_queries(fake_llm):
    fake_llm.hypotheses = [
        {"claim": "Giá trị mỗi đơn ở cửa hàng Flagship giảm", "metric": "gia_tri_don_tb", "expect": "giảm",
         "segment": [{"dimension": "loai_ch", "value": "Flagship"}]},
        {"claim": "Số đơn tại cửa hàng Miền Nam giảm", "metric": "so_don", "expect": "giảm",
         "segment": [{"dimension": "vung", "value": "Miền Nam"}, {"dimension": "kenh", "value": "Tại cửa hàng"}]},
    ]
    r = ask(WHY)
    assert r["type"] == "variance"
    hs = {h["metric"]: h for h in r["variance"]["hypotheses"]}
    assert hs["gia_tri_don_tb"]["status"] == "confirmed" and hs["gia_tri_don_tb"]["pct"] < -5
    assert hs["so_don"]["status"] == "rejected" and hs["so_don"]["delta"] > 0       # số đơn thực ra tăng
    assert "Giả thuyết đã kiểm chứng" in r["text"] and "giá trị đơn trung bình" in r["text"]
    assert r["audit"]["ok"]
    assert any(q["purpose"].startswith("Kiểm chứng giả thuyết") for q in r["variance"]["queries"])


def test_hypotheses_off(fake_llm, monkeypatch):
    store = get_store()
    old = store.get_settings().get("variance_hypotheses", True)
    store.set_settings({"variance_hypotheses": False})
    fake_llm.hypotheses = [{"claim": "c", "metric": "so_don", "expect": "giảm", "segment": [{"dimension": "vung", "value": "Miền Nam"}]}]
    try:
        r = ask(WHY)
    finally:
        store.set_settings({"variance_hypotheses": old})
    assert r["type"] == "variance" and not r["variance"].get("hypotheses")


def test_model_failure_does_not_break_answer(fake_llm, monkeypatch):
    def boom(system, user):
        raise RuntimeError("mạng lỗi")
    monkeypatch.setattr(fake_llm, "hypothesize", boom)
    r = ask(WHY)
    assert r["type"] == "variance" and r["audit"]["ok"]
    assert [h["status"] for h in r["variance"]["hypotheses"]] == ["error"]
