#!/usr/bin/env python3
"""Orchestrate: ima 知识库 / X 书签长文 -> (转换) -> SiYuan，靠 id-ledger 做增量。

本脚本只做「编排 + 去重 + 搬运」，不重写它依赖的任何能力：

  * 微信文章正文       wechat2md skill      (node wechat2md.mjs <url>)
  * 知识库里的文章链接  ima skill           (node ima_api.cjs <endpoint> <json>)
  * X 书签 / X 长文     twitter-cli         (twitter bookmarks --json / twitter article -m)
  * 写进思源           siyuan skill         (python3 siyuan.py create/attr/sql)
  * 已同步 id 台账      id-ledger-query     (POST/GET /v1/projects/<key>/ids)

子命令
  doctor   依赖与凭据体检（不写任何东西）
  seed     从已有 SiYuan 文档回填台账（跑一次；台账丢了重跑即可恢复）
  plan     拉取来源 -> 去掉已同步 -> 写 plan-<source>.json
  publish  按 plan 转换 + 发布到 SiYuan，并逐条写台账
  status   台账里两个 project 各有多少 id

常规增量：plan ->（可选：agent 改标题）-> publish
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(
    os.environ.get("SYNC_TO_SIYUAN_CONFIG", "~/.config/sync-to-siyuan.json")
).expanduser()

DEFAULTS: dict = {
    "notebook": "20260804153230-7rdiobh",  # 00-Inspires
    "paths": {"wechat": "/wechat", "x": "/x"},
    "ledger_projects": {"wechat": "siyuan-wechat", "x": "siyuan-x-article"},
    "ima": {"knowledge_base": "ima_kb_lBjZm8kf", "scan_limit": 50, "fetch_delay": 12, "stub_on_share": False},
    "x": {"count": 100},
    "data_dir": "~/.cache/sync-to-siyuan",
}

ARTICLE_URL_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/i/article/(\d+)")
SYNC_ID_RE = re.compile(r"sync-id[:：]\s*([A-Za-z0-9:._-]{4,})")
WECHAT_SLUG_RE = re.compile(r"mp\.weixin\.qq\.com/s/([A-Za-z0-9_-]+)")

WECHAT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# 公众号「分享页」模板：微信只对微信内返回正文，HTTP 抓到的页面没有 #js_content，
# 只有一坨 window.cgiDataNew 之类的内联 JS。整页丢给 wechat2md 会转出 2MB 的 JS 垃圾，
# 进 SiYuan 后页面直接卡死 —— 所以在转换前后各设一道闸，宁可失败也不写垃圾。
WECHAT_CONTENT_MARKER = 'id="js_content"'
# 贴图文章（item_show_type=8）没有 #js_content，正文/图片全在 window.cgiDataNew 里，
# wechat2md 现在能直接解这个 JS 字面量 —— 所以这类页面照常转，不算「抓不到」。
WECHAT_PICDATA_MARKERS = ("cgiDataNew", "content_noencode", "picture_page_info_list")
MAX_DOC_BYTES = 400_000
GARBAGE_MARKERS = ("__canAsyncImport", "cgiDataNew", "window.page_begintime", "使用完整服务")

LEDGER_UA = "sync-to-siyuan/1.0"  # Cloudflare answers 1010 to urllib's default UA


class SyncError(Exception):
    pass


class SharePageError(SyncError):
    """公众号只给了分享页模板（微信内才有正文）：拿不到正文，但能拿到标题与摘要。"""

    def __init__(self, message: str, title: str = "", desc: str = ""):
        super().__init__(message)
        self.title = title
        self.desc = desc


# ------------------------------------------------------------------ 基础工具


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        return json.loads(json.dumps(DEFAULTS))
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"配置文件读不出来 {CONFIG_PATH}: {exc}")
    return deep_merge(DEFAULTS, raw)


def data_dir(config: dict) -> Path:
    path = Path(os.path.expanduser(config["data_dir"]))
    path.mkdir(parents=True, exist_ok=True)
    return path


def run(cmd: list[str], *, input_text: str | None = None, cwd: Path | None = None,
        timeout: int = 300, env: dict | None = None) -> str:
    proc = subprocess.run(
        cmd, input=input_text, text=True, capture_output=True,
        cwd=str(cwd) if cwd else None, timeout=timeout, env=env,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:800]
        raise SyncError(f"命令失败({proc.returncode}): {' '.join(cmd)}\n{detail}")
    return proc.stdout


def run_json(cmd: list[str], **kwargs) -> dict | list:
    out = run(cmd, **kwargs)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise SyncError(f"输出不是 JSON（{' '.join(cmd)}）: {exc}\n{out[:300]}")


def find_file(explicit: str | None, candidates: list[str], env_var: str, what: str) -> Path:
    """显式参数 > 环境变量 > 已知安装路径。"""
    paths: list[str] = []
    if explicit:
        paths.append(explicit)
    if os.environ.get(env_var):
        paths.append(os.environ[env_var])
    paths.extend(candidates)
    for candidate in paths:
        path = Path(os.path.expanduser(candidate))
        if path.is_file():
            return path
    raise SyncError(f"找不到 {what}（试过: {', '.join(paths)}；也可用 {env_var} 指定）")


def find_bin(explicit: str | None, env_var: str, name: str) -> str:
    from shutil import which

    for candidate in (explicit, os.environ.get(env_var), name):
        if not candidate:
            continue
        if "/" in candidate:
            if Path(os.path.expanduser(candidate)).is_file():
                return candidate
            continue
        resolved = which(candidate)
        if resolved:
            return resolved
    raise SyncError(f"找不到 {name} 可执行文件（可用 {env_var} 指定）")


# ---------------------------------------------------------------- 依赖定位


def ima_skill() -> Path:
    script = find_file(
        None,
        [
            "~/.agents/skills/ima-skill/ima_api.cjs",
            "~/.pi/agent/skills/ima-skill/ima_api.cjs",
            "~/.hermes/skills/note-taking/ima-skill/ima_api.cjs",
            "~/.claude/skills/ima-skill/ima_api.cjs",
        ],
        "IMA_SKILL",
        "ima skill (ima_api.cjs)",
    )
    return script.parent


def wechat2md_script() -> Path:
    return find_file(
        None,
        [
            "~/.agents/skills/wechat2md/scripts/wechat2md.mjs",
            "~/.pi/agent/skills/wechat2md/scripts/wechat2md.mjs",
            "~/.claude/skills/wechat2md/scripts/wechat2md.mjs",
        ],
        "WECHAT2MD_SKILL",
        "wechat2md skill (wechat2md.mjs)",
    )


def siyuan_script() -> Path:
    return find_file(
        None,
        [
            "~/Workspace/siyuan-skill/scripts/siyuan.py",
            "~/.agents/skills/siyuan/scripts/siyuan.py",
            "~/.pi/agent/skills/siyuan/scripts/siyuan.py",
            "~/.claude/skills/siyuan/scripts/siyuan.py",
        ],
        "SIYUAN_SKILL",
        "siyuan skill (siyuan.py)",
    )


def siyuan_env() -> dict:
    """siyuan skill 只认环境变量；缺失时回落到本机已有的思源客户端配置。"""
    env = os.environ.copy()
    if env.get("SIYUAN_API_URL") and env.get("SIYUAN_TOKEN"):
        return env
    fallback = Path("~/.siyuan-sisyphus/config.json").expanduser()
    if fallback.is_file():
        try:
            raw = json.loads(fallback.read_text(encoding="utf-8"))
            profile = (raw.get("profiles") or {}).get(raw.get("currentProfile") or "default") or {}
            env.setdefault("SIYUAN_API_URL", profile.get("apiUrl") or "")
            env.setdefault("SIYUAN_TOKEN", profile.get("token") or "")
        except (OSError, json.JSONDecodeError):
            pass
    return env


def sy(args: list[str], *, input_text: str | None = None, json_out: bool = False) -> str:
    cmd = [sys.executable, str(siyuan_script()), *args]
    if json_out and "--json" not in cmd:
        cmd.append("--json")
    return run(cmd, input_text=input_text, env=siyuan_env(), timeout=180)


def sy_sql(query: str) -> list[dict]:
    out = sy(["sql", query, "--json"], json_out=False)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise SyncError(f"siyuan sql 输出不可解析: {out[:300]}")


# ------------------------------------------------------------------ 台账


class LedgerError(Exception):
    """台账读写失败。默认降级（还有 SiYuan 扫描兜底），seed 时严格失败。"""


class Ledger:
    def __init__(self, base_url: str | None, token: str | None, project: str, strict: bool = False):
        self.base = (base_url or "").rstrip("/")
        self.token = token or ""
        self.project = project
        self.strict = strict
        self.error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.token)

    def degrade(self, action: str, exc: Exception) -> None:
        self.error = f"{action}: {exc}"
        if self.strict:
            raise SystemExit(f"台账不可用（{action}）: {exc}")
        log(f"warn: 台账 {action} 失败，改用 SiYuan 扫描兜底（{exc}）")

    def project_url(self, suffix: str = "") -> str:
        quoted = urllib.parse.quote(self.project, safe="")
        return f"{self.base}/v1/projects/{quoted}/ids{suffix}"

    def _request(self, method: str, url: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("User-Agent", LEDGER_UA)
        # Cloudflare 会掐掉复用的 keep-alive 连接，偶发 handshake/read 超时 —— 每次新建连接并退避重试
        request.add_header("Connection", "close")
        if body:
            request.add_header("Content-Type", "application/json")
        failure: Exception | None = None
        for attempt in range(4):
            if attempt:
                time.sleep(attempt * 1.5)
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    raw = response.read().decode()
                return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                failure = LedgerError(f"{method} {url} -> HTTP {exc.code}: {exc.read().decode()[:300]}")
                if exc.code < 500:
                    raise failure
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                failure = LedgerError(f"账本不可达: {getattr(exc, 'reason', exc)}")
        raise failure or LedgerError("账本请求失败")

    def add(self, ids: list[str]) -> dict:
        if not self.enabled or not ids:
            return {"received": 0, "inserted": 0}
        try:
            return self._request("POST", self.project_url(), {"ids": ids})
        except LedgerError as exc:
            self.degrade("write", exc)
            return {"received": len(ids), "inserted": 0, "error": str(exc)}

    def existing(self) -> list[dict]:
        if not self.enabled:
            return []
        try:
            return self._all()
        except LedgerError as exc:
            self.degrade("read", exc)
            return []

    def ids(self) -> set[str]:
        return {str(item.get("id")) for item in self.existing() if item.get("id")}

    def synced_count(self) -> int | None:
        """读不到就是 None —— 不能把「台账挂了」显示成「0 条已同步」。"""
        items = self.existing()
        return None if self.error else len(items)

    def _all(self) -> list[dict]:
        items: list[dict] = []
        cursor = None
        since = datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        while True:
            query = {"limit": "1000", "since": since}
            if cursor:
                query["cursor"] = cursor
            page = self._request("GET", self.project_url("?" + urllib.parse.urlencode(query)))
            items.extend(page.get("items") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                return items


def ledger_for(source: str, config: dict, strict: bool = False) -> Ledger:
    return Ledger(
        os.environ.get("ID_LEDGER_URL"),
        os.environ.get("ID_LEDGER_TOKEN"),
        config["ledger_projects"][source],
        strict=strict,
    )


# ------------------------------------------------------------------ 来源：ima


def ima_call(endpoint: str, payload: dict, config: dict) -> dict:
    skill = ima_skill()
    out = run(
        ["node", str(skill / "ima_api.cjs"), f"openapi/wiki/v1/{endpoint}", json.dumps(payload, ensure_ascii=False)],
        cwd=skill,
        timeout=120,
    )
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise SyncError(f"ima 返回不可解析: {out[:300]}")
    if data.get("code") not in (0, None):
        raise SyncError(f"ima {endpoint} 失败 code={data.get('code')} msg={data.get('msg')}")
    return data.get("data") or {}


def ima_kb_id(config: dict) -> str:
    configured = str(config["ima"].get("knowledge_base") or "").strip()
    if configured.startswith("ima_kb_"):
        return configured
    query = configured or "知识库"
    data = ima_call("search_knowledge_base", {"query": "" if not configured else query, "cursor": "", "limit": 20}, config)
    bases = data.get("info_list") or []
    if not bases:
        raise SyncError("ima 里没有可用的知识库")
    if not configured:
        raise SyncError(
            "config 里没配 ima.knowledge_base；可选：" + "、".join(f"{b['kb_name']}({b['kb_id']})" for b in bases)
        )
    for base in bases:
        if configured in (base.get("kb_name"), base.get("kb_id")):
            return base["kb_id"]
    raise SyncError(f"没找到知识库「{configured}」；可选：" + "、".join(b["kb_name"] for b in bases))


def ima_media_cache_path(config: dict) -> Path:
    return data_dir(config) / "ima-media-cache.json"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def wechat_id_from_url(url: str) -> str:
    match = WECHAT_SLUG_RE.search(url or "")
    if match:
        return f"wx:{match.group(1)}"
    digest = hashlib.sha1((url or "").strip().encode()).hexdigest()[:16]
    return f"wx:{digest}"


def ima_wechat_entries(kb_id: str, limit: int, config: dict) -> list[dict]:
    """按知识库目录顺序取条目（新收藏在前），支持一层层递归子文件夹。"""
    entries: list[dict] = []
    queue: list[str | None] = [None]
    seen_folders: set[str] = set()
    while queue and len(entries) < limit:
        folder = queue.pop(0)
        cursor = ""
        while len(entries) < limit:
            payload = {"knowledge_base_id": kb_id, "cursor": cursor, "limit": min(50, limit - len(entries))}
            if folder:
                payload["folder_id"] = folder
            data = ima_call("get_knowledge_list", payload, config)
            for item in data.get("knowledge_list") or []:
                if item.get("folder_id"):  # 文件夹条目
                    if item["folder_id"] not in seen_folders:
                        seen_folders.add(item["folder_id"])
                        queue.append(item["folder_id"])
                    queue.sort()
                    continue
                entries.append(item)
                if len(entries) >= limit:
                    break
            cursor = data.get("next_cursor") or ""
            if data.get("is_end") or not cursor:
                break
    return entries


def plan_wechat(config: dict, limit: int) -> dict:
    kb_id = ima_kb_id(config)
    entries = ima_wechat_entries(kb_id, limit, config)
    cache_path = ima_media_cache_path(config)
    cache: dict = load_json(cache_path, {})

    items: list[dict] = []
    skipped_non_wechat = 0
    for entry in entries:
        media_id = entry.get("media_id") or ""
        media_type = entry.get("media_type")
        if media_type not in (6, 2, None):
            skipped_non_wechat += 1
            continue
        cached = cache.get(media_id) or {}
        url = cached.get("url") or ""
        if not url:
            try:
                info = ima_call("get_media_info", {"media_id": media_id}, config)
            except SyncError as exc:
                log(f"warn: get_media_info 失败 {media_id}: {exc}")
                continue
            url = ((info.get("url_info") or {}).get("url") or "").strip()
            if not url:
                continue
            cache[media_id] = {"url": url, "title": entry.get("title") or "", "media_type": info.get("media_type")}
        if "mp.weixin.qq.com" not in url:
            skipped_non_wechat += 1
            continue
        items.append(
            {
                "id": wechat_id_from_url(url),
                "source": "wechat",
                "title": entry.get("title") or cached.get("title") or "",
                "url": url,
                "media_id": media_id,
                "author": "",
                "published": "",
                "status": "new",
            }
        )
    save_json(cache_path, cache)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "wechat",
        "knowledge_base": kb_id,
        "window": len(entries),
        "counts": {
            "window": len(entries),
            "candidates": len(items),
            "skipped_non_wechat": skipped_non_wechat,
        },
        "items": items,
    }


# ------------------------------------------------------------------- 来源：X


def tweet_article_id(tweet: dict) -> str | None:
    """X 长文（article）识别：articleTitle 只在长文上出现；否则看展开后的 url。"""
    if (tweet.get("articleTitle") or "").strip():
        return str(tweet.get("id"))
    haystack = [str(tweet.get("text") or ""), *(tweet.get("urls") or [])]
    quoted = tweet.get("quotedTweet") or {}
    haystack.extend([str(quoted.get("text") or ""), *(quoted.get("urls") or [])])
    for chunk in haystack:
        match = ARTICLE_URL_RE.search(chunk)
        if match:
            return match.group(1)
    return None


def fetch_bookmarks(count: int) -> list[dict]:
    twitter = find_bin(None, "TWITTER_CLI", "twitter")
    payload = run_json([twitter, "bookmarks", "-n", str(count), "--json"], timeout=600)
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("items") or payload.get("tweets") or []
    return [tweet for tweet in payload if isinstance(tweet, dict) and tweet.get("id")]


def plan_x(config: dict, count: int) -> dict:
    tweets = fetch_bookmarks(count)
    items = []
    filtered: list[dict] = []
    for tweet in tweets:
        article_id = tweet_article_id(tweet)
        if not article_id:
            continue
        handle = (tweet.get("author") or {}).get("screenName") or "i"
        if article_id == str(tweet["id"]):  # 只有 articleTitle，没有 /i/article/ 链接
            article_id = None
            url = f"https://x.com/{handle}/status/{tweet['id']}"
        else:
            url = f"https://x.com/i/article/{article_id}"
        items.append(
            {
                "id": f"x:{tweet['id']}",
                "source": "x",
                "title": (tweet.get("articleTitle") or "").strip(),
                "url": url,
                "tweet_id": str(tweet["id"]),
                "article_id": str(article_id),
                "author": (tweet.get("author") or {}).get("name") or "",
                "handle": (tweet.get("author") or {}).get("screenName") or "",
                "published": tweet.get("createdAtISO") or "",
                "status": "new",
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "x",
        "counts": {"bookmarks": len(tweets), "candidates": len(items), "non_article": len(tweets) - len(items)},
        "items": items,
        "filtered": filtered,
    }


# ------------------------------------------------------------------ SiYuan 侧


def siyuan_synced_ids() -> set[str]:
    """地面真相：文档属性 custom-sync-id + 正文里的 sync-id 行。"""
    ids: set[str] = set()
    try:
        rows = sy_sql("select value from attributes where name = 'custom-sync-id'")
        for row in rows:
            value = (row.get("value") or "").strip()
            if value:
                ids.add(value)
    except SyncError as exc:
        log(f"warn: 读文档属性失败，仅靠正文兜底（{exc}）")
    try:
        rows = sy_sql("select content from blocks where type = 'p' and content like '%sync-id:%'")
        for row in rows:
            for match in SYNC_ID_RE.finditer(row.get("content") or ""):
                ids.add(match.group(1).rstrip(".,;"))
    except SyncError as exc:
        log(f"warn: 正文扫描失败（{exc}）")
    return ids


def siyuan_doc_paths(prefix: str) -> set[str]:
    safe = prefix.replace("'", "''")
    rows = sy_sql(f"select hpath from blocks where type = 'd' and hpath like '{safe}/%'")
    return {(row.get("hpath") or "").strip() for row in rows}


def sanitize_segment(title: str, fallback: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", title or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = text[:80].strip()
    return text or fallback


def unique_hpath(base: str, title: str, item_id: str, existing: set[str]) -> str:
    segment = sanitize_segment(title, item_id.replace(":", "-"))
    candidate = f"{base}/{segment}"
    if candidate not in existing:
        return candidate
    suffix = hashlib.sha1(item_id.encode()).hexdigest()[:4]
    candidate = f"{base}/{segment} ({suffix})"
    return candidate


def publish_doc(notebook: str, hpath: str, markdown: str, item: dict) -> str:
    out = sy(["create", notebook, hpath, "-f", "-", "--json"], input_text=markdown, json_out=True)
    try:
        doc_id = json.loads(out).get("docId") or ""
    except json.JSONDecodeError:
        doc_id = ""
    if not doc_id:
        raise SyncError(f"create 没返回 docId: {out[:200]}")
    attrs = [f"custom-sync-id={item['id']}"]
    if item.get("url"):
        attrs.append(f"custom-source-url={item['url']}")
    if item.get("source"):
        attrs.append(f"custom-sync-source={item['source']}")
    # 文档刚 create 出来时 blocks 索引还没落库，属性反查会失败 —— 退避重试
    for attempt in range(6):
        try:
            sy(["attr", "set", doc_id, *attrs])
            return doc_id
        except SyncError:
            if attempt == 5:
                raise
            log(f"… 文档索引未就绪，{1.5 * (attempt + 1):.1f}s 后重试写入块属性（{doc_id}）")
            time.sleep(1.5 * (attempt + 1))
    return doc_id


# ------------------------------------------------------------------- 转换/排版


def fetch_wechat_html(url: str, timeout: int = 60) -> str:
    """自己把 HTML 拿回来，只做一件事：把懒加载的图片补上 src。

    微信正文里的图片只有 `data-src`（没有 `src`），turndown 会整张丢掉 ——
    实测一篇 28 图的文章转出来 0 张图。补 src 之后再交给 wechat2md，
    表格/代码块等转换逻辑仍然全部由它负责。
    """
    request = urllib.request.Request(
        url, headers={"User-Agent": WECHAT_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        html = response.read().decode("utf-8", errors="replace")

    def fix(match: re.Match) -> str:
        tag = match.group(0)
        data_src = re.search(r'data-src="([^"]+)"', tag)
        if not data_src:
            return tag
        tag = re.sub(r'\s+src="[^"]*"', "", tag)
        return tag.replace("<img", f'<img src="{data_src.group(1)}"', 1)

    return re.sub(r"<img\b[^>]*>", fix, html)


def has_pictext_data(html: str) -> bool:
    return all(marker in html for marker in WECHAT_PICDATA_MARKERS)


def share_page_meta(html: str) -> tuple[str, str]:
    """分享页上唯一能拿到的信息：标题 + 摘要（meta description / content_noencode）。"""
    title = ""
    match = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    if match:
        title = match.group(1)
    if not title:
        match = re.search(r"msg_title\s*=\s*window\.title\s*=\s*'([^']*)'", html)
        if match:
            title = match.group(1)
    desc = ""
    match = re.search(r'<meta name="description" content="([^"]*)"', html)
    if match:
        desc = match.group(1)
    if not desc:
        match = re.search(r"content_noencode:\s*'([^']*)'", html)
        if match:
            desc = match.group(1)
    # 微信只对 ASCII 做了 \xNN 转义（\x26quot; 等），整串 unicode_escape 会把 UTF-8 中文弄坏
    for escaped, plain in (
        ("\\x26quot;", '"'),
        ("\\x26amp;", "&"),
        ("\\x26", "&"),
        ("\\x0a", " "),
        ("\\x3d", "="),
        ("\\x27", "'"),
        ("\\x22", '"'),
    ):
        desc = desc.replace(escaped, plain)
    desc = re.sub(r"\s+", " ", desc).strip()
    return title.strip(), desc.strip()


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    raw = text[3:end]
    body = text[end + 4 :].lstrip("\n")
    meta: dict = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"').strip()
    return meta, body


def strip_leading_noise(body: str) -> str:
    """去掉正文开头的空行、残留 H1、分隔线（wechat2md/twitter article 的头部残渣）。"""
    lines = body.lstrip("\n").splitlines()
    while lines:
        head = lines[0].strip()
        if not head or head.startswith("# ") or re.fullmatch(r"-{3,}", head):
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def source_block(item: dict, author: str, published: str) -> str:
    label = author or item.get("handle") or ""
    link = f"[{label or '原文'}]({item['url']})" if label else f"[原文]({item['url']})"
    meta = " · ".join(part for part in (link, published) if part)
    return f"> {meta}\n> sync-id: {item['id']}\n"


def share_stub_markdown(item: dict, error: "SharePageError") -> tuple[str, dict]:
    """分享页只能拿到标题+摘要时，发一张「链接卡片」而不是一篇假文章。"""
    title = error.title or item.get("title") or "未命名文章"
    parts = [
        "> [!TIP] 正文需在微信里打开",
        "> 这篇文章微信只对「微信内打开」返回正文；HTTP 拿到的是分享页（无 `#js_content`），"
        "所以这里只有标题、摘要和原链接。",
        "",
    ]
    if error.desc:
        parts += ["**摘要（微信给的分享描述）**", "", error.desc, ""]
    markdown = f"{source_block(item, '', '')}\n" + "\n".join(parts)
    guard_markdown(markdown, item)
    return markdown, {"title": title, "author": "", "published": "", "bytes": len(markdown.encode())}


def guard_markdown(markdown: str, item: dict) -> None:
    """转换结果的安检：整页 JS 混入、体积离谱，都不许进库（会卡死 SiYuan 页面）。"""
    size = len(markdown.encode())
    if size > MAX_DOC_BYTES:
        raise SyncError(
            f"转换结果 {size} 字节，超过 {MAX_DOC_BYTES} 上限（公众号正文不会这么大，疑似整页 JS 混入），丢弃"
        )
    hit = next((marker for marker in GARBAGE_MARKERS if marker in markdown), None)
    if hit:
        raise SyncError(f"转换结果里混进了页面 JS（命中 {hit!r}），丢弃")


def article_markdown(item: dict, config: dict) -> tuple[str, dict]:
    """把来源正文抓成 markdown，并拼上 SiYuan 侧的来源/同步标记。"""
    tmp = data_dir(config) / "tmp"
    tmp.mkdir(exist_ok=True)
    out_path = tmp / (item["id"].replace(":", "-") + ".md")
    out_path.unlink(missing_ok=True)  # 上一轮的产物不能顶替这一轮
    if item["source"] == "wechat":
        script = wechat2md_script()
        deps = data_dir(config) / "w2m"
        deps.mkdir(exist_ok=True)
        try:
            html = fetch_wechat_html(item["url"])
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SyncError(f"抓取文章 HTML 失败: {getattr(exc, 'reason', exc)}")
        html_path = tmp / (item["id"].replace(":", "-") + ".html")
        html_path.write_text(html, encoding="utf-8")
        if WECHAT_CONTENT_MARKER not in html and not has_pictext_data(html):
            title, desc = share_page_meta(html)
            raise SharePageError(
                "公众号只返回了分享页模板，页面上既没有 #js_content 也没有 cgiDataNew 数据（正文仅微信内可见）",
                title=title,
                desc=desc,
            )
        stdout = run(
            ["node", str(script), str(html_path), "-o", str(out_path)],
            cwd=deps,
            timeout=240,
        )
        # wechat2md 抓不到正文时仍然 exit 0，只在 stdout 留一行 FAIL，也不写文件
        if "FAIL" in stdout:
            raise SyncError(f"wechat2md 抓取失败: {stdout.strip().splitlines()[-1][:200]}")
        if not out_path.is_file():
            raise SyncError("wechat2md 没有产出文件（风控/已删/网络？重跑会重试）")
        raw = out_path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(raw)
        author = " · ".join(part for part in (meta.get("author"), meta.get("site")) if part and part != meta.get("author"))
        author = meta.get("author") or meta.get("site") or item.get("author") or ""
        published = meta.get("published") or ""
        title = item.get("title") or meta.get("title") or ""
        body = re.sub(r"^\s*#\s+.*\n", "", body, count=1)
        if len(body.strip()) < 120:
            raise SyncError(f"正文过短（{len(body.strip())} 字），疑似风控/已删，不发布")
    else:
        twitter = find_bin(None, "TWITTER_CLI", "twitter")
        run([twitter, "article", item["tweet_id"], "-m", "-o", str(out_path)], timeout=240)
        if not out_path.is_file():
            raise SyncError("twitter article 没有产出文件（长文正文取不到？）")
        raw = out_path.read_text(encoding="utf-8")
        title = item.get("title") or ""
        published = ""
        author = item.get("author") or ""
        lines = raw.splitlines()
        kept: list[str] = []
        for line in lines:
            if line.startswith("# ") and not title:
                title = line[2:].strip()
                continue
            if re.match(r"^- (Author|Published|URL|Likes|Retweets|Replies|Bookmarks|Views):", line):
                if line.startswith("- Published:"):
                    published = line.split(":", 1)[1].strip()
                if line.startswith("- Author:") and not author:
                    author = line.split(":", 1)[1].strip()
                continue
            kept.append(line)
        body = "\n".join(kept).lstrip("\n")
        if len(body.strip()) < 120:
            raise SyncError(f"正文过短（{len(body.strip())} 字），长文没取到，不发布")

    title = (title or item.get("title") or "未命名文章").strip()
    # SiYuan 的文档名就是标题，正文里再来一个同名 H1 就是重复标题
    markdown = f"{source_block(item, author, published)}\n{strip_leading_noise(body)}\n"
    guard_markdown(markdown, item)
    return markdown, {"title": title, "author": author, "published": published, "bytes": len(markdown.encode())}


# --------------------------------------------------------------------- 命令


def cmd_doctor(args) -> int:
    config = load_config()
    checks: list[dict] = []

    def check(name: str, fn):
        try:
            detail = fn()
            checks.append({"check": name, "ok": True, "detail": detail})
        except Exception as exc:  # noqa: BLE001 - doctor 要报告全部问题
            checks.append({"check": name, "ok": False, "detail": str(exc)[:300]})

    check("ima skill", lambda: str(ima_skill()))
    check("ima 凭据", lambda: "已配置" if (os.environ.get("IMA_OPENAPI_CLIENTID") and os.environ.get("IMA_OPENAPI_APIKEY")) else (_ for _ in ()).throw(SyncError("缺少 IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY")))
    check("ima 知识库", lambda: ima_kb_id(config))
    check("wechat2md", lambda: str(wechat2md_script()))

    def w2m_deps():
        deps = data_dir(config) / "w2m"
        out = run(["node", "-e", "require.resolve('jsdom')"], cwd=deps if deps.is_dir() else None)
        return "jsdom 就位" if out is not None else ""

    check("wechat2md 依赖", w2m_deps)
    check("twitter-cli", lambda: find_bin(None, "TWITTER_CLI", "twitter"))

    def twitter_auth():
        twitter = find_bin(None, "TWITTER_CLI", "twitter")
        return run([twitter, "status"], timeout=60).strip().splitlines()[-1][:120]

    check("twitter 登录", twitter_auth)
    check("siyuan skill", lambda: str(siyuan_script()))
    check("siyuan 连通", lambda: sy(["ping"]).strip().splitlines()[-1][:120])

    def notebook_perm():
        out = sy(["perms", "show"], json_out=True)
        return "已确认" if '"reviewed": true' in out or "'reviewed': True" in out else out[-200:]

    check("siyuan 权限表", notebook_perm)
    check("id-ledger 配置", lambda: "已配置" if (os.environ.get("ID_LEDGER_URL") and os.environ.get("ID_LEDGER_TOKEN")) else (_ for _ in ()).throw(SyncError("缺少 ID_LEDGER_URL / ID_LEDGER_TOKEN")))
    check("id-ledger 读取", lambda: f"{len(ledger_for('wechat', config).ids())} 个已同步 id（wechat 项目）")

    print(json.dumps({"ok": all(c["ok"] for c in checks), "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if all(c["ok"] for c in checks) else 1


def cmd_status(args) -> int:
    config = load_config()
    report = {}
    for source in ("wechat", "x"):
        ledger = ledger_for(source, config)
        count = ledger.synced_count()
        report[source] = {
            "project": ledger.project,
            "enabled": ledger.enabled,
            "synced": count,
            **({"error": ledger.error} if count is None else {}),
        }
    report["siyuan"] = {"notebook": config["notebook"], "synced_docs": len(siyuan_synced_ids())}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_seed(args) -> int:
    config = load_config()
    ids = siyuan_synced_ids()
    by_source = {"wechat": [], "x": []}
    for item in sorted(ids):
        if item.startswith("wx:"):
            by_source["wechat"].append(item)
        elif item.startswith("x:"):
            by_source["x"].append(item)

    report = {}
    for source, values in by_source.items():
        ledger = ledger_for(source, config, strict=True)
        if not ledger.enabled:
            raise SystemExit("台账未配置：设置 ID_LEDGER_URL / ID_LEDGER_TOKEN")
        inserted = 0
        for start in range(0, len(values), 500):
            result = ledger.add(values[start : start + 500])
            inserted += int(result.get("inserted") or 0)
        report[source] = {"project": ledger.project, "found": len(values), "inserted": inserted}
    print(json.dumps({"ok": True, "seeded": report}, ensure_ascii=False, indent=2))
    return 0


def cmd_plan(args) -> int:
    config = load_config()
    sources = ["wechat", "x"] if args.source == "all" else [args.source]
    out_dir = data_dir(config)
    summary: dict = {"ok": True, "plans": {}}
    for source in sources:
        plan = plan_wechat(config, args.limit) if source == "wechat" else plan_x(config, args.count)
        ledger = ledger_for(source, config)
        known = ledger.ids() | siyuan_synced_ids()
        new_items = []
        for item in plan["items"]:
            if item["id"] in known:
                continue
            new_items.append(item)
        plan["items"] = new_items
        plan["counts"]["new"] = len(new_items)
        plan["counts"]["already_synced"] = plan["counts"]["candidates"] - len(new_items)
        path = Path(args.out) if args.out else out_dir / f"plan-{source}.json"
        save_json(path, plan)
        summary["plans"][source] = {
            "plan": str(path),
            "counts": plan["counts"],
            "items": [
                {"id": item["id"], "title": item["title"], "url": item["url"]} for item in new_items
            ],
        }
        log(f"{source}: 候选 {plan['counts']['candidates']}，新增 {len(new_items)} -> {path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_publish(args) -> int:
    config = load_config()
    plan_path = Path(args.plan).expanduser()
    if not plan_path.is_file():
        raise SystemExit(f"plan 不存在: {plan_path}")
    plan = load_json(plan_path, None)
    if not isinstance(plan, dict) or "items" not in plan:
        raise SystemExit(f"plan 结构不对: {plan_path}")
    source = plan.get("source") or "wechat"
    notebook = args.notebook or config["notebook"]
    base = (config["paths"].get(source) or f"/{source}").rstrip("/")
    ledger = ledger_for(source, config, strict=bool(args.strict_ledger))
    already = ledger.ids() | (set() if args.ignore_siyuan else siyuan_synced_ids())
    existing_paths = siyuan_doc_paths(base)

    report: dict = {
        "source": source,
        "plan": str(plan_path),
        "notebook": notebook,
        "base_path": base,
        "published": [],
        "skipped": [],
        "failed": [],
        "counts": {"total": len(plan["items"])},
    }
    delay = float(config["ima"].get("fetch_delay") or 0)
    fetched = 0
    for item in plan["items"]:
        item_id = item.get("id") or ""
        if args.only and item_id not in args.only:
            continue
        if item.get("status") == "skip":
            report["skipped"].append({"id": item_id, "reason": item.get("skip_reason") or "marked skip"})
            continue
        if item_id in already:
            report["skipped"].append({"id": item_id, "reason": "already synced"})
            continue
        if source == "wechat" and fetched and delay:
            # 批量抓公众号的礼貌间隔（skill 建议 10~20s，随机抖动）
            time.sleep(random.uniform(delay * 0.6, delay * 1.4))
        fetched += 1
        stub = False
        try:
            markdown, meta = article_markdown(item, config)
        except SharePageError as exc:
            if not args.stub_on_share:
                report["failed"].append(
                    {"id": item_id, "stage": "share_page", "error": str(exc)[:400], "title": exc.title or item.get("title")}
                )
                log(f"⛔ {item_id} 分享页（微信内才有正文），跳过：{exc.title or item.get('title')}")
                continue
            markdown, meta = share_stub_markdown(item, exc)
            stub = True
        except (SyncError, OSError) as exc:
            message = str(exc)
            stage = "guard" if ("丢弃" in message or "上限" in message) else "convert"
            report["failed"].append({"id": item_id, "stage": stage, "error": message[:400]})
            continue
        hpath = unique_hpath(base, item.get("title") or meta["title"], item_id, existing_paths)
        payload = {**item, "title": meta["title"] or item.get("title")}
        if args.dry_run:
            report["published"].append({"id": item_id, "doc": None, "path": hpath, "dry_run": True, "bytes": meta["bytes"]})
            continue
        try:
            doc_id = publish_doc(notebook, hpath, markdown, payload)
        except SyncError as exc:
            report["failed"].append({"id": item_id, "stage": "publish", "error": str(exc)[:400]})
            continue
        existing_paths.add(hpath)
        ledger.add([item_id])
        already.add(item_id)
        report["published"].append(
            {
                "id": item_id,
                "doc": doc_id,
                "path": hpath,
                "title": payload["title"],
                "bytes": meta["bytes"],
                "url": item.get("url"),
                **({"stub": True} if stub else {}),
            }
        )
        log(f"✅ {item_id} -> {hpath}")

    report["counts"].update(
        {
            "published": len(report["published"]),
            "skipped": len(report["skipped"]),
            "failed": len(report["failed"]),
        }
    )
    out_path = Path(args.report) if args.report else plan_path.with_name(
        f"report-{source}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    save_json(out_path, report)
    print(json.dumps({"ok": not report["failed"], "report": str(out_path), "counts": report["counts"]}, ensure_ascii=False, indent=2))
    return 0 if not report["failed"] else 1


# ---------------------------------------------------------------------- CLI


def config_default_stub() -> bool:
    return bool(load_config()["ima"].get("stub_on_share"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ima 微信文章 / X 长文书签 -> SiYuan（增量，id-ledger 去重）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="依赖与凭据体检")
    p_doctor.set_defaults(func=cmd_doctor)

    p_status = sub.add_parser("status", help="台账 & SiYuan 已同步数量")
    p_status.set_defaults(func=cmd_status)

    p_seed = sub.add_parser("seed", help="从已有 SiYuan 文档回填台账")
    p_seed.set_defaults(func=cmd_seed)

    p_plan = sub.add_parser("plan", help="拉取来源并过滤已同步")
    p_plan.add_argument("--source", choices=["wechat", "x", "all"], default="all")
    p_plan.add_argument("--limit", type=int, default=None, help="ima 知识库扫描窗口（默认 50）")
    p_plan.add_argument("--count", type=int, default=None, help="拉取多少条 X 书签（默认 100）")
    p_plan.add_argument("--out", help="plan 输出路径（默认数据目录 plan-<source>.json）")
    p_plan.set_defaults(func=cmd_plan)

    p_pub = sub.add_parser("publish", help="按 plan 转换并发布到 SiYuan")
    p_pub.add_argument("--plan", required=True)
    p_pub.add_argument("--notebook", help="覆盖目标笔记本 id")
    p_pub.add_argument("--only", nargs="*", help="只处理这些 id")
    p_pub.add_argument("--dry-run", action="store_true", help="只转换不写库")
    p_pub.add_argument("--report", help="报告输出路径")
    p_pub.add_argument("--ignore-siyuan", action="store_true", help="只信台账，不扫 SiYuan 文档")
    p_pub.add_argument("--strict-ledger", action="store_true", help="台账不可用就直接失败")
    p_pub.add_argument(
        "--stub-on-share",
        action="store_true",
        default=bool(config_default_stub()),
        help="分享页（微信内才有正文）也发一张链接卡片；默认跳过记 failed",
    )
    p_pub.set_defaults(func=cmd_publish)

    args = parser.parse_args(argv)
    config = load_config()
    if getattr(args, "limit", None) is None:
        args.limit = int(config["ima"].get("scan_limit") or 50)
    if getattr(args, "count", None) is None:
        args.count = int(config["x"].get("count") or 100)
    try:
        return args.func(args)
    except SyncError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())