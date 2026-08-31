"""问小汉启动入口。

用法：
    python run.py                # 启动服务（自动建表）
    python run.py --seed         # 首次初始化：管理员/样书/埋点/训练/评测集
    python run.py --port 8300
"""
import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="问小汉 · 智能阅读与图书推荐系统")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8300")))
    parser.add_argument("--seed", action="store_true", help="初始化演示数据后启动")
    parser.add_argument("--seed-only", action="store_true", help="仅初始化数据，不启动服务")
    parser.add_argument("--eval", action="store_true", help="启动前先跑评测门禁")
    args = parser.parse_args()

    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # 确保目录与表结构
    from askhanvon.config import settings

    os.makedirs(settings.data_dir, exist_ok=True)
    from askhanvon.db import get_db

    get_db()

    if args.seed or args.seed_only:
        from scripts.seed_data import main as seed_main

        seed_main()
    if args.seed_only:
        return

    if args.eval:
        from askhanvon.evals.runner import render_report, run_all_gates

        results = run_all_gates()
        print(render_report(results))
        if not results.get("all_pass"):
            print("门禁未通过：服务仍将启动（可在后台查看详情）")

    import uvicorn

    print()
    print("=" * 60)
    print("  问小汉 · 智能阅读与图书推荐系统")
    print("  问答首页:  http://127.0.0.1:" + str(args.port) + "/web/index.html")
    print("  管理后台:  http://127.0.0.1:" + str(args.port) + "/web/admin.html")
    print("  API 健康:  http://127.0.0.1:" + str(args.port) + "/api/health")
    print("=" * 60)
    uvicorn.run(app=app(), host=args.host, port=args.port, log_level="warning")


def app():
    from askhanvon.server.app import app as fastapi_app

    return fastapi_app


if __name__ == "__main__":
    main()
