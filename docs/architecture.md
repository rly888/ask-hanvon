# 架构文档

对应《开发计划》§1 分层架构与 §3.5 目标服务拓扑。本系统为**单体多模块**实现，
模块边界 = 未来微服务边界，数据所有权按域前缀分表落地。

## 1. 分层结构与代码映射

```
┌─────────────────────────────────────────────────────────────┐
│ 终端层   askhanvon/web（Web/H5，统一 OpenAPI 契约；电纸书可复用 API） │
├─────────────────────────────────────────────────────────────┤
│ 接入层   FastAPI 中间件（§接入：trace/限流/IP黑名单/指标）              │
│         LLM 流式为 SSE（POST + ReadableStream，禁缓冲）              │
├─────────────────────────────────────────────────────────────┤
│ 网关层   server/routes_public（JWT 鉴权 · 限流 · 错误降级话术 · 审计）  │
├─────────────────────────────────────────────────────────────┤
│ 业务应用层                                                      │
│  ├ 对话与交互   conversation/  意图路由 · 会话记忆(TTL) · 画像注入     │
│  ├ Agent 大脑  agent/  Planner → Executor → Synthesizer（轻量自研）  │
│  ├ 工具中心    tools/  MCP schema + 注册表（RBAC/注入/审计强制通道）    │
│  ├ RAG 引擎    rag/    BM25(FTS5)+向量 双路 → 融合 → Rerank → 上下文  │
│  ├ 推荐引擎    recommend/  多路召回 → 精排(LTR) → 规则重排 → 可解释     │
│  ├ 生成引擎    generation/  LLM+引用格式化+交叉验证+拒答+版权护栏        │
│  └ 运营策略    ops/  策略中心 · A/B 实验 · Campaign/优先图书            │
├─────────────────────────────────────────────────────────────┤
│ 数据层（SQLite WAL，按域分表 = 数据所有权）                            │
│  图书域 books_/chapters_/chunks_/chunks_fts      ← pipeline/ 独占写入 │
│  用户域 users_/chat_*/memory_*/user_library      ← server+conversation│
│  订单域 orders_                                  ← tools/purchase     │
│  运营域 ops_*                                    ← ops/              │
│  埋点离线 event_queue/events_/features_*/rec_*    ← events/+offline/  │
│  模型治理 mh_calls/mh_quota_daily                ← modelhub/          │
│  评测域 eval_cases/eval_runs                     ← evals/            │
│  审计域 audit_logs/injection_hits                ← security/registry  │
├─────────────────────────────────────────────────────────────┤
│ 基础设施   obs/（JSON 日志 · Prometheus 指标 · trace_id 贯穿）          │
│ 离线链路   events/(队列消费) + offline/(特征/训练/候选集)（§3.1）        │
└─────────────────────────────────────────────────────────────┘
```

## 2. 两条主链路（§1.2 端到端时序的实现）

### 问答链路（RAG 主链路）

```
POST /api/chat (SSE)
 → 安全闸 injection.check_user_message（命中即拒绝+留痕）
 → intent.route_intent（规则优先/弱模型兜底，多轮指代→Query改写）
 → planner.plan（意图→工具计划）
 → tools.book_qa.ask_rag_stream：
     retriever.retrieve   BM25(FTS5, 标题词×2加权) + 向量(本地hash/API) minmax加权融合
     injection.check_retrieved（资料区注入扫描，不可信片段剔除）
     rerank.rerank_chunks（标题+正文复合文本词面重排；可切 API rerank）
     context.build_context（token 预算/去重/每书限3块/locator 元数据/置信度）
     answer.generate_stream（LLM 流式 + 引用[n]生成）
     citation.validate_citations（词元∨二元组∨8gram 交叉验证，无据→拒答）
     moderation.copyright_guard（连续引用>阈值自动节选）
 → synthesizer（QA 结果透传 + 结构化 payload）
 → 会话记忆写入（last_book/last_intent, TTL 30min）
 → 持久化消息 + chat_message 埋点
```

### 推荐链路（在线打分 + 离线闭环）

```
离线: 埋点 → event_queue → Consumer(Flink等价) → events 明细 + 增量特征
      → features.recompute(全量) → train.rank(逻辑回归 LTR, 时间切分防泄漏)
      → train.cf(item-item 共现余弦) → rec_models 模型库
      → precompute_candidates(召回候选集预计算)
在线: GET /api/recommend
      → recall_candidates(编辑位/CF/内容/热门/分类 5路)
      → rank_candidates(特征拼接 × 策略权重或训练权重)
      → A/B 分桶(rec_rank_v1: A=规则 B=训练)
      → apply_rules(类目多样性/优先位保障/长尾曝光位, 每项带 rules_applied)
      → explain(命中通道+理由+打分明细, 100% 可追踪)
      → impression 埋点(带 variant) → A/B 指标回流 → 赢家晋升
```

## 3. 与《开发计划》§3 增补能力的对应

| 增补能力 | 实现位置 | 说明 |
|---|---|---|
| §3.1 离线数据链路 | events/ + offline/ | 事件队列消费（Kafka 等价）、特征注册表（血缘文档化）、LTR/CF 训练、候选集预计算 |
| §3.2 模型网关与治理 | modelhub/ | 统一 chat/embed/rerank 接入；strong/weak 分级 + 降级链；mh_calls 审计 + mh_quota_daily 配额与成本计量；无 Key 自动离线兜底 |
| §3.3 评估与上线门禁 | evals/ | RAG(102题黄金集)/Agent(10任务)/推荐(留出点击) 三套；门禁判定落库 eval_runs；`python -m scripts.run_eval` |
| §3.4 安全专项 | security/ + tools/registry | 注入检测（用户输入+资料区双闸）、工具 RBAC、购买二次确认+风控、滑动窗口限流、输出版权/违禁审核 |
| §3.5 服务拓扑 | 单体模块边界 | 表 1 的模块即拓扑节点，域前缀分表=数据所有权，跨域一律走 API/函数边界 |

## 4. 可替换组件（生产化路径）

| 单体实现 | 生产替换 | 替换点 |
|---|---|---|
| SQLite FTS5 BM25 | Elasticsearch | db.fts_search / pipeline 索引写入 |
| 本地 hash 向量 + 暴力余索 | Milvus / ES kNN | rag/retriever.VectorSearch |
| 进程内 TTL 缓存/会话 | Redis | conversation/session.memory_* |
| event_queue + Consumer 线程 | Kafka + Flink | events/collector |
| 逻辑回归 LTR | XGBoost/DNN 精排 | offline/train + recommend/rank |
| 智谱 OpenAI 兼容 | 多供应商 | 环境变量 LLM_* / modelhub gateway |
| 单体 FastAPI | k8s 微服务拆分 | 按 §3.5 模块边界直接拆 |

## 5. 非功能实现

- **可观测**：trace_id 贯穿日志与响应头；/api/metrics 输出 Prometheus 文本
  （http_requests_total、tool_calls_total、mh_calls_total、answer/chat/recommend 延迟直方图）。
- **降级话术**：LLM 不可用/配额超限 → 抽取式兜底或「我暂时查不到，请稍后再试」。
- **限流**：IP 维度（默认 120/min）+ chat 用户/IP 维度（30/min）+ 模型配额（用户/日）。
- **审计**：工具调用全部落 audit_logs（allow/deny + 理由）；注入命中落 injection_hits；
  模型调用落 mh_calls（provider/model/tier/tokens/cost/latency）。
