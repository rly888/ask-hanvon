# 工具中心 Schema（MCP 工具清单）

> 《开发计划》§8.2「定义工具中心 schema：列出全部工具的输入/输出契约、权限边界、幂等要求」。
> 运行时获取：`GET /api/mcp/tools/list`（MCP 兼容格式）；调用：`POST /api/mcp/tools/call`。

## 设计原则

1. **schema 先行**：每个工具先声明契约再写实现（ToolSchema 数据类）。
2. **注册表是唯一执行通道**：RBAC → 参数注入扫描 → 契约校验 → 执行 → 审计 → 降级，
   Agent 与外部 MCP 客户端都无法绕过。
3. **权限边界**：匿名可用核心阅读工具；写操作（书架/画像/购买）必须登录；
   订单类工具为高危（dangerous + confirmation_required）。
4. **幂等**：查询类天然幂等；purchase_confirm 显式幂等（重放返回原回执）；
   purchase_init 非幂等（每次产生新订单，由风控限频）。

## 工具清单

### 1. book_search — 书籍搜索（匿名可用）
| 项 | 值 |
|---|---|
| 输入 | `query: string(必填)`, `top_k: int=8`, `category: string?` |
| 输出 | `results: [{book_id,title,author,category,cover_emoji,description,snippet,score,match}]`, `total` |
| 实现 | 元数据匹配（书名/作者/标签）+ 内容命中（RAG 检索按书聚合）双路合并 |

### 2. book_qa — 图书阅读问答（匿名可用，RAG 封装）
| 项 | 值 |
|---|---|
| 输入 | `query: string(必填)`, `book_title: string?`（限定书） |
| 输出 | `answer, citations:[{idx,book_id,book_title,vol,chapter_no,chapter_title,para_start,para_end,pages,quote}], refused, confidence, verified_ratio, model, degraded, retrieval[]` |
| 语义 | 只依据书库检索片段回答，逐句 [n] 引用 + locator；无据可依 → `refused=true` |
| 降级 | LLM 不可用 → 抽取式带引用回答（`degraded=true`） |

### 3. recommend_books — 图书推荐（匿名可用）
| 项 | 值 |
|---|---|
| 输入 | `scene: string="homepage"`, `top_k: int=6`, `book_title: string?`（找类似） |
| 输出 | `items: [{position,book_id,title,author,category,score,reasons[],channels[],breakdown{features,rules_applied},variant}]` |
| 语义 | 多路召回→精排→规则重排→可解释；曝光自动埋点（带 A/B variant） |

### 4. compare_books — 图书比较（匿名可用）
| 项 | 值 |
|---|---|
| 输入 | `titles: string[](必填, 2-3 本)` |
| 输出 | `comparison: {columns:[{title,found,author,category,key_points[]}], fields}` |

### 5. my_library — 藏书库（需登录）
| 项 | 值 |
|---|---|
| 输入 | `action: enum[list,collect,uncollect,history](必填)`, `book_title: string?`（collect/uncollect 必填） |
| 输出 | `items[]` 或 `{action,book_id,title,message}` |
| 副作用 | collect/uncollect 落库 + 埋点 |

### 6. user_profile — 用户画像（需登录）
| 项 | 值 |
|---|---|
| 输入 | `action: enum[get,set_pref](必填)`, `categories: string[]?`（set_pref 必填） |
| 输出 | `{profile{pref_categories,category_dist,recent_books,total_clicks,read_minutes}}` 或 `{pref_categories,message}` |
| 隐私 | 最小授权：只暴露偏好类聚合字段 |

### 7. purchase_init — 创建订单 ⚠️（需登录 · 高危 · 二次确认 · 非幂等）
| 项 | 值 |
|---|---|
| 输入 | `book_title: string(必填)`, `qty: int=1(1-5)` |
| 输出 | `{order_id, book_title, qty, price, confirm_token, expires_in:120, message, risk_flags}` |
| 风控 | 1 小时内 ≥5 单标记 velocity_high，≥8 单拒绝；审计全量留痕 |
| 语义 | 只创建待确认订单，**绝不自动支付**（防 Agent 幻觉下单） |

### 8. purchase_confirm — 确认支付 ⚠️（需登录 · 高危 · 幂等）
| 项 | 值 |
|---|---|
| 输入 | `order_id: string(必填)`, `confirm_token: string(必填)` |
| 输出 | `{order_id, status:"paid", amount, book_title, paid_at, message}` |
| 幂等 | 已支付订单重放 → `{already_paid: true}` 返回原回执 |
| 校验 | 订单归属人校验 / 令牌校验（错误令牌记审计 deny）/ 2 分钟有效期 |

## MCP 端点

```
GET  /api/mcp/tools/list   → {tools:[{name,description,inputSchema,outputSchema,annotations}],protocol}
POST /api/mcp/tools/call   body: {name, arguments} → ToolResult
POST /api/tools/{name}     body: {arguments}      → ToolResult（等价便捷入口）
```

普通用户只见匿名可用工具的 manifest；admin 可见全部。

## 注册表强制管线（invoke 顺序）

```
存在性 → RBAC(security/rbac) → 参数注入扫描(security/injection, ≥0.7 拒绝+留痕)
→ 契约校验(type/required/enum) → 执行(异常→降级话术+degraded 标记)
→ audit_logs(allow/deny+理由) → tool_calls_total 指标
```
