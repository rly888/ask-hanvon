"""离线训练 CLI：python -m scripts.train_rec [--candidates]"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    parser = argparse.ArgumentParser(description="问小汉 · 特征重算 + LTR/CF 训练")
    parser.add_argument("--candidates", action="store_true", help="同时预计算召回候选集")
    args = parser.parse_args()

    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    from askhanvon.offline.features import recompute_features
    from askhanvon.offline.train import precompute_candidates, train_all

    print("特征重算:", json.dumps(recompute_features(), ensure_ascii=False))
    print("离线训练:", json.dumps(train_all(), ensure_ascii=False))
    if args.candidates:
        print("候选集预计算:", json.dumps(precompute_candidates(), ensure_ascii=False))


if __name__ == "__main__":
    main()
