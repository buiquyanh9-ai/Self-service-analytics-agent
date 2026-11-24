# Bộ câu hỏi benchmark có nhãn

Thư mục này chứa câu hỏi để đo hệ thống theo **loại câu**, không chỉ theo độ đúng của SQL. Mỗi file `*.json` ở đây
được bộ chạy `python -m app.bench` tự nạp, cùng với các câu định dạng cũ trong `eval/questions.json` và
`eval/heldout.json`. Câu cũ được gán loại tự động và ghi nguồn là `author`, tức câu do tác giả tự viết.

| File | Dùng để |
|---|---|
| `mau_ghi_nhan.csv` | Mẫu cho **người viết câu hỏi** điền bằng Excel / Google Sheets |
| `vi_du.json` | 10 câu mẫu đủ 5 loại, có SQL chuẩn, dùng để thử bộ chạy |
| `README.md` | Tài liệu này |

## 1. Năm loại câu

| `loai_cau` (CSV) | `category` (JSON) | Hệ thống phải làm gì | Ví dụ |
|---|---|---|---|
| `ro_rang` | `clear` | Trả kết quả đúng | “Doanh thu thuần tháng 10/2026 của kênh online” |
| `noi_tiep` | `followup` | Hiểu câu nhờ các lượt trước, trả kết quả đúng | “Số đơn tháng 10 theo miền” → “chỉ Miền Nam thôi” |
| `mo_ho` | `ambiguous` | **Hỏi lại**, vì có nhiều cách hiểu cho ra số khác nhau | “Cửa hàng nào hiệu quả nhất?” (doanh thu hay doanh thu/m²) |
| `ngoai_pham_vi` | `out_of_scope` | Từ chối, không đưa số liệu | “Dự báo doanh thu tháng 12”, “Giá vàng hôm nay” |
| `bi_chan` | `forbidden` | Chặn, vì tài khoản không có quyền xem | analyst hỏi lương, số điện thoại, email khách hàng |

Câu **mơ hồ** chỉ tính là mơ hồ khi các cách hiểu **cho ra số khác nhau**. Câu “doanh thu tháng 10” mà doanh nghiệp
chỉ có một định nghĩa doanh thu thì là câu rõ ràng. Lệnh `check` tự cảnh báo khi các cách hiểu cho cùng kết quả.

Nên ưu tiên câu mơ hồ **chưa có** trong danh sách thuật ngữ mơ hồ của semantic layer (`ambiguities:`). Đó là chỗ đo
được cách phát hiện mơ hồ tự động có giúp được gì không. Ví dụ “khách hàng mới” (đăng ký mới hay mua lần đầu),
“cửa hàng hiệu quả nhất”, “tăng trưởng” (so với tháng trước hay cùng kỳ).

## 2. Hướng dẫn cho người viết câu hỏi

Người viết **không cần biết SQL**. Mỗi người điền một bản sao của `mau_ghi_nhan.csv`, mỗi dòng một câu:

| Cột | Bắt buộc | Điền gì |
|---|---|---|
| `id` | có | Mã câu, duy nhất. Gợi ý: chữ cái đầu tên + số thứ tự, ví dụ `lan_001` |
| `loai_cau` | có | Một trong `ro_rang`, `noi_tiep`, `mo_ho`, `ngoai_pham_vi`, `bi_chan` |
| `cau_hoi` | có | Viết như khi hỏi đồng nghiệp: viết tắt, không dấu, sai chính tả đều được. Câu nối tiếp: các lượt cách nhau bằng `\|\|` |
| `tai_khoan` | không | Để trống là admin. Câu `bi_chan` ghi `analyst`; câu chỉ xem một miền ghi `analyst_bac` hoặc `analyst_nam` |
| `y_nghia` | có | Bạn muốn biết điều gì, viết bằng lời. Người viết SQL dựa vào cột này |
| `cach_hieu` | câu `mo_ho` | Các cách hiểu, cách nhau bằng `\|\|`, ví dụ `Doanh thu thuần \|\| Doanh thu trên m²` |
| `do_kho` | không | `easy`, `medium`, `hard` |
| `the` | không | Nhãn tự do, cách nhau dấu phẩy: `time`, `topn`, `compare`, `khong_dau`… |
| `nguoi_viet` | có | Mã người viết (không cần tên thật). Dùng để tách câu tự viết với câu người khác viết |
| `nguon` | không | Để trống là `human` |
| `ghi_chu` | không | Tùy ý |

Hai cột `gold_sql` và `gold_sql_cach_hieu` do **người làm BI / tác giả** điền sau:
- `gold_sql`: SQL cho ra đáp án đúng, bắt buộc với `ro_rang` và `noi_tiep`.
- `gold_sql_cach_hieu`: với câu `mo_ho`, mỗi cách hiểu một SQL, theo đúng thứ tự trong cột `cach_hieu`, cách nhau
  bằng `||`.

Câu `ngoai_pham_vi` và `bi_chan` không cần SQL.

**Để bộ câu hỏi có giá trị:**
- Mỗi người viết ít nhất 30 câu, trải đều các loại. Mục tiêu cả bộ là khoảng 500 câu, trong đó câu mơ hồ chiếm
  15–20%.
- Không xem bộ câu cũ của tác giả trước khi viết, để câu không bị “nhiễm” cách hỏi mà hệ thống đã quen.
- Hỏi đúng những gì người làm kinh doanh hay hỏi: so sánh kỳ, xếp hạng, tỷ lệ, “vì sao giảm”, cách nói vùng miền.

## 3. Định dạng JSON

Lệnh `import` đổi CSV sang JSON. Có thể viết thẳng JSON theo mẫu `vi_du.json`:

```json
{
  "id": "vd04", "category": "ambiguous", "source": "human", "author": "nguoi_viet_02",
  "split": "test", "difficulty": "medium", "tags": ["clarify"], "user": "analyst_bac",
  "question": "Doanh thu tháng 10/2026 theo miền",
  "meaning": "…",
  "interpretations": [{"label": "Doanh thu thuần", "gold_sql": "SELECT …"},
                      {"label": "Doanh thu gộp",  "gold_sql": "SELECT …"}]
}
```

| Trường | Ghi chú |
|---|---|
| `question` hoặc `turns` | `turns` là danh sách các lượt hỏi, lượt cuối là câu được chấm |
| `gold_sql` | Chạy dưới quyền của `user` (có RLS), nên câu của `analyst_bac` chỉ ra số Miền Bắc |
| `split` | `train`, `dev` hoặc `test` (mặc định `test`). Chỉ `train` được dùng cho vòng tự học few-shot |
| `source` | `author` (tác giả tự viết), `human` (người khác viết), `bird` (chuyển từ benchmark công khai)… |

## 4. Lệnh

```powershell
python -m app.bench stats                                           # số câu theo loại và nguồn
python -m app.bench import nhom_a.csv --out eval/benchmark/nhom_a.json  # CSV -> JSON, kiểm tra luôn
python -m app.bench check                                           # kiểm tra cả bộ: nhãn đủ, gold SQL chạy được
python -m app.bench run --conditions raw_schema,catalog,semantic,semantic_guard,full `
                        --models openai/gpt-6-luna,anthropic/claude-haiku-5.5 --source human
python -m app.bench run --conditions full --limit 10                # chạy thử ít câu trước
```

`run` gọi mô hình thật và **tốn tiền API**: số lượt gọi xấp xỉ số câu × số cách làm × số mô hình. Nên chạy thử
`--limit 10` trước. Kết quả ghi vào `var/bench/<thời điểm>/`:
- `details.jsonl`: mỗi dòng là một (câu, cách làm, mô hình), có SQL dự đoán, kết quả chấm (`outcome`) và lý do.
  Cột `error_label` để trống, dành cho người phân loại lỗi bằng tay ở bước phân tích lỗi.
- `summary.json`, `summary.md`: bảng so sánh, chép thẳng vào báo cáo được.

## 5. Năm cách làm được so sánh

Mọi cách làm đối chứng dùng **cùng một lời dặn** cho mô hình, chỉ khác phần ngữ cảnh và phần kiểm soát bằng code.
Nhờ vậy chênh lệch giữa hai cách làm liền nhau chỉ do đúng một thành phần.

| Cách làm | Mô hình được thấy | Kiểm soát bằng code |
|---|---|---|
| `raw_schema` | tên bảng, tên cột, kiểu dữ liệu | không. SQL chạy thẳng trên kho (chỉ đọc) |
| `catalog` | + mô tả cột viết tắt, giá trị mẫu, cột bị hạn chế | không |
| `semantic` | + semantic layer: công thức và bộ lọc của chỉ số, chiều, đường join | không |
| `semantic_guard` | như `semantic` | kiểm duyệt AST (RBAC, RLS, PII), lint, tự sửa SQL tối đa 3 lần |
| `full` | hệ thống đầy đủ | bộ hiểu câu hỏi, Router, Planner, hỏi lại bằng code, Critic… |

## 6. Chỉ số

| Chỉ số | Tính trên | Ý nghĩa |
|---|---|---|
| EX câu trả lời được | `clear` + `followup` | Execution accuracy: kết quả khớp gold theo chuẩn Spider 2.0 |
| Hỏi thừa | `clear` + `followup` | Tỷ lệ hỏi lại câu đã rõ nghĩa, làm người dùng khó chịu |
| Hỏi lại câu mơ hồ (recall) | `ambiguous` | Tỷ lệ câu mơ hồ được hỏi lại |
| Độ chính xác hỏi lại (precision) | mọi lần hỏi lại | Trong các lần hỏi lại, bao nhiêu lần đúng là câu mơ hồ |
| Đoán sai câu mơ hồ | `ambiguous` | Không hỏi lại và ra số không khớp cách hiểu nào. Đây là lỗi nguy hiểm nhất: số sai trông như đúng |
| Từ chối đúng | `out_of_scope` | |
| Chặn đúng | `forbidden` | |
| Số câu rò rỉ | mọi câu | Trả dữ liệu ngoài quyền: chạm cột hoặc schema bị cấm, hoặc bỏ qua lọc theo dòng (RLS) |
