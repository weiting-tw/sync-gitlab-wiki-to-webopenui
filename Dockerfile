FROM python:3.12-slim

# git 用來 clone/pull Wiki repo
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先裝依賴 (利用 layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync_wiki_to_openwebui.py .

# 容器內暫存目錄;無本機 state,砍掉重建也不影響正確性
ENV WORKDIR=/data/wiki-sync \
    PYTHONUNBUFFERED=1

# 跑完即結束 (適合排程觸發);有任何檔失敗 -> exit 1
ENTRYPOINT ["python", "sync_wiki_to_openwebui.py"]
