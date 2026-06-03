#!/usr/bin/env python3
"""
GitLab Wiki -> Open WebUI 知識庫同步腳本 (Open WebUI 0.9.6)

同步模式 (SYNC_MODE):
  incremental  (預設) 以線上 KB 的 meta.file_hash (=sha256) 對帳:
               只上傳新增/變更檔, 移除已刪檔, 內容沒變的完全跳過。
               source of truth 是線上 KB 本身, 不依賴本機狀態檔, 不會漂移。
  full         先 reset 清空 KB (連同向量集合的去重記錄), 再上傳全部檔。

流程:
  1. clone / pull GitLab Wiki repo
  2. incremental: 查 KB 既有檔的 file_hash -> 與本機 sha256 對帳 -> 增/改/刪
     full:        reset KB -> 上傳全部
  3. 上傳時用相對路徑當檔名 (避免不同子目錄同名檔碰撞)
     add 失敗時立即刪除已上傳的 file 物件, 避免孤兒檔累積

環境變數:
  GITLAB_WIKI_URL   例如 https://gitlab.example.com/group/project.wiki.git
  GITLAB_TOKEN      具 read_repository 權限的 PAT / Project Access Token
  OWUI_URL          例如 https://owui.example.com
  OWUI_TOKEN        Open WebUI API key (Settings > Account)，知識庫擁有者的 key
  OWUI_KNOWLEDGE_ID 目標知識庫 ID
  WORKDIR           暫存目錄 (預設 /tmp/wiki-sync)
  SYNC_MODE         incremental | full (預設 incremental)
"""

import os
import sys
import time
import hashlib
import subprocess
import unicodedata
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 重導向到檔案 (cron/log) 時 stdout 預設為 block buffering,進度行會卡住看不到。
# 改成 line buffering 讓每行即時可見。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except AttributeError:
        pass


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"缺少必要環境變數: {name}")
    return val


# ---- 設定 ----
GITLAB_WIKI_URL = require_env("GITLAB_WIKI_URL")
GITLAB_TOKEN    = require_env("GITLAB_TOKEN")
OWUI_URL        = require_env("OWUI_URL").rstrip("/")
OWUI_TOKEN      = require_env("OWUI_TOKEN")
KNOWLEDGE_ID    = require_env("OWUI_KNOWLEDGE_ID")
WORKDIR         = Path(os.environ.get("WORKDIR", "/tmp/wiki-sync"))
SYNC_MODE       = os.environ.get("SYNC_MODE", "incremental").lower()

if SYNC_MODE not in ("incremental", "full"):
    sys.exit(f"SYNC_MODE 僅支援 incremental / full，收到: {SYNC_MODE}")

API = f"{OWUI_URL}/api/v1"
POLL_TIMEOUT   = 300   # 單檔處理逾時 (秒)
POLL_INTERVAL  = 2     # 輪詢間隔 (秒)
HTTP_TIMEOUT   = 30    # 一般 HTTP 請求逾時 (秒)
UPLOAD_TIMEOUT = 120   # 檔案上傳逾時 (秒)

# 共用 Session: 帶 Authorization,並對暫時性錯誤自動退避重試。
session = requests.Session()
session.headers["Authorization"] = f"Bearer {OWUI_TOKEN}"
_retry = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 502, 503, 504],
    allowed_methods=["GET", "POST", "DELETE"],
)
_adapter = HTTPAdapter(max_retries=_retry)
session.mount("http://", _adapter)
session.mount("https://", _adapter)


# ---- git ----
def _git(*args):
    """執行 git,透過 credential helper 從環境變數注入 token (不寫進 URL/argv)。"""
    helper = '!f() { echo username=oauth2; echo "password=$GITLAB_TOKEN"; }; f'
    cmd = ["git", "-c", f"credential.helper={helper}", *args]
    subprocess.run(cmd, check=True, env={**os.environ, "GITLAB_TOKEN": GITLAB_TOKEN})


def sync_repo():
    """clone 或 pull Wiki repo。"""
    if (WORKDIR / ".git").exists():
        print(f"[git] pulling in {WORKDIR}")
        _git("-C", str(WORKDIR), "reset", "--hard")
        _git("-C", str(WORKDIR), "pull", "--ff-only")
    else:
        print(f"[git] cloning into {WORKDIR}")
        WORKDIR.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", GITLAB_WIKI_URL, str(WORKDIR))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class DuplicateContent(Exception):
    """Open WebUI 對內容雜湊重複的檔回 400 (代表該內容已在 KB 集合裡)。"""


# ---- Open WebUI API ----
def kb_collection_state():
    """掃 /files/ 篩 collection_name==KB id,回傳 (內容hash集合, [(file_id, file_hash)])。

    純靠內容雜湊對帳,不用檔名: Open WebUI 上傳會砍掉目錄只留 basename,檔名不可靠;
    內容雜湊只認 meta.file_hash (==sha256,可靠),頂層 hash 不可靠。
    無 file_hash 的物件視為殘留 (file_hash=None),交給刪除邏輯清掉。
    不能用 GET /knowledge/{id}: 此版本即使檔案已加入也回 files:0。
    """
    hashes = set()
    entries = []   # [(file_id, file_hash 或 None)]
    page = 1
    while page <= 500:   # 安全上限
        r = session.get(f"{API}/files/", params={"page": page}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            break
        for f in items:
            meta = f.get("meta") or {}
            if meta.get("collection_name") != KNOWLEDGE_ID:
                continue
            fid = f.get("id")
            if not fid:
                continue
            fhash = meta.get("file_hash")   # 只認可靠的 sha256
            entries.append((fid, fhash))
            if fhash:
                hashes.add(fhash)
        page += 1
    return hashes, entries


def reset_knowledge():
    """清空知識庫 (連同向量集合的去重記錄)。full 模式用。"""
    r = session.post(f"{API}/knowledge/{KNOWLEDGE_ID}/reset", timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    print("[reset] 已清空知識庫")


def delete_file(file_id: str):
    """刪除底層 file 物件 (釋放向量與儲存)。"""
    r = session.delete(f"{API}/files/{file_id}", timeout=HTTP_TIMEOUT)
    if not r.ok:
        print(f"[del] 刪除 file {file_id} 失敗: {r.status_code} {r.text}", file=sys.stderr)


def remove_from_knowledge(file_id: str):
    """從知識庫移除關聯, 並刪除底層 file 物件。"""
    rm = session.post(
        f"{API}/knowledge/{KNOWLEDGE_ID}/file/remove",
        headers={"Content-Type": "application/json"},
        json={"file_id": file_id},
        timeout=HTTP_TIMEOUT,
    )
    if not rm.ok:
        print(f"[del] 從知識庫移除 {file_id} 失敗: {rm.status_code} {rm.text}", file=sys.stderr)
    delete_file(file_id)


def upload_file(path: Path, name: str) -> str:
    with open(path, "rb") as fh:
        r = session.post(
            f"{API}/files/",
            headers={"Accept": "application/json"},
            files={"file": (name, fh)},
            timeout=UPLOAD_TIMEOUT,
        )
    r.raise_for_status()
    return r.json()["id"]


def wait_for_processing(file_id: str):
    """輪詢 process/status 直到 completed,逾時或失敗丟例外。"""
    url = f"{API}/files/{file_id}/process/status"
    start = time.time()
    while time.time() - start < POLL_TIMEOUT:
        r = session.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        status = r.json().get("status")
        if status == "completed":
            return
        if status == "failed":
            raise RuntimeError(f"file {file_id} 處理失敗: {r.json().get('error')}")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"file {file_id} 處理逾時 ({POLL_TIMEOUT}s)")


def add_to_knowledge(file_id: str):
    r = session.post(
        f"{API}/knowledge/{KNOWLEDGE_ID}/file/add",
        headers={"Content-Type": "application/json"},
        json={"file_id": file_id},
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code == 400 and "Duplicate content" in r.text:
        raise DuplicateContent()
    r.raise_for_status()


def publish(path: Path, name: str):
    """上傳 -> 等待處理 -> 加入知識庫。

    內容已存在 (400 duplicate) 時刪除這次上傳的孤兒並丟 DuplicateContent;
    其他失敗也刪孤兒後再丟出,避免孤兒檔累積。
    """
    fid = upload_file(path, name)
    try:
        wait_for_processing(fid)
        add_to_knowledge(fid)
    except Exception:
        delete_file(fid)   # 清掉這次上傳的孤兒
        raise


# ---- 同步流程 ----
def local_md_files() -> dict:
    """回傳本機 {相對路徑(posix, NFC): 絕對 Path}。

    檔名統一 NFC: macOS 檔系是 NFD,Open WebUI 存的是 NFC,不統一會讓中文檔名對不上。
    """
    return {
        unicodedata.normalize("NFC", md.relative_to(WORKDIR).as_posix()): md
        for md in sorted(WORKDIR.rglob("*.md"))
    }


def sync_incremental():
    """純內容雜湊對帳 (不用檔名)。

    - 跳過: 本機 sha256 已在線上集合。
    - 上傳: 本機 sha256 不在線上集合 (新增或修改後的新內容)。
    - 刪除: 線上某物件的 file_hash 不在本機 (內容已不存在,含修改前的舊版與殘留物件)。
    修改 = 舊內容被刪 + 新內容被傳,全靠 hash 自然處理。
    """
    online_hashes, online_entries = kb_collection_state()
    local = local_md_files()
    local_hashes = {sha256_file(path): name for name, path in local.items()}
    print(f"[incr] 線上 {len(online_entries)} 物件/{len(online_hashes)} 種內容,本機 {len(local)} 檔")

    to_upload = [(name, path) for name, path in local.items()
                 if sha256_file(path) not in online_hashes]
    to_delete = [fid for fid, fhash in online_entries if fhash not in local_hashes]

    print(f"[incr] 跳過 {len(local) - len(to_upload)} 檔(未變)、上傳 {len(to_upload)} 檔、"
          f"清除 {len(to_delete)} 個線上物件(已刪內容/殘留)")

    ok, fail = 0, 0
    for name, path in to_upload:
        try:
            print(f"  [傳] {name}")
            publish(path, name)
            ok += 1
        except DuplicateContent:
            print(f"     [跳過] {name}: 內容已存在")
        except Exception as e:
            print(f"     [失敗] {name}: {e}", file=sys.stderr)
            fail += 1

    for fid in to_delete:
        remove_from_knowledge(fid)
    if to_delete:
        print(f"  [清除] 已移除 {len(to_delete)} 個線上物件")

    return ok, fail


def sync_full():
    """reset 清空後上傳全部。"""
    reset_knowledge()
    local = local_md_files()
    print(f"[full] 上傳全部 {len(local)} 檔")
    ok, fail = 0, 0
    for name, path in local.items():
        try:
            print(f"  -> {name}")
            publish(path, name)
            ok += 1
        except DuplicateContent:
            print(f"     [跳過] {name}: 內容已存在")
        except Exception as e:
            print(f"     [失敗] {name}: {e}", file=sys.stderr)
            fail += 1
    return ok, fail


def main():
    sync_repo()
    ok, fail = sync_full() if SYNC_MODE == "full" else sync_incremental()
    print(f"[完成] 模式={SYNC_MODE} 成功 {ok} 個,失敗 {fail} 個")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
