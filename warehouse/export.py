"""
Xuất kho DuckDB ra từng file CSV riêng (mỗi bảng một file, chia thư mục theo schema) để xem bằng Excel / VS Code,
kèm file mục lục DANH_SACH_BANG.md mô tả bảng và cột (lấy từ catalog.yml nếu có).

Chỉ ĐỌC kho (read_only), không sửa file .duckdb nên không ảnh hưởng tới bot. Bản xuất nằm trong var/export/
(đã có trong .gitignore). Sinh lại dữ liệu hoặc áp dụng quy tắc sửa thì chạy lại lệnh này để cập nhật bản xuất.

    python warehouse/export.py                          # doanh nghiệp mẫu Astra -> var/export/
    python warehouse/export.py --workspace giaovan_apex  # doanh nghiệp Apex -> workspaces/giaovan_apex/var/export/
    python warehouse/export.py --rows 1000              # mỗi bảng chỉ lấy 1000 dòng đầu (xem nhanh)
    python warehouse/export.py --schema raw dw          # chỉ xuất một số schema
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
import time

import duckdb
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOM = "\ufeff".encode("utf-8")  # để Excel đọc đúng tiếng Việt
LAYER_NOTE = {
    "raw": "Dữ liệu nguồn, còn bẩn đúng như khi nạp vào",
    "ref": "Danh mục tham chiếu (seed)",
    "stg": "Dữ liệu đã chuẩn hóa (staging)",
    "dw": "Kho phục vụ phân tích — bot truy vấn tầng này",
    "mart": "Kho phục vụ phân tích — bot truy vấn tầng này",
}


def load_catalog(ws_root: pathlib.Path) -> dict:
    path = ws_root / "config" / "catalog.yml"
    if not path.exists():
        return {}
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("tables", {}) or {}


def md_cell(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def export(ws_root: pathlib.Path, out: pathlib.Path, rows: int | None, schemas: list[str] | None) -> None:
    db = ws_root / "var" / "warehouse.duckdb"
    if not db.exists():
        raise SystemExit(f"Không thấy {db}. Chạy run.ps1 (hoặc python warehouse/generate.py) để sinh dữ liệu trước.")
    try:
        con = duckdb.connect(str(db), read_only=True)
    except duckdb.Error as e:
        raise SystemExit(f"Không mở được kho ở chế độ đọc: {e}\nNếu đang chạy generate/build, đợi xong rồi thử lại.")

    tables = con.execute("""
        SELECT table_schema, table_name FROM information_schema.tables
        WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'main') ORDER BY 1, 2""").fetchall()
    if schemas:
        tables = [t for t in tables if t[0] in schemas]
    if not tables:
        raise SystemExit("Không có bảng nào để xuất.")

    catalog = load_catalog(ws_root)
    # --out có thể trỏ tới thư mục đang chứa tài liệu khác: chỉ ghi các file xuất, không xóa cả thư mục.
    out.mkdir(parents=True, exist_ok=True)

    index = [f"# Danh sách bảng — `{db.relative_to(ROOT)}`", "",
             f"Xuất lúc {time.strftime('%d/%m/%Y %H:%M')}" + (f", mỗi bảng tối đa {rows:,} dòng." if rows else ", đầy đủ mọi dòng."),
             "File CSV mã hóa UTF-8 có BOM, mở thẳng bằng Excel được. Đây là BẢN SAO để xem; bot vẫn đọc file .duckdb.", ""]
    detail: list[str] = []
    cur_schema = None
    t_all = time.time()
    for schema, table in tables:
        fq = f"{schema}.{table}"
        n = con.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()[0]
        cols = con.execute("""SELECT column_name, data_type FROM information_schema.columns
                              WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position""", [schema, table]).fetchall()
        dest_dir = out / schema
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / f"{table}.csv"
        tmp = dest.with_suffix(".tmp")
        limit = f" LIMIT {int(rows)}" if rows else ""
        con.execute(f"""COPY (SELECT * FROM "{schema}"."{table}"{limit}) TO '{tmp.as_posix()}' (HEADER, DELIMITER ',')""")
        with open(dest, "wb") as f, open(tmp, "rb") as src:
            f.write(BOM)
            shutil.copyfileobj(src, f, 1 << 20)
        tmp.unlink()
        size_mb = dest.stat().st_size / 1e6
        print(f"  {fq:<32} {n:>10,} dòng  {len(cols):>3} cột  {size_mb:7.1f} MB")

        meta = catalog.get(fq, {}) or {}
        if schema != cur_schema:
            cur_schema = schema
            index += ["", f"## `{schema}` — {LAYER_NOTE.get(schema, '')}", "",
                      "| Bảng | Số dòng | Số cột | Mô tả | File |", "|---|---:|---:|---|---|"]
        index.append(f"| [`{table}`](#{schema}{table}) | {n:,} | {len(cols)} | {md_cell(meta.get('description'))} "
                     f"| [{schema}/{table}.csv]({schema}/{table}.csv) |")

        col_meta = meta.get("columns", {}) or {}
        detail += ["", f'<a id="{schema}{table}"></a>', f"### `{fq}`", ""]
        if meta.get("description"):
            detail.append(md_cell(meta["description"]))
        if meta.get("grain"):
            detail.append(f"Mỗi dòng là: `{meta['grain']}`.")
        detail += ["", "| Cột | Kiểu | Mô tả |", "|---|---|---|"]
        for c, typ in cols:
            cm = col_meta.get(c) or {}
            desc = cm.get("description", "") if isinstance(cm, dict) else ""
            detail.append(f"| `{c}` | {typ} | {md_cell(desc)} |")

    (out / "DANH_SACH_BANG.md").write_text("\n".join(index + ["", "---", "", "## Chi tiết cột"] + detail) + "\n",
                                           encoding="utf-8")
    con.close()
    print(f"\nXong {len(tables)} bảng trong {time.time() - t_all:.1f} s -> {out}")
    print(f"Mục lục: {out / 'DANH_SACH_BANG.md'}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Xuất kho DuckDB ra CSV từng bảng để xem.")
    ap.add_argument("--workspace", default="", help="tên workspace (bỏ trống = doanh nghiệp mẫu ở gốc dự án)")
    ap.add_argument("--rows", type=int, default=None, help="chỉ lấy N dòng đầu mỗi bảng")
    ap.add_argument("--schema", nargs="*", help="chỉ xuất các schema này (vd: raw dw)")
    ap.add_argument("--out", default=None, help="thư mục xuất (mặc định <workspace>/var/export)")
    a = ap.parse_args(argv)
    ws_root = ROOT / "workspaces" / a.workspace if a.workspace else ROOT
    if not (ws_root / "var").is_dir():
        raise SystemExit(f"Không thấy workspace: {ws_root}")
    out = pathlib.Path(a.out) if a.out else ws_root / "var" / "export"
    export(ws_root, out, a.rows, a.schema)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
