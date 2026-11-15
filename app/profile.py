"""
Hồ sơ doanh nghiệp (config/profile.yml): tên, ngành, quy ước kho dữ liệu, câu hỏi mẫu và lời tự giới thiệu của trợ lý.

Đổi bộ file cấu hình (profile + catalog + semantic layer + policies) là hệ thống phục vụ một doanh nghiệp khác mà không sửa mã.
Lời tự giới thiệu được dựng bằng code từ cấu hình đang nạp, không do LLM viết, nên không bịa tính năng hay dữ liệu.
"""
from __future__ import annotations

import re

from . import config
from .textutil import norm

DEFAULTS = {
    "company": {"name": "doanh nghiệp của bạn", "short_name": "", "industry": "", "description": ""},
    "assistant": {"name": "Trợ lý dữ liệu", "language": "Vietnamese"},
    "warehouse": {"engine": "DuckDB", "schema": "dw", "schema_note": "", "anchor": None, "notes": [], "core_tables": []},
    "domain_pack": None,      # gói kiểm tra theo ngành viết bằng code (vd retail_abc); không khai báo thì chỉ chạy phần tổng quát
    "quality": {"skip_drift_columns": []},     # cột không đem kiểm trôi lệch phân phối (vd trạng thái có vòng đời)
    "console_example": "",    # câu SQL mẫu ở trang tự viết SQL; để trống thì biên dịch từ chỉ số đầu tiên
    "console_tries": [],      # vài lệnh để người dùng thử xem hệ thống chặn thế nào
    "out_of_scope": ["dự báo tương lai hay khuyến nghị đầu tư", "sửa, xóa dữ liệu (chỉ đọc)",
                     "trả dữ liệu bị hạn chế theo vai trò của bạn", "câu hỏi ngoài dữ liệu của doanh nghiệp"],
    "sample_questions": [],
}


def get_profile() -> dict:
    raw = config.profile()
    out = {}
    for key, default in DEFAULTS.items():
        val = raw.get(key)
        out[key] = {**default, **(val or {})} if isinstance(default, dict) else (val or default)
    return out


# ---------------------------------------------------------------------- nhận diện câu hỏi "bạn là gì / hỗ trợ gì"
_SUBJ = r"(ban|may|he thong|ung dung|tro ly|chatbot|bot|cong cu|phan mem|app)( nay)?"
_INTRO = re.compile(
    r"^(xin chao|chao( ban)?|hello|hi|hey|help|tro giup|giup( toi| minh)?( voi)?|huong dan( su dung)?)$"
    rf"|(?<!gia )(?<!nguoi )(?<!hang )\b{_SUBJ} la (ai|gi|cai gi)\b"
    rf"|\b{_SUBJ} (co the |co )?(lam|giup|ho tro)( duoc)?( cho (toi|minh))?( nhung)? (gi|viec gi|nhung gi)\b"
    r"|\b(ho tro|giup|lam) duoc (nhung )?(gi|viec gi)\b"
    r"|\b(co|gom) (nhung )?(tinh nang|chuc nang) (gi|nao)\b"
    r"|\b(cach|huong dan) (su dung|dung)\b"
    r"|\b(toi|minh) (co the|nen) hoi (nhung )?gi\b|\bhoi duoc (nhung )?gi\b"
    # hỏi xác nhận / hỏi về bản thân trợ lý: "bạn là trợ lý ... đúng không", "bạn có phải là AI", "bạn tên gì".
    # Neo ở đầu câu vì "bán" bỏ dấu cũng là "ban" ("giá bán là ...").
    rf"|^(vay |the |co phai )?{_SUBJ} (co )?(phai )?la (mot |1 )?(tro ly|chatbot|bot|ai|cong cu|he thong|phan mem|ung dung|nguoi|may)\b"
    rf"|^(vay |the )?{_SUBJ} (co )?phai (la )?"
    rf"|^{_SUBJ} ten (la )?gi\b|^ten (cua )?{_SUBJ} la gi\b|\bai (tao ra|lam ra|phat trien|xay dung) {_SUBJ}\b"
    rf"|^{_SUBJ} (truy cap|doc|xem|biet|co|hieu) (duoc )?(nhung )?(du lieu|kho du lieu|bang|so lieu) (nao|gi)\b"
    rf"|^{_SUBJ} (hoat dong|lam viec) (nhu the nao|the nao|ra sao)\b")
# "nó/đây là gì", "nó hỗ trợ gì": chỉ nhận khi gõ có dấu, vì không dấu thì "no la gi" trùng với "nợ là gì"
_INTRO_ACCENTED = re.compile(
    r"^\s*(nó|đây|cái này)\s+là\s+(ai|gì|cái gì)\b"
    r"|^\s*(nó|cái này)\s+(có thể\s+|có\s+)?(làm|giúp|hỗ trợ)(\s+được)?(\s+những)?\s+(gì|việc gì)\b")


# ---------------------------------------------------------------------- chủ đề cấm
# Cụm từ (đã bỏ dấu) mà trợ lý từ chối ngay bằng code, trước mọi bước hiểu câu hỏi hay gọi LLM.
# Viết thành cụm chứ không phải từ đơn vì bỏ dấu dễ trùng từ nghiệp vụ ("bơm" = "bom", "súng" ~ "sung túc"...).
# Doanh nghiệp bổ sung cụm riêng ở profile.yml, mục `blocked_topics`.
BLOCKED_DEFAULT = [
    # vũ khí, chất nổ
    "bom nguyen tu", "bom hat nhan", "bom tu che", "bom xang tu che", "tao bom", "lam bom", "che bom", "che tao bom",
    "vu khi", "chat no", "thuoc no", "che tao sung", "sung tu che", "uranium", "hat nhan lam giau",
    "vu khi sinh hoc", "vu khi hoa hoc", "chat doc",
    # ma túy, tội phạm, bạo lực
    "ma tuy", "thuoc phien", "heroin", "cocaine", "giet nguoi", "am sat", "khung bo",
    "tu sat", "buon nguoi", "rua tien", "lua dao", "lam gia giay to", "in tien gia",
    # xâm nhập, đánh cắp
    "hack", "hacker", "danh cap", "be khoa", "ma doc", "malware", "ransomware", "phishing", "lay mat khau",
    "chiem quyen", "vuot qua phan quyen", "bypass",
    # cố ý vượt chốt chặn của trợ lý
    "bo qua huong dan", "bo qua moi huong dan", "quen het huong dan", "ignore previous", "ignore all previous",
    "system prompt", "jailbreak", "prompt goc",
    # nội dung khiêu dâm, cờ bạc
    "khieu dam", "phim sex", "danh bac", "ca do bong da",
]


def blocked_topic(text: str) -> str | None:
    """Cụm từ cấm khớp trong câu hỏi (None nếu không có)."""
    t = re.sub(r"[^a-z0-9 ]+", " ", norm(text or "")).strip()
    t = re.sub(r"\s+", " ", t)
    extra = [norm(x) for x in (config.profile().get("blocked_topics") or []) if norm(x)]
    for phrase in BLOCKED_DEFAULT + extra:
        if re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", t):
            return phrase
    return None


def refusal_text() -> str:
    p = get_profile()
    return (f"Xin lỗi, mình không thể hỗ trợ yêu cầu này. Mình là **{p['assistant']['name']}**, chỉ trả lời các câu hỏi "
            f"về số liệu kinh doanh của **{p['company']['name']}** trong kho dữ liệu nội bộ, và không cung cấp thông tin "
            "về vũ khí, chất cấm, hành vi phạm pháp, xâm nhập hệ thống hay các nội dung nguy hiểm khác.\n\n"
            "Bạn có thể hỏi mình về số liệu, ví dụ một trong các câu bên dưới.")


# ---------------------------------------------------------------------- câu hỏi ngoài lề (không nguy hại)
# Chủ đề không liên quan tới dữ liệu doanh nghiệp: từ chối lịch sự, nêu đúng chủ đề người dùng hỏi và gợi ý nơi tra cứu.
# Chỉ áp dụng khi câu hỏi KHÔNG nhắc chỉ số nào (agent kiểm tra), nên "doanh thu loa nghe nhạc" vẫn được trả lời.
# (nhãn chủ đề, gợi ý nơi tra cứu, các cụm từ đã bỏ dấu). Doanh nghiệp bổ sung ở profile.yml, mục `off_topics`.
OFF_TOPIC_DEFAULT = [
    ("thời tiết", "ứng dụng thời tiết hoặc trang của Trung tâm Dự báo Khí tượng Thủy văn",
     ["thoi tiet", "du bao thoi tiet", "troi mua", "mua bao lon", "con bao so", "nang nong", "ret dam"]),
    ("thể thao", "các trang tin thể thao",
     ["bong da", "da bong", "da banh", "tran dau", "cau thu", "doi tuyen", "world cup", "ngoai hang anh", "premier league",
      "champions league", "v league", "sea games", "olympic", "bong ro", "tennis", "quan vot", "ai vo dich", "ket qua tran"]),
    ("giá vàng, chứng khoán, tỷ giá", "trang của ngân hàng hoặc công ty chứng khoán",
     ["gia vang", "chung khoan", "co phieu", "vn index", "vnindex", "ty gia", "gia usd", "bitcoin", "tien ao", "crypto"]),
    ("tin tức, thời sự", "các báo điện tử",
     ["tin tuc", "thoi su", "chinh tri", "bau cu", "tong thong", "chien tranh", "bao chi hom nay"]),
    ("ẩm thực, nấu ăn", "các trang công thức nấu ăn",
     ["nau an", "cach nau", "mon an", "cong thuc nau", "an gi", "quan an", "nha hang ngon", "bun bo", "an pho"]),
    ("phim ảnh, âm nhạc, giải trí", "các ứng dụng giải trí",
     ["xem phim", "phim hay", "bo phim", "bai hat", "ca si", "dien vien", "nghe nhac gi", "game hay", "choi game"]),
    ("chuyện vui, thơ văn", None,
     ["ke chuyen", "chuyen cuoi", "truyen cuoi", "lam tho", "bai tho", "hat cho", "noi dua"]),
    ("du lịch", "các trang du lịch",
     ["du lich", "dia diem choi", "di choi o dau", "khach san", "ve may bay"]),
    ("tử vi, xổ số", None,
     ["tu vi", "xo so", "xs mb", "ket qua xo so", "cung hoang dao", "boi bai", "phong thuy"]),
    ("sức khỏe, y tế", "bác sĩ hoặc cơ sở y tế",
     ["benh gi", "uong thuoc", "trieu chung", "dau dau", "giam can", "suc khoe"]),
    ("lập trình, bài tập, dịch thuật", "công cụ chuyên dụng cho việc đó",
     ["viet code", "lap trinh", "giai toan", "bai tap", "dich sang", "dich giup", "viet van", "viet email", "viet bai", "email xin", "don xin nghi"]),
    ("kiến thức chung", "công cụ tìm kiếm",
     ["thu do cua", "dan so viet nam", "lich su viet nam", "o dau tren the gioi", "ai phat minh"]),
]


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", norm(text or ""))).strip()


def off_topic(text: str) -> dict | None:
    """Chủ đề ngoài lề mà câu hỏi nhắc tới: {'label', 'where'} hoặc None."""
    t = _plain(text)
    extra = [(x.get("label"), x.get("where"), x.get("phrases") or []) for x in (config.profile().get("off_topics") or [])
             if isinstance(x, dict) and x.get("label")]
    for label, where, phrases in OFF_TOPIC_DEFAULT + extra:
        for ph in phrases:
            p = _plain(ph)
            if p and re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", t):
                return {"label": label, "where": where}
    return None


def off_topic_text(topic: dict | None) -> str:
    """Lời từ chối lịch sự cho câu ngoài lề; topic=None khi không rõ chủ đề nhưng câu không hỏi số liệu nào."""
    p = get_profile()
    name, company = p["assistant"]["name"], p["company"]["name"]
    if topic:
        head = (f"Cảm ơn bạn đã hỏi! Tiếc là chuyện **{topic['label']}** nằm ngoài phạm vi của mình. Mình là **{name}**, "
                f"chỉ làm việc với số liệu kinh doanh trong kho dữ liệu của **{company}**, nên không có thông tin đáng tin "
                "cậy để trả lời câu này.")
        if topic.get("where"):
            head += f" Bạn có thể xem ở {topic['where']} để có thông tin chính xác nhất nhé."
    else:
        head = (f"Câu này có vẻ không liên quan tới số liệu của **{company}**, nên mình xin phép không trả lời. "
                f"Mình là **{name}**, chỉ hỗ trợ tra cứu và phân tích dữ liệu kinh doanh của công ty.")
    return head + "\n\nNếu bạn cần số liệu, mình rất sẵn lòng giúp, ví dụ một trong các câu bên dưới."


# ---------------------------------------------------------------------- xã giao: cảm ơn, tạm biệt, hỏi thăm
_SMALLTALK = [
    (re.compile(r"^(cam on|cam on ban|cam on nhieu|thanks|thank you|thank|tks|ok cam on|hay qua|tot qua|tuyet voi)( nhe| nha| nhieu| a| ban| tro ly)*$"),
     "Không có gì ạ! Bạn cần thêm số liệu nào cứ hỏi mình nhé."),
    (re.compile(r"^(tam biet|bye|goodbye|hen gap lai|chao tam biet)( nhe| nha| ban| a)*$"),
     "Tạm biệt bạn! Khi cần tra cứu số liệu, cứ quay lại hỏi mình nhé."),
    (re.compile(r"^(ban|tro ly)( co)? khoe khong( a)?$|^(ban|tro ly) (the nao|sao roi|on khong)( a)?$"),
     "Mình vẫn sẵn sàng làm việc, cảm ơn bạn đã hỏi thăm! Hôm nay bạn muốn xem số liệu gì?"),
    (re.compile(r"^(ok|oke|okay|okie|uhm|um|vang|da vang|duoc|duoc roi|hieu roi|da hieu|ro roi|biet roi|nhat tri|dong y)"
                r"( nhe| nha| a| ban| cam on| roi)*$"),
     "Vâng! Bạn cần thêm số liệu nào cứ hỏi mình nhé."),
    (re.compile(r"^(sai roi|sai|sai bet|khong dung|chua dung|nham roi|khong phai|ket qua sai|so lieu sai|tra loi sai|"
                r"sai so lieu|khong chinh xac|chua chinh xac)( roi| nhe| nha| a| ban| oi)*$"),
     "Cảm ơn bạn đã báo! Bạn bấm “Chưa đúng” ngay dưới câu trả lời đó để mình ghi lại, rồi hỏi lại và nêu rõ chỉ số, "
     "khoảng thời gian, bộ lọc mong muốn (ví dụ “doanh thu thuần Miền Nam tháng trước”) để mình tính lại."),
]


# ---------------------------------------------------------------------- yêu cầu sửa/xóa dữ liệu, yêu cầu dự báo
# Trợ lý chỉ đọc kho dữ liệu. Câu ra lệnh sửa/xóa dữ liệu phải được từ chối rõ ràng, không được hiểu thành một câu hỏi
# số liệu ("Xóa các đơn bị hủy" từng trả về... số đơn hủy). Động từ phải đi cùng đối tượng dữ liệu, để "sửa lại theo tháng"
# (chỉnh câu hỏi) không bị chặn nhầm.
_WRITE = re.compile(
    r"^((hay|vui long|lam on|giup( toi| minh| em)?|ban|toi muon|minh muon|toi can|cho toi|can|nho|hay giup( toi| minh)?)\s+)*"
    # không có "cập nhật": "cập nhật doanh thu tháng này" thường là hỏi số mới nhất
    r"(xoa|xoa bo|sua|sua doi|chinh sua|ghi de|nhap them|them moi|chen|update|delete|insert|remove)\s+"
    r"((het|toan bo|tat ca|cac|nhung|mot so|lai|giup|di|bot|cho)\s+)*"
    r"(du lieu|ban ghi|dong|don|don hang|bang|gia|so lieu|khach hang|san pham|cua hang|thong tin|ma|nhan vien|kho|ton kho|"
    r"doanh thu|ke hoach|hoa don|phieu|record|row|table)\b"
    r"|\b(drop|truncate|alter)\s+(table|schema|view|database)\b|\bdelete\s+from\b|\binsert\s+into\b|\bupdate\s+\w+(\.\w+)?\s+set\b")


def write_request(text: str) -> bool:
    """Người dùng ra lệnh sửa/xóa/thêm dữ liệu (tiếng Việt hoặc câu lệnh SQL)."""
    return bool(_WRITE.search(_plain(text)))


def write_refusal_text() -> str:
    return ("Mình chỉ được **đọc** dữ liệu, không thể xóa, sửa hay thêm dữ liệu trong kho. Thay đổi dữ liệu phải làm qua "
            "quy trình quản trị có kiểm duyệt (Admin có trang Chất lượng dữ liệu để áp dụng quy tắc sửa và hoàn tác).\n\n"
            "Nếu bạn cần xem số liệu, mình rất sẵn lòng giúp, ví dụ một trong các câu bên dưới.")


_FORECAST = re.compile(r"\b(du bao|du doan|uoc doan|tien doan|forecast|se (la|dat|ban duoc|co|tang|giam|thu ve|len|xuong)|"
                       r"sap toi|ngay mai|tuan (sau|toi)|thang (sau|toi)|quy (sau|toi)|nam (sau|toi))\b")
_PLAN_WORDS = re.compile(r"\b(ke hoach|chi tieu|muc tieu|target|ngan sach)\b")


def forecast_request(text: str) -> bool:
    """Câu hỏi về số liệu tương lai (dự báo). Câu hỏi về kế hoạch/chỉ tiêu đã lập thì không tính."""
    t = _plain(text)
    return bool(_FORECAST.search(t)) and not _PLAN_WORDS.search(t)


def forecast_text(anchor: str, suggestion: str | None = None) -> str:
    import datetime as _dt
    d = _dt.date.fromisoformat(anchor)
    return (f"Mình chỉ trả lời số liệu **đã có** trong kho (đến ngày {d.day:02d}/{d.month:02d}/{d.year}), không dự báo số liệu "
            "tương lai, vì một con số dự báo cần mô hình và giả định mà kho dữ liệu không có.\n\n"
            + (f"Bạn có thể xem xu hướng các kỳ đã qua để tự đánh giá, ví dụ: “{suggestion}”."
               if suggestion else "Bạn có thể xem xu hướng các kỳ đã qua, ví dụ một trong các câu bên dưới."))


def smalltalk_reply(text: str) -> str | None:
    t = _plain(text)
    return next((reply for rx, reply in _SMALLTALK if rx.search(t)), None)


def is_intro_question(text: str) -> bool:
    """Người dùng đang hỏi trợ lý là gì, làm được gì, dùng thế nào (hoặc chỉ chào)."""
    raw = (text or "").strip().lower()
    if not raw or len(raw) > 120:
        return False
    if _INTRO_ACCENTED.search(raw):
        return True
    return bool(_INTRO.search(re.sub(r"[^a-z0-9 ]+", " ", norm(raw)).strip()))


# ---------------------------------------------------------------------- nội dung dựng từ cấu hình
def _semantic():
    from .semantic import get_semantic
    return get_semantic()


def sample_questions() -> list[str]:
    qs = list(get_profile()["sample_questions"])
    if qs:
        return qs
    sem = _semantic()        # doanh nghiệp chưa khai báo câu mẫu: tự gợi ý từ chỉ số và chiều phân tích
    out = []
    for mname, m in list(sem.metrics.items())[:6]:
        dim = next((d for dn, d in sem.dims.items() if m.get("model") and sem.dim_supported(m["model"], dn)), None)
        out.append(f"{m['label']} theo {dim['label'].lower()}" if dim else m["label"])
    if sem.metrics:
        out.append(f"{next(iter(sem.metrics.values()))['label']} là gì?")
    return out


def intro_text() -> str:
    """Lời tự giới thiệu: trợ lý là gì, hỗ trợ gì, có dữ liệu nào, không làm gì."""
    p = get_profile()
    c, a = p["company"], p["assistant"]
    sem = _semantic()
    who = f"Mình là **{a['name']}**, trợ lý phân tích dữ liệu tự phục vụ của **{c['name']}**"
    who += f" (ngành {c['industry']})." if c.get("industry") else "."
    if c.get("description"):
        who += " " + c["description"].strip()
    areas = [m.get("label") or n for n, m in sem.models.items()]
    metrics = [m["label"] for m in sem.metrics.values()]
    dims = [d["label"] for d in sem.dims.values()]
    lines = [
        who, "",
        "**Mình hỗ trợ bạn:**",
        "- Trả lời câu hỏi số liệu bằng tiếng Việt: mình lập kế hoạch truy vấn, sinh SQL theo đúng định nghĩa chỉ số, "
        "chạy trên kho dữ liệu và trả về bảng, biểu đồ, lời diễn giải. Mọi con số đều lấy từ kết quả truy vấn.",
        "- So sánh giữa các kỳ, xếp hạng, lọc và chia theo các chiều phân tích.",
        "- Phân tích vì sao một chỉ số tăng hoặc giảm.",
        "- Giải thích định nghĩa chỉ số, tìm bảng và cột trong kho dữ liệu, báo tình trạng chất lượng dữ liệu.",
        "- Hỏi lại khi câu hỏi mơ hồ; cho bạn xem và duyệt SQL trước khi chạy truy vấn lớn.",
        "",
    ]
    data = "**Dữ liệu hiện có:** " + ("; ".join(areas) if areas else "chưa khai báo") + "."
    if metrics:
        data += f" Có {len(metrics)} chỉ số đã định nghĩa, ví dụ: " + ", ".join(x.lower() for x in metrics[:8]) + "."
    if dims:
        data += " Phân tích được theo: " + ", ".join(x.lower() for x in dims[:10]) + "."
    lines += [data, "", "**Mình không làm:** " + "; ".join(p["out_of_scope"]) + ".", ""]
    qs = sample_questions()[:5]
    if qs:
        lines.append("Bạn có thể bắt đầu bằng một câu như:")
        lines += [f"- {q}" for q in qs]
    return "\n".join(lines).strip()


STARTERS = [   # (khóa, tiêu đề, mô tả một dòng) — bốn việc chính trợ lý làm được, hiện trên màn hình chào
    ("lookup", "Tra cứu số liệu", "Hỏi một chỉ số theo thời gian, khu vực, nhóm… và nhận bảng, biểu đồ, lời giải thích."),
    ("compare", "So sánh và xếp hạng", "So với kỳ trước, cùng kỳ năm ngoái, hoặc tìm top đầu, top cuối."),
    ("why", "Tìm nguyên nhân", "Hỏi vì sao một chỉ số tăng hay giảm; trợ lý phân rã theo từng chiều và kiểm định."),
    ("define", "Hỏi định nghĩa", "Một chỉ số được tính thế nào, do bộ phận nào chịu trách nhiệm."),
]
_DEFINE = re.compile(r"\b(la gi|dinh nghia|duoc tinh (nhu the nao|the nao|ra sao)|cong thuc|nghia la gi)\b")
_COMPARE = re.compile(r"\b(so voi|so sanh|cung ky|top|cao nhat|thap nhat|nhieu nhat|it nhat|ban chay|tot nhat|xep hang)\b")


def starter_groups(per_group: int = 3) -> list[dict]:
    """Câu hỏi mẫu của doanh nghiệp chia theo bốn việc trợ lý làm được. Nhóm nào trống thì tự đặt một câu từ semantic layer."""
    from .nlu import is_why_question
    groups = {k: [] for k, _, _ in STARTERS}
    for q in sample_questions():
        n = norm(q)
        key = "why" if is_why_question(q) else "define" if _DEFINE.search(n) else "compare" if _COMPARE.search(n) else "lookup"
        groups[key].append(q)
    sem = _semantic()
    first = next(iter(sem.metrics.items()), None)
    if first:
        name, m = first
        label = m["label"]
        dim = next((d for dn, d in sem.dims.items() if d.get("kind") != "time" and dn in sem.dims_for_metric(name)), None)
        auto = {"lookup": f"{label} theo {dim['label'].lower()}" if dim else label,
                "compare": f"Top 5 {dim['label'].lower()} có {label.lower()} cao nhất" if dim else f"{label} so với kỳ trước",
                "why": f"Vì sao {label.lower()} tháng trước thay đổi?",
                "define": f"{label} là gì?"}
        for k, qs in groups.items():
            if not qs:
                qs.append(auto[k])
    return [{"key": k, "title": t, "hint": hint, "questions": groups[k][:per_group]} for k, t, hint in STARTERS if groups[k]]


def console_example() -> str:
    """Câu SQL mẫu cho trang tự viết SQL: lấy từ hồ sơ nếu có, không thì biên dịch từ chỉ số đầu tiên của semantic layer."""
    p = get_profile()
    if p.get("console_example"):
        return p["console_example"].strip()
    sem = _semantic()
    for name in sem.metrics:
        dim = next((d for d, x in sem.dims.items() if x.get("kind") != "time" and d in sem.dims_for_metric(name)), None)
        try:
            return sem.compile({"metrics": [name], "dimensions": [dim] if dim else [], "limit": 10})["sql"]
        except Exception:
            continue
    return "SELECT 1"


def console_tries() -> list[str]:
    p = get_profile()
    if p.get("console_tries"):
        return list(p["console_tries"])
    a = anchor()
    return ([f"DROP TABLE {a[0]}"] if a else []) + ["SELECT * FROM read_csv('x.csv')"]


def public_summary() -> dict:
    """Thông tin hồ sơ gửi cho giao diện (màn hình chào, tiêu đề)."""
    p = get_profile()
    sem = _semantic()
    return {"company": p["company"]["name"], "short_name": p["company"].get("short_name") or "",
            "schema": p["warehouse"]["schema"], "console_example": console_example(), "console_tries": console_tries(),
            "industry": p["company"].get("industry") or "", "description": p["company"].get("description") or "",
            "assistant": p["assistant"]["name"], "intro": intro_text(), "out_of_scope": list(p["out_of_scope"]),
            "areas": [m.get("label") or n for n, m in sem.models.items()],
            "n_metrics": len(sem.metrics), "n_dimensions": sum(1 for d in sem.dims.values() if d.get("kind") != "time"),
            "metric_examples": [m["label"] for m in list(sem.metrics.values())[:8]],
            "starters": starter_groups(), "confusable_terms": confusable_terms()}


def metric_brief(name: str | None, hint: str | None = None) -> dict | None:
    """Giải thích ngắn một chỉ số cho người mới: tên, bộ phận dùng, định nghĩa bằng lời, dùng khi nào."""
    m = _semantic().metrics.get(name or "")
    if not m:
        return None
    return {"metric": name, "label": m.get("label") or name, "owner": m.get("owner") or "",
            "description": m.get("description") or "", "hint": hint or ""}


def _option_hint(metric: str | None) -> str:
    for amb in _semantic().cfg.get("ambiguities") or []:
        for o in amb.get("options") or []:
            if o.get("metric") == metric and o.get("hint"):
                return o["hint"]
    return ""


def confusable_terms() -> list[dict]:
    """Các từ có nhiều cách tính (semantic_layer.yml: ambiguities, loại không phải xếp hạng), để màn hình chào giải thích
    trước cho người mới, thay vì để họ gặp lần đầu khi trợ lý hỏi lại."""
    out = []
    for amb in _semantic().cfg.get("ambiguities") or []:
        if amb.get("requires_no_metric"):
            continue
        opts = [b for b in (metric_brief(o.get("metric"), o.get("hint")) for o in amb.get("options") or []) if b]
        if len(opts) >= 2:
            out.append({"id": amb["id"], "term": (amb.get("triggers") or [amb["id"]])[0], "options": opts})
    return out


def anchor() -> tuple[str, str] | None:
    a = get_profile()["warehouse"].get("anchor") or {}
    return (a["table"], a["column"]) if a.get("table") and a.get("column") else None


def serving_schema() -> str:
    """Schema phục vụ phân tích (tầng đã làm sạch) của doanh nghiệp đang nạp."""
    return get_profile()["warehouse"]["schema"]


def core_tables() -> list[str]:
    """Các bảng luôn kèm vào kết quả tìm schema cho LLM (bảng sự kiện chính và các chiều hay dùng)."""
    return list(get_profile()["warehouse"].get("core_tables") or [])


def domain_pack() -> str | None:
    return get_profile().get("domain_pack")
