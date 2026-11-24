# 02 · Hướng dẫn sử dụng cho người mới

Không cần biết SQL. Nếu hệ thống chưa chạy, xem [03 · Chạy trên máy cá nhân](03-chay-local.md) trước.

## 1. Đăng nhập

Mở địa chỉ của hệ thống (chạy trên máy mình thì là <http://127.0.0.1:8000>). Màn hình đăng nhập có sẵn các nút tài
khoản dùng thử; bấm vào một nút là vào.

| Tài khoản | Mật khẩu | Thấy gì |
|---|---|---|
| `admin` | `admin123` | Mọi dữ liệu và các trang quản trị |
| `analyst` | `analyst123` | Dữ liệu toàn quốc, không thấy cột nhạy cảm (số điện thoại, lương) |
| `analyst_bac` | `analyst123` | Như trên, nhưng chỉ dữ liệu Miền Bắc |
| `analyst_nam` | `analyst123` | Như trên, nhưng chỉ dữ liệu Miền Nam |

Doanh nghiệp mẫu thứ hai (Giao vận Apex) có `admin`, `analyst` và `analyst_trung` (chỉ Miền Trung), cùng mật khẩu.

Nên bắt đầu bằng `analyst`.

## 2. Màn hình chính Sola

![Màn hình chào](screenshots/welcome.png)

- **Giữa:** “Hỏi dữ liệu. Hiểu doanh nghiệp.”, quả cầu dữ liệu có thể kéo để xoay và bốn thẻ: **Tra cứu số liệu**,
  **So sánh và xếp hạng**, **Tìm nguyên nhân**, **Hỏi định nghĩa**. Bấm thẻ hoặc câu trong dòng **Thử hỏi** để hỏi ngay.
- **Dưới:** ô gõ câu hỏi và nút **Chỉ số** mở từ điển. **Enter** gửi, **Shift + Enter** xuống dòng.
- **Trái:** tên doanh nghiệp, nút **Hội thoại mới**, danh sách **Gần đây**, mục **Khám phá** (các trang ở mục 6).
  Nút cạnh logo Sola thu gọn/mở rộng thanh bên; trạng thái mô hình, tài khoản và nút đăng xuất nằm phía dưới.
- **Góc trên bên phải:** trạng thái nguồn dữ liệu và ngày cập nhật, nút **Từ điển**, nút **Cách dùng** (dấu hỏi),
  nút đổi nền sáng/tối. Di chuột lên trạng thái để xem thông tin mô hình.
- **Điện thoại:** bấm nút **Mở menu** ở thanh trên để mở thanh bên; một số nhãn ở thanh trên được ẩn để dành chỗ.

**Từ điển chỉ số** trong **Khám phá**, **Từ điển** trên thanh trên, **Chỉ số** trong ô hỏi và **Khám phá từ điển**
trên màn hình chào đều mở cùng ngăn bên. Có thể tìm chỉ số, đọc định nghĩa/bộ phận phụ trách và bấm **Hỏi thử**.

## 3. Hỏi thế nào

Một câu hỏi tốt nêu đủ ba thứ: **số gì**, **thời gian nào**, **chia theo gì**.

| Muốn | Ví dụ (doanh nghiệp Astra) |
|---|---|
| Tra một con số | Doanh thu thuần theo miền tháng 10/2026 |
| So sánh | Doanh thu thuần quý 3/2026 so với cùng kỳ năm trước |
| Xếp hạng | Top 5 cửa hàng có doanh thu thuần cao nhất tháng 10/2026 |
| Tìm nguyên nhân | Vì sao doanh thu thuần tháng 9/2026 giảm so với cùng kỳ? |
| Hỏi định nghĩa | Doanh thu thuần là gì? |
| Hỏi về trợ lý | Bạn là ai? Bạn làm được gì? |

Hỏi nối tiếp được: sau câu đầu, gõ *“còn Miền Nam thì sao?”* hoặc bấm một gợi ý ở dòng **Bạn có thể hỏi tiếp**. Trợ lý nhớ ngữ
cảnh trong cùng cuộc hội thoại.

## 4. Đọc câu trả lời

![Câu trả lời](screenshots/chat.png)

| Phần | Ý nghĩa |
|---|---|
| **Mình hiểu là** | Các nhãn chỉ số, thời gian, cách chia và bộ lọc đã dùng. **Đọc dòng này trước.** Bấm tên chỉ số để xem định nghĩa; nếu khác ý bạn, hỏi lại rõ hơn |
| **Biểu đồ** / **Bảng dữ liệu** | Chuyển tab để xem kết quả. Nếu có biểu đồ thì tab này mở trước; nút tải CSV xuất các dòng kết quả đang có |
| Lời giải thích | Tóm tắt điểm đáng chú ý trong bảng |
| **Đã đối chiếu … số liệu** | Các con số trong lời giải thích đã được tính lại từ kết quả; chi tiết nằm trong Cách tính |
| **Định nghĩa chuẩn của công ty** | Con số dùng định nghĩa chung hoặc truy vấn mẫu đã xác nhận. Nếu ghi **Trợ lý tự viết truy vấn, nên xem cách tính** thì nên kiểm tra kỹ hơn |
| Lưu ý khi đọc kết quả | Điều cần biết về dữ liệu, ví dụ có vấn đề chất lượng đã biết trong kỳ đó |
| **Bạn có thể hỏi tiếp** | Gợi ý bấm được, giữ ngữ cảnh của cuộc hội thoại |
| **Cách tính** | Hộp thoại “Cách tính câu trả lời này”: định nghĩa và bộ phận phụ trách, kiểm tra, SQL; phần Chi tiết kỹ thuật có kế hoạch, chi phí, nguồn gốc cột, đối chiếu từng con số và các bước xử lý khi có |
| **Xem SQL** | Hộp thoại truy vấn chỉ đọc, bảng nguồn, dung lượng và kiểm tra; có nút **Sao chép SQL** và **Xem cách tính** |
| **Sao chép**, nút đánh giá | Sao chép lời giải thích hoặc gửi đánh giá **Kết quả đúng** / **Kết quả chưa đúng** |
| **Cảnh báo vàng** | Mô hình ngôn ngữ tạm thời lỗi. Câu hỏi nêu rõ chỉ số và thời gian vẫn được trả lời; câu khác hãy thử lại sau ít phút |

## 5. Ba tình huống hay gặp

**Trợ lý hỏi lại.** Một số từ có nhiều cách tính. Hỏi *“doanh thu tháng trước theo miền”*, trợ lý sẽ hỏi bạn muốn
doanh thu gộp hay thuần, vì hai phòng ban định nghĩa khác nhau. Chọn một; trợ lý nhớ lựa chọn trong cuộc hội thoại đó.

**Trợ lý chờ bạn duyệt.** Câu hỏi quét nhiều dữ liệu (ví dụ toàn bộ lịch sử theo từng cửa hàng) sẽ dừng lại. Bạn
xem SQL (có thể sửa), ước lượng dung lượng và số bước kiểm tra đạt, rồi bấm **Duyệt & chạy truy vấn** hoặc **Không chạy**.
SQL đã sửa được kiểm tra lại quyền và chi phí. Bạn cũng có thể thu hẹp thời gian rồi hỏi lại.

**Trợ lý từ chối.** Xảy ra khi câu hỏi cần dữ liệu ngoài quyền của tài khoản (cột nhạy cảm, miền khác), ngoài phạm
vi (dự báo, khuyến nghị đầu tư), hoặc yêu cầu sửa xóa dữ liệu. Hệ thống chỉ đọc.

## 6. Các trang trong mục Khám phá

Các trang dành riêng cho admin có biểu tượng khóa trong menu và dòng “Quản trị” ở đầu trang.

| Trang | Dùng để | Ai thấy |
|---|---|---|
| Từ điển chỉ số | Mở ngăn bên để tìm định nghĩa chỉ số và hỏi thử | Mọi người |
| Dữ liệu đầu vào | Xem từng dòng của các bảng trong kho | Mọi người |
| Chỉ số & bảng | Xem có những chỉ số, cách tính và bảng nào | Mọi người |
| Tự viết SQL | Người biết SQL tự viết truy vấn; hệ thống vẫn kiểm tra quyền và chi phí, tự sửa lỗi thường gặp | Mọi người |
| Lịch sử câu hỏi | Các câu đã hỏi và truy vấn đã chạy | Mọi người (admin thấy của tất cả) |
| Chất lượng dữ liệu | Vấn đề dữ liệu được phát hiện tự động và cách xử lý | Admin |
| Nguồn gốc dữ liệu | Mỗi bảng, mỗi cột được tạo từ nguồn nào | Admin |
| Đo độ chính xác | Chạy bộ câu hỏi kiểm tra (đúng số) và bộ kịch bản hội thoại có AI chấm (đúng ý), so sánh bật/tắt từng tính năng | Admin |
| Cài đặt | Mô hình ngôn ngữ, đường nhanh, đa nhiệm, tác tử kiểm tra, ngưỡng duyệt | Admin |

## 7. Thử trong 10 phút

Làm lần lượt trên doanh nghiệp Astra:

1. Đăng nhập `analyst`. Hỏi *“Doanh thu tháng trước theo miền”*. Trợ lý hỏi lại; chọn **thuần**.
2. Bấm gợi ý **Chia theo kênh bán** ở dòng **Bạn có thể hỏi tiếp**. Chuyển tab **Biểu đồ** / **Bảng dữ liệu**, thử tải CSV và mở **Cách tính**, **Xem SQL**.
3. Hỏi *“Doanh thu thuần theo cửa hàng”*. Trợ lý dừng chờ duyệt; xem SQL rồi bấm **Duyệt & chạy truy vấn**.
4. Đăng xuất, vào `analyst_bac`, hỏi *“Doanh thu thuần theo miền tháng 10/2026”*. Chỉ còn Miền Bắc.
5. Vào trang **Tự viết SQL**, chạy câu mẫu có sẵn. Hệ thống tự sửa tên cột sai và cảnh báo về điều kiện ngày. Thử
   các lệnh ở mục “thử xem hệ thống chặn thế nào”.
6. Đăng nhập `admin`. Hỏi *“Vì sao doanh thu thuần tháng 9/2026 giảm so với cùng kỳ?”*. Trợ lý phân rã theo từng
   chiều và kết luận đây là lỗi dữ liệu (đơn giá bị ghi sai đơn vị ở một nhóm cửa hàng).
7. Hỏi *“Vì sao doanh thu thuần Miền Trung tháng 6/2026 giảm?”*. Lần này là nguyên nhân kinh doanh (hết hàng).

## 8. Mẹo và lưu ý

- Câu trả lời lạ thì đọc lại các nhãn “Mình hiểu là” để kiểm tra trợ lý có hiểu khác ý bạn không.
- Nêu thời gian cụ thể (*tháng 10/2026*) cho kết quả nhanh và ít phải duyệt hơn.
- “Tháng trước”, “hôm nay” tính theo **ngày mới nhất có dữ liệu**, hiện ở thanh trên, không theo ngày trên máy bạn.
- Khi chưa cấu hình khóa mô hình, giao diện ghi **Chưa có mô hình ngôn ngữ**. Chỉ câu hỏi được bộ luật hiểu trọn vẹn mới được trả lời; câu khác cần mô hình.
- Dữ liệu trong hai doanh nghiệp mẫu là giả lập; đừng dùng con số ở đây cho việc thật.
