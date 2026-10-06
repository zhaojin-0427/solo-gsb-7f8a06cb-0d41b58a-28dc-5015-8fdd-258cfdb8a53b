FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# lxml 在 slim 镜像上使用预编译 wheel，无需编译工具链
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 以非 root 运行
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# 单进程即可：分析是内存内短时任务，多 worker 会让任务态相互隔离
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
