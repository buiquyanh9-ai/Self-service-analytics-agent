"""
Mở kho DuckDB trên trình duyệt bằng DuckDB UI (http://localhost:4213): duyệt mọi schema/bảng/cột, xem dữ liệu,
viết SQL tự do. Chỉ ĐỌC kho nên chạy song song với bot được. Lần đầu cần mạng để tải extension `ui`.

    python warehouse/view.py                          # doanh nghiệp mẫu Astra
    python warehouse/view.py --workspace giaovan_apex  # doanh nghiệp Apex

Ctrl+C để tắt. Lưu ý: khi đang mở, nút "Áp dụng quy tắc sửa" (cần ghi vào kho) sẽ báo lỗi khóa file; tắt UI rồi bấm lại.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time
import webbrowser

import duckdb

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser(description="Xem kho DuckDB trên trình duyệt (DuckDB UI).")
    ap.add_argument("--workspace", default="", help="tên workspace (bỏ trống = doanh nghiệp mẫu ở gốc dự án)")
    a = ap.parse_args()
    ws_root = ROOT / "workspaces" / a.workspace if a.workspace else ROOT
    db = ws_root / "var" / "warehouse.duckdb"
    if not db.exists():
        raise SystemExit(f"Không thấy {db}. Chạy run.ps1 (hoặc python warehouse/generate.py) để sinh dữ liệu trước.")
    con = duckdb.connect(str(db), read_only=True)
    con.execute("INSTALL ui; LOAD ui")
    url = con.execute("CALL start_ui_server()").fetchone()[0].split(" at ")[-1].strip()
    print(f"Đang mở {db.relative_to(ROOT)} tại {url}  (Ctrl+C để tắt)")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        con.execute("CALL stop_ui_server()")
        con.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
