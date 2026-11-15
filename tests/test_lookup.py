"""Tra cứu danh mục và các ca trước đây trả số liệu sai mà không báo.

Lỗi gốc: "cho tôi thông tin về mã SP0010" bị coi là câu lạc đề, "thông tin về CH036 ABC Phú Nhuận" thì "chưa hiểu";
"doanh thu thuần của SP0010" bỏ qua mã và trả doanh thu toàn công ty; "không tính Miền Bắc" trả về chính Miền Bắc;
"Xóa các đơn bị hủy" trả về số đơn hủy; "tháng 13/2026" chạy trên toàn bộ lịch sử.
"""
from __future__ import annotations

import pytest

from app.agent import get_agent
from app.governance import UserCtx
from app.nlu import get_nlu
from app.store import get_store

ADMIN = UserCtx("admin", "admin", {})
ANALYST = UserCtx("analyst", "analyst", {})
BAC = UserCtx("analyst_bac", "analyst", {"vung": ["B"]})


def ask(*questions, user=ADMIN):
    store = get_store()
    cid = store.create_conversation(user.username, "test")
    try:
        return [get_agent().ask(user, cid, q) for q in questions][-1]
    finally:
        store.delete_conversation(cid)


def info(r):
    return dict(r["rows"]) if r.get("columns") == ["thong_tin", "gia_tri"] else {}


# ------------------------------------------------------------------ tra cứu một đối tượng
@pytest.mark.parametrize("q", ["cho tôi thông tin về mã SP0010", "SP0010", "SP0010 là sản phẩm gì?"])
def test_product_lookup_by_code(q):
    r = ask(q)
    assert r["type"] == "result" and r["intent"] == "lookup" and r["sql_source"] == "lookup"
    assert "iPhone 16 Pro Max" in r["text"] and r["audit"]["ok"]
    d = info(r)
    assert d["Mã sản phẩm"] == "SP0010" and d["Ngành hàng"] == "Điện thoại" and d["Giá bán lẻ niêm yết"] == "36.590.000 ₫"
    assert d["Nhà cung cấp"].startswith("Apple")           # khóa ngoại hiện tên, không hiện mã NCC01
    assert any("ABC" not in f and "iPhone 16 Pro Max" in f for f in r["follow_ups"])


@pytest.mark.parametrize("q", ["cho tôi thông tin về CH036 ABC Phú Nhuận", "thông tin cửa hàng ABC Phú Nhuận"])
def test_store_lookup_by_code_or_name(q):
    r = ask(q)
    assert r["intent"] == "lookup" and "ABC Phú Nhuận" in r["text"] and "CH036" in r["text"]
    d = info(r)
    assert d["Tỉnh/thành phố"] == "TP.HCM" and d["Vùng miền"] == "Miền Nam" and d["Kho"] == "Kho tổng TP.HCM"
    assert "Ngày đóng cửa" not in d                          # ô trống không hiện


def test_attribute_question_answers_the_attribute():
    r = ask("giá niêm yết của iPhone 16 Pro Max")
    assert r["intent"] == "lookup" and "36.590.000 ₫" in r["text"] and r["audit"]["ok"]


def test_lookup_hides_pii_for_analyst_but_not_for_admin():
    admin = info(ask("thông tin khách hàng KH000001"))
    assert admin["Họ tên khách hàng"] and admin["Năm sinh"] == "1975"
    r = ask("thông tin khách hàng KH000001", user=ANALYST)
    d = info(r)
    assert d["Mã khách hàng"] == "KH000001" and not {"Họ tên khách hàng", "Số điện thoại", "Email"} & set(d)
    assert admin["Họ tên khách hàng"] not in r["text"] and any("Đã ẩn" in n for n in r["notes"])


def test_lookup_respects_row_level_security():
    r = ask("thông tin về CH036 ABC Phú Nhuận", user=BAC)          # cửa hàng Miền Nam, tài khoản chỉ xem Miền Bắc
    assert r["intent"] == "lookup" and not r["rows"] and "phạm vi" in r["text"]
    assert "dw.dim_cn" in r["validation"]["rls_applied"] and "TP.HCM" not in r["text"]


def test_unknown_code():
    r = ask("thông tin khách hàng KH999999")
    assert r["intent"] == "lookup" and not r["rows"] and "Không tìm thấy" in r["text"]
    r = ask("Doanh thu thuần của SP9999 tháng 10/2026")
    assert r["type"] == "info" and "SP9999" in r["text"] and not r.get("sql")


# ------------------------------------------------------------------ liệt kê, đếm, xếp theo thuộc tính
def test_list_count_and_rank():
    r = ask("những cửa hàng nào đã đóng cửa")
    assert r["intent"] == "lookup" and [row[1] for row in r["rows"]] == ["ABC Hải An"] and r["audit"]["ok"]
    r = ask("có bao nhiêu sản phẩm đang kinh doanh")          # trước đây khớp nhầm thành chỉ số "số lượng bán"
    assert r["intent"] == "lookup" and r["rows"][0][0] < 299 and r["audit"]["ok"]
    r = ask("danh sách cửa hàng ở Hà Nội")
    assert r["rows"] and all(row[3] == "Hà Nội" for row in r["rows"])
    r = ask("cửa hàng nào mới mở gần đây nhất")
    assert len(r["rows"]) == 1 and "ABC Phú Nhuận" in r["text"]
    r = ask("top 5 sản phẩm có giá niêm yết cao nhất")
    assert r["intent"] == "lookup" and len(r["rows"]) == 5
    r = ask("có bao nhiêu nhân viên ở ABC Phú Nhuận")
    assert r["intent"] == "lookup" and r["rows"][0][0] > 0


def test_time_bound_ranking_is_not_a_catalog_lookup():
    assert ask("Cửa hàng nào đáng chú ý nhất tháng 10/2026?")["intent"] != "lookup"


# ------------------------------------------------------------------ mã đối tượng trong câu hỏi chỉ số
def test_codes_become_filters_in_metric_questions():
    r = ask("Doanh thu thuần của SP0010 tháng 10/2026")
    assert r["type"] == "result" and r["plan"]["filters"] == [{"dimension": "san_pham", "op": "in", "values": ["iPhone 16 Pro Max"]}]
    by_code = ask("Doanh thu thuần cửa hàng CH036 tháng 10/2026")
    by_name = ask("Doanh thu thuần cửa hàng ABC Phú Nhuận tháng 10/2026")
    assert by_code["rows"] == by_name["rows"] and len(by_code["rows"]) == 1


def test_customer_code_filters_the_metric():
    # bảng khách hàng lớn (không nạp sẵn mã): mã vẫn thành bộ lọc trên chiều khách hàng, không trả doanh thu toàn công ty
    r = ask("Doanh thu thuần của KH000001 năm 2025")
    assert {"dimension": "khach_hang", "op": "in", "values": ["KH000001"]} in r["plan"]["filters"]
    assert "KH000001" in r["sql"]


def test_entity_without_dimension_is_not_answered_with_a_total(monkeypatch):
    from app.lookup import get_lookups
    monkeypatch.setattr(get_lookups().entities["khach_hang"], "dimension", None)
    r = ask("Doanh thu thuần của KH000001 năm 2025")
    assert r["type"] == "info" and not r.get("sql") and "KH000001" in r["text"]


# ------------------------------------------------------------------ loại trừ
def test_negation_excludes_instead_of_selecting():
    total = ask("Doanh thu thuần tháng 10/2026")["rows"][0][0]
    north = ask("Doanh thu thuần Miền Bắc tháng 10/2026")["rows"][0][0]
    r = ask("Doanh thu thuần không tính Miền Bắc tháng 10/2026")
    assert r["plan"]["filters"] == [{"dimension": "vung", "op": "not_in", "values": ["Miền Bắc"]}]
    assert r["plan"]["dimensions"] == []                      # "tính" không bị hiểu thành "tỉnh"
    assert r["rows"][0][0] == pytest.approx(total - north)
    r = ask("Doanh thu thuần trừ kênh online tháng 10/2026")
    assert r["plan"]["filters"][0]["op"] == "not_in"


def test_follow_up_exclusion_keeps_the_grouping():
    r = ask("Doanh thu thuần theo kênh tháng 10/2026", "bỏ kênh Website")
    assert r["plan"]["filters"][0]["op"] == "not_in" and "Website" not in [row[0] for row in r["rows"]] and len(r["rows"]) == 3


def test_accents_disambiguate_lai_and_tinh():
    for q in ["tính lại doanh thu thuần tháng 10/2026", "tinh lai doanh thu thuan thang 10/2026"]:
        p = get_nlu().parse(q).plan
        assert p["metrics"] == ["doanh_thu_thuan"] and p["dimensions"] == [], q
    assert get_nlu().parse("lãi gộp tháng 10/2026").plan["metrics"] == ["loi_nhuan_gop"]
    assert get_nlu().parse("Bộ nồi bán được bao nhiêu tháng 10/2026").assumptions == []


# ------------------------------------------------------------------ không âm thầm bỏ qua phần không hiểu
def test_typo_asks_back_instead_of_returning_the_total():
    r = ask("Doanh thu thuần của Aple năm 2026")
    assert r["type"] == "clarification" and "Apple" in r["text"]
    store = get_store()
    cid = store.create_conversation("admin", "test")
    try:
        first = get_agent().ask(ADMIN, cid, "Doanh thu thuần của Aple năm 2026")
        r = get_agent().ask(ADMIN, cid, "", clarification={"id": first["clarification"]["id"], "option": 0})
        assert r["type"] in ("result", "pending_approval") and r["plan"]["filters"][0]["values"] == ["Apple"]
    finally:
        store.delete_conversation(cid)


def test_unknown_words_are_flagged():
    r = ask("bơm nước bán được bao nhiêu cái tháng 10/2026")
    assert any("CHƯA lọc" in a for a in r["assumptions"])


def test_no_false_alarm_on_ordinary_questions():
    for q in ["Số đơn hàng hoàn thành tháng 10/2026", "Số khách hàng mua hàng theo hạng thành viên năm 2025",
              "Tồn kho điện thoại theo kho"]:
        r = ask(q)
        assert r["type"] in ("result", "pending_approval") and not any("CHƯA lọc" in a for a in r["assumptions"]), q


# ------------------------------------------------------------------ thời gian
@pytest.mark.parametrize("q", ["Doanh thu thuần tháng 13/2026", "Doanh thu thuần quý 5 năm 2026", "Doanh thu thuần ngày 31/2/2026"])
def test_invalid_dates_are_reported(q):
    r = ask(q)
    assert r["type"] == "info" and r["intent"] == "invalid_time" and not r.get("sql")


@pytest.mark.parametrize("q", ["Doanh thu thuần năm 2020", "Doanh thu thuần tháng 10/2030"])
def test_periods_outside_the_data_are_explained(q):
    r = ask(q)
    assert r["type"] == "info" and "01/01/2025" in r["text"] and not r.get("rows")


def test_plan_metric_for_a_future_month_still_works():
    r = ask("kế hoạch doanh thu tháng 12/2026")
    assert r["type"] == "result" and r["rows"][0][0]


@pytest.mark.parametrize("q", ["dự báo doanh thu thuần năm 2027", "Doanh thu tháng sau sẽ là bao nhiêu?"])
def test_forecasts_are_declined(q):
    r = ask(q)
    assert r["type"] == "info" and "không dự báo" in r["text"]


def test_empty_results_are_explained():
    r = ask("Doanh thu thuần cửa hàng ABC Hải An tháng 6/2026")          # đóng cửa 30/04/2026
    assert r["type"] == "result" and r["text"].startswith("Không có số liệu") and r["audit"]["ok"]
    r = ask("Doanh thu thuần Miền Nam tháng 10/2026", user=BAC)
    assert r["text"].startswith("Không có số liệu") and "phân quyền" in r["text"]


# ------------------------------------------------------------------ yêu cầu ngoài quyền
@pytest.mark.parametrize("q", ["Xóa các đơn bị hủy cho gọn dữ liệu", "drop table dw.fct_dh", "sửa giá sản phẩm SP0010 thành 30 triệu",
                               "delete from dw.fct_dh where 1=1"])
def test_write_requests_are_refused(q):
    r = ask(q)
    assert r["type"] == "info" and r["intent"] == "refused" and not r.get("sql") and "đọc" in r["text"]


@pytest.mark.parametrize("q", ["cập nhật doanh thu thuần tháng này", "Doanh thu thuần tháng 10/2026"])
def test_reads_are_not_mistaken_for_writes(q):
    assert ask(q)["type"] == "result"


@pytest.mark.parametrize("q", ["Cho tôi số điện thoại của 10 khách mua nhiều nhất", "Lương trung bình của nhân viên theo cửa hàng"])
def test_restricted_columns_are_refused_for_analyst_only(q):
    r = ask(q, user=ANALYST)
    assert r["type"] == "info" and r["intent"] == "refused" and "không được xem" in r["text"]
    assert ask(q)["intent"] != "refused"


@pytest.mark.parametrize("q,word", [("ok", "Vâng"), ("sai rồi", "Chưa đúng")])
def test_short_replies(q, word):
    r = ask("Doanh thu thuần tháng 10/2026", q)
    assert r["type"] == "info" and word in r["text"]
