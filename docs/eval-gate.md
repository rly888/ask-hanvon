# 评测门禁

> 《开发计划》§3.3「没有门禁，Phase 验收就会拍脑袋」+ §5 各 Phase 验收指标。
> 原则：**每次模型/索引/策略变更跑回归评测，不达标不上线。**

## 三套评测

### 1. RAG 评测（suite=rag）

- 评测集：`data/evals/rag_golden.json`，102 题（首批 100+ 题黄金标注：问题-标准答案-引用位置
  {书, 章号}），其中 14 题为书库外问题（expect_refusal）。
- 判分：
  - **引用准确率**：回答的 [n] 引用经交叉验证后，locator 命中金标（书+章）；
  - **检索命中**：金标章节出现在上下文 TopN；
  - **答案质量**：LLM-as-judge（1-5，≥3 通过）；无 LLM 时降级为答案-金标词元重合度（≥0.28）；
  - **拒答正确**：不可答题必须拒答；可答题拒答计入 false_refusal。
- 门禁（Phase 1 线，`GATE_*` 环境变量可调）：
  - citation_accuracy ≥ 0.80
  - answer_pass_rate ≥ 0.70
  - refusal_rate_unanswerable ≥ 0.90

### 2. Agent 评测（suite=agent）

- 10 个任务用例（问答/推荐/搜索/比较/购买/闲聊），校验：
  - 意图路由正确率；
  - 工具选择正确（调用集合 = 期望集合）；
  - 幻觉率：QA 回答必须带引用或明确拒答，无引用非拒答 = 幻觉。
- 门禁：tool_success_rate ≥ 0.95；hallucination_rate ≤ 0.05。

### 3. 推荐评测（suite=rec）

- 每个用户按时间留出最后 20% 点击为真值，离线推荐（track=false 不产生曝光）：
  - NDCG@K、命中率@K、覆盖率（去重推荐书目/全目录）、多样性（类目分布）。
- 门禁：ndcg > 0 且 coverage > 0（回归基线；样本充足后 AUC ≥ 0.70 与在线 A/B CTR
  按 Phase 2 验收，见训练产物 metrics）。

## 运行方式

```bash
python -m scripts.run_eval              # 全部门禁 + 报告（exit code 反映通过与否）
python -m scripts.run_eval --suite rag  # 单套件回归
# 管理后台 → 评测门禁 → 在线运行/查看历史（eval_runs 落库）
```

## 最近一次全量门禁实测（LLM 在线：glm-4-flash）

```
[rag]   citation_accuracy=0.9318  answer_pass_rate=0.9432
        refusal_rate_unanswerable=0.9286  false_refusal=0.0682
        p95_latency≈4.8s  ✅ 通过
[agent] intent=1.0  tool_selection=1.0  tool_success=1.0  hallucination=0.0  ✅ 通过
[rec]   ndcg@6=0.8941  hit_rate=1.0  coverage=1.0  diversity=1.0          ✅ 通过
总体: ✅ 全部门禁通过
```

离线兜底模式（无 LLM Key）下 RAG 引用准确率 ~97%（抽取式回答天然来自片段），
全部门禁同样通过——评测在两种模式下都可作为发布门禁。

## 门禁与发布流程

1. 变更（prompt/模型/索引/策略/权重）合入前：`python -m scripts.run_eval`
2. 全绿 → 允许发布；任一红 → 禁止上线（runner exit code 非零）。
3. 每次运行落库 `eval_runs`（指标+门禁+通过标志），管理后台可追溯历史与回归对比。
4. 策略中心的阈值（`gate_*` 经 config 环境变量）随组织阶段上调：Phase 2 建议
   citation_accuracy ≥ 0.90。
