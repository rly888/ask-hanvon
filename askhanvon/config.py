"""全局配置。所有凭据仅从环境变量读取，源码/示例/测试不写入可用凭据字面量。

说明：JWT 密钥的持久化不在本模块（见 server/auth.py，存于数据库 kv 表），
本模块不做任何文件写入；派生路径统一经 under_data_dir 校验防越界。
"""
import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _list(name: str) -> list:
    return [x.strip() for x in (os.environ.get(name, "") or "").split(",") if x.strip()]


def sanitize_dir(path: str) -> str:
    """规范化目录并拒绝包含上跳成分的路径（防路径穿越）。"""
    normalized = os.path.normpath(os.path.abspath(path))
    parts = normalized.replace("\\", "/").split("/")
    if os.pardir in parts or os.curdir in parts:
        raise ValueError(f"非法目录路径（包含上跳成分）: {path}")
    return normalized


def _project_root() -> str:
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(pkg_dir))


def _root_default() -> str:
    env = os.environ.get("DATA_DIR")
    if env:
        return sanitize_dir(env)
    return sanitize_dir(os.path.join(_project_root(), "data"))


@dataclass
class Settings:
    # ---- 服务 ----
    app_host: str = os.environ.get("HOST", "0.0.0.0")
    app_port: int = _int("PORT", 8300)
    data_dir: str = field(default_factory=_root_default)
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")

    # ---- 认证 ----
    token_ttl_hours: int = _int("TOKEN_TTL_HOURS", 72)
    refresh_token_days: int = _int("REFRESH_TOKEN_DAYS", 7)

    # ---- 评测 judge（P0-5：与被测模型解耦）----
    judge_model: str = os.environ.get("JUDGE_MODEL", "")

    # ---- 内置任务调度（P3-5，默认关）----
    scheduler_enabled: bool = os.environ.get("SCHEDULER_ENABLED", "") == "1"

    # ---- LLM 模型网关 ----
    llm_base_url: str = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    llm_strong_model: str = os.environ.get("LLM_STRONG_MODEL", "glm-4.5-flash")
    llm_weak_model: str = os.environ.get("LLM_WEAK_MODEL", "glm-4-flash")
    llm_fallback_models: list = field(default_factory=lambda: _list("LLM_FALLBACK_MODELS"))
    llm_timeout: int = _int("LLM_TIMEOUT_SECONDS", 60)
    llm_temperature: float = _float("LLM_TEMPERATURE", 0.6)

    # ---- Embedding ----
    embed_base_url: str = os.environ.get("EMBED_BASE_URL", "")
    embed_model: str = os.environ.get("EMBED_MODEL", "")
    embed_dim: int = _int("EMBED_DIM", 256)

    # ---- Rerank ----
    rerank_api_url: str = os.environ.get("RERANK_API_URL", "")
    rerank_model: str = os.environ.get("RERANK_MODEL", "")

    # ---- 检索 / RAG ----
    chunk_size: int = _int("CHUNK_SIZE", 480)
    chunk_overlap: int = _int("CHUNK_OVERLAP", 96)
    bm25_weight: float = _float("BM25_WEIGHT", 0.6)
    vector_weight: float = _float("VECTOR_WEIGHT", 0.4)
    retrieval_top_k: int = _int("RETRIEVAL_TOP_K", 40)
    rerank_top_n: int = _int("RERANK_TOP_N", 6)
    context_char_budget: int = _int("CONTEXT_CHAR_BUDGET", 3600)
    min_confidence: float = _float("MIN_CONFIDENCE", 0.28)

    # ---- 生成 ----
    copyright_max_quote_chars: int = _int("COPYRIGHT_MAX_QUOTE_CHARS", 300)

    # ---- 配额与限流 ----
    quota_llm_calls_per_user_day: int = _int("QUOTA_LLM_CALLS_PER_USER_DAY", 300)
    quota_llm_tokens_per_user_day: int = _int("QUOTA_LLM_TOKENS_PER_USER_DAY", 200000)
    rate_limit_chat_per_min: int = _int("RATE_LIMIT_CHAT_PER_MIN", 30)
    rate_limit_api_per_min: int = _int("RATE_LIMIT_API_PER_MIN", 120)

    # ---- 评测门禁（§3.3 Phase 1 达标线）----
    gate_citation_accuracy: float = _float("GATE_CITATION_ACCURACY", 0.80)
    gate_answer_pass_rate: float = _float("GATE_ANSWER_PASS_RATE", 0.70)
    gate_refusal_rate: float = _float("GATE_REFUSAL_RATE", 0.90)
    gate_tool_success_rate: float = _float("GATE_TOOL_SUCCESS_RATE", 0.95)

    # ---- 推荐默认权重（可被策略中心覆盖）----
    rec_channel_weights: dict = field(
        default_factory=lambda: {
            "editorial": 0.30,
            "cf": 0.28,
            "content": 0.18,
            "popular": 0.14,
            "category": 0.10,
        }
    )

    def __post_init__(self) -> None:
        self.data_dir = sanitize_dir(self.data_dir)

    def under_data_dir(self, *names: str) -> str:
        """在 data_dir 下拼接子路径并校验不越界（防路径穿越）。"""
        candidate = os.path.normpath(os.path.abspath(os.path.join(self.data_dir, *names)))
        if os.path.commonpath([candidate, self.data_dir]) != self.data_dir:
            raise ValueError(f"路径越出数据目录: {candidate}")
        return candidate

    # ---- 动态凭据（调用时读取环境变量，不落盘）----
    def llm_api_key(self) -> str:
        return (
            os.environ.get("LLM_API_KEY")
            or os.environ.get("ZHIPU_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )

    def embed_api_key(self) -> str:
        return os.environ.get("EMBED_API_KEY") or self.llm_api_key()

    def rerank_api_key(self) -> str:
        return os.environ.get("RERANK_API_KEY") or ""

    # ---- 派生路径 ----
    @property
    def db_path(self) -> str:
        return self.under_data_dir("askhanvon.db")

    @property
    def logs_dir(self) -> str:
        return self.under_data_dir("logs")

    @property
    def uploads_dir(self) -> str:
        return self.under_data_dir("uploads")


settings = Settings()
