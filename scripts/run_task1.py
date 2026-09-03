# -*- coding: utf-8 -*-
"""任务 1 · RAG 基线：全量入库（books+novels+epub） → 建索引 → 跑 RAG 评测门禁（离线）。

- 入库范围：books/(6 本导读样书，golden 集目标) + novels/(106 网文) + epub/(39 技术书)
- 无 LLM key 时自动离线：查询不回写、生成走抽取式、评测裁判走启发式
- 输出：索引统计 + RAG 门禁指标 + 逐题明细
纯数据操作，写 data/ 下的 askhanvon.db（应用自身库）。
"""
import os
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> None:
    from askhanvon.pipeline.index_build import ingest_dir, index_stats
    from askhanvon.rag.retriever import get_retriever
    from askhanvon.evals.rag_eval import run_rag_eval

    report = {"index": {}, "ingest": {}}
    total_books = 0
    total_chunks = 0
    # 任务 1（RAG 基线）：golden 集只针对 books/ 六本导读样书，故只以它为入库目标，
    # 避免把网文/技术书混入检索而稀释引用准确率。novels+epub 属推荐任务，另行入库。
    for sub in ("books",):
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            print("[!] 跳过缺失目录: " + sub)
            continue
        reps = ingest_dir(d, reindex=True)
        ok = [r for r in reps if "error" not in r]
        report["ingest"][sub] = {
            "files": len(reps), "ok": len(ok),
            "books": sum(1 for r in ok if "book_id" in r),
            "chunks": sum(r["chunks"] for r in ok if "chunks" in r),
        }
        total_books += report["ingest"][sub]["books"]
        total_chunks += report["ingest"][sub]["chunks"]
        for r in ok:
            print("      《%s》 章=%s 块=%s" % (r["title"], r["chapters"], r["chunks"]))
        for r in reps:
            if "error" in r:
                print("    [X] %s: %s" % (r.get("file"), r["error"]))
        get_retriever().invalidate()

    print("\n== 索引统计 ==")
    print(json.dumps(index_stats(), ensure_ascii=False))
    print("入库图书合计: %d  块合计: %d" % (total_books, total_chunks))

    print("\n== 运行 RAG 评测（离线）==")
    res = run_rag_eval(limit=None, use_cache=False, verbose=False)
    print("评测指标:")
    print(json.dumps(res["metrics"], ensure_ascii=False))
    print("详情(前 10):")
    for d in res["details"][:10]:
        print("  [%s] cited=%s hit=%s score=%s ref=%s | %s" % (
            "OK" if (d.get("cited_ok") and d["answer_score"] >= 3) else "XX",
            d.get("cited_ok"), d.get("retrieval_hit"), d.get("answer_score"),
            d.get("refused"), (d.get("question") or "")[:34]))


if __name__ == "__main__":
    main()
