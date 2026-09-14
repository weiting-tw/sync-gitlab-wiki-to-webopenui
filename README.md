# sync-gitlab-wiki-to-webopenui

Sync a GitLab Wiki into an [Open WebUI](https://github.com/open-webui/open-webui) knowledge base, so you can ask questions about your wiki and get answers with links back to the original pages.

繁體中文說明請見 [README.zh-TW.md](README.zh-TW.md)。

## What it does

1. Clones (or pulls) your GitLab Wiki repository.
2. Prepends a source link to each `.md` file, pointing back to the original wiki page.
3. Uploads the pages to an Open WebUI knowledge base.

Because the link travels with the content, the model can cite the page it answered from instead of leaving you to guess.

```markdown
> Source: [guides/approval-flow](https://gitlab.example.com/group/project/-/wikis/guides/approval-flow)

# Approval Flow
...
```

Two sync modes:

- **`incremental`** (default) — uploads only new or changed pages, removes deleted ones, skips everything unchanged.
- **`full`** — resets the knowledge base and re-uploads everything.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then fill it in
```

| Variable | Required | Description |
|---|---|---|
| `GITLAB_WIKI_URL` | ✅ | Wiki repo URL, ending in `.wiki.git` |
| `GITLAB_TOKEN` | ✅ | Personal/Project Access Token with `read_repository` |
| `OWUI_URL` | ✅ | Open WebUI base URL |
| `OWUI_TOKEN` | ✅ | Open WebUI API key (must own the knowledge base) |
| `OWUI_KNOWLEDGE_ID` | ✅ | Target knowledge base ID |
| `WORKDIR` | | Scratch directory, defaults to `/tmp/wiki-sync` |
| `SYNC_MODE` | | `incremental` (default) or `full` |
| `WIKI_WEB_BASE` | | Source-link prefix; derived from `GITLAB_WIKI_URL` by default |
| `SOURCE_LABEL` | | Label used in the source line, defaults to `Source`. Changing it rewrites every page's hash and forces a full re-upload |
| `DRY_RUN` | | Any non-empty value except `0`/`false`/`no`/`off` — print the plan without touching the knowledge base |

## Usage

```bash
set -a; source .env; set +a
python sync_wiki_to_openwebui.py
```

Exit code is `0` when every page succeeded, `1` if any failed.

Preview first, without touching anything online:

```bash
DRY_RUN=1 python sync_wiki_to_openwebui.py
```

### Docker

```bash
docker build -t wiki-sync:latest .
docker run --rm --env-file .env wiki-sync:latest
```

To publish a multi-arch image (the dev machine is often arm64 while servers are amd64):

```bash
docker login
make push IMAGE=<dockerhub-user>/sync-gitlab-wiki-to-webopenui TAG=latest
```

### Scheduling

The script keeps no local state — the knowledge base itself is the source of truth — so a container can be thrown away and recreated freely. Run it hourly with cron:

```cron
0 * * * * docker run --rm --pull always --env-file /path/to/.env \
          <dockerhub-user>/sync-gitlab-wiki-to-webopenui:latest >> /var/log/wiki-sync.log 2>&1
```

Keep the interval longer than one run takes, so executions never overlap. `--pull always` matters: without it Docker keeps using a cached image, and a stale image can fight with a newer one over the same knowledge base.

## How reconciliation works

The knowledge base is the source of truth, not a local state file. The script lists the files already there, compares their content hashes against the local ones, and uploads only what is missing.

Two things worth knowing:

- **Hashes are taken after the source link is added**, since that is what actually gets stored. Comparing raw file hashes would never match and would re-upload everything on every run.
- **Nothing is deleted when any page failed.** The delete list is "online but not local", and a failed upload makes a page look missing — deleting then would take the old version down with it.

## Notes

- Open WebUI keeps only the basename of an uploaded file, so nested pages are flattened: `guides/approval-flow.md` is uploaded as `guides__approval-flow.md`. Without this, same-named pages in different folders would collide.
- Enabling source links changes every page's hash, so the first run after upgrading re-uploads everything. Check that your embedding backend is healthy first, and use `DRY_RUN=1` to preview.
- The source link sits at the top of each page. Long pages get split into several chunks and only the first one carries it, so a retrieval that lands mid-document may not see the link. Telling the model how to derive a page URL from the filename (in the RAG prompt template) covers the rest.
- Pages can fail on the Open WebUI side (`process/status` returns `failed`), usually when the host runs out of file descriptors. The script cleans up the orphaned upload and retries on the next run.

## License

MIT
