# 问小汉 · 生产镜像
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8300

# 系统依赖（jieba 无需编译；pypdf 纯 Python）
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 运行期数据卷（数据库/日志/凭据产物不得打进镜像）
VOLUME ["/data"]

EXPOSE 8300

# tini 收信号，主进程为 uvicorn（run.py）
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "run.py"]
