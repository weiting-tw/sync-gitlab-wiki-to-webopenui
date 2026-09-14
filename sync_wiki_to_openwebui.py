#!/usr/bin/env python3
"""
GitLab Wiki -> Open WebUI 知識庫同步腳本 (實測對應 Open WebUI 0.9.6 / 0.11.3)

同步模式 (SYNC_MODE):
  incremental  (預設) 以線上 KB 的 meta.file_hash (=sha256) 對帳:
               只上傳新增/變更檔, 移除已刪檔, 內容沒變的完全跳過。
               source of truth 是線上 KB 本身, 不依賴本機狀態檔, 不會漂移。
  full         先 reset 清空 KB (連同向量集合的去重記錄), 再上傳全部檔。

流程:
  1. clone / pull GitLab Wiki repo
  2. 每個 .md 先 render: 在檔首插一行來源連結 (指回 GitLab Wiki 頁面),
     讓檢索命中時模型看得到出處、回答能附上可點連結。
  3. incremental: 查 KB 既有檔的 file_hash -> 與 render 後內容的 sha256 對帳 -> 增/改/刪
     full:        reset KB -> 上傳全部
  4. 上傳檔名把目錄攤平成 "guides__approval-flow.md":
     Open WebUI 上傳一律只留 basename (0.11.3 實測仍如此), 用真的斜線會丟失路徑。
     add 失敗時立即刪除已上傳的 file 物件, 避免孤兒檔累積

對帳雜湊是 render 後的內容:
  線上存的是 render 後的檔, 拿原始檔 sha256 去比會永遠對不上而每次全量重傳。

環境變數:
  GITLAB_WIKI_URL   例如 https://gitlab.example.com/group/project.wiki.git
  GITLAB_TOKEN      具 read_repository 權限的 PAT / Project Access Token
  OWUI_URL          例如 https://owui.example.com
  OWUI_TOKEN        Open WebUI API key (Settings > Account)，知識庫擁有者的 key
  OWUI_KNOWLEDGE_ID 目標知識庫 ID
  WORKDIR           暫存目錄 (預設 /tmp/wiki-sync)
  SYNC_MODE         incremental | full (預設 incremental)
  WIKI_WEB_BASE     Wiki 頁面網址前綴 (預設由 GITLAB_WIKI_URL 推導)
  DRY_RUN           非空即開 (只有 0/false/no/off 才關): 只印出將要做什麼與 render 樣本,
                    完全不碰線上 KB

安全性:
  上傳或 render 有任何一檔失敗時, 本次不刪除任何線上物件。刪除名單是「線上有、
  本機沒有」, 失敗的檔會讓本該留著的內容不在本機集合裡, 照刪會留下殘缺的 KB。
"""

import os
import sys
import json
import time
import hashlib
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import quote

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


def parse_flag(name: str) -> bool:
    """旗標一律「非空即開」, 只有明確的假值才關。

    DRY_RUN 是安全閥: 打錯成 DRY_RUN=on/y/enabled 若被當成 False 會靜默跑真同步,
    所以無法辨識的值視為開啟, 而不是視為關閉。
    """
    val = os.environ.get(name, "").strip().lower()
    return bool(val) and val not in ("0", "false", "no", "off")


DRY_RUN         = parse_flag("DRY_RUN")

if SYNC_MODE not in ("incremental", "full"):
    sys.exit(f"SYNC_MODE 僅支援 incremental / full，收到: {SYNC_MODE}")


def default_wiki_web_base(repo_url: str) -> str:
    """https://host/group/proj.wiki.git -> https://host/group/proj/-/wikis/"""
    base = repo_url.rstrip("/")
    for suffix in (".wiki.git", ".wiki"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    else:
        sys.exit(f"GITLAB_WIKI_URL 不像 wiki repo 網址 (需以 .wiki.git 結尾): {repo_url}")
    return f"{base}/-/wikis/"


WIKI_WEB_BASE = os.environ.get("WIKI_WEB_BASE") or default_wiki_web_base(GITLAB_WIKI_URL)
if not WIKI_WEB_BASE.endswith("/"):
    WIKI_WEB_BASE += "/"

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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- 來源連結 ----
def wiki_page_slug(rel_path: str) -> str:
    """相對路徑 -> wiki 頁面 slug: "guides/approval-flow.md" -> "guides/approval-flow"。"""
    return rel_path[:-3] if rel_path.endswith(".md") else rel_path


def wiki_page_url(rel_path: str) -> str:
    """相對路徑 -> 可點的 GitLab Wiki 頁面網址 (每段各自 percent-encode)。

    逐段 encode 而非整串: 斜線要保留成路徑分隔, 中文與空白才需要轉義。
    """
    slug = wiki_page_slug(rel_path)
    return WIKI_WEB_BASE + "/".join(quote(seg, safe="") for seg in slug.split("/"))


def render_content(path: Path, rel_path: str) -> bytes:
    """讀原始 .md, 在檔首插一行來源連結後回傳 bytes。

    連結寫進內容 (而非只放 metadata) 的理由: 內容一定會被切塊進向量庫,
    檢索命中時模型看得到出處; metadata 會不會進 citation 是 Open WebUI 端的事, 不可靠。
    用 blockquote 而非 YAML front matter: front matter 容易被切塊器當雜訊。

    用 utf-8-sig 讀: 前面接了 header 之後, 原本在檔首的 BOM 會被推到正文開頭,
    夾在 "\n\n" 與 "# 標題" 之間讓該行不再被當成標題。utf-8-sig 會順手剝掉它。
    """
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise RuntimeError(f"不是 UTF-8 編碼: {e}") from e
    slug = wiki_page_slug(rel_path)
    header = f"> 來源：[{slug}]({wiki_page_url(rel_path)})\n\n"
    return (header + raw).encode("utf-8")


def upload_name(rel_path: str) -> str:
    """相對路徑 -> 攤平後的上傳檔名: "guides/approval-flow.md" -> "guides__approval-flow.md"。

    Open WebUI 上傳一律只留 basename (0.9.6 與 0.11.3 都實測如此),
    用真的斜線會讓不同子目錄的同名頁在 KB 裡碰撞成一樣的名字。
    """
    return rel_path.replace("/", "__")


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


def upload_file(data: bytes, name: str, metadata: dict) -> str:
    """上傳 render 後的內容。

    metadata 欄位名是 "metadata" (不是 file_metadata), 會存進 file 物件的 meta.data;
    0.9.6 與 0.11.3 都實測可寫入。是否被 citation 採用由 Open WebUI 決定,
    所以只當附加資訊, 真正的來源連結在內容裡。
    """
    r = session.post(
        f"{API}/files/",
        headers={"Accept": "application/json"},
        files={"file": (name, data)},
        data={"metadata": json.dumps(metadata, ensure_ascii=False)},
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


def publish(data: bytes, rel_path: str):
    """上傳 -> 等待處理 -> 加入知識庫。

    內容已存在 (400 duplicate) 時刪除這次上傳的孤兒並丟 DuplicateContent;
    其他失敗也刪孤兒後再丟出,避免孤兒檔累積。
    """
    fid = upload_file(
        data,
        upload_name(rel_path),
        {"source_url": wiki_page_url(rel_path), "wiki_path": rel_path},
    )
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


def rendered_local():
    """回傳 ({相對路徑: (render 後 bytes, 其 sha256)}, [(相對路徑, 錯誤)])。

    每個檔只讀一次、只算一次雜湊。單檔 render 失敗 (例如不是 UTF-8) 只記錄不中斷,
    否則一個壞檔會讓整批同步一個都跑不了。
    """
    out, errors = {}, []
    for rel, path in local_md_files().items():
        try:
            data = render_content(path, rel)
        except Exception as e:
            errors.append((rel, e))
            continue
        out[rel] = (data, sha256_bytes(data))
    return out, errors


def sync_incremental():
    """純內容雜湊對帳 (不用檔名)。

    比對的是 render 後的內容 (線上存的就是這份), 不是原始檔。
    - 跳過: 本機 hash 已在線上集合。
    - 上傳: 本機 hash 不在線上集合 (新增或修改後的新內容)。
    - 刪除: 線上某物件的 file_hash 不在本機 (內容已不存在,含修改前的舊版與殘留物件)。
    修改 = 舊內容被刪 + 新內容被傳,全靠 hash 自然處理。
    """
    online_hashes, online_entries = kb_collection_state()
    local, render_errors = rendered_local()
    report_render_errors(render_errors)
    local_hashes = {digest for _, digest in local.values()}
    print(f"[incr] 線上 {len(online_entries)} 物件/{len(online_hashes)} 種內容,本機 {len(local)} 檔")

    to_upload = [(rel, data) for rel, (data, digest) in local.items()
                 if digest not in online_hashes]
    to_delete = [fid for fid, fhash in online_entries if fhash not in local_hashes]

    print(f"[incr] 跳過 {len(local) - len(to_upload)} 檔(未變)、上傳 {len(to_upload)} 檔、"
          f"清除 {len(to_delete)} 個線上物件(已刪內容/殘留)")

    if DRY_RUN:
        preview(local, len(to_upload), f"{len(to_delete)} 個線上物件")
        return 0, len(render_errors)

    ok, fail = 0, len(render_errors)
    for rel, data in to_upload:
        try:
            print(f"  [傳] {upload_name(rel)}")
            publish(data, rel)
            ok += 1
        except DuplicateContent:
            print(f"     [跳過] {rel}: 內容已存在")
        except Exception as e:
            print(f"     [失敗] {rel}: {e}", file=sys.stderr)
            fail += 1

    # 有任何檔沒成功就不刪: 刪除名單是「線上有、本機沒有」, 而上傳失敗與 render 失敗
    # 都會讓本該留著的內容不在本機集合裡。此時照刪會把舊版一起清掉, 留下殘缺的 KB。
    if fail:
        print(f"  [清除] 略過: 有 {fail} 檔未成功,暫不刪除 {len(to_delete)} 個線上物件",
              file=sys.stderr)
    elif to_delete:
        for fid in to_delete:
            remove_from_knowledge(fid)
        print(f"  [清除] 已移除 {len(to_delete)} 個線上物件")

    return ok, fail


def report_render_errors(errors: list):
    for rel, err in errors:
        print(f"     [render 失敗] {rel}: {err}", file=sys.stderr)
    if errors:
        print(f"[警告] {len(errors)} 檔無法 render,本次不會刪除任何線上物件", file=sys.stderr)


def preview(local: dict, n_upload: int, delete_desc: str):
    """DRY_RUN: 印出將要做什麼, 以及一份 render 後的樣本供肉眼檢查。

    delete_desc 是整句描述而非數字: full 模式清的是整個 KB, 印數字會誤導成「只清這幾個」。
    """
    print(f"\n[dry-run] 不會碰線上 KB。將上傳 {n_upload} 檔、清除 {delete_desc}。")
    print(f"[dry-run] wiki 頁面網址前綴: {WIKI_WEB_BASE}")

    print("\n[dry-run] 檔名與來源連結對照 (前 5 筆):")
    for rel in list(local)[:5]:
        print(f"  {rel}")
        print(f"    -> 上傳檔名 {upload_name(rel)}")
        print(f"    -> 來源連結 {wiki_page_url(rel)}")

    nested = [rel for rel in local if "/" in rel]
    if nested:
        rel = nested[0]
        data, _ = local[rel]
        head = data.decode("utf-8").split("\n")[:6]
        print(f"\n[dry-run] render 後檔首樣本 ({rel}):")
        for line in head:
            print(f"  | {line}")


def sync_full():
    """reset 清空後上傳全部。"""
    local, render_errors = rendered_local()
    report_render_errors(render_errors)
    if DRY_RUN:
        print(f"[full] 會先 reset 清空 KB, 再上傳全部 {len(local)} 檔")
        preview(local, len(local), "整個知識庫 (reset)")
        return 0, len(render_errors)

    reset_knowledge()
    print(f"[full] 上傳全部 {len(local)} 檔")
    ok, fail = 0, len(render_errors)
    for rel, (data, _) in local.items():
        try:
            print(f"  -> {upload_name(rel)}")
            publish(data, rel)
            ok += 1
        except DuplicateContent:
            print(f"     [跳過] {rel}: 內容已存在")
        except Exception as e:
            print(f"     [失敗] {rel}: {e}", file=sys.stderr)
            fail += 1
    return ok, fail


def main():
    sync_repo()
    ok, fail = sync_full() if SYNC_MODE == "full" else sync_incremental()
    print(f"[完成] 模式={SYNC_MODE} 成功 {ok} 個,失敗 {fail} 個")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
