"""
Tra cứu danh mục: thông tin của một đối tượng theo mã hoặc tên ("thông tin về SP0010", "CH036 Astra Phú Nhuận"),
liệt kê ("những bưu cục nào đã đóng cửa", "danh sách cửa hàng ở Hà Nội"), đếm ("có bao nhiêu cửa hàng") và xếp theo một
thuộc tính ("cửa hàng nào mới mở gần đây nhất").

Đối tượng tra cứu được khai báo ở semantic_layer.yml, mục `nlu.lookups`; nhãn cột lấy từ mô tả trong catalog, cột khóa
ngoại có mô tả dạng "-> schema.bảng" được thay bằng tên của đối tượng kia. SQL do code dựng rồi đi qua đúng các chốt
kiểm duyệt như mọi truy vấn khác (RBAC, cột PII/mật, RLS), nên người dùng chỉ thấy cột và dòng mình được phép xem.
Mã đối tượng (SP0010, CH036...) còn được bộ hiểu câu hỏi dùng làm bộ lọc trong câu hỏi về chỉ số.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from . import config
from .catalog import get_catalog
from .semantic import get_semantic, q
from .textutil import fmt_int, fmt_number, norm

SMALL_TABLE = 5000        # bảng danh mục từ chừng này dòng trở xuống thì nạp sẵn mã -> tên
LIST_LIMIT = 200
CODE_LIKE = re.compile(r"(?<![a-z0-9])[a-z]{2,4}\d{3,}(?![a-z0-9])")      # dạng mã: chữ + số (SP0010, CH036, KH000001)
INFO_RE = re.compile(r"\b(thong tin|chi tiet|tra cuu|ho so|la gi|la ai|la (san pham|cua hang|kho|ai|cai) gi|o dau|nam o dau)\b")
COUNT_RE = re.compile(r"\b(bao nhieu|may|tong so|so luong|dem)\b")
LIST_RE = re.compile(r"\b(danh sach|liet ke|nhung|cac|tat ca|nao|gom nhung|ke ten)\b")
SALES_RE = re.compile(r"\b(ban|mua|xuat|nhap|tra|giao|gui|doanh thu|doanh so)\b")
DESC_WORDS = r"(cao|lon|nhieu|dat|moi|gan day|muon|tre|dai)"
ASC_WORDS = r"(thap|nho|it|re|cu|som|lau doi|dau tien|ngan)"
GENERIC_WORDS = {"kho hang", "ma", "mau", "the", "hang", "ban"}       # tên gọi quá chung để nhận loại đối tượng
MONEY_COL = re.compile(r"(^|_)(gia|tien|luong|cuoc|phi|von|chi_phi|doanh_thu|fee|price|cost|amount|revenue)(_|$)")


def _head(desc: str) -> str:
    """Phần đầu mô tả nghiệp vụ của cột, dùng làm nhãn (vd 'Giá bán lẻ niêm yết (VND)' -> 'Giá bán lẻ niêm yết')."""
    return re.split(r"\s*(?:->|\(|—|:|;|,)", desc or "")[0].strip()


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _has(phrase: str, text: str) -> re.Match | None:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) if phrase else None


@dataclass
class Entity:
    id: str
    table: str
    key: str
    name_col: str | None
    label: str
    words: list = field(default_factory=list)       # cụm từ gọi loại đối tượng (đã chuẩn hóa)
    dimension: str | None = None                    # chiều phân tích tương ứng (để dùng mã làm bộ lọc chỉ số)
    states: list = field(default_factory=list)      # (cụm từ, vị từ SQL trên bí danh t, nhãn)
    codes: dict = field(default_factory=dict)       # mã đã chuẩn hóa -> (mã, tên)
    code_re: re.Pattern | None = None               # dạng mã của bảng lớn (không nạp sẵn)
    upper: bool = True
    refs: dict = field(default_factory=dict)        # cột khóa ngoại -> {table, key, name, alias, label}
    attrs: list = field(default_factory=list)       # (cụm từ, tên cột trong kết quả)


class Lookups:
    def __init__(self):
        self.entities: dict[str, Entity] = {}
        cfg = (config.semantic_layer().get("nlu") or {}).get("lookups") or {}
        cat, sem = get_catalog(), get_semantic()
        for eid, c in cfg.items():
            t = cat.get(str(c.get("table", "")))
            if not t or c.get("key") not in t["columns"]:
                continue
            dim = c.get("dimension") if c.get("dimension") in sem.dims else None
            name_col = c.get("name") if c.get("name") in t["columns"] else None
            label = c.get("label") or (t["synonyms"][0] if t["synonyms"] else eid)
            words = [label] + list(c.get("words") or []) + list(t["synonyms"])
            if dim:
                words += [sem.dims[dim]["label"]] + list(sem.dims[dim].get("synonyms") or [])
            words = sorted({norm(w) for w in words if norm(w) and len(norm(w)) > 2 and norm(w) not in GENERIC_WORDS},
                           key=len, reverse=True)
            e = Entity(eid, t["name"], c["key"], name_col, label, words, dim)
            for phrase, pred in (c.get("states") or {}).items():
                e.states.append((norm(phrase), pred, phrase))
            self.entities[eid] = e
        for e in self.entities.values():
            self._refs_attrs(e)
            self._states_from_values(e)
            self._codes(e)
        pats = sorted(((k, e.id) for e in self.entities.values() for k in e.codes), key=lambda x: -len(x[0]))
        self._code_owner = dict(pats)
        self._code_rx = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k, _ in pats) + r")(?![a-z0-9])") if pats else None

    # ------------------------------------------------------------------ chỉ mục
    def _refs_attrs(self, e: Entity) -> None:
        cat, sem = get_catalog(), get_semantic()
        t = cat.get(e.table)
        for c in t["columns"].values():
            m = re.search(r"->\s*([a-z_]+\.[a-z_]+)", c.get("description") or "")
            rt = cat.get(m.group(1)) if m else None
            if rt and rt["name"] != e.table:
                rkey = c["name"] if c["name"] in rt["columns"] else next(iter(rt["columns"]))
                other = next((o for o in self.entities.values() if o.table == rt["name"] and o.name_col), None)
                dim = next((d for d in sem.dims if sem.dim_column(d) and sem.dim_column(d)[0] == rt["name"]
                            and (not other or sem.dim_column(d)[1] == other.name_col)), None)
                rname = other.name_col if other else (sem.dim_column(dim)[1] if dim else None)
                if rname:
                    lab = sem.dims[dim]["label"] if dim else _cap(other.label)
                    alias = dim or f"{c['name']}_ten"
                    e.refs[c["name"]] = {"table": rt["name"], "key": rkey, "name": rname, "alias": alias, "label": lab}
                    for p in {norm(lab)} | {norm(x) for x in (sem.dims[dim].get("synonyms") or [] if dim else [])}:
                        if len(p) > 3:
                            e.attrs.append((p, alias))
                    continue
            for p in {norm(_head(c.get("description")))} | {norm(x) for x in c.get("synonyms") or []}:
                if len(p) > 3:
                    e.attrs.append((p, c["name"]))
        e.attrs.sort(key=lambda x: -len(x[0]))

    def _states_from_values(self, e: Entity) -> None:
        """Giá trị của cột phân loại ít giá trị (trạng thái, chức vụ...) thành điều kiện lọc, trừ cột đã là chiều phân tích."""
        cat, sem = get_catalog(), get_semantic()
        dim_cols = {sem.dim_column(d) for d in sem.dims}
        t = cat.get(e.table)
        for c in t["columns"].values():
            vals = c.get("values") or []
            if c["name"] in (e.key, e.name_col) or (e.table, c["name"]) in dim_cols or not 1 < len(vals) <= 12:
                continue
            for v in vals:
                p = norm(str(v))
                if len(p) >= 4 and not p.isdigit() and not any(s[0] == p for s in e.states):
                    e.states.append((p, f"t.{c['name']} = {q(v)}", f"{_head(c.get('description')) or c['name']}: {v}"))
        e.states.sort(key=lambda s: -len(s[0]))

    def _codes(self, e: Entity) -> None:
        from .warehouse import get_warehouse
        t = get_catalog().get(e.table)
        try:
            if t["rows"] <= SMALL_TABLE:
                cols = f"{e.key}, {e.name_col}" if e.name_col else f"{e.key}, NULL"
                rows = get_warehouse().fetch(f"SELECT DISTINCT {cols} FROM {e.table} WHERE {e.key} IS NOT NULL")
                e.upper = all(str(k) == str(k).upper() for k, _ in rows)
                for k, name in rows:
                    nk = norm(str(k))
                    if len(nk) >= 3 and not nk.isdigit() and re.search(r"\d|_", str(k)):
                        e.codes[nk] = (str(k), name)
            else:
                sample = [str(r[0]) for r in get_warehouse().fetch(
                    f"SELECT {e.key} FROM {e.table} WHERE {e.key} IS NOT NULL LIMIT 200")]
                shapes = {re.sub(r"\d", "#", norm(k)) for k in sample}
                if len(shapes) == 1:
                    shape = shapes.pop()
                    if "#" in shape and re.match(r"[a-z]", shape):     # "kh######" -> kh\d{6}
                        body = "".join(rf"\d{{{len(part)}}}" if part.startswith("#") else re.escape(part)
                                       for part in re.findall(r"#+|[^#]+", shape))
                        e.code_re = re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")
                        e.upper = all(k == k.upper() for k in sample)
        except Exception:
            pass

    # ------------------------------------------------------------------ dùng cho bộ hiểu câu hỏi
    def code_values(self):
        """(mã đã chuẩn hóa, chiều, tên) để bộ hiểu câu hỏi lọc chỉ số theo mã ("doanh thu của SP0010")."""
        sem = get_semantic()
        for e in self.entities.values():
            if e.dimension and e.name_col:
                # chiều phân tích đứng trên cột mã (nhân viên, khách hàng): lọc theo mã; còn lại lọc theo tên
                by_key = (sem.dim_column(e.dimension) or ("", ""))[1] == e.key
                for nk, (code, name) in e.codes.items():
                    if by_key or name:
                        yield nk, e.dimension, code if by_key else name

    def find_codes(self, text: str) -> tuple[list[tuple[Entity, str, int]], list[str]]:
        """Mã đối tượng có trong câu (đã chuẩn hóa): ([(đối tượng, mã gốc, vị trí)], [mã không tìm thấy])."""
        t = re.sub(r"[^a-z0-9 ]+", " ", norm(text))
        hits, spans = [], []
        if self._code_rx:
            for m in self._code_rx.finditer(t):
                e = self.entities[self._code_owner[m.group(1)]]
                hits.append((e, e.codes[m.group(1)][0], m.start()))
                spans.append((m.start(), m.end()))
        for e in self.entities.values():
            for m in e.code_re.finditer(t) if e.code_re else []:
                if not any(a <= m.start() < b for a, b in spans):
                    hits.append((e, m.group(0).upper() if e.upper else m.group(0), m.start()))
                    spans.append((m.start(), m.end()))
        unknown = [m.group(0).upper() for m in CODE_LIKE.finditer(t) if not any(a <= m.start() < b for a, b in spans)]
        return sorted(hits, key=lambda h: h[2]), unknown

    def typed(self, text: str) -> tuple[Entity, re.Match] | None:
        """Loại đối tượng người dùng nhắc tới ("cửa hàng", "bưu cục"...), cụm dài nhất, sớm nhất."""
        best = None
        for e in self.entities.values():
            for w in e.words:
                m = _has(w, text)
                if m and (best is None or (m.start(), -len(w)) < (best[1].start(), -len(best[1].group(0)))):
                    best = (e, m)
        return best

    # ------------------------------------------------------------------ nhận diện câu tra cứu
    def detect(self, question: str, r) -> dict | None:
        """Câu hỏi là tra cứu danh mục? r = kết quả bộ hiểu câu hỏi (NLUResult). Trả về spec hoặc None."""
        if not self.entities or r.intent == "variance":
            return None
        t = re.sub(r"[^a-z0-9 ]+", " ", norm(question))
        t = re.sub(r"\s+", " ", t).strip()
        plan = r.plan or {}
        codes, unknown = self.find_codes(question)
        typed = self.typed(t)
        mode = None
        superl = re.search(r"\b" + DESC_WORDS + r"( [a-z]+){0,2} nhat\b|\bgan day nhat\b|\bmoi nhat\b", t) and "desc" or \
            (re.search(r"\b" + ASC_WORDS + r"( [a-z]+){0,2} nhat\b", t) and "asc")
        ranked_attr = None          # "top 5 sản phẩm có giá niêm yết cao nhất": xếp theo thuộc tính, không phải chỉ số
        if typed and superl and not plan.get("metrics") and not SALES_RE.search(t):
            ranked_attr = self._attr(typed[0], t)
        # "có bao nhiêu sản phẩm đang kinh doanh": đếm đối tượng, kể cả khi bộ luật đã khớp nhầm thành chỉ số
        if typed and not codes and not plan.get("time_range") and not SALES_RE.search(t) and \
                re.search(r"\b(bao nhieu|may|tong so|dem so)\s+(" + "|".join(map(re.escape, typed[0].words)) + r")\b", t):
            mode = "count"
        elif plan.get("metrics") or (r.clarification and not ranked_attr):
            return None
        e, keys, names = None, [], []
        if codes:
            e = codes[0][0]
            keys = list(dict.fromkeys(k for ent, k, _ in codes if ent is e))
        else:       # tên đối tượng do bộ hiểu câu hỏi nhận ra; "nhân viên của Astra Phú Nhuận" thì tên cửa hàng chỉ là bộ lọc
            for f in plan.get("filters") or []:
                ent = next((x for x in self.entities.values() if x.dimension == f["dimension"] and x.name_col), None)
                if ent and f.get("op") != "not_in" and (typed is None or typed[0] is ent):
                    e, names = ent, [(f.get("op") or "in", v) for v in f["values"]]
                    break
        frag = None
        if not codes and not names and not plan.get("metrics") and not plan.get("time_range") and INFO_RE.search(t) \
                and not SALES_RE.search(t):
            # "thông tin về 17 Pro": một phần tên đối tượng (không có tên đầy đủ, không có mã) -> các đối tượng có tên chứa cụm đó
            hit = self.name_fragment(t, typed[0] if typed else None)
            if hit:
                e, frag, matched = hit
                names = [("in", n) for n in matched]
        if e is None and typed:
            e = typed[0]
        if e is None:
            return None
        if unknown and not codes and (typed or INFO_RE.search(t)):
            return {"entity": e.id, "mode": "missing", "codes": unknown}
        attr = self._attr(e, t)
        states =[(p, pred, lab) for p, pred, lab in e.states if _has(p, t)]
        states = [x for x in states if not any(x[0] != y[0] and _has(x[0], y[0]) for y in states)]   # "đã đóng cửa" ⊃ "đóng cửa"
        name_dim = e.dimension if names else None
        filters = [f for f in plan.get("filters") or [] if f["dimension"] != name_dim]
        if not (keys or names) and mode is None:
            # danh mục không phụ thuộc thời gian: "cửa hàng nào ... tháng 10/2026" là câu hỏi hoạt động, không phải tra cứu;
            # "... nhất" mà không rõ xếp theo thuộc tính nào thì để luồng chỉ số (hỏi lại tiêu chí / LLM) xử lý
            if plan.get("time_range") or (superl and not attr) or re.search(r"\bnhat\b", t) and not attr:
                return None
        if mode is None:
            if keys or names:
                mode = "one" if len(keys) + len(names) == 1 else "list"
            elif COUNT_RE.search(t) and not superl and not SALES_RE.search(t) and not re.search(r"\b(trung binh|binh quan)\b", t):
                mode = "count"     # "mỗi cửa hàng bán được bao nhiêu" hỏi số liệu bán, không phải đếm cửa hàng
            elif LIST_RE.search(t) or states or filters or (superl and attr):
                mode = "list"
            else:
                return None
        spec = {"entity": e.id, "mode": mode, "keys": keys, "names": names, "fragment": frag,
                "states": [[p, pred, lab] for p, pred, lab in states],
                "filters": filters, "attr": attr, "order": None, "limit": None,
                "time_ignored": bool(plan.get("time_range"))}
        if superl and attr and mode == "list":
            spec["order"] = [attr, superl]
            spec["limit"] = plan.get("limit") or (1 if re.search(r"\bnao\b", t) and not re.search(r"\b(nhung|cac)\b", t) else 10)
        return spec

    FRAG_RE = re.compile(r"\b(?:thong tin|chi tiet|tra cuu|ho so)(?: (?:ve|cua|cho|san pham|mat hang|cua hang|chi nhanh))*"
                         r" (.+?)(?: (?:la gi|nhu the nao|the nao|ra sao|duoc khong|nhe|nha|voi|a|di|giup minh|giup toi))*$")

    def name_fragment(self, t: str, prefer: Entity | None = None, max_hits: int = 30):
        """Cụm sau "thông tin về ..." là một phần tên của các đối tượng trong danh mục nhỏ (vd "17 pro" trong
        "iPhone 17 Pro Max"). Trả về (đối tượng, cụm gốc, [tên đầy đủ khớp]) hoặc None."""
        m = self.FRAG_RE.search(t)
        if not m:
            return None
        frag = m.group(1).strip()
        if prefer:          # bỏ chữ chỉ loại đối tượng ("sản phẩm iphone 17 pro" -> "iphone 17 pro")
            for w in prefer.words:
                frag = re.sub(r"^" + re.escape(w) + r"\s+", "", frag)
        if len(frag) < 2 or not re.search(r"[a-z0-9]", frag) or frag in GENERIC_WORDS:
            return None
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(frag) + r"(?![a-z0-9])")
        best = None
        for e in ([prefer] if prefer else []) + [x for x in self.entities.values() if x is not prefer]:
            if not e.name_col:
                continue
            hits = list(dict.fromkeys(n for _, n in e.codes.values() if n and pat.search(norm(str(n)))))
            if hits and len(hits) <= max_hits:
                best = (e, frag, sorted(hits, key=lambda n: (len(str(n)), str(n))))
                break
        return best

    @staticmethod
    def _attr(e: Entity, t: str) -> str | None:
        """Thuộc tính người dùng nhắc tới; "đắt/rẻ nhất" mà không nêu cột thì hiểu là theo giá bán."""
        attr = next((col for p, col in e.attrs if _has(p, t)), None)
        if attr is None and re.search(r"\b(dat|re)( [a-z]+){0,2} nhat\b", t):
            money = [c for c in get_catalog().get(e.table)["columns"].values()
                     if MONEY_COL.search(c["name"]) and re.search(r"INT|DOUBLE|DECIMAL|FLOAT|NUMERIC|REAL", c.get("type") or "")]
            sell = [c for c in money if re.search(r"\bban\b|niem yet|sell|price", norm(" ".join([c.get("description") or ""]
                                                                                          + (c.get("synonyms") or []))))]
            attr = (sell or money or [{}])[0].get("name")
        return attr

    # ------------------------------------------------------------------ SQL
    def build(self, spec: dict, role_cfg: dict) -> dict:
        """SQL tra cứu + mô tả cột. Cột bị hạn chế theo vai trò bị bỏ khỏi SELECT (validator vẫn kiểm lại)."""
        cat, sem = get_catalog(), get_semantic()
        e = self.entities[spec["entity"]]
        deny = set(role_cfg.get("deny_sensitivity") or [])
        t = cat.get(e.table)
        sel, joins, cols, hidden, ref_alias = [], [], [], [], {}
        for c in t["columns"].values():
            if c.get("sensitivity") in deny:
                hidden.append(_head(c.get("description")) or c["name"])
                continue
            ref = e.refs.get(c["name"])
            if ref and cat.sensitivity(ref["table"], ref["name"]) not in deny:
                a = f"j{len(ref_alias) + 1}"
                ref_alias[ref["table"]] = ref_alias.get(ref["table"]) or a
                joins.append(f"LEFT JOIN {ref['table']} {a} ON {a}.{ref['key']} = t.{c['name']}")
                sel.append(f"{a}.{ref['name']} AS {ref['alias']}")
                cols.append({"name": ref["alias"], "label": ref["label"], "type": "text"})
            else:
                sel.append(f"t.{c['name']}")
                cols.append({"name": c["name"], "label": _head(c.get("description")) or c["name"], "type": c.get("type") or ""})
        where, conds, unapplied = [], [], []
        if spec["keys"]:
            where.append(f"t.{e.key} IN ({', '.join(q(k) for k in spec['keys'])})")
        if spec["names"]:
            ors = [f"t.{e.name_col} ILIKE {q('%' + v + '%')}" if op == "like" else f"t.{e.name_col} = {q(v)}"
                   for op, v in spec["names"]]
            where.append("(" + " OR ".join(ors) + ")" if len(ors) > 1 else ors[0])
        for p, pred, lab in spec["states"]:
            where.append(pred)
            conds.append(lab)
        for f in spec["filters"]:
            loc = sem.dim_column(f["dimension"])
            alias = "t" if loc and loc[0] == e.table else (ref_alias.get(loc[0]) if loc else None)
            if not alias:
                unapplied.append(sem.dims[f["dimension"]]["label"])
                continue
            vals = f.get("values") or []
            op = f.get("op") or "in"
            if op == "like":
                ex = " OR ".join(f"{alias}.{loc[1]} ILIKE {q('%' + str(v) + '%')}" for v in vals)
                where.append(f"({ex})")
            else:
                where.append(f"{alias}.{loc[1]} {'NOT IN' if op == 'not_in' else 'IN'} ({', '.join(q(v) for v in vals)})")
            conds.append(f"{sem.dims[f['dimension']]['label']}: {'khác ' if op == 'not_in' else ''}{', '.join(map(str, vals))}")
        frm = f"FROM {e.table} t" + "".join("\n" + j for j in joins)
        wh = ("\nWHERE " + "\n  AND ".join(where)) if where else ""
        if spec["mode"] == "count":
            sql = f"SELECT COUNT(*) AS so_luong\n{frm}{wh}"
            cols = [{"name": "so_luong", "label": f"Số {e.label}", "type": "BIGINT"}]
        else:
            order = f"t.{e.key}"
            if spec.get("order"):
                ocol, d = spec["order"]
                order = f"{ocol if ocol not in t['columns'] else 't.' + ocol} {'DESC' if d == 'desc' else 'ASC'} NULLS LAST"
            limit = spec.get("limit") or LIST_LIMIT
            sql = "SELECT " + ",\n       ".join(sel) + f"\n{frm}{wh}\nORDER BY {order}\nLIMIT {limit}"
        return {"sql": sql, "columns": cols, "hidden": hidden, "conds": conds, "unapplied": unapplied}

    # ------------------------------------------------------------------ kết quả -> văn bản có kiểm chứng
    def render(self, spec: dict, built: dict, columns: list, rows: list, user) -> dict:
        e = self.entities[spec["entity"]]
        meta = {c["name"]: c for c in built["columns"]}
        facts: dict = {}
        notes = []

        def fact(fid, value, recipe, text=None):
            facts[fid] = {"id": fid, "value": value, "text": text if text is not None else str(value), "recipe": recipe,
                          "kind": "context" if recipe["op"] == "context" else "number"}
            return facts[fid]["text"]

        if built["hidden"]:
            notes.append(f"Đã ẩn {', '.join(built['hidden'])} vì tài khoản của bạn không được xem dữ liệu cá nhân/mật.")
        if built["unapplied"]:
            notes.append(f"Chưa lọc được danh mục {e.label} theo {', '.join(built['unapplied'])}.")
        if spec.get("time_ignored"):
            notes.append("Đây là thông tin danh mục, không phụ thuộc khoảng thời gian trong câu hỏi.")
        cond = ""
        if built["conds"]:
            cond = " (" + "; ".join(fact(f"cond{i}", c, {"op": "context"}) for i, c in enumerate(built["conds"])) + ")"
        who = ", ".join(spec["keys"]) or ", ".join(v for _, v in spec["names"])
        if not rows or (spec["mode"] == "count" and rows[0][0] is None):
            scope = " trong phạm vi dữ liệu tài khoản của bạn được xem" if user.rls else ""
            ref = f" {fact('ref', who, {'op': 'context'})}" if who else cond
            text = f"Không tìm thấy {e.label}{ref}{scope}."
            return {"text": text, "template": text, "facts": facts, "columns": columns, "rows": rows,
                    "meta": self._meta(columns, meta), "notes": notes, "follow_ups": []}
        if spec["mode"] == "count":
            n = rows[0][0]
            text = f"Có **{fact('n', float(n), {'op': 'cell', 'row': 0, 'col': 'so_luong'}, fmt_int(n))}** {e.label}{cond}."
            return {"text": text, "template": text, "facts": facts, "columns": columns, "rows": rows,
                    "meta": self._meta(columns, meta), "notes": notes, "follow_ups": []}
        disp = [[self._cell(v, c, meta) for v, c in zip(r, columns)] for r in rows]
        ki = columns.index(e.key) if e.key in columns else None
        ni = columns.index(e.name_col) if e.name_col in columns else None
        if spec["mode"] == "one" and len(rows) == 1:
            pairs = [(meta.get(c, {}).get("label", c), v, c) for c, v in zip(columns, disp[0]) if v != "—"]
            out_cols = ["thong_tin", "gia_tri"]
            out_rows = [[lab, v] for lab, v, _ in pairs]
            idx = {c: i for i, (_, _, c) in enumerate(pairs)}
            nm = fact("name", out_rows[idx[e.name_col]][1], {"op": "label", "row": idx[e.name_col], "col": "gia_tri"}) \
                if e.name_col in idx else None
            key = fact("key", out_rows[idx[e.key]][1], {"op": "label", "row": idx[e.key], "col": "gia_tri"}) if e.key in idx else who
            title = f"**{nm}** (mã {key})" if nm and nm != key else f"mã **{key}**"
            attr = spec.get("attr")
            if attr in idx and attr not in (e.key, e.name_col):
                val = fact("attr", out_rows[idx[attr]][1], {"op": "label", "row": idx[attr], "col": "gia_tri"})
                text = f"{_cap(out_rows[idx[attr]][0])} của {e.label} {title} là **{val}**."
            else:
                text = f"Thông tin {e.label} {title}:"
            meta2 = [{"name": "thong_tin", "label": "Thông tin", "kind": "dimension", "format": "text"},
                     {"name": "gia_tri", "label": "Giá trị", "kind": "dimension", "format": "text"}]
            return {"text": text, "template": text, "facts": facts, "columns": out_cols, "rows": out_rows, "meta": meta2,
                    "notes": notes, "follow_ups": self.follow_ups(e, rows[0][ni] if ni is not None else None)}
        if spec.get("order") and spec.get("limit") == 1:
            ocol, d = spec["order"]
            oi = columns.index(ocol)
            olab = meta.get(ocol, {}).get("label", ocol).lower()
            is_date = bool(re.match(r"\d{2}/\d{2}/\d{4}$", disp[0][oi]))
            word = ("mới nhất" if d == "desc" else "sớm nhất") if is_date else ("cao nhất" if d == "desc" else "thấp nhất")
            nm = fact("name", disp[0][ni if ni is not None else ki], {"op": "label", "row": 0,
                                                                      "col": columns[ni if ni is not None else ki]})
            val = fact("val", disp[0][oi], {"op": "label", "row": 0, "col": ocol})
            text = f"{_cap(e.label)} có {olab} {word} là **{nm}** ({val}){cond}."
        elif spec.get("order"):             # "top 5 sản phẩm có giá niêm yết cao nhất"
            ocol, d = spec["order"]
            oi, nci = columns.index(ocol), (ni if ni is not None else ki)
            is_date = bool(re.match(r"\d{2}/\d{2}/\d{4}$", disp[0][oi]))
            word = ("mới nhất" if d == "desc" else "sớm nhất") if is_date else ("cao nhất" if d == "desc" else "thấp nhất")
            n = fact("n", float(len(rows)), {"op": "count", "col": columns[0]}, fmt_int(len(rows)))
            nm = fact("name", disp[0][nci], {"op": "label", "row": 0, "col": columns[nci]})
            val = fact("val", disp[0][oi], {"op": "label", "row": 0, "col": ocol})
            text = (f"{n} {e.label} có {meta.get(ocol, {}).get('label', ocol).lower()} {word}{cond}; "
                    f"dẫn đầu là **{nm}** ({val}).")
        else:
            n = len(rows)
            if n >= (spec.get("limit") or LIST_LIMIT) and not spec.get("limit"):
                text = f"Danh sách {e.label}{cond} (đã cắt bớt, chỉ hiển thị phần đầu)."
            else:
                text = f"Có **{fact('n', float(n), {'op': 'count', 'col': columns[0]}, fmt_int(n))}** {e.label}{cond}"
                frag = spec.get("fragment")
                if frag:        # khớp theo một phần tên: nói rõ cụm đã dùng, kể tên các đối tượng tìm được
                    shown = frag
                    for nm0 in (r[ni] for r in rows if ni is not None):
                        k = norm(str(nm0)).find(frag)
                        if k >= 0 and len(norm(str(nm0))) == len(str(nm0)):
                            shown = str(nm0)[k:k + len(frag)]
                            break
                    text += f" có tên chứa “{fact('frag', shown, {'op': 'context'})}”"
                nci = ni if ni is not None else ki
                if nci is not None and 1 < n <= 8:
                    text += ": " + ", ".join(f"**{fact(f'nm{i}', disp[i][nci], {'op': 'label', 'row': i, 'col': columns[nci]})}**"
                                             for i in range(n))
                text += ". Bảng dưới có đủ thông tin từng " + e.label + "." if n > 1 else "."
        fu = []
        if spec.get("fragment") and ni is not None:         # hỏi tiếp số liệu bán của các đối tượng vừa tìm được
            for nm0 in [rows[0][ni]] + ([rows[-1][ni]] if len(rows) > 1 else []):
                fu += [x for x in self.follow_ups(e, nm0)[:1] if x not in fu]
        return {"text": text, "template": text, "facts": facts, "columns": columns, "rows": disp,
                "meta": self._meta(columns, meta), "notes": notes, "follow_ups": fu}

    @staticmethod
    def _meta(columns, meta) -> list[dict]:
        return [{"name": c, "label": meta.get(c, {}).get("label", c), "kind": "dimension", "format": "text"} for c in columns]

    @staticmethod
    def _cell(v, col, meta):
        if v is None:
            return "—"
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if MONEY_COL.search(col):
                return f"{fmt_int(v)} ₫"
            if re.match(r"(năm|year)\b", (meta.get(col, {}).get("label") or "").lower()) and float(v).is_integer():
                return str(int(v))          # năm sinh 1975, không viết 1.975
            return fmt_number(v)
        s = str(v)
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[ T]00:00:00)?", s)
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else s

    def follow_ups(self, e: Entity, name) -> list[str]:
        """Gợi ý hỏi tiếp số liệu của chính đối tượng vừa tra (chỉ giữ câu bộ hiểu câu hỏi lọc đúng đối tượng này)."""
        if not (e.dimension and name):
            return []
        from .nlu import get_nlu
        from .profile import sample_questions
        sem = get_semantic()
        a = dt.date.fromisoformat(get_catalog().anchor_date())
        last = (a.replace(day=1) - dt.timedelta(days=1))
        period = f"T{last.month}/{last.year}"
        samples = norm(" ".join(sample_questions()))
        cands = [m for m in sem.metrics if e.dimension in sem.dims_for_metric(m)]
        pos = {m: samples.find(norm(sem.metrics[m]["label"])) for m in cands}      # chỉ số có trong câu hỏi mẫu xếp trước
        cands.sort(key=lambda m: (pos[m] < 0, pos[m], list(sem.metrics).index(m)))
        out = []
        for m in cands:
            text = f"{sem.metrics[m]['label']} {name} {period}"
            r = get_nlu().parse(text, {})
            if r.plan.get("metrics") == [m] and any(f["dimension"] == e.dimension for f in r.plan.get("filters") or []):
                out.append(text)
            if len(out) == 2:
                break
        return out


_lookups: Lookups | None = None


def get_lookups(reload: bool = False) -> Lookups:
    global _lookups
    if _lookups is None or reload:
        _lookups = Lookups()
    return _lookups
