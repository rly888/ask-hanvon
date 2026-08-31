"""内置轻量调度器（优化项 P3-5）。

环境变量 SCHEDULER_ENABLED=1 开启（默认关，避免演示环境意外重算）：
- 每小时：特征增量重算（events 增量已在消费侧完成，此处做全量对账）
- 每日：离线训练（LTR/CF）+ 候选集预计算
- 每周：事件明细归档（保留聚合特征）

任务执行失败只记录日志不中断循环；run_cycle() 公开给测试直接验单个周期。
"""
import threading
import time

from ..obs.logging import get_logger, log_fields

logger = get_logger("askhanvon.ops")

JOBS = [
    {"name": "features_hourly", "interval": 3600,
     "fn": "_job_features", "desc": "特征全量对账"},
    {"name": "train_daily", "interval": 86400,
     "fn": "_job_train", "desc": "离线训练 + 候选集预计算"},
    {"name": "purge_weekly", "interval": 604800,
     "fn": "_job_purge", "desc": "事件明细归档"},
]


def _job_features() -> dict:
    from ..offline.features import recompute_features

    return recompute_features()


def _job_train() -> dict:
    from ..offline.features import recompute_features
    from ..offline.train import precompute_candidates, train_all

    recompute_features()
    trained = train_all()
    precompute_candidates()
    return trained


def _job_purge() -> dict:
    from ..events.collector import purge_old_events

    purge_old_events(days=180)
    return {"purged": True}


class Scheduler:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._next_run: dict = {}

    def _due(self) -> list:
        now = time.time()
        due = []
        for job in JOBS:
            nxt = self._next_run.get(job["name"])
            if nxt is None or nxt <= now:
                due.append(job)
                self._next_run[job["name"]] = now + job["interval"]
        return due

    def run_cycle(self) -> list:
        """执行所有到期任务（公开给测试）。返回执行过的任务名。"""
        executed = []
        for job in self._due():
            try:
                fn = globals()[job["fn"]]
                result = fn()
                log_fields(logger, 20, "scheduler.job_done", job=job["name"], result=result)
            except Exception as e:  # noqa: BLE001 — 任务失败不中断调度
                log_fields(logger, 40, "scheduler.job_error", job=job["name"],
                           error=str(e)[:150])
            executed.append(job["name"])
        return executed

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="task-scheduler",
                                        daemon=True)
        self._thread.start()
        log_fields(logger, 20, "scheduler.started")

    def stop(self) -> None:
        self._stop.set()

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception as e:  # noqa: BLE001
                log_fields(logger, 40, "scheduler.worker_error", error=str(e)[:150])
            self._stop.wait(60)


scheduler = Scheduler()
