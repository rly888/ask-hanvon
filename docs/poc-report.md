# 选型 POC 报告（Phase 0 · §8.6）

> 《开发计划》要求「LLM function calling / 向量库 / Rerank 各跑一组真实场景再拍板」。
> 本报告为实测记录（2026-08-28），复现脚本：`python scripts/poc_api.py`。

## 1. LLM 连通性与延迟（OpenAI 兼容协议实测）

| 候选 | 结果 | 延迟 | 结论 |
|---|---|---|---|
| 智谱 glm-4-flash | ✅ 200 | **0.8s**（首 token 体验良好） | ✅ 选为主力弱模型 |
| 智谱 glm-4.5-flash | ✅ 200 | 15.3s（冷启动）/ 常态数秒 | ✅ 选为强模型（复杂推理） |
| DashScope qwen-turbo | ❌ 401（Key 无效） | — | 从降级链剔除 |
| 智谱 embedding-3 | ❌ 1113 余额不足 | — | 不可用 |

**拍板**：LLM 走智谱（免费层可用），模型网关按 strong/weak 分级路由 + 跨模型降级链；
所有 Key 仅从环境变量读取。

### function calling / JSON 输出稳定性

- 意图分类采用「只输出 JSON」提示 + 首尾大括号截取解析（不依赖 response_format），
  glm-4-flash 实测稳定；解析失败自动回退规则路由，不阻塞主链路。
- 工具调用不依赖原生 function calling：Planner 用规则路由（确定性、可测试、零延迟），
  LLM 仅做意图兜底——规避了 function calling 与流式组合的稳定性风险（计划 §4 已提示）。

## 2. 向量库 / Embedding

| 选项 | 结论 |
|---|---|
| Milvus | 需独立部署，当前数据量（百级 chunk）严重过配 |
| ES kNN | 需 ES 集群，同样过配 |
| **SQLite BLOB + 内存矩阵余弦（本地 hash 向量）** | ✅ 零依赖、毫秒级、满足当前量级 |

**拍板**：向量检索接口化（`rag/retriever.VectorSearch`），当前用本地实现；
配置 `EMBED_BASE_URL/EMBED_MODEL` 后自动切换 API 向量（智谱 embedding-3 恢复可用即插即用）；
数据过百万级再上 Milvus。本地向量 = jieba 词元 + 字符二元组特征哈希（256 维，L2 归一化），
作为 BM25 的补充召回，混合融合权重策略化可调（默认 bm25:0.6 / vector:0.4）。

实测证据：RAG 评测检索命中率 97.7%（102 题），BM25（标题词加权）为主要贡献源，
与「数据量 < 千万级可用 ES kNN 少一套组件」的计划判断一致。

## 3. Rerank

| 选项 | 结论 |
|---|---|
| bge-reranker（本地小模型） | 需 GPU/推理服务，当前环境无卡（计划风险清单已列 GPU 缺口） |
| 通用 LLM rerank | 成本高，仅 POC 对比用 |
| **本地词面重排（词元覆盖+二元组+8gram）** | ✅ 毫秒级、可解释、实测有效 |

**拍板**：默认本地词面重排，接口与 API rerank（DashScope gte-rerank 等）对齐，
配置 `RERANK_API_URL/RERANK_MODEL` 即插即用。评测证明该方案在当前语料下
引用准确率 93-97%，满足 Phase 1 门禁；上 GPU 后可无感升级。

## 4. 其他关键选型（随 POC 一并拍板）

| 决策项 | 选型 | 理由 |
|---|---|---|
| 存储层 | SQLite(WAL) 按域分表 | 零运维；域前缀分表保留微服务拆分边界；生产替换 MySQL/PG 只动 db.py |
| 全文检索 | FTS5（jieba 预分词 + bm25()） | 真 BM25；中文按词索引；标题词×2 加权显著提升问答召回 |
| 缓存/记忆 | 进程 TTL 缓存 + memory_short 表 | Redis 语义等价实现，接口不变可直接替换 |
| 消息队列 | event_queue 表 + 消费线程 | Kafka 语义子集（至少一次、批量消费）；量级上来换 Kafka 不动上游 |
| 离线训练 | numpy 逻辑回归（LTR）+ item-item CF | 无训练框架依赖；特征与在线打分同源保证一致性 |
| 服务框架 | FastAPI + SSE | 原生异步 + 流式 + OpenAPI 文档；网关中间件实现限流/审计 |
| 前端 | 原生 HTML/JS | 零构建；端上（H5/小程序/电纸书）复用同一套 OpenAPI |

## 5. 遗留风险（对应计划 §7）

- 智谱免费层配额/限流：已用模型网关配额（用户/日）+ 降级链 + 结果缓存对冲。
- 本地向量的语义能力弱于真实 embedding：以 BM25 为主、向量为辅，API 可用时切换。
- LTR 样本量小（演示埋点 4327 条）：AUC≈0.5 仅作链路验证；A/B 默认 A 组（规则权重），
  训练组经 CTR 提升后再晋升——与「先规则版占位」的计划路径一致。
