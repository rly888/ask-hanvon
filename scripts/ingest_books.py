"""样书入库 CLI：python -m scripts.ingest_books [目录] [--reindex]"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    parser = argparse.ArgumentParser(description="问小汉 · 内容解析入库")
    parser.add_argument("path", nargs="?", default="books", help="书籍文件或目录")
    parser.add_argument("--reindex", action="store_true", help="删除旧块后全量重建")
    args = parser.parse_args()

    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    from askhanvon.pipeline.index_build import ingest_book, ingest_dir
    from askhanvon.rag.retriever import get_retriever

    if os.path.isdir(args.path):
        reports = ingest_dir(args.path, reindex=args.reindex)
    else:
        reports = [ingest_book(args.path, reindex=args.reindex)]
    get_retriever().invalidate()
    for r in reports:
        if "error" in r:
            print("[FAIL]", r.get("file"), "→", r["error"])
        else:
            print("[OK] 《" + r["title"] + "》 章节 " + str(r["chapters"])
                  + " · 块 " + str(r["chunks"]) + " · embedding " + str(r["embedded"]))


if __name__ == "__main__":
    main()
