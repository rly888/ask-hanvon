# 埋点事件模型

> 《开发计划》§8.5「定义埋点事件模型（事件表：行为 → 特征 → 推荐）」。
> 代码即契约：`askhanvon/events/schema.py` 的 EVENT_MODEL，校验不通过直接拒绝入队。

## 事件信封

```json
{
  "event_type": "click",
  "user_id": 3,            // 可空（匿名）
  "session_id": "s_xxx",   // 可空
  "book_id": "b_xxx",      // 图书类事件必填
  "query": "搜索词",        // search 事件必填
  "props": { "scene": "homepage", "variant": "A", "position": 1 },
  "ts": "2026-08-28T22:00:00",  // 服务端补齐
  "client_ts": "..."       // 可选，客户端时间
}
```

## 事件类型（9 类）

| 事件 | 必填字段 | 可选 props | 下游特征 |
|---|---|---|---|
| search | query | — | 用户搜索词 |
| impression | book_id | scene, variant, position | book.impressions、A/B 曝光 |
| click | book_id | scene, source, position | book.clicks/ctr/popularity、user.cat_click、user.recent_books、CF 共现、LTR 标签 |
| read_duration | book_id, seconds | chapter_no | user.read_minutes |
| collect / uncollect | book_id | — | book.collects、书架 |
| purchase | book_id | order_id, amount | book.purchases |
| chat_message | — | intent | 会话量统计 |
| feedback | book_id, action | — | 显式偏好 |

## 数据管道（行为 → 特征 → 推荐）

```
POST /api/events（单条/批量，逐条校验）
  → event_queue（MQ produce 等价）
  → Consumer 线程批量消费（Flink 等价）
      ├→ events 明细表（离线训练样本源）
      ├→ features_book 增量（clicks/impressions/purchases）
      └→ ProfileService 画像增量（cat_click / recent_books）
  → offline.features.recompute_features()（全量重算，幂等）
  → offline.train.train_all()
      ├ rank: LTR 逻辑回归（样本=曝光，标签=7日内点击，80/20 时间切分防泄漏，
      │       输出 AUC/NDCG 入模型库）
      └ cf:   item-item 余弦共现（top50 邻居）
  → offline.train.precompute_candidates()（活跃用户召回候选集预计算）
```

## 特征注册表（含血缘）

| 特征 | 定义 | 来源 | 消费方 |
|---|---|---|---|
| user.cat_click | {分类: 点击次数} 分布 | click × 书目分类 | 推荐内容通道 / 精排 category_pref / 画像 |
| user.recent_books | 最近点击书目(≤10) | click 时间序 | CF 种子书 |
| user.read_minutes | 阅读秒数累计/60 | read_duration | 画像 |
| book.clicks / impressions / ctr | 计数与派生 | click / impression | 热门通道 / 精排 popularity |
| book.purchases / collects | 计数 | purchase / collect | 商品质量 |

在线增量与离线全量同源同口径（离线/在线一致性），注册表代码见
`offline/features.py` FEATURE_REGISTRY。

## A/B 实验闭环

```
创建实验（variants 带 params 覆盖策略项，如 {"rec.use_trained": true}）
  → ab.assign(user) 确定性分桶（blake2b(user+exp) 百分桶 + 权重二次分桶）
  → 曝光/点击事件带 variant 落库
  → ab.metrics 按变体聚合曝光/点击/CTR
  → ab.promote(winner) 赢家 params 写入策略中心，实验收官
```

默认预置实验 `rec_rank_v1`：A=规则权重，B=离线训练权重（LTR）。
