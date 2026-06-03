# sync-gitlab-wiki-to-webopenui

把 GitLab Wiki 同步到 Open WebUI 知識庫的單檔腳本 (對應 Open WebUI 0.9.6)。

## 運作流程

1. `clone` / `pull` GitLab Wiki repo 到暫存目錄
2. 依 `SYNC_MODE` 同步：
   - **incremental(預設)**:純內容 sha256 對帳——只上傳新增/變更的 `.md`,移除已刪內容，內容沒變的完全跳過 (不重傳、不重算向量)
   - **full**:`reset` 清空知識庫後重傳全部
3. 上傳 → 輪詢處理狀態 → 加入知識庫;處理失敗的檔會清掉孤兒並於下次自動重試

### 對帳原理 (為何只靠內容雜湊)

source of truth 是線上 KB 本身，而非本機狀態檔：掃 `/files/` 篩 `meta.collection_name == 知識庫 ID`,取每個檔的 `meta.file_hash`(== 內容 sha256) 組成集合。

- 本機檔 sha256 **在**集合內 → 跳過
- **不在** → 上傳 (新增或修改後的新內容)
- 線上某物件 hash **不在**本機 → 移除 (已刪內容、修改前舊版、殘留物件)

不用檔名比對 (Open WebUI 上傳會砍掉目錄只留 basename、且中文有 NFD/NFC 問題),不用 `GET /knowledge/{id}`(該端點即使檔案已加入也回 `files:0`)。Open WebUI 的「內容重複」400 當最後防線。

## 安裝

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 設定

複製範本並填入實際值：

```bash
cp .env.example .env
# 編輯 .env
```

| 變數 | 必填 | 說明 |
|------|------|------|
| `GITLAB_WIKI_URL` | ✅ | Wiki repo 網址 (`...project.wiki.git`) |
| `GITLAB_TOKEN` | ✅ | 具 `read_repository` 權限的 PAT / Project Access Token |
| `OWUI_URL` | ✅ | Open WebUI 服務網址 |
| `OWUI_TOKEN` | ✅ | Open WebUI API key (知識庫擁有者) |
| `OWUI_KNOWLEDGE_ID` | ✅ | 目標知識庫 ID |
| `WORKDIR` | | 暫存目錄，預設 `/tmp/wiki-sync` |
| `SYNC_MODE` | | `incremental`(預設) 或 `full` |

## 執行

```bash
set -a; source .env; set +a
python sync_wiki_to_openwebui.py
```

退出碼：全數成功為 `0`,有任一檔失敗為 `1`。

## Docker

本機快速跑一次:

```bash
docker build -t wiki-sync:latest .
docker run --rm --env-file .env wiki-sync:latest
```

image 內含 `git` 與依賴,跑完即結束。腳本無本機狀態檔 (source of truth 在線上 KB),容器每次重新 clone 也完全正確,不需掛載 volume。

### 發佈多平台 image 到 Docker Hub

開發機常是 arm64 (Apple Silicon)、部署機是 amd64,所以要發佈同時支援兩種架構的 image。用 `docker buildx`,已包成 `Makefile`:

```bash
# 1. 登入 Docker Hub
docker login

# 2. 多平台建置 (amd64 + arm64) 並推上去;IMAGE 換成你的 Docker Hub namespace/repo
make push IMAGE=<dockerhub-user>/sync-gitlab-wiki-to-webopenui TAG=latest
```

`make push` 等同:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t <dockerhub-user>/sync-gitlab-wiki-to-webopenui:latest --push .
```

(只想驗證兩平台都建得起來、不推送:`make build`。首次會自動建一個 `docker-container` 的 buildx builder。)

部署機直接拉來跑,Docker 會自動選對應架構:

```bash
docker run --rm --env-file .env <dockerhub-user>/sync-gitlab-wiki-to-webopenui:latest
```

## 排程 (cron)

在 host 上用 crontab 定期跑。退出碼非 0 表示有檔失敗,可接通知。**排程間隔需大於單次執行時間,避免重疊。**

### 跑 Docker image (建議)

```cron
0 * * * * docker run --rm --env-file /path/to/.env <dockerhub-user>/sync-gitlab-wiki-to-webopenui:latest >> /var/log/wiki-sync.log 2>&1
```

### 直接跑腳本

```cron
0 * * * * cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python sync_wiki_to_openwebui.py >> sync.log 2>&1
```

> n8n 的 `Execute Command` 是在 n8n 容器內執行,預設沒有 docker CLI 也沒掛 docker socket,**無法直接 `docker run`**。要用 n8n 排程的話,改用 **SSH node** 連到 host 執行上面的指令。

## 已知限制

- 部分檔案可能在 Open WebUI 端處理失敗 (`process/status` 回 `failed`),常見原因是 Open WebUI 主機 file descriptor 耗盡 (`Too many open files`,embedding 連不上)。腳本會清掉孤兒並於下次自動重試;根因需在 Open WebUI 端調高 `nofile` ulimit 並修連線洩漏。
