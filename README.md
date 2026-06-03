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

```bash
docker build -t wiki-sync:latest .
docker run --rm --env-file .env wiki-sync:latest
```

image 內含 `git` 與依賴，跑完即結束。腳本無本機狀態檔 (source of truth 在線上 KB),所以容器每次重新 clone 也完全正確，不需掛載 volume。

## 排程

### cron(直接跑腳本)

```cron
0 * * * * cd /path/to/repo && set -a && . ./.env && set +a && .venv/bin/python sync_wiki_to_openwebui.py >> sync.log 2>&1
```

### n8n(觸發容器)

`Schedule Trigger` → `Execute Command` node:

```
docker run --rm --env-file /path/to/.env wiki-sync:latest
```

退出碼非 0 時節點失敗，可再接通知節點告警。排程間隔需大於單次執行時間，避免重疊。

## 已知限制

- 部分檔案可能在 Open WebUI 端處理失敗 (`process/status` 回 `failed`),常見原因是 Open WebUI 主機 file descriptor 耗盡 (`Too many open files`,embedding 連不上)。腳本會清掉孤兒並於下次自動重試;根因需在 Open WebUI 端調高 `nofile` ulimit 並修連線洩漏。
