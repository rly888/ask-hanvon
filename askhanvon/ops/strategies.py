"""策略中心（运营与策略中枢）：全部可调参数集中在这里，运行期可改，即时生效。

默认值 = 代码内 DEFAULTS；DB ops_strategies 存覆盖值（JSON）。评测门禁阈值
也在本处读取，实现「策略-评测-上线门禁」闭环。
"""
import threading

from ..config import settings
from ..db import dumps, get_db, loads

DEFAULTS = {
    # ---- 检索 / RAG ----
    "retrieval.weights": {"bm25": settings.bm25_weight, "vector": settings.vector_weight},
    "retrieval.top_k": settings.retrieval_top_k,
    "retrieval.rerank_top_n": settings.rerank_top_n,
    "retrieval.fusion": "rrf",           # rrf | linear（P0-3）
    "retrieval.rrf_k": 60,
    "retrieval.query_rewrite": True,     # LLM 查询改写（P1-2）
    "retrieval.multi_query": True,       # 多查询检索（P1-2）
    "retrieval.parent_expand": True,     # 父子块上下文扩展（P1-1）
    "retrieval.sentence_filter": False,  # 句子级压缩（P1-4，实验开关）
    "answer.retry_on_refusal": True,     # 拒答后改写重检一次（P2-3）
    "answer.topic_coherence_check": True,  # 主题一致性守卫（防关键词碎片诱导）
    "answer.min_confidence": settings.min_confidence,
    "answer.llm_intro": False,           # 推荐结果是否用 LLM 生成导语（延迟换体验）
    "answer.semantic_cache": True,       # 语义缓存开关（P0-4）
    "answer.semantic_cache_threshold": 0.90,
    "answer.citation_vector_check": False,  # 引用验证向量通道（P1-3）：
    #   实测会放入关键词诱导型幻觉（如小说情节冒充史实），默认关闭；
    #   接入真实 Embedding API 后可开评估，阈值 0.55 起调
    "answer.citation_vec_threshold": 0.55,
    "answer.answer_level_threshold": 0.12,  # 答案级证据阈值（综述句容忍）
    # ---- 推荐 ----
    "rec.channel_weights": dict(settings.rec_channel_weights),
    "rec.feature_weights": {
        "cf": 1.0, "content": 1.0, "popularity": 0.8, "category_pref": 0.6,
        "freshness": 0.2, "editorial": 1.5,
    },
    "rec.use_trained": False,            # A/B 提升后打开：使用离线训练的精排权重
    "rec.diversity_max_per_category": 2,
    "rec.cold_start_min_events": 5,
    "rec.longtail_enabled": True,
    "rec.exposure_dedup_max": 3,         # 7 天内曝光≥N 次的书去重（P3-1）
    "rec.explore_epsilon": 0.25,         # 尾部探索位 ε-greedy 概率（P3-1）
    "rec.session_boost": True,           # 会话内实时信号加权（P3-4）
    "rec.mmr_lambda": 0.0,               # MMR 多样性，0=关闭（P3-2）
    # ---- 安全 ----
    "security.injection_threshold": 0.7,
    "security.youth_mode": False,
    "security.ip_blacklist": [],
    # ---- 配额 ----
    "quota.llm_calls_per_user_day": settings.quota_llm_calls_per_user_day,
    "quota.llm_tokens_per_user_day": settings.quota_llm_tokens_per_user_day,
}


class Strategies:
    def __init__(self):
        self._cache: dict | None = None
        self._lock = threading.Lock()

    def _overrides(self) -> dict:
        if self._cache is not None:
            return self._cache
        with self._lock:
            if self._cache is not None:
                return self._cache
            rows = get_db().strategy_all()
            self._cache = {r["key"]: loads(r["value"]) for r in rows}
        return self._cache

    def invalidate(self):
        with self._lock:
            self._cache = None

    def get(self, key: str, default=None):
        overrides = self._overrides()
        if key in overrides:
            return overrides[key]
        if key in DEFAULTS:
            return DEFAULTS[key]
        return default

    def set(self, key: str, value, by: str = "admin") -> None:
        get_db().strategy_set(key, dumps(value), by)
        self.invalidate()

    def all_effective(self) -> dict:
        out = dict(DEFAULTS)
        out.update(self._overrides())
        return out


strategies = Strategies()
