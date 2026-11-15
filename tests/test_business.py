"""Câu hỏi nghiệp vụ hằng ngày viết theo cách người dùng thường nói (không theo mẫu), ở chế độ bộ luật offline.

Lỗi gốc: "so sánh doanh thu miền Bắc và miền Nam" cộng gộp hai miền thành một số; "tháng nào doanh thu cao nhất" trả tổng
cả năm; "những cửa hàng chưa đạt kế hoạch" trả cả 43 cửa hàng; "trung bình mỗi cửa hàng bán được bao nhiêu" trả "Có 43 cửa
hàng"; "sản phẩm nào hay bị trả lại nhất", "tháng nào bán tệ nhất", "tình hình kinh doanh tháng 10" thì "chưa hiểu".
"""
from __future__ import annotations

import datetime as dt

from app.agent import get_agent
from app.catalog import get_catalog
from app.governance import UserCtx
from app.store import get_store

ADMIN = UserCtx("admin", "admin", {})


def ask(*questions, pick=None):
    """Hỏi lần lượt trong một hội thoại; pick = lựa chọn khi trợ lý hỏi lại ở câu cuối."""
    store, agent = get_store(), get_agent()
    cid = store.create_conversation(ADMIN.username, "test")
    try:
        r = None
        for q in questions:
            r = agent.ask(ADMIN, cid, q, eval_mode=True)
        if pick is not None:
            assert r["type"] == "clarification", r.get("text")
            r = agent.ask(ADMIN, cid, "", clarification={"id": r["clarification"]["id"], "option": pick},
                          eval_mode=True)
        return r
    finally:
        store.delete_conversation(cid)


def col(r, name):
    i = r["columns"].index(name)
    return [row[i] for row in r["rows"]]


# ------------------------------------------------------------------ so sánh giữa các giá trị
def test_two_values_of_one_dimension_are_split_not_summed():
    r = ask("so sánh doanh thu thuần miền Bắc và miền Nam quý 3 năm 2026")
    assert r["type"] == "result" and r["plan"]["dimensions"] == ["vung"]
    assert sorted(col(r, "vung")) == ["Miền Bắc", "Miền Nam"]


def test_entity_comparison_keeps_the_period_after_so_voi():
    r = ask("doanh thu thuần của Apple so với Samsung năm 2026")
    assert r["plan"]["time_range"]["start"] == "2026-01-01" and not r["plan"].get("compare")
    assert sorted(col(r, "thuong_hieu")) == ["Apple", "Samsung"]


def test_weekend_versus_weekday():
    r = ask("doanh thu thuần cuối tuần so với ngày thường tháng 10/2026")
    assert r["plan"]["dimensions"] == ["cuoi_tuan"] and not r["plan"].get("compare")
    assert sorted(col(r, "cuoi_tuan")) == ["Cuối tuần", "Ngày thường"]


def test_comparison_without_period_uses_last_full_month():
    r = ask("doanh thu thuần ngành hàng Tivi giảm bao nhiêu so với cùng kỳ")
    anchor = dt.date.fromisoformat(get_catalog().anchor_date())
    last = (anchor.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    assert r["plan"]["time_range"]["start"] == last.isoformat() and "cùng kỳ" in r["plan"]["compare"]["label"]


# ------------------------------------------------------------------ xếp hạng
def test_which_month_ranks_months():
    r = ask("tháng nào doanh thu thuần cao nhất năm 2025")
    assert r["plan"]["dimensions"] == ["thang"] and r["plan"]["limit"] == 1 and len(r["rows"]) == 1
    assert "cao nhất" in r["text"]


def test_ranking_periods_drops_the_unfinished_current_month():
    r = ask("tháng nào doanh thu thuần thấp nhất năm 2026")
    anchor = dt.date.fromisoformat(get_catalog().anchor_date())
    assert r["plan"]["time_range"]["end"] == anchor.replace(day=1).isoformat()
    assert any("chưa so được" in a for a in r["assumptions"])


def test_lowest_is_not_called_leader():
    r = ask("cửa hàng nào doanh thu thuần thấp nhất tháng 10/2026")
    assert r["text"].startswith("Thấp nhất là")


def test_superlative_with_words_in_between():
    r = ask("kho nào đang tồn nhiều hàng nhất")
    assert r["plan"]["limit"] == 1 and r["plan"]["order_by"][0]["direction"] == "desc"


def test_ranking_without_criterion_asks_then_answers():
    r = ask("miền nào bán kém nhất tháng 10/2026", pick=0)
    assert r["type"] == "result" and r["plan"]["metrics"] == ["doanh_thu_thuan"]
    assert r["plan"]["order_by"][0]["direction"] == "asc" and len(r["rows"]) == 1


def test_top_customers_and_staff_by_code():
    r = ask("Top 10 khách hàng có doanh thu thuần cao nhất năm 2025")
    assert r["plan"]["dimensions"] == ["khach_hang"] and len(r["rows"]) == 10 and None not in col(r, "khach_hang")
    r = ask("nhân viên nào bán giỏi nhất tháng 10/2026", pick=0)
    assert r["plan"]["dimensions"] == ["nhan_vien"] and len(r["rows"]) == 1 and r["rows"][0][0]


def test_customer_context_is_not_a_breakdown():
    r = ask("doanh thu thuần từ khách hàng ở Hà Nội tháng 10/2026")
    assert "khach_hang" not in r["plan"]["dimensions"] and r["plan"]["filters"][0]["dimension"] == "tinh_kh"


# ------------------------------------------------------------------ thiếu chỉ số: hỏi lại thay vì "chưa hiểu"
def test_missing_metric_is_asked_back():
    r = ask("so sánh miền Bắc với miền Nam quý 3 năm 2026")
    assert r["type"] == "clarification" and "Doanh thu thuần" in r["clarification"]["options"]
    r = ask("so sánh miền Bắc với miền Nam quý 3 năm 2026", pick=0)
    assert r["type"] == "result" and sorted(col(r, "vung")) == ["Miền Bắc", "Miền Nam"]


def test_sales_question_is_not_a_store_count():
    r = ask("trung bình mỗi cửa hàng bán được bao nhiêu một ngày")
    assert r["type"] == "clarification" and r.get("intent") != "lookup"
    r = ask("trung bình mỗi cửa hàng bán được bao nhiêu một ngày", pick=0)
    assert r["plan"]["dimensions"] == ["chi_nhanh"] and r["plan"]["per"] == "ngay" and len(r["rows"]) > 1


def test_off_topic_still_refused():
    r = ask("thời tiết Hà Nội tháng 10 thế nào")
    assert r["type"] == "info" and "thời tiết" in r["text"]


# ------------------------------------------------------------------ trả hàng, tồn kho, kế hoạch
def test_returns_phrasing():
    r = ask("sản phẩm nào hay bị trả lại nhất")
    assert r["plan"]["metrics"] == ["so_phieu_tra"] and r["plan"]["dimensions"] == ["san_pham"] and len(r["rows"]) == 1
    r = ask("lý do trả hàng phổ biến nhất")
    assert r["plan"]["metrics"] == ["so_phieu_tra"] and r["plan"]["dimensions"] == ["ly_do_tra"]


def test_stores_below_plan():
    r = ask("Những cửa hàng nào chưa đạt kế hoạch tháng 10/2026")
    rates = col(r, "ty_le_dat_kh")
    assert r["plan"]["metrics"][0] == "ty_le_dat_kh" and rates and all(x < 100 for x in rates)
    allr = ask("tỷ lệ đạt kế hoạch tháng 10/2026 theo cửa hàng")
    assert len(rates) < len(allr["rows"])
    over = ask("cửa hàng nào vượt kế hoạch tháng 10/2026")
    assert over["plan"]["metrics"] == ["ty_le_dat_kh"] and all(x >= 100 for x in col(over, "ty_le_dat_kh"))


def test_discontinued_products_still_in_stock():
    r = ask("Những sản phẩm đã ngừng kinh doanh nhưng vẫn còn tồn kho tại ngày chốt gần nhất")
    assert {"dimension": "trang_thai_kd", "op": "in", "values": ["Ngừng KD"]} in r["plan"]["filters"]
    assert r["rows"] and all(x > 0 for x in col(r, "ton_kho_sl"))
    assert any("ngày chốt gần nhất" in a for a in r["assumptions"])


# ------------------------------------------------------------------ trung bình, tổng quan, lời khuyên
def test_average_per_day():
    total = ask("doanh thu thuần tháng 10/2026")["rows"][0][0]
    avg = ask("doanh thu thuần trung bình mỗi ngày tháng 10/2026")
    assert avg["plan"]["per"] == "ngay" and abs(avg["rows"][0][0] - total / 31) < 1
    assert "TB mỗi ngày" in avg["columns_meta"][0]["label"]


def test_business_overview():
    r = ask("tình hình kinh doanh tháng 10/2026 thế nào")
    assert r["type"] == "result" and {"doanh_thu_thuan", "so_don", "loi_nhuan_gop"} <= set(r["plan"]["metrics"])
    assert r["plan"]["dimensions"] == []            # "tình" không bị hiểu thành "tỉnh"


def test_advice_question_gives_supporting_numbers():
    r = ask("có nên nhập thêm hàng Samsung không?")
    assert r["type"] == "result" and {"so_luong_ban", "ton_kho_sl"} <= set(r["plan"]["metrics"])
    assert any("không quyết định thay bạn" in a for a in r["assumptions"])
