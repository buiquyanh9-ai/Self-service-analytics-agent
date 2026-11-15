import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# dùng kho metadata tạm để test không làm bẩn var/app.sqlite
os.environ.setdefault("APP_DB", str(pathlib.Path(tempfile.gettempdir()) / "data01_test_app.sqlite"))
# khóa giả đặt TRƯỚC khi nạp .env: test không bao giờ dùng khóa thật hay gọi mạng
os.environ["OPENROUTER_API_KEY"] = "test-key"
os.environ["LLM_MODE"] = "openrouter"
if not (ROOT / "var" / "warehouse.duckdb").exists():
    raise SystemExit("Chưa có kho dữ liệu. Chạy: python warehouse/generate.py && python warehouse/build.py")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch):
    """Mọi test dùng client LLM giả (tests/fake_llm.py); test nào cần client khác thì tự monkeypatch đè lên."""
    from app import llm
    from fake_llm import FakeLLM
    client = FakeLLM()
    monkeypatch.setattr(llm, "get_client", lambda settings, mode, role="main": client)
    return client
