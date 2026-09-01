# 生产模式部署（方案 A：PG + Redis 真实技术栈）

> 配套：`docker-compose.prod.yml`（国内镜像源已注明）· `scripts/smoke_pg.py`（PG 全链路验收）·
> `scripts/locust_scenarios.py`（压测）。本机已就绪镜像：`postgres:16-alpine`、`redis:7-alpine`
> （经 docker.1panel.live / docker.1ms.run 拉取后 tag 为标准名）。

## 架构定位（面试讲法）

**PG 模式下全链路单库生产形态**（不再有"检索核心在 SQLite"）：

| 存储 | 角色（PG 模式） | 说明 |
|---|---|---|
| **PostgreSQL** | 全部数据域：业务 + 图书/章节/知识块（**全文检索用 tsvector 生成列 + GIN**；向量存 BYTEA 由应用层内存矩阵余弦） | 单库闭环，多实例一致性；`python scripts/smoke_pg.py` 16 项全部经过 PG |
| SQLite | 仅 demo 模式（DB_ENGINE 默认）的存储 | 单体演示形态 |
| **Redis** | 限流（SortedSet 滑窗）+ LLM 结果缓存 | 故障自动降级 **DB 原子计数兜底**（rate_counter 表，多 worker 仍严格共享总限） |

**读取路径**：`get_db()` → `HybridStore` 门面按 `PG_DOMAIN_METHODS`（业务域 + **检索域**）路由到 `PgRepository`；
`DB_ENGINE=postgres` 切换只改环境变量，调用方零改动。

**向量检索的数据量路径**：
- 当前（<100 万 chunk）：应用层内存矩阵余弦（chunk 级 256 维 ≈ 1GB/百万），零额外组件；
- 下一步：pgvector（vector 列 + HNSW 索引，SQL 余弦——列结构已预留 BYTEA 可平滑迁移）；
- 更大规模：Milvus / ES kNN —— 均已对应到 `rag/retriever.VectorSearch` 接口位（单点替换）。

## 快速开始

```bash
# 1) 指定数据库口令（示例口令禁止使用！）
export PG_PASSWORD=你自己定一个强口令
# 2) 启动 PG + Redis + 应用（挂 app 容器需要先 build？也可只起 PG/Redis，应用本机跑）
docker compose -f docker-compose.prod.yml up -d
# 3) 初始化（管理员/样书/埋点/训练/评测集——写 PG 业务库 + SQLite 检索库）
docker compose -f docker-compose.prod.yml exec app python run.py --seed-only
# 4) 访问
#    应用 http://127.0.0.1:8300（compose 版）或按 container_port 映射
```

**本机开发模式**（应用跑在本机、只把 PG/Redis 放容器）：

```bash
docker run -d --name askhanvon-pg --network askhanvon-net \
  -e POSTGRES_USER=askhanvon -e POSTGRES_PASSWORD=$PG_PASSWORD \
  -e POSTGRES_DB=askhanvon -p 5432:5432 postgres:16-alpine
docker run -d --name askhanvon-redis --network askhanvon-net \
  -p 6379:6379 redis:7-alpine redis-server --appendonly yes

export DB_ENGINE=postgres
export PG_DSN=postgresql://askhanvon:$PG_PASSWORD@127.0.0.1:5432/askhanvon
export REDIS_URL=redis://127.0.0.1:6379/0
python run.py --seed-only && python run.py
```

## 验收测试

```bash
# PG 业务域全链路（16 项：schema/用户/令牌/入库/RAG/推荐/订单/埋点/特征/策略/A-B/Prompt/评测/审计）
export DB_ENGINE=postgres PG_DSN=... && python scripts/smoke_pg.py
# Redis 限流与缓存
REDIS_URL=... python -c "见 docs/optimization-roadmap.md P4-3 段验证命令"
# 压测（先启动服务）
locust -f scripts/locust_scenarios.py --host http://127.0.0.1:8300 \
  --headless -u 20 -r 5 -t 60s --csv data/locust_report --only-summary
# 单元回归（默认 SQLite 路径不受影响）
python -m pytest tests/ -q
```

## 已实测记录（本机）

- PG smoke：**16/16 PASS**（PostgreSQL 16.15 容器 + 独立 SQLite 检索库 + 智谱 LLM 在线）。
- Redis：限流 [T,T,T,F,F]、结果缓存读写 **PASS**（故障回退进程内已实现）。
- Locust：5 用户轮询题库，chat_qa P95 76ms（结果缓存命中）；推荐 P95 45ms、零失败。
- 93 个单元/集成测试（默认 SQLite 路径）全过。

## 演示双实例

- `http://127.0.0.1:8300`：默认 SQLite 单体（快速演示）
- `http://127.0.0.1:8301`：PG + Redis 完整模式（展示"技术栈"）

## 停止与清理

```bash
docker compose -f docker-compose.prod.yml down          # 保留数据卷
docker compose -f docker-compose.prod.yml down -v       # 连数据卷一起清
```

## 生产化待办（对应 roadmap P4-3 剩余项）

- 备份：`pg_dump` 每日 + WAL 归档（RPO≤5min 演练）；
- Nginx/云负载均衡（SSE 关闭缓冲）；HTTPS（证书）；
- 登录后访问才可用的管控：安全组最小开放 80/443；
- 多 worker：`uvicorn --workers 2`（限流/缓存已在 Redis，事件消费有原子认领）。
