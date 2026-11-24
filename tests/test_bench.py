"""Benchmark có nhãn loại câu (app/bench.py): nạp & kiểm tra bộ câu hỏi, các cách làm đối chứng, chấm điểm theo loại câu.
Mô hình là client giả (fake_llm.complete_json trả JSON do test đặt), không gọi mạng."""
import json

import pytest

from app import bench, config

VI_DU = str(config.EVAL_DIR / "benchmark" / "vi_du.json")
GOLD_TOP5 = ("SELECT sp.ten_sp, SUM(f.tt - f.tien_tra) AS dtt FROM dw.fct_ct_dh f JOIN dw.dim_sp sp ON sp.ma_sp = f.ma_sp "
             "WHERE f.trang_thai = 'HOAN_THANH' AND f.ngay_dh >= DATE '2026-07-01' AND f.ngay_dh < DATE '2026-10-01' "
             "GROUP BY 1 ORDER BY 2 DESC LIMIT 5")


@pytest.fixture(scope="module")
def items():
    return {q["id"]: q for q in bench.load_items([VI_DU])}


def _run(fake_llm, q, condition, reply):
    fake_llm.json_reply = reply
    pred = bench.run_baseline(condition, q, {}, "openrouter")
    return pred, bench.score(q, pred, condition)


def test_legacy_questions_get_a_category():
    by_id = {q["id"]: q for q in bench.load_items()}
    assert by_id["t01"]["category"] == "clear" and by_id["t15"]["category"] == "followup"
    assert by_id["t16"]["category"] == "ambiguous" and by_id["h01"]["origin"] == "heldout.json"
    assert {q["category"] for q in by_id.values()} <= set(bench.CATEGORIES)


def test_example_file_is_valid_and_interpretations_really_differ(items):
    probs = bench.check_items(list(items.values()))
    assert not [p for p in probs if p["level"] == "error"], probs
    assert not any("không thật sự mơ hồ" in p["message"] for p in probs)


def test_check_flags_missing_gold_and_fake_ambiguity():
    same = "SELECT COUNT(*) FROM dw.dim_cn"
    probs = bench.check_items([
        {"id": "x1", "category": "clear", "question": "abc", "author": "a"},
        {"id": "x2", "category": "ambiguous", "question": "def", "author": "a",
         "interpretations": [{"label": "A", "gold_sql": same}, {"label": "B", "gold_sql": same}]},
        {"id": "x2", "category": "nonsense", "question": "ghi"},
    ])
    msgs = {(p["id"], p["message"].split(" ")[0]) for p in probs}
    assert ("x1", "câu") in msgs                                                   # thiếu gold_sql
    assert any("không thật sự mơ hồ" in p["message"] for p in probs if p["id"] == "x2")
    assert any("trùng id" in p["message"] for p in probs) and any("loại câu không hợp lệ" in p["message"] for p in probs)


def test_csv_template_imports(tmp_path):
    items = bench.import_csv(str(config.EVAL_DIR / "benchmark" / "mau_ghi_nhan.csv"))
    by_cat = {q["category"] for q in items}
    assert by_cat == {"clear", "followup", "ambiguous", "out_of_scope", "forbidden"}
    fu = next(q for q in items if q["category"] == "followup")
    amb = next(q for q in items if q["category"] == "ambiguous")
    assert len(fu["turns"]) == 2 and len(amb["interpretations"]) == 2 and all(q.get("author") for q in items)
    assert not [p for p in bench.check_items(items) if p["level"] == "error"]


def test_prompts_differ_only_in_context(items):
    from app.governance import UserCtx
    u = UserCtx("a", "admin", {})
    raw, cat, sem = (bench.baseline_prompt(c, u, [], "q")[0] for c in ("raw_schema", "catalog", "semantic"))
    assert "fct_ct_dh" in raw and "Thành tiền" not in raw and "SEMANTIC LAYER" not in raw
    assert "Thành tiền" in cat and "SEMANTIC LAYER" not in cat
    assert "SEMANTIC LAYER" in sem and "JOIN dw.dim_sp sp ON f.ma_sp = sp.ma_sp" in sem
    head = raw.split("DATABASE SCHEMA")[0]
    assert cat.startswith(head) and sem.startswith(head)


def test_correct_sql_scores_ok_and_wrong_sql_does_not(fake_llm, items):
    q = items["vd01"]
    assert _run(fake_llm, q, "raw_schema", {"action": "sql", "sql": GOLD_TOP5})[1]["outcome"] == "ok"
    bad = GOLD_TOP5.replace("LIMIT 5", "LIMIT 3")
    assert _run(fake_llm, q, "raw_schema", {"action": "sql", "sql": bad})[1]["outcome"] == "wrong_result"
    assert _run(fake_llm, q, "raw_schema", {"action": "sql", "sql": "DROP TABLE dw.dim_sp"})[0]["error_class"] == "invalid_sql"
    assert _run(fake_llm, q, "catalog", {"action": "clarify", "question": "?"})[1]["outcome"] == "over_clarify"


def test_followup_history_reaches_the_baseline_prompt(fake_llm, items):
    _run(fake_llm, items["vd03"], "semantic", {"action": "refuse"})
    assert "tháng 9/2026" in fake_llm.last_prompt[1] and fake_llm.last_prompt[1].endswith("còn tháng 10 thì sao?")


def test_ambiguous_scoring_separates_asking_lucky_guess_and_wrong_guess(fake_llm, items):
    q = items["vd04"]
    assert _run(fake_llm, q, "semantic", {"action": "clarify", "question": "thuần hay gộp?"})[1]["outcome"] == "clarified"
    net = q["interpretations"][0]["gold_sql"]
    assert _run(fake_llm, q, "semantic", {"action": "sql", "sql": net})[1]["outcome"] == "guess_ok"
    wrong = net.replace("f.tt - f.tien_tra", "f.tt")
    assert _run(fake_llm, q, "semantic", {"action": "sql", "sql": wrong})[1]["outcome"] == "guess_wrong"


def test_missing_guardrails_leak_restricted_data_and_guarded_condition_blocks_it(fake_llm, items):
    q = items["vd09"]                                                    # analyst hỏi lương (dữ liệu mật)
    sql = {"action": "sql", "sql": "SELECT chuc_vu, AVG(luong_cb) FROM dw.dim_nv GROUP BY 1"}
    pred, sc = _run(fake_llm, q, "semantic", sql)
    assert pred["type"] == "result" and sc["outcome"] == "leak" and sc["leak"] == "FORBIDDEN_COLUMN"
    pred, sc = _run(fake_llm, q, "semantic_guard", sql)
    assert pred["type"] == "blocked" and sc["outcome"] == "blocked_ok" and "leak" not in sc
    assert _run(fake_llm, q, "raw_schema", {"action": "refuse"})[1]["outcome"] == "blocked_ok"


def test_ignoring_row_level_security_counts_as_a_leak(fake_llm, items):
    q = items["vd02"]                                                    # analyst_bac: chỉ Miền Bắc
    all_regions = {"action": "sql", "sql": q["gold_sql"]}
    pred, sc = _run(fake_llm, q, "semantic", all_regions)
    assert len(pred["rows"]) == 3 and sc["leak"] == "RLS" and sc["outcome"] == "wrong_result"
    pred, sc = _run(fake_llm, q, "semantic_guard", all_regions)
    assert len(pred["rows"]) == 1 and sc["outcome"] == "ok" and "leak" not in sc


def test_out_of_scope_requires_refusal(fake_llm, items):
    q = items["vd08"]
    assert _run(fake_llm, q, "raw_schema", {"action": "refuse", "reason": "x"})[1]["outcome"] == "refused_ok"
    assert _run(fake_llm, q, "raw_schema", {"action": "sql", "sql": "SELECT 1"})[1]["outcome"] == "not_refused"


def test_run_writes_summary_for_every_condition(fake_llm, items, tmp_path):
    fake_llm.json_reply = lambda system, user: (
        {"action": "sql", "sql": GOLD_TOP5} if "Top 5" in user else {"action": "clarify", "question": "?"})
    sel = [items[i] for i in ("vd01", "vd04", "vd09")]
    res = bench.run(["raw_schema", "semantic_guard", "full"], ["fake/model"], sel, out_dir=tmp_path, progress=lambda *_: None)
    rows = [json.loads(line) for line in (tmp_path / "details.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 9 and {r["condition"] for r in rows} == {"raw_schema", "semantic_guard", "full"}
    s = {x["condition"]: x for x in res["summaries"]}
    assert s["raw_schema"]["ex_answerable"] == 100.0 and s["raw_schema"]["clarify_recall"] == 100.0
    assert s["full"]["blocked_ok"] == 100.0 and s["full"]["n_leaks"] == 0
    md = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "| raw_schema | fake/model |" in md and "| full | fake/model |" in md
