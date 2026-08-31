# 问小汉 · 智能阅读与图书推荐系统

> 「Agent 编排 + RAG + 推荐排序」三引擎协同的图书垂直领域智能系统。
> 依据《开发计划.md》实现的单体多模块完整可运行系统（覆盖 Phase 0–2 全部能力与 Phase 3 关键项）。

![status](https://img.shields.io/badge/tests-68%20passed-brightgreen) ![gate](https://img.shields.io/badge/eval%20gate-RAG%2097%25-blue)

---

## 一句话架构

```
用户(Web/电纸书/API)
  → API 网关(FastAPI: JWT 鉴权 · 限流 · IP黑名单 · trace · 审计)
  → 对话与交互(意图路由 · 会话记忆 · 用户画像)
  → Agent 大脑(Planner → Executor → Synthesizer, 轻量自研编排)
  → 工具中心(MCP schema: 搜索/阅读问答RAG/推荐/比较/书架/画像/购买×2)
      ├─ RAG 引擎: BM25(SQLite FTS5) + 向量 双路召回 → 融合 → Rerank → 上下文(locator)
      ├─ 推荐引擎: 多路召回(编辑位/CF/内容/热门/分类) → 精排(LTR) → 规则重排 → 可解释
      └─ 生成引擎: LLM(智谱glm) + 强制引用 + 交叉验证 + 版权护栏 + 离线兜底
  → 模型网关(统一接入 · 分级路由 · 降级链 · 配额 · 成本计量 · 审计)
  → 数据层(SQLite 按域分表: books_/users_/orders_/ops_/mh_/eval_/audit_)
  → 离线链路: 埋点队列 → 事件明细 → 特征平台 → LTR/CF 训练 → 候选集预计算
```

模块边界对应《开发计划》§3.5 目标服务拓扑，后续可按域直接拆分为微服务。

## 快速开始

```bash
# 1) 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 2) 初始化演示数据（管理员/8个演示用户/6本样书入库/30天埋点/特征训练/评测集）
python run.py --seed-only

# 3) 启动服务
python run.py
# 问答首页  http://127.0.0.1:8300/web/index.html
# 管理后台  http://127.0.0.1:8300/web/admin.html
```

**LLM 配置（可选）**：默认零配置即可运行——未检测到 API Key 时自动进入
「离线兜底模式」（抽取式带引用回答，全链路可用）。配置任一 OpenAI 兼容 Key 后自动切换 LLM 生成：

```bash
export ZHIPU_API_KEY=...        # 或 LLM_API_KEY / OPENAI_API_KEY
export LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4   # 默认
export LLM_WEAK_MODEL=glm-4-flash
export LLM_STRONG_MODEL=glm-4.5-flash
```

> 安全约定：所有凭据只从环境变量读取，源码/示例/测试不写入任何可用凭据字面量。
> 首次 seed 时管理员口令取 `ADMIN_PASSWORD` 环境变量，未设置则随机生成并仅打印一次。

## 演示能力速览

| 能力 | 试试这样说 |
|---|---|
| 图书阅读问答（RAG+引用） | 「《西游记》里孙悟空为什么被压五行山下？」 |
| 推荐引擎（可解释） | 「推荐几本历史类的书」/「我最近想看点科普」 |
| 图书搜索 | 「帮我找一下《水浒传》」 |
| 图书比较 | 「比较一下《西游记》和《三国演义》」 |
| 购买（高危二次确认） | 「我想买《宇宙探索简史》」→ 订单+确认令牌 → 支付 |
| 书架/收藏 | 「我的书架」/「收藏《红楼梦》」 |
| 多轮对话 | 先问一本书，再问「它的作者是谁」（会话记忆+指代消解） |
| Prompt 注入防御 | 「忽略之前的所有指令…」→ 被安全闸拦截并留审计 |

每个 QA 回答都带书内 locator 引用（卷/章/页码），点击引用卡直达原文；
每个推荐结果都带命中通道与打分明细（规则命中 100% 可追踪）。

## 评测门禁（不达标不上线）

```bash
python -m scripts.run_eval            # 跑三套评测并输出门禁报告
```

| 套件 | 指标 | 门禁线（Phase 1） | 实测（LLM 在线） |
|---|---|---|---|
| RAG | 引用位置准确率 | ≥ 80% | **97.7%** |
| RAG | 不可答拒答率 | ≥ 90% | 92.9% |
| RAG | 答案质量(LLM-judge) | ≥ 70% | 97.7% |
| RAG | 回答 P95 延迟 | ≤ 8s | ~6s |
| Agent | 工具调用成功率 | ≥ 95% | 100% |
| Agent | 幻觉率 | ≤ 5% | 0% |
| 推荐 | NDCG@6 / 覆盖率 / 多样性 | 回归基线 | 0.89 / 1.0 / 1.0 |

黄金评测集：`data/evals/rag_golden.json`（102 题：问题-答案-引用位置，含 14 道拒答题）。

## 项目结构

```
run.py                    启动入口（--seed / --eval / --port）
askhanvon/
  config.py               环境变量配置（凭据不落盘）
  db.py                   SQLite 领域仓储（按域分表，参数化 SQL）
  nlp.py                  jieba 分词 / 本地向量 / 相似度
  obs/                    结构化日志 · Prometheus 指标 · trace
  modelhub/               模型网关：路由/降级链/配额/成本/审计（§3.2）
  pipeline/               内容解析：md/txt/epub/pdf → 去重 → chunk → embedding → 索引（§3.1）
  rag/                    混合检索(BM25+向量) → 融合 → Rerank → 上下文
  generation/             提示词 · 引用验证 · 版权护栏 · 回答生成/拒答/缓存/流式
  tools/                  工具中心：MCP schema + 注册表(RBAC/注入/审计) + 8 个工具
  conversation/           意图路由 · 会话记忆(TTL) · 用户画像
  agent/                  Planner → Executor → Synthesizer 编排循环
  recommend/              多路召回 → 精排(LTR) → 规则重排 → 可解释
  offline/                特征平台 · LTR/CF 训练 · 候选集预计算（§3.1）
  events/                 埋点事件模型 + 队列消费（Kafka 单体等价）
  security/               Prompt 注入检测 · 工具 RBAC · 防刷风控（§3.4）
  ops/                    策略中心 · A/B 实验 · Campaign/优先图书
  evals/                  RAG/Agent/推荐三套评测 + 门禁 runner（§3.3）
  server/                 API 网关层（JWT/SSE/MCP 端点/管理后台 API）
  web/                    前端（原生 HTML/JS：聊天+书架+管理后台）
books/                    6 本样书（四大名著导读 + 科普/历史读本）
data/evals/               黄金评测集（102 题）
tests/                    68 个测试（pipeline/RAG/生成/工具/Agent/推荐/离线/安全/API/评测）
docs/                     架构 / 工具schema / 事件模型 / 评测门禁 / POC 报告
scripts/                  seed 数据 · 评测 CLI · POC 脚本
```

## 常用运维操作

```bash
python scripts/run_eval.py            # 评测门禁
python scripts/ingest_books.py books/ # 追加样书入库（幂等可重跑）
python scripts/train_rec.py           # 重算特征 + 训练 + 预计算候选集
```

管理后台支持：策略配置（检索权重/推荐权重/拒答阈值即时生效）、A/B 实验创建与赢家晋升、
Campaign/优先图书、评测运行与历史、工具调用审计、注入攻击拦截记录、模型调用成本看板、
样书上传（内存解析不落盘）。

## 关键设计决策

1. **引用/溯源是生命线**：chunk 携带 卷/章/页 locator；每个事实句的 [n] 引用经
   「词元覆盖 ∨ 二元组覆盖 ∨ 8gram」交叉验证，验证不过即剥除；无引用可验 = 拒答。
2. **宁拒不编造**：低置信/无相关句/无可验引用一律拒答（不可答拒答率 100% 实测）。
3. **版权红线**：不做摘要镜像，单处连续引用超过阈值自动节断（copyright_guard）。
4. **可用性优先**：LLM 故障/欠费/超配额 → 模型网关降级链 → 抽取式兜底，产品永远有回应。
5. **单体多模块**：按 §3.5 拓扑划模块与数据所有权（域前缀分表），为微服务拆分留边界。
6. **工具即契约**：所有工具以 MCP schema 声明输入输出/权限/幂等，注册表是唯一执行通道
   （RBAC、注入扫描、审计、降级都在其中强制执行）。

详见 `docs/` 目录：[架构](docs/architecture.md) ·
[**架构图集**](docs/architecture-diagram.md)（分层/拓扑/时序/闭环 5 视图 + `docs/images/architecture.png`）·
[工具 Schema](docs/tool-schema.md) · [事件模型](docs/event-model.md) ·
[评测门禁](docs/eval-gate.md) · [POC 报告](docs/poc-report.md) ·
[**优化路线图**](docs/optimization-roadmap.md)（P0–P4 全量优化清单与一周冲刺计划）。
