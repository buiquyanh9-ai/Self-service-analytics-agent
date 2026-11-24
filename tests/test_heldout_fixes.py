"""Lỗi lộ ra khi chạy benchmark (python -m app.bench, 16/11/2026) trên tập heldout — test chống tái phát.

h22: mô hình trả order_by dạng object -> bước diễn giải đọc plan gốc, lỗi KeyError: 0
h23: chỉ số từ hai bảng sự kiện (doanh thu ÷ kế hoạch): cửa hàng có kế hoạch nhưng không bán -> NULL, bị lọc mất
h32/h18: danh sách trong ngoặc sau "theo <chiều>" bị hiểu thành bộ lọc
h08: "so sánh <kỳ A> với <kỳ B>" bị hiểu thành một khoảng chia theo tháng
h24: "(tính theo ngày khiếu nại)" bị hiểu thành chia theo từng ngày
"""
import datetime as dt

from app.agent import get_agent
from app.nlu import get_nlu
from app.semantic import get_semantic
from app.warehouse import get_warehouse


def plan(q):
    return get_nlu().parse(q, {}).plan


def test_enumerated_groups_in_parentheses_are_not_a_filter():
    p = plan("Kế hoạch doanh thu tháng 11/2026 theo loại cửa hàng (Flagship, Tiêu chuẩn, Mini)?")
    assert p["dimensions"] == ["loai_ch"] and p["filters"] == []


def test_partial_enumeration_in_parentheses_still_filters():
    p = plan("Doanh thu thuần theo miền (Miền Bắc, Miền Nam) tháng 10/2026")
    assert p["dimensions"] == ["vung"] and p["filters"] == [{"dimension": "vung", "op": "in", "values": ["Miền Bắc", "Miền Nam"]}]


def test_compare_two_named_periods():
    p = plan("So sánh số đơn hàng tháng 10/2026 với tháng 9/2026 theo loại cửa hàng giúp mình")
    assert p["dimensions"] == ["loai_ch"]
    assert p["time_range"]["start"] == "2026-10-01" and p["compare"]["start"] == "2026-09-01"


def test_naming_the_date_column_is_not_a_daily_breakdown():
    assert "ngay" not in plan("Doanh thu thuần tháng 10/2026 tính theo ngày giao hàng")["dimensions"]
    assert plan("Doanh thu thuần theo ngày tháng 10/2026")["dimensions"] == ["ngay"]


def test_stored_plan_is_the_normalized_one():
    s = {"plan": {"metrics": ["gia_tri_ton_kho"], "dimensions": ["nhom_sp"], "limit": 5,
                  "order_by": {"field": "gia_tri_ton_kho", "direction": "desc"},
                  "time_range": {"start": "2026-11-08", "end": "2026-11-09", "label": "08/11/2026"}},
         "notes": [], "anchor": dt.date(2026, 11, 9)}
    assert get_agent().n_sql_generator(s) == "optimizer"
    assert s["plan"]["order_by"] == [{"field": "gia_tri_ton_kho", "direction": "desc"}]


def test_store_with_plan_but_no_sales_counts_as_zero_attainment():
    c = get_semantic().compile({"metrics": ["ty_le_dat_kh"], "dimensions": ["chi_nhanh"],
                                "time_range": {"start": "2026-10-01", "end": "2026-11-01"},
                                "having": [{"metric": "ty_le_dat_kh", "op": "<", "value": 90}]})
    rows = get_warehouse().execute(c["sql"], max_rows=1000)["rows"]
    gold = get_warehouse().scalar(
        "WITH tt AS (SELECT ma_cn, SUM(tt - tien_tra) dt FROM dw.fct_ct_dh WHERE trang_thai = 'HOAN_THANH' "
        "AND ngay_dh >= DATE '2026-10-01' AND ngay_dh < DATE '2026-11-01' GROUP BY 1), "
        "kh AS (SELECT ma_cn, SUM(dt_ke_hoach) kh FROM dw.fct_kh_dt WHERE thang = DATE '2026-10-01' GROUP BY 1) "
        "SELECT COUNT(*) FROM kh LEFT JOIN tt USING (ma_cn) WHERE COALESCE(tt.dt, 0) * 100.0 / NULLIF(kh.kh, 0) < 90")
    assert len(rows) == gold
