"""FastAPI backend: đăng nhập 2 vai trò, chat với agent, HITL duyệt SQL, SQL console, catalog, log, DQ, lineage, eval."""
from __future__ import annotations

import csv
import io
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, config, conv_eval, cost, dq, evals, llm, profile, sqlcheck
from .agent import SAMPLE_QUESTIONS, get_agent
from .auth import current_user, require_admin
from .catalog import get_catalog
from .governance import UserCtx, validate
from .lineage import get_model_lineage
from .retriever import get_retriever
from .semantic import get_semantic
from .store import get_store

@asynccontextmanager
async def lifespan(_app):
    get_agent()              # seed few-shot, nạp catalog/NLU
    get_retriever()
    try:
        dq.latest_report(run_if_missing=True)
    except Exception:
        pass
    yield


app = FastAPI(title="DATA-01 · Text-to-SQL Self-Service Analytics", version="1.0", lifespan=lifespan)
_eval_lock = threading.Lock()


# ------------------------------------------------------------------ models
class LoginIn(BaseModel):
    username: str
    password: str


class ChatIn(BaseModel):
    message: str = ""
    conversation_id: str | None = None
    clarification: dict | None = None


class ApproveIn(BaseModel):
    run_id: str
    action: str = "approve"
    sql: str | None = None


class SqlIn(BaseModel):
    sql: str
    conversation_id: str | None = None


class FeedbackIn(BaseModel):
    run_id: str
    rating: int
    comment: str | None = None
    corrected_sql: str | None = None


class CatalogIn(BaseModel):
    key: str
    description: str | None = None
    synonyms: list[str] | None = None


class SettingsIn(BaseModel):
    values: dict


class EvalIn(BaseModel):
    split: str = "all"
    mode: str | None = None


class ConvEvalIn(BaseModel):
    judge: bool = True
    ids: list[str] | None = None


# ------------------------------------------------------------------ auth
@app.post("/api/login")
def login(body: LoginIn):
    r = auth.login(body.username, body.password)
    if not r:
        raise HTTPException(401, "Sai tên đăng nhập hoặc mật khẩu")
    return r


@app.get("/api/public")
def public_info():
    """Thông tin hiện trên màn hình đăng nhập, không cần đăng nhập: doanh nghiệp đang phục vụ và (nếu bật) tài khoản demo.

    Tài khoản demo chỉ được liệt kê khi policies.yml đặt `demo_login: true` (dữ liệu giả lập); triển khai thật thì tắt.
    """
    p, pol = profile.get_profile(), config.policies()
    demo = []
    for u in auth.demo_users():      # tài khoản có mật khẩu bí mật (PASSWORD_<TÊN>) không bao giờ nằm trong danh sách này
        role = (pol.get("roles") or {}).get(u.get("role"), {})
        demo.append({"username": u["username"], "password": u["password"], "full_name": u.get("full_name") or u["username"],
                     "role_label": role.get("label") or u.get("role"),
                     "rls": "; ".join(f"{k}: {', '.join(map(str, v))}" for k, v in (u.get("rls") or {}).items())})
    return {"company": p["company"]["name"], "industry": p["company"].get("industry") or "", "assistant": p["assistant"]["name"],
            "workspace": config.WORKSPACE or "(mặc định)", "demo_users": demo}


@app.get("/api/me")
def me(user: UserCtx = Depends(current_user)):
    return {"username": user.username, "role": user.role, "rls": user.rls, "full_name": user.full_name,
            "role_label": user.role_cfg.get("label"), "can_manage_schema": user.role_cfg.get("can_manage_schema", False)}


@app.get("/api/config")
def app_config(user: UserCtx = Depends(current_user)):
    st = get_store().get_settings()
    sem = get_semantic()
    return {"samples": SAMPLE_QUESTIONS, "mode": llm.resolve_mode(st), "llm_mode_setting": st["llm_mode"],
            "credentials": llm.any_credentials(), "model": llm.effective_model(st), "router_model": llm.router_model(st, llm.resolve_mode(st)), "always_review": st["always_review"],
            "anchor": get_catalog().anchor_date(), "profile": profile.public_summary(),
            "workspace": config.WORKSPACE or "(mặc định)", "workspaces": [w or "(mặc định)" for w in config.list_workspaces()],
            "metrics": [{"name": n, "label": m["label"], "owner": m.get("owner"), "description": m.get("description") or "",
                         "format": m.get("format") or ""} for n, m in sem.metrics.items()],
            "dimensions": [{"name": n, "label": d["label"]} for n, d in sem.dims.items()],
            "cost": user.role_cfg.get("cost"), "hitl": config.policies().get("hitl")}


# ------------------------------------------------------------------ hội thoại
@app.get("/api/conversations")
def conversations(user: UserCtx = Depends(current_user)):
    return get_store().list_conversations(user.username)


@app.post("/api/conversations")
def new_conversation(user: UserCtx = Depends(current_user)):
    return {"id": get_store().create_conversation(user.username)}


def _own_conv(cid: str, user: UserCtx) -> dict:
    c = get_store().get_conversation(cid)
    if not c or c["username"] != user.username:
        raise HTTPException(404, "Không tìm thấy hội thoại")
    return c


@app.get("/api/conversations/{cid}")
def get_conversation(cid: str, user: UserCtx = Depends(current_user)):
    c = _own_conv(cid, user)
    return {"id": cid, "title": c["title"], "memory": c["memory"], "messages": get_store().get_messages(cid)}


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str, user: UserCtx = Depends(current_user)):
    _own_conv(cid, user)
    get_store().delete_conversation(cid)
    return {"ok": True}


@app.post("/api/chat")
def chat(body: ChatIn, user: UserCtx = Depends(current_user)):
    store = get_store()
    cid = body.conversation_id or store.create_conversation(user.username)
    _own_conv(cid, user)
    if not body.message.strip() and not body.clarification:
        raise HTTPException(400, "Câu hỏi rỗng")
    resp = get_agent().ask(user, cid, body.message.strip(), clarification=body.clarification)
    resp["conversation_id"] = cid
    return resp


@app.post("/api/approve")
def approve(body: ApproveIn, user: UserCtx = Depends(current_user)):
    return get_agent().approve(user, body.run_id, body.action, body.sql)


# ------------------------------------------------------------------ SQL console
@app.post("/api/sql/validate")
def sql_validate(body: SqlIn, user: UserCtx = Depends(current_user)):
    v = validate(body.sql, user)
    out = v.to_dict()
    if v.ok:
        est = cost.estimate(v.qualified, v.rls_applied)
        out["cost"] = dict(est, **cost.decide(est, user.role_cfg))
        out["lint"] = sqlcheck.lint(v, dq.known_issues())
        out["metrics_matched"] = sqlcheck.matched_metrics(v)
        out["lineage"] = sqlcheck.query_lineage(v)
    return out


@app.post("/api/sql/run")
def sql_run(body: SqlIn, user: UserCtx = Depends(current_user)):
    store = get_store()
    cid = body.conversation_id or store.create_conversation(user.username, "SQL Console")
    resp = get_agent().run_sql(user, cid, body.sql)
    resp["conversation_id"] = cid
    return resp


# ------------------------------------------------------------------ phản hồi -> few-shot
@app.post("/api/feedback")
def feedback(body: FeedbackIn, user: UserCtx = Depends(current_user)):
    store = get_store()
    store.add_feedback(body.run_id, user.username, body.rating, body.comment, body.corrected_sql)
    logs = [l for l in store.get_logs(None, 1000) if l["run_id"] == body.run_id]
    added = None
    if logs:
        lg = logs[0]
        sql = body.corrected_sql or (lg["sql"] if body.rating > 0 else None)
        if sql and lg["question"] and not lg["question"].startswith("[SQL"):
            v = validate(sql, user)
            if not v.ok:
                return {"ok": False, "error": "SQL sửa chưa hợp lệ: " + v.errors[0]["message"]}
            store.add_fewshot(lg["question"], sql, source="feedback", verified=True)
            get_retriever().reload_fewshots()
            added = lg["question"]
    return {"ok": True, "fewshot_added": added}


# ------------------------------------------------------------------ catalog & semantic
@app.get("/api/schema")
def schema(user: UserCtx = Depends(current_user)):
    cat, sem = get_catalog(), get_semantic()
    role = user.role_cfg
    tables = []
    serving = [x for x in cat.tables.values() if x["schema"] == profile.serving_schema()]
    for t in cat.visible_tables(role) if role.get("can_manage_schema") else serving:
        d = cat.describe(t["name"], role, max_values=15)
        d.update({"schema": t["schema"], "bytes": t["bytes"], "synonyms": t["synonyms"]})
        tables.append(d)
    metrics = [sem.describe_metric(n) for n in sem.metrics]
    dims = [{"name": n, "label": d["label"], "expr": d.get("expr"), "synonyms": d.get("synonyms", [])} for n, d in sem.dims.items()]
    return {"tables": tables, "metrics": metrics, "dimensions": dims}


# ------------------------------------------------------------------ xem dữ liệu đầu vào
LAYER_LABEL = {"raw": "Dữ liệu nguồn (raw)", "ref": "Danh mục tham chiếu (ref)", "stg": "Dữ liệu đã chuẩn hóa (staging)"}


@app.get("/api/data")
def data_tables(user: UserCtx = Depends(current_user)):
    """Danh sách bảng người dùng được xem dữ liệu, nhóm theo tầng (nguồn → chuẩn hóa → kho phục vụ)."""
    cat, role = get_catalog(), user.role_cfg
    serving = profile.serving_schema()
    order = {"raw": 0, "ref": 1, "stg": 2}
    tabs = sorted(cat.visible_tables(role), key=lambda t: (order.get(t["schema"], 3 if t["schema"] == serving else 4), t["name"]))
    return {"serving_schema": serving, "anchor": cat.anchor_date(), "data_start": cat.data_start(),
            "tables": [{"name": t["name"], "schema": t["schema"], "rows": t["rows"], "bytes": t["bytes"],
                        "description": t["description"], "n_columns": len(t["columns"]),
                        "layer": LAYER_LABEL.get(t["schema"]) or ("Kho dữ liệu phân tích (" + t["schema"] + ")")} for t in tabs]}


@app.get("/api/data/{table}")
def data_preview(table: str, limit: int = 50, offset: int = 0, q: str | None = None,
                 user: UserCtx = Depends(current_user)):
    """Xem các dòng dữ liệu của một bảng. Đi qua cùng chốt kiểm duyệt với mọi truy vấn: schema theo vai trò,
    bỏ cột bị hạn chế (PII/mật), tiêm RLS — nên người dùng chỉ thấy đúng phần dữ liệu mình được phép."""
    from .warehouse import QueryError, get_warehouse
    cat, role = get_catalog(), user.role_cfg
    fq = cat.resolve(table)
    if not fq or cat.get(fq)["schema"] not in role.get("schemas", []):
        raise HTTPException(404, "Bảng không tồn tại hoặc tài khoản của bạn không được xem bảng này.")
    t = cat.get(fq)
    deny = set(role.get("deny_sensitivity", []))
    cols = [c for c, x in t["columns"].items() if x.get("sensitivity") not in deny]
    hidden = [c for c in t["columns"] if c not in cols]
    if not cols:
        raise HTTPException(403, "Mọi cột của bảng này đều bị hạn chế với vai trò của bạn.")
    limit, offset = max(1, min(int(limit), 200)), max(0, int(offset))
    where = ""
    if q and q.strip():       # tìm nhanh: chứa chuỗi ở bất kỳ cột nào được phép xem
        lit = "'%" + q.strip().replace("'", "''").replace("%", "").replace("_", "\\_")[:80] + "%'"
        where = " WHERE " + " OR ".join(f'CAST("{c}" AS VARCHAR) ILIKE {lit} ESCAPE \'\\\'' for c in cols)
    part = (t.get("partition") or {}).get("column")
    # mới nhất trước (theo cột phân vùng), rồi theo mọi cột còn lại để phân trang ổn định (không lặp/sót dòng)
    keys = ([f'"{part}" DESC'] if part in cols else []) + [f'"{c}"' for c in cols if c != part]
    order = " ORDER BY " + ", ".join(keys)
    sel = ", ".join(f'"{c}"' for c in cols)
    sql = f"SELECT {sel} FROM {fq}{where}{order} LIMIT {limit} OFFSET {offset}"
    v = validate(sql, user, max_rows=limit, dry_run=False)
    if not v.ok:
        raise HTTPException(403, v.errors[0]["message"])
    vc = validate(f"SELECT COUNT(*) AS n FROM {fq}{where}", user, dry_run=False)
    try:
        res = get_warehouse().execute(v.sql_exec, max_rows=limit)
        total = get_warehouse().scalar(vc.sql_exec) if vc.ok else None
    except QueryError as e:
        raise HTTPException(400, str(e))
    meta = [{"name": c, "type": t["columns"][c]["type"], "description": t["columns"][c].get("description") or ""} for c in res["columns"]]
    return {"table": fq, "description": t["description"], "grain": t.get("grain"), "columns": meta, "rows": res["rows"],
            "total": total, "limit": limit, "offset": offset, "hidden_columns": hidden, "rls_applied": bool(v.rls_applied),
            "elapsed_ms": res["elapsed_ms"]}


@app.put("/api/admin/catalog")
def update_catalog(body: CatalogIn, user: UserCtx = Depends(require_admin)):
    get_store().set_override(body.key, body.description, body.synonyms, user.username)
    get_catalog().reload()
    get_retriever().reload()
    from .nlu import get_nlu
    get_nlu(reload=True)
    return {"ok": True}


@app.get("/api/admin/settings")
def get_settings(user: UserCtx = Depends(require_admin)):
    return {"settings": get_store().get_settings(), "credentials": llm.any_credentials(), "policies": config.policies()["roles"]}


@app.put("/api/admin/settings")
def put_settings(body: SettingsIn, user: UserCtx = Depends(require_admin)):
    get_store().set_settings(body.values)
    return get_store().get_settings()


# ------------------------------------------------------------------ log
@app.get("/api/logs")
def logs(user: UserCtx = Depends(current_user)):
    store = get_store()
    rows = store.get_logs(None if user.role_cfg.get("can_view_all_logs") else user.username)
    return {"logs": rows, "stats": store.log_stats() if user.role_cfg.get("can_view_all_logs") else None,
            "feedback": store.feedback_stats()}


@app.get("/api/logs.csv")
def logs_csv(user: UserCtx = Depends(current_user)):
    rows = get_store().get_logs(None if user.role_cfg.get("can_view_all_logs") else user.username, 5000)
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=query_log.csv"})


# ------------------------------------------------------------------ chất lượng dữ liệu
@app.get("/api/dq")
def dq_report(user: UserCtx = Depends(require_admin)):
    return dq.latest_report()


@app.post("/api/dq/run")
def dq_run(user: UserCtx = Depends(require_admin)):
    return dq.run_checks()


@app.post("/api/dq/fix/{finding_id}")
def dq_fix(finding_id: str, user: UserCtx = Depends(require_admin)):
    try:
        return dq.apply_fix(finding_id, user.username)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/dq/revert/{fix_id}")
def dq_revert(fix_id: str, user: UserCtx = Depends(require_admin)):
    try:
        return dq.revert_fix(fix_id, user.username)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ lineage
@app.get("/api/lineage")
def lineage_graph(user: UserCtx = Depends(require_admin)):
    ml = get_model_lineage()
    return {"graph": ml.graph(), "models": {k: {"desc": v["desc"], "sources": v["sources"], "columns": v["columns"]}
                                            for k, v in ml.models.items()}}


@app.get("/api/lineage/column")
def lineage_column(col: str, user: UserCtx = Depends(require_admin)):
    ml = get_model_lineage()
    return {"column": col, "upstream": ml.upstream_path(col), "raw_sources": sorted(ml.upstream_raw(col)),
            "downstream": ml.downstream(col)}


# ------------------------------------------------------------------ eval & few-shot
@app.get("/api/eval")
def eval_list(user: UserCtx = Depends(require_admin)):
    return {"runs": get_store().list_evals(), "questions": evals.load_questions(),
            "conversations": [{"id": c["id"], "name": c.get("name"), "tags": c.get("tags", []), "turns": len(c["turns"])}
                              for c in conv_eval.load_scenarios()]}


@app.get("/api/eval/{eid}")
def eval_get(eid: int, user: UserCtx = Depends(require_admin)):
    r = get_store().get_eval(eid)
    if not r:
        raise HTTPException(404, "Không tìm thấy")
    return r


@app.post("/api/eval/run")
def eval_run(body: EvalIn, user: UserCtx = Depends(require_admin)):
    if not _eval_lock.acquire(blocking=False):
        raise HTTPException(409, "Đang có một lượt eval chạy")
    try:
        return evals.run_eval(body.split, body.mode)
    finally:
        _eval_lock.release()


@app.post("/api/eval/optimize")
def eval_optimize(body: EvalIn, user: UserCtx = Depends(require_admin)):
    if not _eval_lock.acquire(blocking=False):
        raise HTTPException(409, "Đang có một lượt eval chạy")
    try:
        return evals.optimize(body.mode)
    finally:
        _eval_lock.release()


@app.post("/api/eval/conversations")
def eval_conversations(body: ConvEvalIn, user: UserCtx = Depends(require_admin)):
    """Eval hội thoại: kiểm tra bằng code + mô hình chấm từng lượt."""
    if not _eval_lock.acquire(blocking=False):
        raise HTTPException(409, "Đang có một lượt eval chạy")
    try:
        return conv_eval.run_conversations(judge=body.judge, ids=body.ids)
    finally:
        _eval_lock.release()


@app.post("/api/eval/ablation")
def eval_ablation(body: ConvEvalIn, user: UserCtx = Depends(require_admin)):
    """Chạy eval hội thoại với từng tính năng bị tắt để so sánh đóng góp của mỗi tính năng."""
    if not _eval_lock.acquire(blocking=False):
        raise HTTPException(409, "Đang có một lượt eval chạy")
    try:
        return conv_eval.ablation(judge=body.judge, ids=body.ids)
    finally:
        _eval_lock.release()


@app.get("/api/fewshots")
def fewshots(user: UserCtx = Depends(require_admin)):
    return get_store().list_fewshots()


@app.delete("/api/fewshots/{fid}")
def delete_fewshot(fid: int, user: UserCtx = Depends(require_admin)):
    get_store().delete_fewshots(fid=fid)
    get_retriever().reload_fewshots()
    return {"ok": True}


@app.delete("/api/fewshots")
def reset_fewshots(source: str = "optimizer", user: UserCtx = Depends(require_admin)):
    get_store().delete_fewshots(source=source)
    get_retriever().reload_fewshots()
    return {"ok": True}


@app.get("/api/health")
def health():
    cat = get_catalog()
    return {"ok": True, "tables": len(cat.tables), "anchor": cat.anchor_date(),
            "workspace": config.WORKSPACE or "(mặc định)", "company": profile.get_profile()["company"]["name"]}


# ------------------------------------------------------------------ giao diện
app.mount("/static", StaticFiles(directory=str(config.WEB)), name="static")


@app.get("/")
def index():
    # no-cache để trình duyệt luôn lấy index.html mới (file JS/CSS có số phiên bản trong đường dẫn)
    return FileResponse(str(config.WEB / "index.html"), headers={"Cache-Control": "no-cache"})


# đường dẫn của các bản giao diện cũ: chuyển về trang chủ
@app.get("/v2")
@app.get("/classic")
def old_ui():
    return RedirectResponse("/", status_code=308)
