---
name: sync-to-siyuan
description: 把「收藏的长文」增量搬进 SiYuan（思源）——ima 知识库里收藏的微信公众号文章，和 X（Twitter）书签里的长文/Article。流程是 ima 取链接 → wechat2md 转 Markdown / twitter article 取 Markdown → siyuan-skill 发布，所有内容 id 写远程 id-ledger 去重。当用户说「同步文章到思源 / 把知识库里的公众号文章存进思源 / 把推特长文书签搬到思源 / 增量同步文章 / 检查有没有新的文章没进思源」时使用。
---

# 文章 → SiYuan（增量编排）

两条来源，一个出口：**收藏的长文进思源，一篇一篇文档，重复跑安全。**

本 skill 只做 **编排、去重、排版**。抓正文、转 Markdown、写库都不在这里重写：

| 依赖 | 负责 | 调用 |
| --- | --- | --- |
| [ima skill](https://ima.qq.com) | 知识库条目列表、原文 URL | `node ima_api.cjs openapi/wiki/v1/<endpoint> '<json>'` |
| [wechat2md](https://github.com/rogeecn/wechat2md) | 微信 HTML → Markdown | `node wechat2md.mjs <url> -o out.md` |
| [twitter-cli](https://github.com/public-clis/twitter-cli) | 书签列表、X 长文 Markdown | `twitter bookmarks -n N --json` / `twitter article <id> -m` |
| [siyuan skill](https://github.com/rogeecn/siyuan-skill) | 建文档、块属性、只读 SQL、权限 GUARD | `python3 siyuan.py create/attr/sql` |
| [id-ledger-query](https://github.com/rogeecn/cf-id-ledger) | 已同步 id 台账 | `POST/GET /v1/projects/<key>/ids` |

## 配置（一次）

`~/.config/sync-to-siyuan.json`（`scripts/sync.py` 里是同一份默认值，可整段不写）：

```json
{
  "notebook": "20260804153230-7rdiobh",
  "paths": { "wechat": "/wechat", "x": "/x" },
  "ledger_projects": { "wechat": "siyuan-wechat", "x": "siyuan-x-article" },
  "ima": { "knowledge_base": "ima_kb_lBjZm8kf", "scan_limit": 50, "fetch_delay": 12 },
  "x": { "count": 100 },
  "data_dir": "~/.cache/sync-to-siyuan"
}
```

- `notebook` 是目标笔记本 id（默认 **00-Inspires**）。该库权限必须是 `R,W`，否则 siyuan skill 的 GUARD 直接拒绝。
- 环境变量：`IMA_OPENAPI_CLIENTID` / `IMA_OPENAPI_APIKEY`、`ID_LEDGER_URL` / `ID_LEDGER_TOKEN`、`SIYUAN_API_URL` / `SIYUAN_TOKEN`。
  **这些变量在 `~/.zshenv` 里，自动化/非交互 shell 不会自动加载 —— 先 `source ~/.zshenv`。**
  SiYuan 两兄弟缺失时脚本会回落到 `~/.siyuan-sisyphus/config.json` 里的当前 profile。

## 第一次使用

```bash
source ~/.zshenv
python3 scripts/sync.py doctor                      # 依赖 + 凭据 + 权限 + 台账 体检
python3 scripts/sync.py seed                        # 把已有 SiYuan 文档里的 sync-id 回填台账
```

`seed` 只做一次。台账丢了、换机器、或别的工具发过同一条，重跑 `seed` 就恢复——**SiYuan 里的文档才是地面真相**。

## 常规增量

```bash
python3 scripts/sync.py plan                       # 两条来源都扫 → 计划文件
python3 scripts/sync.py plan --source wechat --limit 50
python3 scripts/sync.py plan --source x --count 100
# 看一眼 plan 里的 title/url（可选：改标题，或把不想要的 status 设成 "skip"）
python3 scripts/sync.py publish --plan ~/.cache/sync-to-siyuan/plan-wechat.json
python3 scripts/sync.py publish --plan ~/.cache/sync-to-siyuan/plan-x.json --only x:2122895694470185360
python3 scripts/sync.py status                     # 台账 + SiYuan 各自多少条
```

`plan` 只产计划、不写库；`publish` 逐条「转换 → create → 写块属性 → 写台账」，单条失败不影响其余，最后输出 `report-<source>-<时间>.json`。

### 去重契约（为什么重复跑安全）

一条内容算「已同步」，满足任一即跳过：

1. 它的 id 在台账里（`GET /v1/projects/siyuan-wechat|siyuan-x-article/ids`）；
2. SiYuan 里已有文档带同样的 `custom-sync-id` 块属性，或正文里有 `sync-id: <id>` 行。

两个来源取并集。台账快而权威；SiYuan 扫描慢但是地面真相 —— 台账被清空、project key 写错、或别的工具发过同一条，靠扫描仍然不会重复建文档。

`publish` 开跑前会再读一次并集，所以拿一份旧 plan 重跑也不会重复发布。**唯一会破坏契约的做法是绕过 `sync.py` 直接调 siyuan.py 建文档。**

### id 规则

| 来源 | id | 依据 |
| --- | --- | --- |
| 微信文章 | `wx:<slug>` | `https://mp.weixin.qq.com/s/<slug>` 的 slug；非 `/s/<slug>` 形态退化成正文字典序 SHA1 前 16 位 |
| X 长文 | `x:<tweet_id>` | 发长文的推文 id（不是 article id） |

## 两条来源各自怎么取数

### 微信（ima 知识库）

1. `get_knowledge_list` 按目录顺序翻页（`knowledge_base_id`，必要时带 `folder_id` 递归子文件夹），拿 `media_id` / `title` / `media_type`；
2. 只保留 `media_type=6`（公众号文章；`2` 也接受，看 URL 再说）；
3. 对每个候选调 `get_media_info` 换出真实 `mp.weixin.qq.com` URL —— **链接不落库，只在内存/缓存里**（`~/.cache/sync-to-siyuan/ima-media-cache.json`，按 media_id 缓存，第二轮不用再调）；
4. **自己把 HTML 拿回来**（iPhone UA）→ 把 `<img data-src>` 补成 `<img src>` → 交给 `wechat2md` 转 Markdown。
   微信正文图片是懒加载，只有 `data-src`、没有 `src`，turndown 会整张丢掉（实测一篇 28 图的文章转出来 0 张图）。补 src 是唯一由本 skill 亲自做的一步，表格/代码块等转换规则全部仍由 wechat2md 负责。
   **贴图文章**（微信只给分享页、没有 `#js_content`）由 wechat2md 解 `window.cgiDataNew` 处理（`content_noencode` + `picture_page_info_list[].cdn_url`），本 skill 只负责放行。
5. 剥掉 wechat2md 写的 YAML frontmatter，标题/作者/发布时间转成文档顶部的一行来源块。
6. 批量抓取之间默认等 12s（`ima.fetch_delay`，±40% 抖动）——公众号抓取的礼貌间隔。

### X 书签（文章类）

1. `twitter bookmarks -n N --json`；
2. 只留**长文**：有 `articleTitle`，或正文/`urls` 里有 `x.com/i/article/<id>`（普通短推文不归本 skill 管——那是 x-bookmark-sync → memos 的活）；
3. `twitter article <tweet_id> -m` 取 Markdown，去掉顶部的 `- Author/Published/Likes/…` 元数据行。

### 发布后的文档形状

SiYuan 的**文档名就是标题**，正文里不再重复一个 H1：

```markdown
> [<作者>](<原链接>) · <发布时间>
> sync-id: wx:xxxxxxxx

<正文（含远程图片链接）>
```

外加块属性 `custom-sync-id` / `custom-source-url` / `custom-sync-source`（**去重与审计都靠它**，别删）。路径 `/wechat/<标题>`、`/x/<标题>`；同名冲突自动加 `(abcd)` 后缀。

文档刚 create 出来时 SiYuan 的 blocks 索引还没落库，`attr set` 会反查失败 —— `publish` 会退避重试 6 次，仍失败的那条正文里还有 `sync-id:` 行，去重不会漏。

> `/X 收藏/YYYY/MM-DD` 里的那些 `custom-tweet-id` 卡片是另一条流程（短卡片，只有推文正文/文章链接，没有长文正文）。本 skill 只往 `/x/` 放长文，两者不冲突、不互相去重。

## agent 在这一步做什么

`plan` 给的标题是**抽取**来的，不是理解出来的：

- 微信：知识库标题通常就是文章标题，一般不用动；空标题才补。
- X：`articleTitle` 是对的作者标题；为空时读正文补一句 ≤30 字的中文（说清「这条给了什么」，别写「分享一个…」）。
- 明显是广告/重复内容、或你不想收的：把 `status` 设 `"skip"` 并写 `skip_reason`，`publish` 会跳过并列进报告。

改完 plan 再 `publish`；不要为了排版去改 `scripts/sync.py`。

## 排错

| 症状 | 原因 / 处理 |
| --- | --- |
| `缺少依赖。先在当前目录执行: npm i …` | wechat2md 从 **cwd** 解析依赖。`sync.py` 固定在 `<data_dir>/w2m` 跑；首次先 `cd ~/.cache/sync-to-siyuan/w2m && npm i jsdom turndown turndown-plugin-gfm` |
| `权限拒绝：… 需要 W` | 目标笔记本权限不是 `RW`。`python3 <siyuan-skill>/scripts/siyuan.py perms set <nb> R,W && perms confirm`（**先问用户**） |
| 微信抓回空正文 / 极短 | 风控或已删除。等 60s 再试，或跳过这一条（别把空文档发进去） |
| `get_media_info` 没有 `url_info` | IMA 侧媒体不可访问（附件/文件类）。跳过即可 |
| `转换结果 … 超过 400000 上限（疑似整页 JS 混入）` | 第二道闸。说明 wechat2md 又把整页 JS 当正文了 —— 先升级 wechat2md（贴图文章支持），再重跑；**绝不要调高 `MAX_DOC_BYTES` 放行** |
| `stage: share_page` | 页面既没有 `#js_content` 也没有 `cgiDataNew` 数据（真·只在微信里能看）。默认记 failed 不建文档；要留链接就 `--stub-on-share` 发一张卡片 |
| ledger 返回 `{"error":"since is required"}` | 手写 curl 时漏了 `since`；`sync.py` 已自带 `since=1970` |
| `台账 read 失败…改用 SiYuan 扫描兜底` | Cloudflare 偶发掐连接，已重试 4 次；读到 SiYuan 就够，事后 `seed` 补齐台账 |
| `status` 里 `synced: null` | 同上，台账读不到时报 null，**不会把「没读到」显示成 0** |
| 非交互 shell 里所有凭据都空 | 先 `source ~/.zshenv` |
| 文档建了但台账没写 | 下次 `publish` 会被 SiYuan 扫描拦住；跑一次 `seed` 把台账补齐 |

## 文件

```
SKILL.md
scripts/sync.py        # doctor / seed / plan / publish / status
```

数据目录 `~/.cache/sync-to-siyuan/`：`plan-<source>.json`、`report-<source>-*.json`、`tmp/*.html|*.md`（转换中间产物）、`w2m/`（wechat2md 的 npm 依赖）、`ima-media-cache.json`（media_id → URL）。

## 一次完整跑的实测口径（2026-11-18）

`plan --source all` → `publish`：微信窗口 50 条 → 50 篇全部入库，X 书签 100 条里 3 条长文入库；重跑 plan 两条来源 `new: 0`；台账与 SiYuan 扫描 53 = 53。单篇微信约 25–45s（抓 HTML 3MB + 转换 + 建文档 + 礼貌间隔），50 篇要 20 分钟上下 —— 命令跑长任务时记得给自己留足够的 timeout。

50 篇正文合计 227KB（最大一篇 11.7KB），其中 9 篇是贴图文章（1–6KB + 1–8 张图）。**历史教训**：贴图文章曾因为整页转出 2MB 内联 JS，把 SiYuan 页面卡死 —— 现在体积闸 + JS 特征闸 + wechat2md 的 `cgiDataNew` 解析三重兜底。