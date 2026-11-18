# sync-to-siyuan-skill

把「收藏的长文」增量搬进 SiYuan（思源笔记）：**ima 知识库里收藏的微信公众号文章**，和 **X（Twitter）书签里的长文/Article**。

本仓库只做三件事：**编排、去重、排版**。抓正文、转 Markdown、写库全部交给既有 skill：

| 依赖 | 负责 | 调用 |
| --- | --- | --- |
| [ima-skill](https://ima.qq.com) | 知识库条目列表、原文 URL | `node ima_api.cjs openapi/wiki/v1/<endpoint> '<json>'` |
| [wechat2md](https://github.com/rogeecn/wechat2md) | 微信 HTML → Markdown | `node wechat2md.mjs article.html -o out.md` |
| [twitter-cli](https://github.com/public-clis/twitter-cli) | 书签列表、X 长文 Markdown | `twitter bookmarks -n N --json` / `twitter article <id> -m` |
| [siyuan-skill](https://github.com/rogeecn/siyuan-skill) | 建文档、块属性、只读 SQL、权限 GUARD | `python3 siyuan.py create/attr/sql` |
| [id-ledger-query](https://github.com/rogeecn/cf-id-ledger) | 已同步 id 台账 | `POST/GET /v1/projects/<key>/ids` |

## 效果

```
/wechat/NAS 家庭影音新神器，一句话「找片+下载+刮削」
/x/如何用自己的数据微调一个小模型
```

每篇一个文档，正文保留远程图片，顶部一行来源：

```markdown
> [可爱的小Cherry](https://mp.weixin.qq.com/s/pteLxFMpqBepmwSqAz_C0g) · 2026年11月17日 21:26
> sync-id: wx:pteLxFMpqBepmwSqAz_C0g
```

## 安装

```bash
git clone https://github.com/rogeecn/sync-to-siyuan-skill ~/Workspace/sync-to-siyuan-skill
ln -sfn ~/Workspace/sync-to-siyuan-skill ~/.agents/skills/sync-to-siyuan

# wechat2md 从 cwd 解析依赖，装一次
mkdir -p ~/.cache/sync-to-siyuan/w2m && cd ~/.cache/sync-to-siyuan/w2m
npm i jsdom turndown turndown-plugin-gfm
```

依赖的 skill 按 `~/.agents/skills/`、`~/.pi/agent/skills/`、`~/.claude/skills/` 顺序自动探测，也可用 `IMA_SKILL` / `WECHAT2MD_SKILL` / `SIYUAN_SKILL` / `TWITTER_CLI` 指定。

## 凭据

| 用途 | 环境变量 |
| --- | --- |
| ima 知识库 | `IMA_OPENAPI_CLIENTID` / `IMA_OPENAPI_APIKEY` |
| id 台账 | `ID_LEDGER_URL` / `ID_LEDGER_TOKEN` |
| 思源 | `SIYUAN_API_URL` / `SIYUAN_TOKEN` |

凭据只从环境变量读。非交互 shell 记得先 `source ~/.zshenv`；思源两个变量缺失时会回落到 `~/.siyuan-sisyphus/config.json` 的当前 profile。

## 配置

`~/.config/sync-to-siyuan.json`（可整段不写，用脚本内默认值）：

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

目标笔记本权限必须是 `R,W`：`python3 <siyuan-skill>/scripts/siyuan.py perms set <nb> R,W && perms confirm`。

## 用法

```bash
python3 scripts/sync.py doctor                    # 依赖 + 凭据 + 权限 + 台账 体检
python3 scripts/sync.py seed                      # 首次：把已有 SiYuan 文档里的 sync-id 回填台账

python3 scripts/sync.py plan                      # 两条来源都扫（默认 ima 窗口 50 / X 书签 100）
python3 scripts/sync.py publish --plan ~/.cache/sync-to-siyuan/plan-wechat.json
python3 scripts/sync.py publish --plan ~/.cache/sync-to-siyuan/plan-x.json --only x:2122895694470185360
python3 scripts/sync.py status                    # 台账 & SiYuan 各多少条
```

`plan` 只写计划文件，`publish` 才动库；中间可以改 `title`，或把不想收的条目标成 `status: "skip"`。

## 增量去重契约

一条内容算「已同步」，满足任一即跳过：

1. id 在台账里（`GET /v1/projects/siyuan-wechat|siyuan-x-article/ids`）；
2. SiYuan 里已有文档带同样的 `custom-sync-id` 块属性，或正文有 `sync-id: <id>` 行。

两个来源取并集：台账快而权威，SiYuan 扫描慢但是地面真相 —— 台账被清空、project key 写错、或别的工具发过同一条，都不会重复建文档。`publish` 开跑前会再读一次并集，所以拿旧 plan 重跑也安全。

id 规则：微信 `wx:<slug>`（`mp.weixin.qq.com/s/<slug>`），X 长文 `x:<tweet_id>`（发长文的推文 id）。

## 三个已知的实现细节

- **图片**：微信正文图片是懒加载，只有 `data-src` 没有 `src`，turndown 会整张丢掉（实测 28 图 → 0 图）。本 skill 自己抓 HTML 把 `data-src` 补成 `src`，再喂给 wechat2md —— 表格/代码块等转换规则仍全部由 wechat2md 负责。
- **贴图文章**：有一类文章微信只返回分享页模板，页面里没有 `#js_content`，正文和图片全在 `window.cgiDataNew` 里。整页转会把 2MB 内联 JS 当正文写进思源（页面直接卡死）。wechat2md 现在会解这个字面量（`content_noencode` + `picture_page_info_list[].cdn_url`），本 skill 在转换前后另加两道闸：体积 >400KB、或命中 `cgiDataNew`/`__canAsyncImport`/`使用完整服务` 一律丢弃。真拿不到正文的页面记 `failed`，不会写入空文档。
- **索引异步**：文档刚 `create` 出来时 SiYuan 的 blocks 索引还没落库，`attr set` 会反查失败，`publish` 会退避重试 6 次；仍失败的那条正文里还有 `sync-id:` 行，去重不会漏。

更多细节（X 长文识别、排错表、实测口径）见 [SKILL.md](SKILL.md)。

## 许可

MIT