"""评测门禁 CLI：python -m scripts.run_eval [--suite rag|agent|rec|all] [--limit N] [--verbose]"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from askhanvon.evals.runner import render_report, run_all_gates, run_suite  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="问小汉 · 评测门禁")
    parser.add_argument("--suite", default="all",
                        choices=["rag", "agent", "rec", "security", "all"])
    parser.add_argument("--limit", type=int, default=None, help="RAG 评测题数（调试用）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    if args.suite == "all":
        results = run_all_gates(verbose=args.verbose)
        print(render_report(results))
        sys.exit(0 if results.get("all_pass") else 1)
    report = run_suite(args.suite, verbose=args.verbose, limit=args.limit)
    print(render_report({"all_pass": report["gate_passed"], "suites": {args.suite: report}}))
    sys.exit(0 if report["gate_passed"] else 1)


if __name__ == "__main__":
    main()
