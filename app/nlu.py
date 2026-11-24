"""
Bộ hiểu câu hỏi tiếng Việt OFFLINE (không cần LLM) -> kế hoạch truy vấn trên semantic layer.
Dùng khi chưa cấu hình Claude, hoặc làm phương án dự phòng khi LLM lỗi.

Các bước: chuẩn hóa (bỏ dấu) -> ý định -> thời gian (khoảng + so sánh) -> chỉ số (khớp cụm dài nhất)
-> giá trị thực thể (entity linking với danh mục giá trị trong kho) -> chiều phân tích -> top N / HAVING
-> hợp nhất ngữ cảnh hội thoại (câu hỏi nối tiếp) -> phát hiện mơ hồ (hỏi lại).
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field

from . import config
from .catalog import get_catalog
from .semantic import get_semantic
from .textutil import norm

# Cách gọi riêng của từng doanh nghiệp (tên gọi khác của giá trị, tiền tố "cửa hàng ...", thứ tự ưu tiên chiều...)
# không nằm trong mã: khai báo ở semantic_layer.yml, mục `nlu`. Mã chỉ giữ quy tắc chung của tiếng Việt.
# cụm từ sau khi bỏ dấu trùng với từ thông dụng khác ("hãng" = "hàng", "năm" = "nam", "sàn" = "sản"...)
AMBIGUOUS_NORM = {"hang", "nam", "san", "the", "ban", "ma", "mau", "kho hang"}
MONTH_WORDS = {"gieng": 1, "chap": 12, "muoi mot": 11, "muoi hai": 12, "muoi": 10}
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
FOLLOWUP_START = ("con ", "the con", "vay con", "vay ", "con neu", "neu ", "chi ", "loc ", "bo ", "them ", "so voi",
                  "chia theo", "theo ", "tach theo", "ve bieu do", "hien thi", "doi sang", "chuyen sang", "top ", "con lai")
# từ thuộc các cụm nối tiếp ở trên: không tính là "từ lạ" khi quyết định câu có phải câu hỏi nối tiếp hay không
FOLLOW_WORDS = ({w for p in FOLLOWUP_START for w in p.split()} | {"thi", "sao", "the", "nao"}
                # từ so sánh / biến động / biểu đồ đã được nhận diện ở bước khác (so sánh, tăng trưởng, cùng kỳ...)
                | set("so sanh voi vs doi chieu cung ky yoy mom tang truong giam bien dong thay bieu do tron cot duong "
                      "pie ty trong co cau chiem xu huong dien trend lay giu".split()))
GROUP_MARKERS = ("theo", "tung", "moi", "cac", "giua cac", "nhung", "phan theo", "chia theo", "tach theo", "tat ca cac")
# từ hư và từ hỏi: không mang ý nghĩa truy vấn nên không tính là "chưa hiểu"
FILLER = set("la bao nhieu co cua cac nhung theo trong tai o va nao gi ai duoc da dang se thi sao con cho toi minh biet hay xem "
             "voi ve den tu hien thi ket qua nhe a vui long giup xin ban ra di top cao thap nhat lon nho it so sanh luong "
             "cuoi dau het tinh".split())      # "cuối tháng 6", "đầu năm", "tính đến": đi kèm biểu thức thời gian đã hiểu
# từ tiếng Việt thường gặp trong câu hỏi số liệu mà không cần hiểu riêng (không coi là "từ lạ")
KNOWN_WORDS = set("hoan thanh vi giam tang chay ngung kinh doanh van chot gan thu mua hang khach don san pham tien so lieu "
                  "du lieu bao cao tong cong toan bo tung nhom loai muc gia tri phan tram ty le trung binh dem tinh rieng "
                  "chung lai luot bi duoc dang roi nua them lan dong ngay thang quy nam tuan hien nay vua moi qua truoc sau "
                  "len xuong dat vuot kem tot xau manh yeu nhanh cham the nhu lam sao khi nao bang nhau khac biet chenh lech "
                  "co cau ti trong xep hang thu tu dung sai chi tiet tom tat liet ke danh sach hay nen can muon".split())
NEGATION_RE = re.compile(r"(?<![a-z0-9])(khong tinh|khong ke|khong bao gom|khong gom|ngoai tru|loai tru|tru di|tru|bo qua|loai bo)"
                         r"(?![a-z0-9])")
# giữa từ phủ định và giá trị chỉ được có tên chiều / từ hư: "không tính (các) (cửa hàng) Miền Bắc"
NEG_GAP = re.compile(r"^\s*((cac|nhung|kenh|mien|vung|khu vuc|nganh hang|nganh|cua hang|chi nhanh|thuong hieu|hang|san pham|"
                     r"tinh|thanh pho|nhom|loai|phan khuc|dich vu|buu cuc|ra|di)\s*)*$")
WHY_RE = re.compile(r"\b(vi sao|tai sao|nguyen nhan|ly do (gi|nao)|giai thich (bien dong|su|vi)|why|dieu gi khien|do dau)\b")


def is_why_question(question: str) -> bool:
    """Câu hỏi tìm nguyên nhân biến động ("vì sao ... giảm")."""
    return bool(WHY_RE.search(norm(question)))


@dataclass
class Span:
    start: dt.date
    end: dt.date           # không bao gồm
    label: str
    pos: int
    relative: str | None = None   # 'prev' nếu là kỳ trước tương đối


@dataclass
class NLUResult:
    intent: str = "metric_query"
    plan: dict = field(default_factory=dict)
    assumptions: list = field(default_factory=list)
    clarification: dict | None = None
    follow_up: bool = False
    chart_hint: str | None = None
    matched: dict = field(default_factory=dict)
    confidence: float = 0.8
    metric_topic: str | None = None


def _fmt(d: dt.date) -> str:
    return d.strftime("%d/%m/%Y")


def _add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def month_span(y, m):
    s = dt.date(y, m, 1)
    return s, _add_months(s, 1)


def quarter_span(y, qn):
    s = dt.date(y, 3 * (qn - 1) + 1, 1)
    return s, _add_months(s, 3)


def previous_period(s: dt.date, e: dt.date) -> tuple[dt.date, dt.date, str]:
    if s.day == 1 and e.day == 1:
        months = (e.year - s.year) * 12 + e.month - s.month
        ps = _add_months(s, -months)
        return ps, s, _period_label(ps, s)
    length = (e - s).days
    return s - dt.timedelta(days=length), s, _period_label(s - dt.timedelta(days=length), s)


def same_period_last_year(s, e):
    ps, pe = _add_months(s, -12), _add_months(e, -12)
    return ps, pe, _period_label(ps, pe)


def _period_label(s: dt.date, e: dt.date) -> str:
    last = e - dt.timedelta(days=1)
    if s.day == 1 and e.day == 1:
        months = (e.year - s.year) * 12 + e.month - s.month
        if months == 1:
            return f"T{s.month}/{s.year}"
        if months == 3 and s.month in (1, 4, 7, 10):
            return f"Q{(s.month - 1) // 3 + 1}/{s.year}"
        if months == 12 and s.month == 1:
            return f"năm {s.year}"
        return f"T{s.month}/{s.year}–T{last.month}/{last.year}"
    if s == last:
        return _fmt(s)
    return f"{_fmt(s)}–{_fmt(last)}"


class OfflineNLU:
    def __init__(self):
        self.sem = get_semantic()
        self.cfg = config.semantic_layer()
        self._build_indexes()

    # ------------------------------------------------------------------ chỉ mục
    def _build_indexes(self):
        self.metric_phr = []
        for name, m in self.sem.metrics.items():
            for s in set([m["label"]] + m.get("synonyms", [])):
                p = norm(re.sub(r"\(.*?\)", "", s))
                if p:
                    self.metric_phr.append((p, name))
        hints = self.cfg.get("nlu") or {}          # cách gọi riêng của doanh nghiệp, xem chú thích trong semantic_layer.yml
        for k, lst in (hints.get("metric_phrases") or {}).items():
            if k in self.sem.metrics:
                self.metric_phr += [(norm(p), k) for p in lst if norm(p)]
        self.metric_phr.sort(key=lambda x: -len(x[0]))

        self.dim_phr = []
        for name, d in self.sem.dims.items():
            if d.get("kind") == "time":
                continue
            for s in set([d["label"]] + d.get("synonyms", [])):
                p = norm(re.sub(r"\(.*?\)", "", s))
                if p and len(p) > 1 and p not in AMBIGUOUS_NORM:
                    self.dim_phr.append((p, name))
        for k, lst in (hints.get("dim_phrases") or {}).items():
            if k in self.sem.dims:
                self.dim_phr += [(norm(p), k) for p in lst if norm(p)]
        self.dim_phr.sort(key=lambda x: -len(x[0]))
        # các khai báo tùy chọn khác của doanh nghiệp (xem chú thích trong semantic_layer.yml, mục nlu)
        self.marker_only = {d for d in hints.get("marker_only_dims") or [] if d in self.sem.dims}
        self.default_metric = {d: m for d, m in (hints.get("default_metric") or {}).items()
                               if d in self.sem.dims and m in self.sem.metrics}
        self.having_phr = [dict(h, _phr=sorted({norm(p) for p in h.get("phrases") or [] if norm(p)}, key=len, reverse=True))
                           for h in hints.get("having_phrases") or [] if h.get("metric") in self.sem.metrics]
        ov = hints.get("overview") or {}
        self.overview = {"phrases": [norm(p) for p in ov.get("phrases") or [] if norm(p)],
                         "metrics": [m for m in ov.get("metrics") or [] if m in self.sem.metrics]}
        self.decisions = [dict(x, _phr=[norm(p) for p in x.get("phrases") or [] if norm(p)],
                               metrics=[m for m in x.get("metrics") or [] if m in self.sem.metrics],
                               dimensions=[d for d in x.get("dimensions") or [] if d in self.sem.dims])
                          for x in hints.get("decision_support") or []]
        self.decisions = [x for x in self.decisions if x["_phr"] and x["metrics"]]
        # tên chiều chứa trọn một tên chỉ số ("loại chi phí" chứa "chi phí"): phải giữ chỗ trước khi khớp chỉ số
        self.dim_over = [p for p, _ in self.dim_phr
                         if any(re.search(r"(?<![a-z0-9])" + re.escape(mp) + r"(?![a-z0-9])", p) for mp, _ in self.metric_phr)]

        cat = get_catalog()
        self.value_phr = []  # (phrase, dim, canonical value, op)
        # thứ tự ưu tiên khi một cụm từ khớp giá trị của nhiều chiều: theo khai báo, rồi tới các chiều còn lại
        prio = [d for d in (hints.get("dim_priority") or []) if d in self.sem.dims]
        prio += [d for d, x in self.sem.dims.items() if x.get("kind") != "time" and d not in prio]
        skip_bare = set(hints.get("skip_bare_values") or [])      # giá trị trùng từ thông dụng: chỉ nhận qua tên gọi khác
        like_dims = hints.get("like_dimensions") or {}            # chiều nhiều giá trị dài (tên sản phẩm): lọc kiểu "chứa"
        self.context_remap = [r for r in (hints.get("context_remap") or []) if r.get("to") in self.sem.dims]
        table_aliases: dict = {}                                  # tên gọi khác lấy từ bảng tra cứu trong kho
        for dim, sql in (hints.get("alias_sql") or {}).items():
            try:
                from .warehouse import get_warehouse
                for alias, val in get_warehouse().fetch(sql):
                    table_aliases.setdefault(dim, {}).setdefault(val, set()).add(norm(alias))
            except Exception:
                pass
        for dim in prio:
            fc = (hints.get("value_forms") or {}).get(dim) or {}
            named = (hints.get("value_aliases") or {}).get(dim) or {}
            for v in self.sem.dim_values(dim):
                forms = set() if dim in skip_bare else {norm(v)}
                if fc:      # vd "Astra Cầu Giấy" -> "cầu giấy", "cửa hàng cầu giấy", "chi nhánh cầu giấy"
                    base = v
                    for pre in fc.get("strip") or []:
                        base = base.replace(pre, "")
                    base = norm(re.sub(fc["strip_regex"], "", base) if fc.get("strip_regex") else base)
                    forms |= {f"{norm(p)} {base}" for p in fc.get("prefixes") or []}
                    if fc.get("bare", True):
                        forms.add(base)
                forms |= {a for a in table_aliases.get(dim, {}).get(v, set()) if len(a) > 2}
                forms |= {norm(f) for f in named.get(v, [])}
                op = "like" if dim in like_dims else "in"
                for f in forms:
                    if f and len(f) >= 2 and not f.isdigit():
                        self.value_phr.append((f, dim, v, op))
        for dim, lc in like_dims.items():       # tên rút gọn (vd bỏ dung lượng "256GB") -> lọc theo dòng sản phẩm
            for v in self.sem.dim_values(dim) if lc and lc.get("short_regex") else []:
                short = re.sub(lc["short_regex"], "", v)
                if short != v:
                    self.value_phr.append((norm(short), dim, short, "like"))
        try:                                    # mã đối tượng ("SP0010", "CH036") -> lọc theo đúng tên của đối tượng đó
            from .lookup import get_lookups
            self.value_phr += [(code, dim, name, "in") for code, dim, name in get_lookups().code_values()]
        except Exception:
            pass
        self.value_phr.sort(key=lambda x: (-len(x[0]), prio.index(x[1]) if x[1] in prio else 99))
        # từ bộ luật biết (để phân biệt từ lạ / gõ sai với từ thông thường khi quyết định có cảnh báo hay không)
        self.vocab = set(FILLER) | FOLLOW_WORDS | KNOWN_WORDS
        for p in [p for p, _ in self.metric_phr] + [p for p, _ in self.dim_phr] + [x[0] for x in self.value_phr]:
            self.vocab |= set(re.findall(r"[a-z0-9]+", p))
        for amb in self.cfg.get("ambiguities") or []:
            for x in (amb.get("triggers") or []) + (amb.get("weak_triggers") or []) + (amb.get("unless_any") or []):
                self.vocab |= set(re.findall(r"[a-z0-9]+", norm(x)))
        self.anchor = dt.date.fromisoformat(cat.anchor_date())
        self.data_start = dt.date.fromisoformat(cat.data_start())
        # thuật ngữ mơ hồ do doanh nghiệp khai báo (semantic_layer.yml: ambiguities)
        self.term_amb, self.rank_amb = [], []
        for amb in self.cfg.get("ambiguities") or []:
            a = dict(amb, _pref_key=amb.get("pref_key") or amb["id"],
                     _triggers=[norm(x) for x in amb.get("triggers", []) if norm(x)],
                     _weak=[norm(x) for x in amb.get("weak_triggers", []) if norm(x)],
                     _unless=[norm(x) for x in amb.get("unless_any", []) if norm(x)])
            (self.rank_amb if amb.get("requires_no_metric") else self.term_amb).append(a)

    # ------------------------------------------------------------------ thuật ngữ mơ hồ
    @staticmethod
    def _clarify(amb: dict) -> dict:
        return {"id": amb["id"], "question": amb["question"], "pref_key": amb["_pref_key"],
                "options": [{"label": o["label"], "metric": o.get("metric")} for o in amb["options"]]}

    def ambiguity_metrics(self, topic: str | None = None) -> list[str]:
        """Các chỉ số dễ nhầm của một thuật ngữ mơ hồ (topic = '__amb__:<id>'); không nêu thì lấy thuật ngữ đầu tiên."""
        ambs = self.term_amb + self.rank_amb
        amb = next((a for a in ambs if topic == f"__amb__:{a['id']}"), self.term_amb[0] if self.term_amb else None)
        names = [o["metric"] for o in amb["options"] if o.get("metric") in self.sem.metrics] if amb else []
        return names or list(self.sem.metrics)[:2]

    def pref_key_for(self, metrics: list) -> str | None:
        """Khóa ghi nhớ lựa chọn của người dùng cho nhóm chỉ số này (để lần sau không hỏi lại)."""
        chosen = {m for m in metrics if m}
        for a in self.term_amb + self.rank_amb:
            if chosen and chosen <= {o.get("metric") for o in a["options"]}:
                return a["_pref_key"]
        return None

    def detect_ambiguity(self, question: str, memory: dict | None = None) -> dict | None:
        """Chốt chặn trước khi gọi LLM: câu hỏi chạm thuật ngữ mơ hồ đã khai báo thì hỏi lại bằng code.

        Chỉ nhận tín hiệu chắc chắn: bỏ qua trigger yếu (vd "top") và trường hợp câu hỏi đã có từ phân biệt (unless_any),
        vì khi đó LLM có thể hiểu được một tiêu chí mà bộ luật không biết.
        """
        r = self.parse(question, memory)
        hit = r.matched.get("ambiguity") or {}
        if not r.clarification or r.intent not in ("metric_query", "variance") or hit.get("weak"):
            return None
        t = norm(question)
        amb = next((a for a in self.term_amb + self.rank_amb if a["id"] == hit.get("id")), None)
        if amb and any(re.search(r"(?<![a-z0-9])" + re.escape(u) + r"(?![a-z0-9])", t) for u in amb["_unless"]):
            return None
        return r.clarification

    # ------------------------------------------------------------------ tiện ích span
    @staticmethod
    def _free(mask, s, e):
        return not any(mask[s:e])

    @staticmethod
    def _take(mask, s, e):
        for i in range(s, e):
            mask[i] = True

    def _find_all(self, text, phrase, mask):
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text):
            if self._free(mask, m.start(), m.end()):
                yield m

    # ------------------------------------------------------------------ thời gian
    def parse_time(self, t: str, mask, bad: list | None = None) -> list[Span]:
        A = self.anchor
        spans: list[Span] = []
        bad = [] if bad is None else bad

        def add(m, s, e, label, rel=None):
            if not self._free(mask, m.start(), m.end()):
                return
            self._take(mask, m.start(), m.end())
            spans.append(Span(s, e, label, m.start(), rel))

        def year_of_month(mo, explicit=None):
            if explicit:
                return int(explicit)
            return A.year if mo <= A.month else A.year - 1

        # khoảng "từ ... đến ..."
        for m in re.finditer(r"tu (?:ngay )?(\d{1,2})/(\d{1,2})/(\d{4}) (?:den|toi|-) (?:ngay )?(\d{1,2})/(\d{1,2})/(\d{4})", t):
            s = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            e = dt.date(int(m.group(6)), int(m.group(5)), int(m.group(4))) + dt.timedelta(days=1)
            add(m, s, e, _period_label(s, e))
        for m in re.finditer(r"tu thang (\d{1,2})(?:/(\d{4}))?(?: nam (\d{4}))? (?:den|toi|-) thang (\d{1,2})(?:/(\d{4}))?(?: nam (\d{4}))?", t):
            y2 = m.group(5) or m.group(6)
            y1 = m.group(2) or m.group(3) or y2
            m1, m2 = int(m.group(1)), int(m.group(4))
            yy2 = year_of_month(m2, y2)
            yy1 = int(y1) if y1 else (yy2 if m1 <= m2 else yy2 - 1)
            s, _ = month_span(yy1, m1)
            _, e = month_span(yy2, m2)
            add(m, s, e, _period_label(s, e))
        for m in re.finditer(r"(\d+) thang (dau|cuoi) nam(?: (\d{4}))?", t):
            n, y = int(m.group(1)), int(m.group(3) or A.year)
            if m.group(2) == "dau":
                s, e = dt.date(y, 1, 1), _add_months(dt.date(y, 1, 1), n)
            else:
                s, e = _add_months(dt.date(y + 1, 1, 1), -n), dt.date(y + 1, 1, 1)
            add(m, s, e, f"{n} tháng {m.group(2) == 'dau' and 'đầu' or 'cuối'} năm {y}")
        for m in re.finditer(r"nua (dau|cuoi) nam(?: (\d{4}))?", t):
            y = int(m.group(2) or A.year)
            s, e = (dt.date(y, 1, 1), dt.date(y, 7, 1)) if m.group(1) == "dau" else (dt.date(y, 7, 1), dt.date(y + 1, 1, 1))
            add(m, s, e, f"{'nửa đầu' if m.group(1) == 'dau' else 'nửa cuối'} năm {y}")
        for m in re.finditer(r"(?:tu )?dau nam(?: (\d{4}))? (?:den nay|toi nay|den hien tai)|tu dau nam|ytd|luy ke nam", t):
            y = int(m.group(1)) if m.lastindex and m.group(1) else A.year
            s, e = dt.date(y, 1, 1), A + dt.timedelta(days=1)
            add(m, s, e, f"từ đầu năm {y} đến {_fmt(A)}")
        # N đơn vị gần nhất
        for m in re.finditer(r"(\d+) (ngay|tuan|thang|quy|nam) (?:gan nhat|gan day|qua|vua qua|truoc do|cuoi cung)", t):
            n, u = int(m.group(1)), m.group(2)
            e = A + dt.timedelta(days=1)
            s = {"ngay": lambda: e - dt.timedelta(days=n), "tuan": lambda: e - dt.timedelta(weeks=n),
                 "thang": lambda: _add_months(e, -n), "quy": lambda: _add_months(e, -3 * n),
                 "nam": lambda: _add_months(e, -12 * n)}[u]()
            add(m, s, e, f"{n} {dict(ngay='ngày', tuan='tuần', thang='tháng', quy='quý', nam='năm')[u]} gần nhất (đến {_fmt(A)})")
        # ngày cụ thể
        for m in re.finditer(r"(?:ngay )?(\d{1,2})/(\d{1,2})/(\d{4})", t):
            try:
                s = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                if self._free(mask, m.start(), m.end()):
                    self._take(mask, m.start(), m.end())
                    bad.append(m.group(0))
                continue
            add(m, s, s + dt.timedelta(days=1), _fmt(s))
        # tháng
        def invalid(m):         # "tháng 13/2026": ghi nhận để báo lại, không để rơi về "toàn bộ dữ liệu"
            if self._free(mask, m.start(), m.end()):
                self._take(mask, m.start(), m.end())
                bad.append(m.group(0))

        for m in re.finditer(r"(?:thang|t)\s?(\d{1,2})\s?(?:/|-)\s?(\d{4})", t):
            mo = int(m.group(1))
            if 1 <= mo <= 12:
                s, e = month_span(int(m.group(2)), mo)
                add(m, s, e, f"T{mo}/{m.group(2)}")
            elif m.group(0).startswith("thang"):
                invalid(m)
        for m in re.finditer(r"thang (\d{1,2}) nam (\d{4})", t):
            mo = int(m.group(1))
            if 1 <= mo <= 12:
                s, e = month_span(int(m.group(2)), mo)
                add(m, s, e, f"T{mo}/{m.group(2)}")
            else:
                invalid(m)
        for m in re.finditer(r"thang (\d{1,2})(?![0-9/])", t):
            mo = int(m.group(1))
            if 1 <= mo <= 12:
                y = year_of_month(mo)
                s, e = month_span(y, mo)
                add(m, s, e, f"T{mo}/{y}")
            else:
                invalid(m)
        for w, mo in MONTH_WORDS.items():
            for m in re.finditer(r"thang " + w + r"(?![a-z])(?: nam (\d{4}))?", t):
                y = year_of_month(mo, m.group(1))
                s, e = month_span(y, mo)
                add(m, s, e, f"T{mo}/{y}")
        # quý
        for m in re.finditer(r"(?<![a-z0-9])(?:quy|q)\s?(\d|iv|iii|ii|i)(?![a-z0-9])\s?(?:/|-|nam)?\s?(\d{4})?", t):
            g = m.group(1)
            qn = int(g) if g.isdigit() else ROMAN[g]
            if not 1 <= qn <= 4:
                invalid(m)
                continue
            if m.group(2):
                y = int(m.group(2))
            else:
                y = A.year if 3 * (qn - 1) + 1 <= A.month else A.year - 1
            s, e = quarter_span(y, qn)
            add(m, s, e, f"Q{qn}/{y}")
        # tương đối
        cur_m = month_span(A.year, A.month)
        prev_m = month_span(*(lambda d: (d.year, d.month))(_add_months(dt.date(A.year, A.month, 1), -1)))
        qn = (A.month - 1) // 3 + 1
        cur_q = quarter_span(A.year, qn)
        pq = _add_months(cur_q[0], -3)
        prev_q = quarter_span(pq.year, (pq.month - 1) // 3 + 1)
        monday = A - dt.timedelta(days=A.weekday())
        rel = [
            (r"hom nay", (A, A + dt.timedelta(days=1), f"hôm nay ({_fmt(A)})", None)),
            (r"hom qua", (A - dt.timedelta(days=1), A, f"hôm qua ({_fmt(A - dt.timedelta(days=1))})", None)),
            (r"tuan nay", (monday, A + dt.timedelta(days=1), f"tuần này ({_fmt(monday)}–{_fmt(A)})", None)),
            (r"tuan truoc|tuan roi", (monday - dt.timedelta(days=7), monday, f"tuần trước ({_fmt(monday - dt.timedelta(days=7))}–{_fmt(monday - dt.timedelta(days=1))})", "prev")),
            (r"thang nay|thang hien tai", (cur_m[0], A + dt.timedelta(days=1), f"tháng này (T{A.month}/{A.year}, đến {_fmt(A)})", None)),
            (r"thang truoc|thang roi|thang vua qua", (prev_m[0], prev_m[1], f"tháng trước (T{prev_m[0].month}/{prev_m[0].year})", "prev")),
            (r"quy nay|quy hien tai", (cur_q[0], A + dt.timedelta(days=1), f"quý này (Q{qn}/{A.year}, đến {_fmt(A)})", None)),
            (r"quy truoc|quy roi", (prev_q[0], prev_q[1], f"quý trước ({_period_label(*prev_q)})", "prev")),
            (r"nam nay|nam hien tai", (dt.date(A.year, 1, 1), A + dt.timedelta(days=1), f"năm nay ({A.year}, đến {_fmt(A)})", None)),
            (r"nam ngoai|nam truoc|nam roi|nam vua qua", (dt.date(A.year - 1, 1, 1), dt.date(A.year, 1, 1), f"năm {A.year - 1}", "prev")),
            (r"ky truoc|cung ky truoc", None),
        ]
        for pat, val in rel:
            for m in re.finditer(r"(?<![a-z0-9])(?:" + pat + r")(?![a-z0-9])", t):
                if val is None:
                    continue
                add(m, val[0], val[1], val[2], val[3])
        # năm
        for m in re.finditer(r"(?:nam )?(?<![0-9/])(20\d{2})(?![0-9])", t):
            y = int(m.group(1))
            add(m, dt.date(y, 1, 1), dt.date(y + 1, 1, 1), f"năm {y}")
        spans.sort(key=lambda s: s.pos)
        return spans

    # ------------------------------------------------------------------ phân tích chính
    def parse(self, question: str, memory: dict | None = None) -> NLUResult:
        memory = memory or {}
        prefs = memory.get("prefs", {})
        prev = memory.get("last_plan")
        t = norm(question)
        mask = [False] * len(t)
        r = NLUResult()
        # Bỏ dấu thì "lại" trùng "lãi" (lợi nhuận) và "tính" trùng "tỉnh": "tính lại doanh thu" từng thành lợi nhuận theo tỉnh.
        # Câu gõ có dấu: chữ gốc là "lại"/"tính" thì giữ chỗ luôn. Câu không dấu: giữ chỗ cụm động từ + "lai" (trừ "lãi gộp...").
        toks = list(re.finditer(r"[a-z0-9]+", t))
        orig = [(w, x) for w in re.findall(r"[^\s,;:?!()\"“”']+", question or "") for x in re.findall(r"[a-z0-9]+", norm(w))]
        accented = any(ord(ch) > 127 for ch in question or "")
        if [x for _, x in orig] == [k.group(0) for k in toks]:
            for i, ((w, x), k) in enumerate(zip(orig, toks)):
                ww = unicodedata.normalize("NFC", w.lower()).strip(".")
                # "trả lại", "hoàn lại", "đổi lại": để cụm "trả lại" (trả hàng) khớp được
                if x == "lai" and i and orig[i - 1][1] in ("tra", "hoan", "doi"):
                    continue
                # "lại", "tính", "tình" có dấu; hoặc "Lai" không dấu trong câu gõ có dấu (Gia Lai) -> không phải "lãi"/"tỉnh"
                if (x, ww) in (("lai", "lại"), ("tinh", "tính"), ("tinh", "tình")) or (accented and ww == "lai"):
                    self._take(mask, k.start(), k.end())
        for mm in re.finditer(r"(?<![a-z0-9])((tinh|xem|hoi|lam|kiem tra|sua|chay|noi|tra loi|gui|thu|tong hop|dem) lai"
                              r"(?![a-z0-9])(?! (gop|rong|lo|suat))|tinh hinh(?![a-z0-9]))", t):
            self._take(mask, mm.start(), mm.end())

        # ý định
        if WHY_RE.search(t):
            r.intent = "variance"
        elif re.search(r"\b(dinh nghia|duoc tinh (nhu the nao|the nao|ra sao)|cong thuc|tinh nhu the nao|nghia la gi|la gi)\b", t):
            r.intent = "metric_info"
        elif re.search(r"\b(bang nao|cot nao|co nhung bang|danh sach bang|schema|luoc do|cau truc du lieu|cot .* nghia)\b", t):
            r.intent = "schema_info"
        elif re.search(r"\b(chat luong du lieu|du lieu (bat thuong|bi loi|co van de)|kiem tra du lieu|data quality|drift|sai lech du lieu)\b", t):
            r.intent = "dq"
        elif re.search(r"^(xin chao|chao|hello|hi|help|huong dan|ban (la ai|lam duoc gi|co the lam gi))\b", t):
            r.intent = "help"

        # so sánh
        cmp_pos = None
        m = re.search(r"\b(so voi|so sanh voi|vs|doi chieu voi)\b", t)
        if m:
            cmp_pos = m.start()
        yoy = bool(re.search(r"\b(cung ky|yoy|nam truoc cung ky)\b", t))
        growth = bool(re.search(r"\b(tang truong|mom|tang giam|bien dong|thay doi)\b", t))

        bad_time: list[str] = []
        spans = self.parse_time(t, mask, bad_time)
        if bad_time:
            r.matched["invalid_time"] = bad_time
        # "so sánh số đơn tháng 10 với tháng 9": hai kỳ nối bằng "với/và" sau chữ "so sánh" là so sánh kỳ, không phải
        # một khoảng thời gian chia theo tháng
        if cmp_pos is None and re.search(r"(?<![a-z0-9])(so sanh|doi chieu)(?![a-z0-9])", t):
            sp = sorted(spans, key=lambda x: x.pos)
            if len(sp) == 2:
                mc = re.search(r"(?<![a-z0-9])(so voi|voi|va)(?![a-z0-9])", t[sp[0].pos:sp[1].pos])
                if mc:
                    cmp_pos = sp[0].pos + mc.start()
        cur = [s for s in spans if cmp_pos is None or s.pos < cmp_pos]
        if spans:
            r.matched["time"] = [s.label for s in spans]      # người dùng nêu rõ thời gian (không phải mặc định/kế thừa)
        cmp = [s for s in spans if cmp_pos is not None and s.pos > cmp_pos]

        # "trung bình mỗi ngày", "bình quân một tháng", "mỗi ngày trung bình": chia chỉ số cho số ngày/tháng có phát sinh,
        # không phải chia theo từng ngày. Giữ chỗ "mỗi ngày" để không bị hiểu thành nhóm theo ngày.
        per = None
        pm = re.search(r"(?<![a-z0-9])(?:trung binh|binh quan)(?![a-z0-9])[a-z0-9 ]{0,40}?(?<![a-z0-9])"
                       r"((?:moi|mot|1|cho moi|tren moi|(?<!cua )hang) (ngay|tuan|thang))(?![a-z0-9])"
                       r"|(?<![a-z0-9])((?:moi|mot) (ngay|tuan|thang)) (?:trung binh|binh quan)(?![a-z0-9])", t)
        if pm:
            g = 1 if pm.group(1) else 3
            if self._free(mask, pm.start(g), pm.end(g)):
                per = pm.group(g + 1)
                self._take(mask, pm.start(g), pm.end(g))
                kw = next(self._find_all(t, "trung binh", mask), None) or next(self._find_all(t, "binh quan", mask), None)
                if kw:
                    self._take(mask, kw.start(), kw.end())

        # danh sách trong ngoặc ngay sau "theo <chiều>" ("theo loại cửa hàng (Flagship, Tiêu chuẩn, Mini)") chỉ liệt kê các
        # nhóm sẽ thấy trong kết quả, không phải bộ lọc: giữ chỗ cả cụm, ghi lại các giá trị để quyết định sau khi biết chiều
        enum_vals, enum_items = [], {}
        for pg in re.finditer(r"\(([^()]{1,160})\)", question or ""):
            before_n, inner = norm(question[:pg.start()]), norm(pg.group(1))
            if not inner or not re.search(r"(?<![a-z0-9])(theo|tung|cac|moi)(?![a-z0-9])[a-z0-9 ]{1,40}$", before_n):
                continue
            items = [x for x in re.split(r"[,/;]|\s(?:và|hoặc|va|hoac)\s", pg.group(1)) if x.strip()]
            if len(items) < 2:
                continue
            pos = t.find(inner, max(0, len(before_n) - 1))
            if pos < 0:
                continue
            found = [(dim, val) for p, dim, val, _ in self.value_phr
                     if re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", inner)]
            enum_vals += found
            for dim, _ in found:
                enum_items[dim] = len(items)
            self._take(mask, pos, pos + len(inner))

        # chỉ số
        metrics, mpos = [], []
        held = [(mm.start(), mm.end()) for p in self.dim_over for mm in self._find_all(t, p, mask)]
        for s0, e0 in held:             # giữ chỗ cho tên chiều chứa tên chỉ số, trả lại sau khi khớp xong chỉ số
            self._take(mask, s0, e0)
        for p, name in self.metric_phr:
            for mm in self._find_all(t, p, mask):
                self._take(mask, mm.start(), mm.end())
                if name not in metrics:
                    metrics.append(name)
                    mpos.append(mm.start())
        order = sorted(range(len(metrics)), key=lambda i: mpos[i])
        metrics = [metrics[i] for i in order]
        r.matched["metrics"] = list(metrics)                  # chỉ số được gọi tên trong câu (trước khi suy mặc định)
        # thuật ngữ mơ hồ còn sót lại ngoài mọi cụm chỉ số đã khớp (vd "doanh thu" không nói gộp hay thuần)
        vague = None
        for amb in self.term_amb:
            for trig in amb["_triggers"]:
                for mm in self._find_all(t, trig, mask):
                    self._take(mask, mm.start(), mm.end())
                    vague = vague or dict(amb, _hit=trig)
        revenue_generic = vague is not None
        for s0, e0 in held:
            for i in range(s0, e0):
                mask[i] = False

        # từ phủ định ("không tính", "trừ"...): giữ chỗ để "tính" không bị hiểu thành "tỉnh", rồi áp lên giá trị đứng ngay sau
        negs = []
        for mm in NEGATION_RE.finditer(t):      # ("tính" có thể đã được giữ chỗ ở bước đầu: vẫn nhận "không tính")
            self._take(mask, mm.start(), mm.end())
            negs.append(mm.end())
        neg_strict = len(negs)
        # câu nối tiếp "bỏ kênh Website": "bỏ" đầu câu là loại trừ khi đứng ngay trước một giá trị. Không giữ chỗ, vì "bo"
        # còn là "bộ" ("Bộ nồi inox"): nếu đó là tên giá trị thì cụm giá trị khớp từ chính chữ "bo" và không bị phủ định
        mm = re.match(r"(?:vay |the |con )?(bo)(?= )(?! (loc|het|qua|bo loc|chia|nhom))", t)
        if mm:
            negs.append(mm.end(1))

        # giá trị thực thể -> bộ lọc
        filters: dict[str, dict] = {}
        negated: set[str] = set()
        val_pos: dict[str, list[int]] = {}

        def add_value(d2, val, op, start, text):
            f = filters.setdefault(d2, {"dimension": d2, "op": op, "values": []})
            if val not in f["values"]:
                f["values"].append(val)
            if any(e0 <= start and NEG_GAP.match(t[e0:start]) for e0 in negs):
                negated.add(d2)
            val_pos.setdefault(d2, []).append(start)
            r.matched.setdefault("values", []).append({"text": text, "dimension": d2, "value": val})

        for p, dim, val, op in self.value_phr:
            for mm in self._find_all(t, p, mask):
                d2 = dim
                for rule in self.context_remap:     # vd tỉnh đứng sau "khách hàng ở" -> tỉnh của khách, không phải của cửa hàng
                    if rule.get("dimension") == dim and re.search(rule["before_regex"], t[:mm.start()][-25:]):
                        d2 = rule["to"]
                self._take(mask, mm.start(), mm.end())
                add_value(d2, val, op, mm.start(), p)
        # mã của bảng danh mục lớn (khách hàng KH000123) không nạp sẵn: lọc theo đúng mã khi chiều phân tích dùng cột mã
        try:
            from .lookup import get_lookups
            for e, code, _ in get_lookups().find_codes(question)[0]:
                loc = self.sem.dim_column(e.dimension) if e.dimension and e.code_re else None
                if loc and loc[1] == e.key:
                    for mm in self._find_all(t, norm(code), mask):
                        self._take(mask, mm.start(), mm.end())
                        add_value(e.dimension, code, "in", mm.start(), norm(code))
        except Exception:
            pass
        for d2 in negated:
            f = filters[d2]
            f["op"] = "not_in"
            label = self.sem.dims[d2]["label"] if d2 in self.sem.dims else d2
            r.assumptions.append(f"Loại trừ {label.lower()}: {', '.join(map(str, f['values']))}.")
        if neg_strict and not negated:
            r.assumptions.append("Câu hỏi có ý loại trừ nhưng mình chưa xác định được loại trừ giá trị nào — "
                                 "kết quả chưa loại trừ gì, hãy kiểm tra lại bộ lọc.")

        # chiều phân tích
        dims: list[str] = []
        grain = None
        rank_time = False           # "tháng nào doanh thu cao nhất": xếp hạng các kỳ
        # "ngày thường", "ngày trong tuần", "ngày lễ" không phải kỳ; "tính theo ngày khiếu nại / ngày tạo đơn" là chọn cột ngày
        # để tính, không phải chia theo từng ngày
        not_time = (r"(?! \d| thuong| trong tuan| le\b)(?! (khieu nai|tao|dat|giao|nhan|chot|tra|nhap|dang ky|mua|ban|phat"
                    r"|gui|lap|xuat|hoan|thanh toan|ghi nhan)(?![a-z0-9]))")
        for gm in re.finditer(r"\b(theo|tung|moi|hang|cac|qua cac|tren tung|va) (ngay|tuan|thang|quy|nam)\b" + not_time, t):
            if mask[gm.start(2)]:
                continue  # "mua hàng tháng này": "tháng này" là biểu thức thời gian
            grain = gm.group(2)
            break
        if grain is None:
            for gm in re.finditer(r"\b(ngay|tuan|thang|quy|nam) nao\b" + not_time, t):
                if not mask[gm.start(1)]:
                    grain, rank_time = gm.group(1), True
                    self._take(mask, gm.start(), gm.end())
                    break
        if grain is None and re.search(r"\b(xu huong|dien bien|bieu do duong|theo thoi gian|trend)\b", t):
            grain = "auto"
        for p, dim in self.dim_phr:
            for mm in self._find_all(t, p, mask):
                self._take(mask, mm.start(), mm.end())
                before = t[:mm.start()].rstrip()
                after = t[mm.end():].lstrip()
                if dim in filters and not re.search(r"(theo|tung|cac|moi)$", before):
                    continue  # "ngành hàng Điện thoại" -> chỉ là bộ lọc
                # "top 10 khách hàng", "nhân viên nào", "từng khách hàng" -> nhóm; "doanh thu từ khách hàng ở Hà Nội" -> không
                if dim in self.marker_only and not (
                        re.search(r"(theo|tung|cac|moi|nhung|top\s?\d+|\d+|tat ca)$", before)
                        or re.match(r"(nao|ban|mua|co|dat|dan dau)\b", after) or re.search(r"\bla ai\b", after)):
                    continue
                if dim not in dims:
                    dims.append(dim)
                val_pos.setdefault(dim, []).append(mm.start())
                r.matched.setdefault("dimensions", []).append(p)

        # so sánh giữa các giá trị của cùng một chiều ("Apple so với Samsung năm 2026", "cuối tuần so với ngày thường"):
        # "so với" không phải so sánh kỳ, thời gian đứng sau "so với" là kỳ đang hỏi
        if cmp_pos is not None and not cur and not yoy and any(
                any(p < cmp_pos for p in ps) and any(p > cmp_pos for p in ps) for ps in val_pos.values()):
            cur, cmp, cmp_pos = cur + cmp, [], None
        # nhắc từ hai giá trị trở lên của cùng một chiều ("miền Bắc và miền Nam", "online và offline"): tách từng giá trị
        # thay vì cộng gộp thành một con số
        for d2, f in filters.items():
            if f["op"] in ("in", "like") and len(f["values"]) >= 2 and d2 not in dims and d2 not in negated:
                dims.append(d2)
        # danh sách trong ngoặc: chỉ thành bộ lọc khi nhận ra đủ mọi mục đã liệt kê và đó chỉ là một phần các giá trị
        for d2 in dict.fromkeys(d for d, _ in enum_vals):
            vals = list(dict.fromkeys(v for d, v in enum_vals if d == d2))
            allv = {str(v) for v in self.sem.dim_values(d2) or []}
            if d2 in dims and d2 not in filters and len(vals) == enum_items.get(d2) and allv and set(map(str, vals)) < allv:
                filters[d2] = {"dimension": d2, "op": "in", "values": vals}
        # "top 3 tỉnh phát ..., cho xem tên tỉnh và tỷ lệ hoàn": chiều nhắc lại trong vế "cho xem" trỏ cùng cột với một chiều
        # đã có -> chỉ là cột hiển thị, không thêm cách chia mới
        om = re.search(r"(?<![a-z0-9])(cho (?:minh |toi |em )?xem|hien thi|kem theo|kem|gom co|gom|liet ke)(?![a-z0-9])", t)
        if om and len(dims) > 1:
            col = lambda d: (self.sem.dim_column(d) or (None, None))[1]
            for d2 in list(dims):
                ps = val_pos.get(d2) or []
                if ps and min(ps) > om.start() and d2 not in filters and col(d2) \
                        and any(o != d2 and col(o) == col(d2) for o in dims):
                    dims.remove(d2)

        # top N / thứ tự
        limit, direction = None, None
        for mt in re.finditer(r"\btop\s?(\d+)\b|\b(\d+) (?:[a-z ]{0,30}?)(cao nhat|lon nhat|nhieu nhat|tot nhat|thap nhat|it nhat|kem nhat|ban chay nhat)\b", t):
            g = 1 if mt.group(1) else 2
            if mask[mt.start(g)]:
                continue  # con số thuộc biểu thức thời gian
            limit = int(mt.group(g))
            r.matched["limit"] = limit
            break
        def superl(pat):        # "... nhất" mà chữ "nhất" không thuộc biểu thức thời gian ("3 tháng gần nhất")
            return any(not mask[x.end() - 1] for x in re.finditer(pat, t))
        if superl(r"\b(thap|it|kem|te|cham|yeu|nho)( [a-z]+){0,2} nhat\b") or re.search(r"\bkem hieu qua\b", t):
            direction = "asc"
        elif superl(r"\b(cao|lon|nhieu|tot|gioi|manh|pho bien|chay)( [a-z]+){0,2} nhat\b|\bhay( [a-z]+){1,3} nhat\b") \
                or re.search(r"\b(ban chay|top|dan dau)\b", t):
            direction = "desc"
        ranked = dims or rank_time
        if direction and limit is None and ranked and re.search(r"\b(nao|gi|ai)\b", t) and not re.search(r"\b(nhung|cac)\b", t):
            limit = 1
        if direction and limit is None and ranked:
            limit = 10

        # HAVING
        having = []
        hm = re.search(r"\b(tren|hon|lon hon|vuot|duoi|nho hon|it hon|>|<)\s?(\d+(?:[.,]\d+)?)\s?"
                       r"(ty|ti|trieu|nghin|ngan|k|don|san pham|%|phan tram)?(?![a-z0-9])", t)
        if hm and (dims or grain) and hm.group(3) and not mask[hm.start(2)]:
            val = float(hm.group(2).replace(",", "."))
            unit = hm.group(3) or ""
            val *= {"ty": 1e9, "ti": 1e9, "trieu": 1e6, "nghin": 1e3, "ngan": 1e3, "k": 1e3}.get(unit, 1)
            op = "<" if hm.group(1) in ("duoi", "nho hon", "it hon", "<") else ">"
            h0 = {"op": op, "value": val}
            if unit in ("%", "phan tram"):      # "tỷ lệ giao đúng hạn dưới 81%": điều kiện đặt lên chỉ số dạng phần trăm
                pct = [m0 for m0 in metrics if self.sem.metrics[m0].get("format") == "percent"]
                if pct:
                    h0["metric"] = pct[0]
            having.append(h0)
        # cụm từ mang sẵn điều kiện ("chưa đạt kế hoạch" -> tỷ lệ đạt kế hoạch < 100): mỗi nhóm lấy cụm khớp đầu tiên
        phrase_having, groups = [], set()
        for h in self.having_phr:
            g = h.get("group") or h["metric"]
            if g in groups:
                continue
            if any(re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", t) for p in h["_phr"]):
                groups.add(g)
                phrase_having.append({"metric": h["metric"], "op": h.get("op", ">"), "value": float(h.get("value", 0))})

        # biểu đồ
        if re.search(r"\b(bieu do tron|pie|ty trong|co cau|chiem bao nhieu)\b", t):
            r.chart_hint = "pie"
        elif re.search(r"\b(bieu do cot|cot)\b", t) and "bieu do" in t:
            r.chart_hint = "bar"
        elif re.search(r"\b(bieu do duong|duong)\b", t) and "bieu do" in t:
            r.chart_hint = "line"

        # ---------------------------------------------------------------- câu không nêu chỉ số
        def has(p):
            return re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", t)
        # xin lời khuyên ("có nên nhập thêm hàng Samsung không"): đưa số liệu liên quan, không quyết định thay người dùng
        advice = re.search(r"\b(co nen|nen|co can|can)\b.*\bkhong\b|\b(co nen|nen)\b", t)
        for x in self.decisions:
            if any(has(p) for p in x["_phr"]) and (advice or not metrics):
                metrics = list(x["metrics"])
                if not dims and not grain:
                    dims = [d for d in x["dimensions"] if d not in filters or len(filters[d]["values"]) > 1]
                period = None
                if not cur:
                    n = int(x.get("last_months") or 3)
                    e = self.anchor + dt.timedelta(days=1)
                    cur = [Span(_add_months(e, -n), e, f"{n} tháng gần nhất (đến {_fmt(self.anchor)})", 0)]
                period = cur[0].label
                if x.get("note"):
                    r.assumptions.append(re.sub(r"\s+", " ", x["note"].replace("{period}", period)))
                r.matched["decision"] = x.get("id")
                break
        else:
            if metrics and re.search(r"\b(co nen|nen)\b.*\bkhong\b", t):
                r.assumptions.append("Mình không đưa ra khuyến nghị thay bạn — dưới đây là số liệu liên quan để bạn tự cân nhắc.")
        if not metrics and not revenue_generic:
            if self.overview["metrics"] and any(has(p) for p in self.overview["phrases"]):
                metrics = list(self.overview["metrics"])
                r.matched["overview"] = True
                if not cur:     # không nêu thời gian: tháng gần nhất đã đủ dữ liệu
                    ps, pe = month_span(*(lambda d: (d.year, d.month))(_add_months(dt.date(self.anchor.year, self.anchor.month, 1), -1)))
                    cur = [Span(ps, pe, f"T{ps.month}/{ps.year}", 0)]
                    r.assumptions.append(f"Chưa nêu thời gian — xem tháng gần nhất đã đủ dữ liệu ({cur[0].label}).")
            else:
                m0 = next((self.default_metric[d] for d in dims + list(filters) if d in self.default_metric), None)
                if m0:
                    metrics = [m0]
                    r.assumptions.append(f"Câu hỏi không nêu chỉ số — tính theo {self.sem.metrics[m0]['label'].lower()}.")

        # ---------------------------------------------------------------- hợp nhất ngữ cảnh
        # Chỉ coi là câu hỏi nối tiếp khi câu mới thật sự nói về dữ liệu: có ít nhất một tín hiệu đã hiểu (thời gian,
        # giá trị lọc, chiều, top N, so sánh, biểu đồ...) và gần như không còn từ lạ. Nếu không, một câu lạc đề
        # ("cách tạo bom", "thời tiết thế nào") sẽ kế thừa nguyên chỉ số + thời gian của câu trước và trả số liệu sai ngữ cảnh.
        signal = bool(cur or cmp or filters or dims or grain or limit or direction or having or yoy or growth
                      or r.chart_hint or cmp_pos is not None)
        stray = [k.group(0) for k in re.finditer(r"[a-z]+", t)          # chữ số (top 2, trên 15 tỷ) không tính
                 if k.group(0) not in FILLER and k.group(0) not in FOLLOW_WORDS and not any(mask[k.start():k.end()])]
        explicit = t.startswith(FOLLOWUP_START) or "thi sao" in t
        if metrics or revenue_generic:      # câu đã nêu chỉ số: chỉ nối tiếp khi người dùng nói rõ ("còn ... thì sao")
            follow = bool(prev) and explicit
        else:
            follow = (bool(prev) and (explicit or "the nao" in t or r.intent == "metric_query")
                      and signal and len(stray) <= 1)
        r.follow_up = follow
        if follow and prev:
            if not metrics and not revenue_generic:
                metrics = list(prev.get("metrics", []))
            if not cur and prev.get("time_range"):
                tr = prev["time_range"]
                cur = [Span(dt.date.fromisoformat(tr["start"]), dt.date.fromisoformat(tr["end"]), tr.get("label", ""), 0)] \
                    if tr.get("start") and tr.get("end") else []
            pf = {f["dimension"]: f for f in prev.get("filters", [])}
            if not re.search(r"\b(tat ca|toan bo|bo loc|bo het|toan quoc|ca nuoc)\b", t):
                for k, f in pf.items():
                    filters.setdefault(k, f)
            if not dims and not grain:
                dims = [d for d in prev.get("dimensions", []) if self.sem.dims[d].get("kind") != "time"]
                pg = [d for d in prev.get("dimensions", []) if self.sem.dims[d].get("kind") == "time"]
                grain = pg[0] if pg else None
                # bộ lọc mới trên chiều đang nhóm -> bỏ nhóm theo chiều đó
                # (bộ lọc loại trừ thì giữ nhóm: "theo kênh" rồi "bỏ kênh Website" = các kênh còn lại)
                dims = [d for d in dims if d not in filters or d in pf or filters[d].get("op") == "not_in"]
            if limit is None and prev.get("limit") and not re.search(r"\b(tat ca|toan bo)\b", t):
                limit = prev.get("limit")
            if not direction and prev.get("order_by"):
                direction = prev["order_by"][0].get("direction")
            if not cmp and prev.get("compare") and not cur:
                c = prev["compare"]
                cmp = [Span(dt.date.fromisoformat(c["start"]), dt.date.fromisoformat(c["end"]), c.get("label", ""), 0)]

        # ---------------------------------------------------------------- mơ hồ
        if vague:
            pm = prefs.get(vague["_pref_key"])
            if pm in self.sem.metrics:
                if pm not in metrics:
                    metrics.insert(0, pm)
                term = (vague.get("triggers") or [vague["_hit"]])[0]
                r.assumptions.append(f"“{term[:1].upper() + term[1:]}” hiểu là {self.sem.metrics[pm]['label']} "
                                     "(theo lựa chọn trước đó trong hội thoại).")
            elif not any(m in metrics for m in (o.get("metric") for o in vague["options"])):
                r.clarification = self._clarify(vague)
                r.matched["ambiguity"] = {"id": vague["id"], "trigger": vague["_hit"], "weak": False}
        if not metrics and r.clarification is None and r.intent in ("metric_query", "variance"):
            for amb in self.rank_amb:           # xếp hạng không nêu tiêu chí ("bán chạy nhất", "tốt nhất"...)
                hit = next((x for x in amb["_triggers"] + amb["_weak"]
                            if re.search(r"(?<![a-z0-9])" + re.escape(x) + r"(?![a-z0-9])", t)), None)
                if not hit:
                    continue
                pm = prefs.get(amb["_pref_key"])
                if pm in self.sem.metrics:
                    metrics = [pm]
                    r.assumptions.append(f"Xếp hạng theo {self.sem.metrics[pm]['label']} (theo lựa chọn trước đó).")
                else:
                    r.clarification = self._clarify(amb)
                    r.matched["ambiguity"] = {"id": amb["id"], "trigger": hit, "weak": hit in amb["_weak"]}
                break

        if phrase_having and (dims or grain) and r.clarification is None:
            for h in phrase_having:     # chỉ số của điều kiện lên đầu; thành phần của nó vẫn hiện trong bảng kết quả
                if h["metric"] not in metrics:
                    comps = set(self.sem.base_metrics(h["metric"]))
                    metrics = [h["metric"]] + [m for m in metrics if m not in comps]
            having += phrase_having

        # ---------------------------------------------------------------- thời gian -> plan
        time_range, compare = None, None
        if cur:
            if len(cur) > 1 and not cmp:
                s, e = min(x.start for x in cur), max(x.end for x in cur)
                if re.search(r"\b(va|voi|,)\b", t) and grain is None and all(x.start.day == 1 for x in cur):
                    grain = "thang" if all((x.end - x.start).days <= 31 for x in cur) else ("quy" if all((x.end - x.start).days <= 92 for x in cur) else "nam")
                time_range = {"start": s.isoformat(), "end": e.isoformat(), "label": _period_label(s, e)}
            else:
                c0 = cur[0]
                time_range = {"start": c0.start.isoformat(), "end": c0.end.isoformat(), "label": c0.label}
        if cmp_pos is not None or (r.intent == "variance") or (growth and time_range and not dims):
            # "giảm bao nhiêu so với cùng kỳ" mà không nêu kỳ: tháng gần nhất đã đủ dữ liệu, thay vì so toàn bộ dữ liệu
            if time_range is None and (r.intent == "variance" or (cmp_pos is not None and not cmp and metrics)):
                ps = month_span(*(lambda d: (d.year, d.month))(_add_months(dt.date(self.anchor.year, self.anchor.month, 1), -1)))
                time_range = {"start": ps[0].isoformat(), "end": ps[1].isoformat(), "label": f"T{ps[0].month}/{ps[0].year}"}
                r.assumptions.append(f"Chưa nêu kỳ phân tích — dùng tháng gần nhất đã đủ dữ liệu ({time_range['label']}).")
            if time_range:
                s, e = dt.date.fromisoformat(time_range["start"]), dt.date.fromisoformat(time_range["end"])
                if cmp and cmp[0].relative != "prev":
                    compare = {"start": cmp[0].start.isoformat(), "end": cmp[0].end.isoformat(), "label": cmp[0].label}
                elif yoy:
                    ps, pe, lab = same_period_last_year(s, e)
                    compare = {"start": ps.isoformat(), "end": pe.isoformat(), "label": lab + " (cùng kỳ)"}
                elif cmp_pos is not None or r.intent == "variance" or growth:
                    ps, pe, lab = previous_period(s, e)
                    compare = {"start": ps.isoformat(), "end": pe.isoformat(), "label": lab + " (kỳ trước)"}
        # xếp hạng các kỳ ("tháng nào bán tệ nhất"): kỳ đang chạy dở chưa đủ dữ liệu, so với kỳ trọn vẹn là sai -> bỏ ra
        if (rank_time or (direction and grain in ("tuan", "thang", "quy", "nam"))) and r.intent == "metric_query":
            A = self.anchor
            ps = {"tuan": A - dt.timedelta(days=A.weekday()), "thang": dt.date(A.year, A.month, 1),
                  "quy": dt.date(A.year, 3 * ((A.month - 1) // 3) + 1, 1), "nam": dt.date(A.year, 1, 1)}.get(grain)
            if ps:
                pe = ps + dt.timedelta(weeks=1) if grain == "tuan" else _add_months(ps, {"thang": 1, "quy": 3, "nam": 12}[grain])
                s0 = dt.date.fromisoformat(time_range["start"]) if time_range else self.data_start
                e0 = dt.date.fromisoformat(time_range["end"]) if time_range else A + dt.timedelta(days=1)
                if A + dt.timedelta(days=1) < pe and s0 < ps < e0:
                    time_range = {"start": s0.isoformat(), "end": ps.isoformat(), "label": _period_label(s0, ps)}
                    r.assumptions.append(f"Bỏ {_period_label(ps, pe)} khỏi xếp hạng vì kỳ này mới có dữ liệu đến {_fmt(A)}, "
                                         "chưa so được với các kỳ trọn vẹn.")
        if time_range is None and r.intent == "metric_query" and metrics:
            semi = all(self.sem.models[mdl].get("semi_additive") for m in metrics for mdl in self.sem.metric_models(m))
            win = self.sem.data_window(metrics) if semi else None
            if semi:        # tồn kho: không cộng dồn qua thời gian, lấy ảnh chụp mới nhất
                r.assumptions.append("Không nêu thời gian — lấy số liệu tại ngày chốt gần nhất"
                                     + (f" ({_fmt(dt.date.fromisoformat(win[1]))})." if win else "."))
            else:
                r.assumptions.append(f"Không nêu thời gian — tính trên toàn bộ dữ liệu ({_fmt(self.data_start)}–{_fmt(self.anchor)}).")
        if time_range and time_range["end"] > self.anchor.isoformat():
            if time_range["start"] <= self.anchor.isoformat():
                r.assumptions.append(f"Dữ liệu mới có đến {_fmt(self.anchor)} (ngày cuối có thể chưa nạp đủ).")
        if grain == "auto":
            if time_range:
                days = (dt.date.fromisoformat(time_range["end"]) - dt.date.fromisoformat(time_range["start"])).days
                grain = "ngay" if days <= 45 else ("tuan" if days <= 120 else "thang")
            else:
                grain = "thang"

        all_dims = ([grain] if grain else []) + [d for d in dims if d not in ("ngay", "tuan", "thang", "quy", "nam")]
        # metric không hỗ trợ chiều -> bỏ chiều + ghi chú
        if metrics:
            ok_dims = set.intersection(*[set(self.sem.dims_for_metric(m)) for m in metrics])
            dropped = [d for d in all_dims if d not in ok_dims]
            if dropped:
                if grain in dropped and grain in ("ngay", "tuan") and "thang" in ok_dims:
                    all_dims = ["thang" if d == grain else d for d in all_dims]
                    r.assumptions.append("Chỉ số chỉ có độ chi tiết theo tháng — đã chuyển sang theo tháng.")
                    dropped = [d for d in dropped if d != grain]
                for d in dropped:
                    if d in all_dims:
                        all_dims.remove(d)
                    r.assumptions.append(f"Bỏ chiều '{self.sem.dims[d]['label']}' vì chỉ số không phân tích được theo chiều này.")
            for d in list(filters):
                if d not in ok_dims:
                    r.assumptions.append(f"Bỏ bộ lọc '{self.sem.dims[d]['label']}' vì chỉ số không hỗ trợ.")
                    filters.pop(d)
        plan = {"metrics": metrics, "dimensions": all_dims, "filters": list(filters.values()),
                "time_range": time_range, "compare": compare if compare and r.intent != "variance" else None,
                "limit": limit}
        if r.intent == "variance":
            plan["variance_compare"] = compare
        if direction and metrics:
            plan["order_by"] = [{"field": metrics[0], "direction": direction}]
        if having and metrics:
            for h in having:
                h.setdefault("metric", metrics[0])
            plan["having"] = having
        if per and metrics and per not in all_dims:
            plan["per"] = per
        r.matched["direction"] = direction
        r.plan = plan
        if r.intent == "metric_query" and not metrics and r.clarification is None:
            r.intent = "unknown"
            r.confidence = 0.2
        if r.intent in ("metric_info", "schema_info", "dq", "help"):
            r.metric_topic = metrics[0] if metrics else (f"__amb__:{vague['id']}" if vague else None)
            r.clarification = None
        # những từ có nghĩa mà bộ luật chưa dùng tới: tín hiệu cho biết plan có thể chưa thể hiện hết câu hỏi
        soft = set(FILLER) | ({str(limit)} if limit else set()) | ({grain} if grain else set())
        soft |= {"cung", "ky", "yoy"} if yoy else set()
        soft |= set(m.group(0).split()) if m else set()                 # "so với"
        soft |= set(re.findall(r"[a-z0-9]+", hm.group(0))) if having and hm else set()    # "trên 15 tỷ", "dưới 81%"
        for h in phrase_having if having else []:                       # "chưa đạt kế hoạch", "còn tồn"
            soft |= {w for x in self.having_phr if x["metric"] == h["metric"] for p in x["_phr"] for w in p.split()}
        toks = list(re.finditer(r"[a-z0-9]+", t))
        words = [(w, x) for w in re.findall(r"[^\s,;:?!()\"“”']+", question) for x in re.findall(r"[a-z0-9]+", norm(w))]
        if [x for _, x in words] == [k.group(0) for k in toks]:         # khớp từng từ gốc (có dấu) với vị trí đã chuẩn hóa
            left = [w for (w, x), k in zip(words, toks) if x not in soft and not any(mask[k.start():k.end()])]
        else:
            left = [k.group(0) for k in toks if k.group(0) not in soft and not any(mask[k.start():k.end()])]
        r.matched["unparsed"] = list(dict.fromkeys(left))
        return r

    def unknown_terms(self, r: NLUResult) -> tuple[list[str], dict | None]:
        """Từ lạ còn sót trong câu (bộ luật không biết, không phải từ thường) và gợi ý giá trị gần giống nhất nếu có.

        Trả về (các cụm từ lạ theo cách người dùng gõ, gợi ý {text, dimension, value} hoặc None). Dùng để không âm thầm bỏ
        qua tên người dùng gõ sai ("Aple") hay sản phẩm không có trong kho ("bơm nước") rồi trả số liệu của toàn bộ dữ liệu.
        """
        import difflib
        left = r.matched.get("unparsed") or []
        if not left:
            return [], None
        normed = [norm(w) for w in left]
        unknown = [w for w, n in zip(left, normed) if n and not n.isdigit() and n not in self.vocab and len(n) > 1]
        if not unknown:
            return [], None
        best = None
        for size in (3, 2, 1):                      # cụm 1-3 từ liên tiếp có chứa từ lạ, so với tên giá trị cùng số từ
            for i in range(len(left) - size + 1):
                win = left[i:i + size]
                if not any(w in unknown for w in win):
                    continue
                key = norm(" ".join(win))
                if len(key) < 4:
                    continue
                for p, dim, val, _ in self.value_phr:
                    if p.count(" ") != size - 1 or abs(len(p) - len(key)) > 2:
                        continue
                    ratio = difflib.SequenceMatcher(None, key, p).ratio()
                    if ratio >= 0.8 and (best is None or ratio > best[0]):
                        best = (ratio, " ".join(win), dim, val)
            if best:
                break
        sugg = {"text": best[1], "dimension": best[2], "value": best[3]} if best else None
        return unknown, sugg

    def draft_for_llm(self, question: str, memory: dict | None = None) -> dict | None:
        """Bản nháp plan do bộ luật lập, đưa cho LLM kiểm tra và hoàn thiện (None nếu bộ luật không lập được)."""
        r = self.parse(question, memory)
        if r.intent != "metric_query" or not r.plan.get("metrics") or r.clarification:
            return None
        plan = {k: v for k, v in r.plan.items() if v not in (None, [], {})}
        return {"plan": plan, "unparsed": r.matched.get("unparsed", [])}


_nlu: OfflineNLU | None = None


def get_nlu(reload: bool = False) -> OfflineNLU:
    global _nlu
    if _nlu is None or reload:
        _nlu = OfflineNLU()
    return _nlu
