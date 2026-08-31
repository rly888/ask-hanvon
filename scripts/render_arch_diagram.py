"""渲染问小汉整体架构图 PNG（scripts/render_arch_diagram.py）。

输出：docs/images/architecture.png（供文档/汇报直接使用）。
布局：分层纵向堆叠 + 三引擎横向并排；文本框尺寸由 matplotlib bbox 自适应。
"""
import os
import textwrap

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "images", "architecture.png")

# ---- 中文字体兜底（Windows/macOS/Linux 常见路径）----
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf", "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
for p in _FONT_CANDIDATES:
    if os.path.exists(p):
        try:
            font_manager.fontManager.addfont(p)
        except Exception:  # noqa: BLE001
            continue
plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["font.sans-serif"] = (
    ["Microsoft YaHei", "SimHei", "PingFang SC", "WenQuanYi Micro Hei",
     "Noto Sans CJK SC", "DejaVu Sans"]
)
print("font resolved:", font_manager.findfont("Microsoft YaHei"))

# ---- 内容模型 ----
LAYERS = [
    ("终端层", ["Web / H5（/web 原生前端） · 电纸书 · 小程序 · 音箱 —— 统一 OpenAPI 契约"]),
    ("接入 / 网关层", ["FastAPI 中间件：JWT 鉴权（access+refresh） · IP 黑名单 · 滑动窗口限流",
                     "trace_id 贯穿 · 请求指标 · CORS · LLM 流式 SSE（禁缓冲）"]),
    ("对话与交互层", ["意图路由（规则 → 弱模型 JSON 兜底，多轮指代改写） · 会话记忆（30min TTL）",
                     "用户画像（偏好分布 / recent_books → prompt 注入）"]),
    ("Agent 大脑", ["Planner（意图 → 工具计划） → Executor（无依赖并行 + 超时分层）",
                    "→ Synthesizer（文本 / 卡片 / 对比 / 订单） · 拒答改写重检 · 追问建议"]),
    ("工具中心（MCP schema × 8）",
     ["book_search · book_qa(RA) · recommend_books · compare_books · my_library",
      "user_profile · purchase_init / purchase_confirm（高危·二次确认）",
      "注册表=唯一通道：RBAC → 参数注入扫描 → 契约校验 → 审计 → 降级"]),
]

ENGINES = [
    ("RAG 引擎", ["BM25(FTS5·短语通道·标题词×2) ⊕ 向量 → RRF 融合",
                  "→ Rerank → 上下文（父块扩展·预算·locator 卷/章/页）"]),
    ("推荐引擎", ["召回（编辑位/CF/内容/热门/分类/会话实时）→ 精排（LTR|规则）",
                  "→ 规则重排（曝光去重·多样性·MMR·探索位）→ 可解释（reasons/通道/明细）"]),
    ("生成引擎", ["模型网关（strong/weak 分级·降级链·配额·成本计量）→ LLM 流式",
                  "→ [n] 引用交叉验证 → 版权护栏 → 主题锚词守卫 → 拒答/缓存（精确+语义）"]),
]

DATA_LAYER = ("数据层（SQLite 按域分表 = 数据所有权边界）",
              ["books/chunks(+FTS) · users/chat/memory/library · orders · ops_*(策略/实验/Prompt)",
               "event_queue/events/features/rec_models · mh_calls/配额 · eval_* · audit_*"])

OFFLINE_LAYER = ("离线链路", ["埋点队列（原子认领+租约回收，多实例安全）→ 事件明细 → 特征平台（增量+全量对账）",
                             "→ LTR 逻辑回归 + item-item CF 训练（时间切分防泄漏）→ 候选集预计算 → 在线打分（特征同源）"])

INFRA_LAYER = ("基础设施", ["JSON 结构化日志 + trace · Prometheus 指标 · 内置调度器（SCHEDULER_ENABLED=1）",
                          "Dockerfile + compose · CI（compileall + pytest + 三套门禁） · 评测黄金集 102 题"])


def wrap_text(text: str, width: int) -> list:
    return textwrap.wrap(text, width=width, break_long_words=False)


def draw_layer(ax, y_top: float, title: str, lines: list) -> float:
    """绘制一个层，返回其底端 y。"""
    wrapped = []
    for ln in lines:
        wrapped += wrap_text(ln, 46)
    n_lines = max(1, len(wrapped))
    h = 2.2 + n_lines * 2.4 + 2.6  # 单位: 百分之一画布高度
    body = "\n".join(wrapped)
    ax.text(
        50, y_top - h / 2, title + ("\n\n" if body else "") + body,
        ha="center", va="center", fontsize=13 if not body else 11.5,
        linespacing=1.55,
        bbox=dict(boxstyle="round,pad=0.55", fc="#eef2ff", ec="#4f46e5", lw=1.4),
    )
    return y_top - h


def main() -> None:
    fig, ax = plt.subplots(figsize=(16, 21))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    ax.set_title("问小汉 · Agent + RAG + 推荐 三引擎协同架构\n"
                 "（约 8400 行 Python 单体多模块，模块边界 = 未来微服务边界）",
                 fontsize=17, pad=14)

    y = 97.0
    for title, lines in LAYERS:
        y = draw_layer(ax, y, title, lines) - 1.2

    # 三引擎并列行
    eng_top = y
    eng_h = 12.5
    for cx, (title, lines) in zip((16, 50, 84), ENGINES):
        wrapped = []
        for ln in lines:
            wrapped += wrap_text(ln, 28)
        body = "\n".join(wrapped)
        ax.text(
            cx, eng_top - eng_h / 2, title + ("\n\n" if body else "") + body,
            ha="center", va="center", fontsize=12.5,
            linespacing=1.5,
            bbox=dict(boxstyle="round,pad=0.5", fc="#fdf4ff", ec="#a21caf", lw=1.4),
        )
    y = eng_top - eng_h - 1.4

    # 三引擎之下：数据层 → 离线链路 → 基础设施
    for title, lines in (DATA_LAYER, OFFLINE_LAYER, INFRA_LAYER):
        y = draw_layer(ax, y, title, lines) - 1.2

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fig.savefig(OUT, dpi=150, bbox_inches="tight", facecolor="white")
    print("saved:", OUT)
    import os as _os

    print("size:", _os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
