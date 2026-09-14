# sync-gitlab-wiki-to-webopenui

把 GitLab Wiki 同步到 [Open WebUI](https://github.com/open-webui/open-webui) 知識庫，讓你能用問答的方式查 wiki，而且答案會附上原始頁面的連結。

English version: [README.md](README.md)

## 這個工具做什麼

1. clone(或 pull)你的 GitLab Wiki repo
2. 在每個 `.md` 檔開頭插入一行來源連結，指回原始的 wiki 頁面
3. 上傳到 Open WebUI 知識庫

因為連結跟著內容一起進到知識庫，模型回答時就能指出資料來自哪一頁，不用自己猜。

```markdown
> 來源：[guides/approval-flow](https://gitlab.example.com/group/project/-/wikis/guides/approval-flow)

# Approval Flow
...
```

兩種同步模式：

- **`incremental`**(預設)——只上傳新增或變更的頁面，移除已刪除的，沒變的完全跳過
- **`full`**——清空知識庫後重新上傳全部

## 設定

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # 接著填入實際值
```

| 變數 | 必填 | 說明 |
|---|---|---|
| `GITLAB_WIKI_URL` | ✅ | Wiki repo 網址，結尾是 `.wiki.git` |
| `GITLAB_TOKEN` | ✅ | 具 `read_repository` 權限的存取權杖 |
| `OWUI_URL` | ✅ | Open WebUI 服務網址 |
| `OWUI_TOKEN` | ✅ | Open WebUI API key(需為知識庫擁有者) |
| `OWUI_KNOWLEDGE_ID` | ✅ | 目標知識庫 ID |
| `WORKDIR` | | 暫存目錄，預設 `/tmp/wiki-sync` |
| `SYNC_MODE` | | `incremental`(預設)或 `full` |
| `WIKI_WEB_BASE` | | 來源連結的網址前綴，預設由 `GITLAB_WIKI_URL` 推導 |
| `DRY_RUN` | | 非空即開(只有 `0`/`false`/`no`/`off` 才關)——只印出計畫，完全不碰線上知識庫 |

## 執行

```bash
set -a; source .env; set +a
python sync_wiki_to_openwebui.py
```

全部成功時退出碼為 `0`，有任一頁失敗則為 `1`。

正式跑之前可以先預覽，不會動到線上任何東西：

```bash
DRY_RUN=1 python sync_wiki_to_openwebui.py
```

### Docker

```bash
docker build -t wiki-sync:latest .
docker run --rm --env-file .env wiki-sync:latest
```

發佈多平台 image(開發機常是 arm64、伺服器是 amd64)：

```bash
docker login
make push IMAGE=<dockerhub-user>/sync-gitlab-wiki-to-webopenui TAG=latest
```

### 排程

腳本沒有本機狀態檔——線上知識庫本身就是 source of truth——所以容器砍掉重建完全不影響正確性。用 cron 每小時跑一次：

```cron
0 * * * * docker run --rm --pull always --env-file /path/to/.env \
          <dockerhub-user>/sync-gitlab-wiki-to-webopenui:latest >> /var/log/wiki-sync.log 2>&1
```

排程間隔要大於單次執行時間，避免重疊。`--pull always` 很重要：少了它 Docker 會一直用本機快取的舊 image，而新舊版本對同一個知識庫的認知不同，會互相把對方的資料刪掉。

## 對帳原理

source of truth 是線上知識庫，不是本機狀態檔。腳本列出知識庫裡已有的檔案，比對內容雜湊，只上傳缺少的部分。

兩件值得知道的事：

- **雜湊算的是加上來源連結之後的內容**，因為線上存的就是那一份。拿原始檔的雜湊去比永遠對不上，結果會變成每次都全量重傳。
- **只要有任何一頁失敗就不刪除。** 刪除名單是「線上有、本機沒有」，而上傳失敗會讓一頁看起來像不存在，這時候照刪會把線上的舊版一起清掉。

## 注意事項

- Open WebUI 只保留上傳檔名的 basename，所以巢狀頁面會被攤平：`guides/approval-flow.md` 上傳後是 `guides__approval-flow.md`。不這樣做的話，不同資料夾下的同名頁會在知識庫裡撞成一個。
- 啟用來源連結會讓每一頁的雜湊改變，所以升級後第一次執行會全量重傳。先確認 embedding 服務正常，並用 `DRY_RUN=1` 預覽。
- 來源連結位於每頁開頭。長頁面會被切成多個片段，只有第一段帶著連結，命中中後段時模型就看不到它。補救方式是在 RAG 提示樣板裡告訴模型如何從檔名推導出頁面網址。
- 頁面可能在 Open WebUI 端處理失敗(`process/status` 回 `failed`)，常見原因是主機 file descriptor 耗盡。腳本會清掉孤兒檔並於下次自動重試。

## 授權

MIT
