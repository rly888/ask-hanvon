"""Locust 压测脚本（面试"实测数据"来源）。

用法（先启动服务）：
    locust -f scripts/locust_scenarios.py --host http://127.0.0.1:8300
    或非 GUI：locust -f scripts/locust_scenarios.py --host http://127.0.0.1:8300 \\
        --headless -u 20 -r 5 -t 60s --csv data/locust_report

场景配比（贴近真实流量）：70% 问答 / 20% 推荐 / 10% 搜索。
注意：问答走 LLM API，并发压力真实反映在模型网关；服务端测的是检索+编排+限流。
"""
from locust import HttpUser, between, task

_QUESTION_INDEX = 0


class BookUser(HttpUser):
    wait_time = between(1.0, 3.0)

    # 题库（离线模式也走 RAG 链路，压测可复现）
    QUESTIONS = [
        "《西游记》里孙悟空为什么被压五行山下？",
        "《三国演义》中赤壁之战怎么赢的？",
        "《红楼梦》里黛玉是怎么死的？",
        "《水浒传》中林冲为什么上梁山？",
        "太阳系一共有几颗行星？",
        "火星为什么是红色的？",
        "活字印刷术是谁发明的？",
        "关羽千里走单骑讲了什么？",
        "《宇宙探索简史》讲的是什么？",
    ]

    @task(7)
    def ask_qa(self):
        global _QUESTION_INDEX
        q = self.QUESTIONS[_QUESTION_INDEX % len(self.QUESTIONS)]
        _QUESTION_INDEX += 1
        self.client.post("/api/chat", json={"message": q, "stream": False},
                         name="chat_qa")

    @task(2)
    def recommend(self):
        self.client.get("/api/recommend?top_k=6", name="recommend")

    @task(1)
    def search(self):
        self.client.get("/api/search?q=西游记", name="search")
