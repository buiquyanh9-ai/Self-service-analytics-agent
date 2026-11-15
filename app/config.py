"""Đường dẫn, nạp file cấu hình YAML và thiết lập runtime (có thể chỉnh trong trang Admin)."""
from __future__ import annotations

import os
import pathlib
import threading
from functools import lru_cache

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_dotenv(path: pathlib.Path) -> None:
    """Nạp KEY=VALUE từ file .env ở gốc dự án (nếu có). Biến đã đặt sẵn trong môi trường được giữ nguyên."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")

# Workspace = một doanh nghiệp: bộ cấu hình (config/), bộ câu hỏi đánh giá (eval/) và dữ liệu chạy (var/) riêng.
# Không đặt WORKSPACE thì dùng doanh nghiệp mẫu ở gốc dự án; đặt WORKSPACE=<tên> thì dùng workspaces/<tên>/.
# Mỗi tiến trình phục vụ một doanh nghiệp, nên dữ liệu và bộ nhớ của hai doanh nghiệp không bao giờ lẫn nhau.
WORKSPACES_DIR = ROOT / "workspaces"
WORKSPACE = os.environ.get("WORKSPACE", "").strip()
WS_ROOT = WORKSPACES_DIR / WORKSPACE if WORKSPACE else ROOT


def list_workspaces() -> list[str]:
    """Tên các workspace đã có cấu hình ('' = doanh nghiệp mẫu ở gốc dự án)."""
    found = sorted(p.name for p in WORKSPACES_DIR.iterdir() if (p / "config").is_dir()) if WORKSPACES_DIR.is_dir() else []
    return [""] + found


if WORKSPACE and not (WS_ROOT / "config").is_dir():
    raise SystemExit(f"Không tìm thấy workspace '{WORKSPACE}' (thiếu thư mục {WS_ROOT / 'config'}). "
                     f"Workspace hiện có: {', '.join(w for w in list_workspaces() if w) or '(chưa có)'}. "
                     "Tạo mới bằng: python -m app.onboard <tên> --db <file.duckdb>")
VAR = WS_ROOT / "var"
CONFIG = WS_ROOT / "config"
WEB = ROOT / "web"
EVAL_DIR = WS_ROOT / "eval"
WAREHOUSE_DB = pathlib.Path(os.environ.get("WAREHOUSE_DB", VAR / "warehouse.duckdb"))
APP_DB = pathlib.Path(os.environ.get("APP_DB", VAR / "app.sqlite"))
STATS_FILE = VAR / "catalog_stats.json"
RULES_FILE = VAR / "active_rules.json"
SECRET = os.environ.get("APP_SECRET", "dev-secret-change-me-" + str(ROOT))

_lock = threading.Lock()


def _load_yaml(name: str) -> dict:
    with open(CONFIG / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=None)
def semantic_layer() -> dict:
    return _load_yaml("semantic_layer.yml")


@lru_cache(maxsize=None)
def catalog_yaml() -> dict:
    return _load_yaml("catalog.yml")


@lru_cache(maxsize=None)
def policies() -> dict:
    return _load_yaml("policies.yml")


@lru_cache(maxsize=None)
def contracts() -> dict:
    return _load_yaml("contracts.yml")


@lru_cache(maxsize=None)
def profile() -> dict:
    """Hồ sơ doanh nghiệp (config/profile.yml). Thiếu file thì trả {} và app/profile.py dùng giá trị mặc định."""
    return _load_yaml("profile.yml") if (CONFIG / "profile.yml").is_file() else {}


DEFAULT_SETTINGS = {
    # nhà cung cấp mô hình: auto (OpenRouter nếu có OPENROUTER_API_KEY, kế đến Claude), openrouter, claude. Không còn offline.
    "llm_mode": os.environ.get("LLM_MODE", "auto"),
    "llm_model": os.environ.get("LLM_MODEL", "claude-opus-5-5"),
    "llm_effort": os.environ.get("LLM_EFFORT", "medium"),
    "router_model": os.environ.get("ROUTER_MODEL", ""),   # trống = llm.ROUTER_DEFAULT_MODEL (model nhanh)
    "max_repair_attempts": 3,
    "query_timeout_s": 20,
    "fast_path": True,            # câu bộ luật hiểu trọn vẹn thì trả lời không cần LLM
    "latency_budget_s": 10,       # thời gian chờ mục tiêu mỗi câu (giây): quá ngân sách thì bỏ lượt LLM viết lời
    "speculative_plan": True,     # Planner chạy đón trước song song với Router (dùng nếu Router không viết lại câu hỏi)
    "multi_task": True,           # câu nhiều ý: trả lời song song mọi ý (tối đa 4) thay vì chỉ ý đầu
    "variance_hypotheses": True,  # câu "vì sao": mô hình nêu giả thuyết nguyên nhân, code chạy truy vấn kiểm chứng từng cái
    "critic": True,               # tác tử kiểm tra: đối chiếu kế hoạch với câu hỏi bằng code (vá phần bị bỏ sót)
    "critic_llm": True,           # ... và LLM chấm kết quả của Planner (chạy song song với lượt viết lời)
    "critic_revise": True,        # LLM chấm "chưa đúng" thì Planner lập lại kế hoạch một lần
    "critic_revise_until_s": 6,   # chỉ chấm (và lập lại) nếu lúc có kết quả câu hỏi mới tốn chưa tới từng này giây
    "always_review": False,            # luôn yêu cầu duyệt SQL trước khi chạy
    "fewshot_k": 4,
    "fewshot_reuse_threshold": 0.8,    # Jaccard tối thiểu để tái sử dụng SQL đã xác nhận (kèm điều kiện trùng slot số/thời gian)
    "anchor_date": None,               # None = ngày dữ liệu mới nhất trong kho
}


def reload_configs():
    semantic_layer.cache_clear()
    catalog_yaml.cache_clear()
    policies.cache_clear()
    contracts.cache_clear()
    profile.cache_clear()
