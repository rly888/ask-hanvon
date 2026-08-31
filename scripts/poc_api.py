"""选型 POC：LLM / Embedding 连通性与延迟实测（开发计划 Phase 0 §8.6）。

只从环境变量读取凭据，不写入任何可用凭据字面量。
运行：python scripts/poc_api.py
"""
import os
import time

import requests

ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_chat(name: str, base: str, model: str) -> str:
    key = os.environ.get("ZHIPU_API_KEY" if "zhipu" in name else "DASHSCOPE_API_KEY")
    if not key:
        return f"{name}: SKIP (no key)"
    try:
        t0 = time.time()
        r = requests.post(
            f"{base}/chat/completions",
            timeout=30,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "max_tokens": 50,
                "messages": [{"role": "user", "content": "回复两个字：成功"}],
            },
        )
        d = r.json()
        ok = r.status_code == 200 and "choices" in d
        txt = d["choices"][0]["message"]["content"][:30] if ok else str(d)[:120]
        return f"{name}: {'OK' if ok else 'FAIL'} {time.time() - t0:.1f}s | {txt}"
    except Exception as e:  # noqa: BLE001
        return f"{name}: ERR {str(e)[:100]}"


def test_embed(name: str, base: str, model: str) -> str:
    key = os.environ.get("ZHIPU_API_KEY" if "zhipu" in name else "DASHSCOPE_API_KEY")
    if not key:
        return f"{name}: SKIP (no key)"
    try:
        r = requests.post(
            f"{base}/embeddings",
            timeout=30,
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "input": "测试文本"},
        )
        d = r.json()
        ok = r.status_code == 200 and "data" in d and d["data"]
        if ok:
            return f"{name}: OK dim={len(d['data'][0]['embedding'])}"
        return f"{name}: FAIL {str(d)[:100]}"
    except Exception as e:  # noqa: BLE001
        return f"{name}: ERR {str(e)[:100]}"


if __name__ == "__main__":
    print(test_chat("zhipu.glm-4-flash", ZHIPU_BASE, "glm-4-flash"))
    print(test_chat("zhipu.glm-4.5-flash", ZHIPU_BASE, "glm-4.5-flash"))
    print(test_chat("dashscope.qwen-turbo", DASHSCOPE_BASE, "qwen-turbo"))
    print(test_embed("zhipu.embedding-3", ZHIPU_BASE, "embedding-3"))
    print(test_embed("dashscope.text-embedding-v4", DASHSCOPE_BASE, "text-embedding-v4"))
