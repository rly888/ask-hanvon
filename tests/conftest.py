"""pytest 全局夹具：测试进程使用独立 DATA_DIR + 离线模式（无 LLM Key）。"""
import os
import tempfile

# 必须在导入 askhanvon 之前设置环境变量
_TEST_DATA_DIR = os.path.join(tempfile.gettempdir(), "askhanvon_test_" + str(os.getpid()))
os.environ["DATA_DIR"] = _TEST_DATA_DIR
# 测试确定性：清掉可能存在的 LLM Key，走离线兜底链路
os.environ.pop("ZHIPU_API_KEY", None)
os.environ.pop("DASHSCOPE_API_KEY", None)
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ["LOG_LEVEL"] = "ERROR"

import pytest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOKS_DIR = os.path.join(ROOT, "books")


@pytest.fixture(scope="session", autouse=True)
def _seed_books():
    """所有测试共享：先入库真实样书。"""
    from askhanvon.pipeline.index_build import ingest_dir

    ingest_dir(BOOKS_DIR, reindex=True)


@pytest.fixture(scope="session")
def ingested_db():
    """全量真实样书入库（会话级一次）。"""
    from askhanvon.pipeline.index_build import ingest_dir

    reports = ingest_dir(BOOKS_DIR, reindex=True)
    return reports


@pytest.fixture(scope="session")
def sample_book(ingested_db):
    """返回第一本书的 book_id。"""
    ok = [r for r in ingested_db if "book_id" in r]
    assert ok, "样书入库失败"
    return ok[0]["book_id"]
