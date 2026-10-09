# QQ 群通知实时摘要推送

通过 NapCat(OneBot v11) 只读接收指定 QQ 群的新消息，自动过滤闲聊、提取通知/待办/截止时间，
生成摘要后推送到微信（WxPusher）。

## 界面预览

手机待办台：完成度卡片、紧急/待办标签、截止时间、群来源，以及“查看完整原文”和“纠错”入口。

<p align="center">
  <img src="docs/screenshots/mobile-tasks.jpg" width="380" alt="手机待办台：完成度卡片、紧急与待办标签、群来源、查看完整原文与纠错入口" />
</p>

群聊降噪效果（26 秒）：假群里的一天 500 条消息，经本地规则过滤、重复合并后只剩 23 次通知，
并顺手建好 63 项待办。画面里的群、人、消息全部是程序生成的虚构示例：

<p align="center">
  <img src="docs/demo/demo.gif" width="720" alt="假群聊回放演示：500 条群消息 → 过滤 377 条 + 重复 54 条 → 68 条要点 → 23 次通知 → 63 项待办" />
</p>

这条片子由 `python tools/make_demo.py` 生成，**数字全部来自真实回放**，可用
`python main.py simulate --count 500` 自己复现；也提供 [mp4 版](docs/demo/demo.mp4)（0.5 MB）。

## 功能概览

- 只接收白名单 QQ 群，消息在本地完成筛选、摘要和待办提取。
- 普通讨论降噪，只有明确通知、待办或紧急事项才进入推送链路。
- 可将低优先级群标记为安静群，只接收明确通知，不立即推送讨论。
- 群里发的 PDF、Word、Excel、PPT、zip 和截图可自动解析并纳入摘要。
- 班级群和闲聊群转发的同一条通知支持跨群去重。
- 支持 WxPusher、Server酱、PushPlus、Webhook 等推送通道。
- 提供移动端待办台、截止提醒、候选确认、完成/忽略/稍后提醒和周复盘。
- 支持 NapCat/OneBot v11 实时接收和重启后的 24 小时历史补采。

## 组成

| 模块 | 作用 |
| --- | --- |
| `qq_digest.py` | 本地筛选/摘要引擎（规则 + 可选百炼 LLM） |
| `qq_live_digest/receiver.py` | OneBot v11 HTTP 接收器，监听 NapCat 上报 |
| `qq_live_digest/catchup.py` | 调用 NapCat API 补采历史消息，按 `msg_id` 去重 |
| `qq_live_digest/store.py` | SQLite + JSONL：消息去重、摘要归档、投递去重、模型用量与重启恢复 |
| `qq_live_digest/llmstats.py` | 模型用量词表与日 / 周 / 月聚合、token 成本折算 |
| `qq_live_digest/summarizer.py` | 分级筛选、待办/截止提取、推送文本生成 |
| `qq_live_digest/push.py` | WxPusher / Server酱 / PushPlus / Webhook / QQ 私聊，失败自动回退 |
| `qq_live_digest/service.py` | 10 分钟滚动窗口、紧急立即推、无重点不推、失败重试 |
| `qq_live_digest/bot.py` | 可选的 QQ 官方机器人，当前关闭 |
| 外部 `watchdog.ps1` | 可选的健康检查、自动重启和故障告警脚本，部署在 NapCat 目录 |
| `main.py` | CLI：run / catchup / tick / preview / send-test / doctor / stats / decisions / llm-stats / feedback / groups / simulate |

## 环境要求

- Windows 10/11；PowerShell 计划任务和隐藏启动脚本仅适用于 Windows。
- Python 3.12+。
- NapCat（或兼容 OneBot v11 的实现），用于接收 QQ 群消息。
- 至少一个推送通道；推荐 WxPusher。

## 安装与配置

```powershell
git clone https://github.com/wc985732-lang/qq-live-digest.git
cd qq-live-digest

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Copy-Item .env.example .env
# 编辑 .env，至少填写要监控的群、OneBot token 和一个推送通道
```

必填项通常是：

- `QQ_DIGEST_GROUPS`：要监控的群号，逗号分隔。
  留空则**不处理任何群**（不会默认接收全部群）。
- `QQ_DIGEST_ONEBOT_TOKEN`：与 NapCat OneBot HTTP 上报配置一致。
- `WXPUSHER_APP_TOKEN`、`WXPUSHER_UIDS`：推荐使用的微信推送通道。
- `DASHSCOPE_API_KEY`：可选；不填会使用本地规则摘要。

启动前先自检：

```powershell
.\.venv\Scripts\python.exe main.py doctor
.\.venv\Scripts\python.exe main.py run
```

需要登录后自动启动时，运行 `install-task.ps1` 注册 Windows 计划任务。

## 运行与自检

```powershell
cd <项目目录>

# 查看状态，等价于双击“检查状态.cmd”
.\检查状态.cmd

# 查看统计
.\.venv\Scripts\python.exe main.py stats

# 手动补采最近 24 小时（默认每个群最多 50 条）
.\.venv\Scripts\python.exe main.py catchup

# 预览待处理消息会推什么，不发送
.\.venv\Scripts\python.exe main.py preview

# 测试微信推送
.\.venv\Scripts\python.exe main.py send-test

# 全链路自检（配置 / 存储 / NapCat / 接收服务 / 待办台 / 访问层）
.\.venv\Scripts\python.exe main.py doctor

# 决策日志：最近 20 条消息为什么被推 / 没被推
.\.venv\Scripts\python.exe main.py decisions --limit 20

# 只查某一条消息的完整决策轨迹
.\.venv\Scripts\python.exe main.py decisions --msg-id <msg_id>

# 只查「延后未决」：本该推、但被夜间静默 / 额度 / 大模型失败推迟的
.\.venv\Scripts\python.exe main.py decisions --outcome deferred

# 回看最近几条摘要：每条都给出置信度与「为什么推」
.\.venv\Scripts\python.exe main.py show --limit 5

# 模型用量与成本：日视图（默认）、周 / 月视图、最近明细
.\.venv\Scripts\python.exe main.py llm-stats
.\.venv\Scripts\python.exe main.py llm-stats --period week
.\.venv\Scripts\python.exe main.py llm-stats --period month --recent 20

# 人工反馈回收：候选确认率 / 忽略率 / 纠错类型 / 按群规则建议
.\.venv\Scripts\python.exe main.py feedback --days 30

# 群级策略：每个群最终生效的安静群 / 关键词 / 最低分 / 模型档 / 免打扰
.\.venv\Scripts\python.exe main.py groups

# 假群聊回放：500 条消息走完整链路，一条真实推送都不发（不联网、不碰 data/）
.\.venv\Scripts\python.exe main.py simulate --count 500

# 脱敏评测集：召回 / 误报 / 待办 / 截止时间 / 去重 / 延迟 / 成本基线（同样离线）
.\.venv\Scripts\python.exe main.py benchmark
```

### 模型用量与成本 `main.py llm-stats`

每次调用模型都会往 SQLite 的 `llm_calls` 表落**一行**：provider、模型名、输入/输出 token、
耗时、失败原因，以及**是否重试过、是否降级回本地规则**。视图按日 / 周 / 月聚合：

```powershell
.\.venv\Scripts\python.exe main.py llm-stats                  # 最近 14 天，按天
.\.venv\Scripts\python.exe main.py llm-stats --period week    # 最近 8 周
.\.venv\Scripts\python.exe main.py llm-stats --period month   # 最近 6 个月
.\.venv\Scripts\python.exe main.py llm-stats --recent 20      # 附最近 20 条调用明细
.\.venv\Scripts\python.exe main.py llm-stats --json           # 交给脚本消费
```

每期给出调用次数、成功 / 失败 / 跳过、重试与降级次数、输入输出 token 与费用；末尾还有合计、
用途分布（候选精炼 / 图片识别 / 文档理解）、模型分布和失败原因 TOP。

| 列 | 含义 |
| --- | --- |
| 调用 / 成功 / 失败 | 一次**逻辑调用**算一次：同一批里重试多次仍然只算一行 |
| 跳过 | 本该调用但没调（例如没配 `DASHSCOPE_API_KEY`）——让「为什么一条都没有」有答案 |
| 重试 | 这一行发生过重试（`attempts > 1`） |
| 降级 | 最终失败并回退本地规则（不会阻塞推送） |
| 费用 | 按 `QQ_DIGEST_LLM_PRICE_IN` / `QQ_DIGEST_LLM_PRICE_OUT`（**元 / 百万 token**）折算 |

费用只在展示时折算、不入库，所以改价目表可以重算历史；两个单价都留空就只统计 token。
`doctor` 新增「模型用量」一行，给出最近 24 小时的调用次数、token 与费用，有失败会提示看明细。
`llm_calls` 与 `decisions` 一样，随数据保留天数在 `prune` 时一起清理。

### 决策日志 `main.py decisions`

每条消息在「入口 → 筛选 → 去重 → 投递」这条链上只留**一条最终结论**，按 `msg_id` 就能读成一段轨迹：

| 结论 | 含义 |
| --- | --- |
| `pushed` | 进了摘要，并且确实推到至少一个通道 |
| `held` | 进了摘要，但这次没投出去（暂无可用通道 / 全部失败 / 稍后重试） |
| `filtered` | 被本地规则挡下，`reason` 会写清是阈值、闲聊还是安静群 |
| `deduped` | 与最近几小时已推内容重复，或与本批另一条要点相同（`dedupe_reason` 给出对照文本） |
| `truncated` | 命中但超出 `QQ_DIGEST_MAX_ITEMS` 上限 |
| `deferred` | 本该推送，但被夜间静默 / 当日额度 / 大模型失败推迟；**过程态**，下个窗口会再试 |
| `duplicate` / `rejected` | 入口就挡下了：`msg_id` 重复，或群不在白名单 |

`reason` 里带着**当时的分值和阈值**（例如「分值 2 < 阈值 3」），所以改了配置之后旧记录依然解释得通。

`deferred` 与其他结论的区别在于它是**过程态**：消息还没落定，所以这条会按 `msg_id` 就地更新（夜里
tick 几百次也只有一行，`reason` 保留最新一次的原因）。等它真的推出去或最终被挡下，这条过程行会被
最终结论覆盖——不会出现「同时又延后又已推送」的自相矛盾。而**还没到合并窗口**的消息仍是
「尚未决定」，一行都不留，避免每分钟刷噪声。

`main.py decisions` 的抬头把两者分开显示：先是「已决分布」，再另起一行「延后未决 N 条」；
`doctor` 的本地存储一行也会给出 `decisions_deferred`，夜里一眼能看出积压了多少条待推。

### 置信度与「为什么」`main.py show`

每条进摘要的候选都自带一个 0–1 的**置信度**和一组**触发规则**，回答「凭什么判它值得推 / 值得办」：

| 等级 | 分数 | 含义 |
| --- | --- | --- |
| 把握较高 | ≥ 0.75 | 证据充分，直接进摘要 / 待办 |
| 把握中等 | 0.55 – 0.75 | 可用，保留依据方便回查 |
| 把握较低 | < 阈值（默认 0.55） | 只进「待确认」，等你点头才转正式待办；阈值 `QQ_DIGEST_CANDIDATE_MIN_CONFIDENCE` 是**硬门槛**，低于它的候选不会绕过确认直接进正式待办 |

触发规则是**人话 + 权重**的形式，例如 `+0.18 分值 10，高出阈值 3 两分以上`、
`-0.24 原话有“记得”等不确定措辞`。权重写在解释里，所以改规则就会改解释，两者不会各说各话。
摘要候选（`assess_notice`）与待办分类（`assess_task`）是两套尺度，分别回答「值不值得看」和
「能不能直接执行」；不进候选的条目不会被硬凑一个分数。

同一套解释出现在三个地方：

- 推送正文多一行「为什么：…（把握较高 82%）」；
- 待办台的候选卡片给出把握度与「依据：…」（直接读入库的触发规则）；
- `python main.py show` 回看最近几条摘要时逐条打印「为什么：…」。

A7 之前归档的老摘要没有这条记录，`show` 会如实写「这条没有置信度记录」，不假装算过 0 分。
实现是纯函数（`qq_live_digest/confidence.py`，不碰数据库、不联网），所以推送、待办台和回归测试
看到的是同一套结果。

### 人工确认与反馈回收 `main.py feedback`

低置信度的行动项不会直接混进正式待办：它们进「待确认」，由你在待办台点头才转正（Roadmap `A8`）。
这条链路有两半：

- **进待确认**：待办分类用 A7 的置信度打分，低于 `QQ_DIGEST_CANDIDATE_MIN_CONFIDENCE`
  （默认 0.55）的候选被硬挡住，不会绕过确认直接进正式待办；判定原因里写明分数与阈值，
  待办台卡片与 `doctor` 的「候选置信度」一行都能看到。
- **反馈回收**：确认 / 忽略 / 纠错都会记成事件；`main.py feedback` 汇总最近 N 天的确认率、
  忽略率、纠错类型与按群分布，并把纠错样本转成**可读的规则建议**（例如「某群：误判紧急 3 次，
  建议收紧该群规则」）。

```powershell
.\.venv\Scripts\python.exe main.py feedback            # 最近 30 天
.\.venv\Scripts\python.exe main.py feedback --days 7   # 只看最近一周
.\.venv\Scripts\python.exe main.py feedback --json     # 交给脚本消费
```

它**只汇总与建议，不自动改配置**——「怎么改规则」始终由人决定。
`doctor` 的「反馈闭环」一行给出近 30 天的候选 / 确认 / 忽略 / 纠错概况。

### 群级个性化策略 `main.py groups`

一个群一个脾气：有的群只该收通知、有的群要多盯几个关键词、有的群夜里干脆别打扰。
`A9` 让每个群在全局配置之上覆盖少量开关，**没写的字段一律继承全局**——只改一个群不会牵连别的群。

| 字段 | 作用 | 默认（继承自） |
| --- | --- | --- |
| `quiet` | 安静群：只留明确通知，普通讨论不入摘要 / 待办 | `QQ_DIGEST_QUIET_GROUPS` |
| `keywords` | 本群额外关键词，命中即视为明确通知（安静 / 免打扰也会放行） | 全局词表 |
| `min_score` | 本群进摘要的最低分 | `QQ_DIGEST_MIN_SCORE` |
| `model` | 本群走哪一档模型：`rule` / `light` / `strong` / `default`，对接 `A6` 分级路由 | 路由判据 |
| `quiet_hours` | 本群免打扰时段，按**消息时间**算，支持跨零点（如 `23:00-06:30`） | 不继承，只在本群写时生效 |

配置写在一条 JSON 环境变量 `QQ_DIGEST_GROUP_POLICIES` 里，键写群号或群名都行：

```ini
QQ_DIGEST_GROUP_POLICIES={"123456": {"quiet": true, "min_score": 5, "keywords": ["考试", "选课"]}, "学院通知群": {"model": "light", "quiet_hours": "23:00-06:30"}}
```

```powershell
.\.venv\Scripts\python.exe main.py groups           # 逐群打印最终生效的开关，以及本群覆盖了哪些字段
.\.venv\Scripts\python.exe main.py groups --json    # 交给脚本消费
```

坏 JSON / 认不出的字段 / 非法时段只会丢掉自己，不会让程序报错。原有的 `QQ_DIGEST_QUIET_GROUPS`
仍然生效，只有被群策略显式写成 `"quiet": false` 时才让位；`min_score` 被覆盖后，决策原因会改写成
「分值 X < 本群阈值 Y」。模型档只对**单个群的批次**生效：混群批次、或写了 `light` 但没配
`QQ_DIGEST_LLM_MODEL_LIGHT` 时，都回落到默认路由判据。推送渠道的每群覆盖尚未纳入本项。

`doctor` 的「群白名单」一行会提示有几个群配了策略。

### 假群聊回放 `main.py simulate`

想验证「500 条群消息最后剩下几条通知」，不用真去加群、也不用量自己的数据：

```powershell
.\.venv\Scripts\python.exe main.py simulate --count 500              # 500 条 / 16 小时 / 默认配置
.\.venv\Scripts\python.exe main.py simulate --quiet-hours 23:00-07:00 # 看夜间静默把消息推成「延后未决」
.\.venv\Scripts\python.exe main.py simulate --budget 0                # 不限额度，看自然聚合的结果
.\.venv\Scripts\python.exe main.py simulate --out events.jsonl --write-only   # 只导出 fixture
```

它生成的是一整天的高校群消息流——闲聊、通知、作业、考试安排、活动报名、广告、图片、文件、
跨群重复转发，字段与 OneBot 上报完全一致，所以走的是**和生产完全相同的代码路径**：
入库 → 判定 → 去重 → 摘要 → 投递 → 决策日志。

三件事是刻意的：

- **不联网**：通道是内存里的假通道，大模型关闭，自动回退本地规则；配置里没有任何凭证。
- **不碰 `data/`**：默认在一个临时目录里跑，跑完可以直接删。
- **确定性**：`--seed` 相同必然得到同一份数据，`msg_id` 形如 `sim-20261008-00042`，
  可以拿 `main.py decisions --msg-id` 逐条回查为什么它被推 / 被挡。

### 评测集基线 `main.py benchmark`

想知道"改了规则或模型之后是变好还是变坏"，跑一遍脱敏评测集就行：

```powershell
.\.venv\Scripts\python.exe main.py benchmark                    # 500 条 / seed=20261008
.\.venv\Scripts\python.exe main.py benchmark --json             # 全部指标 + 每类消息明细
.\.venv\Scripts\python.exe main.py benchmark --fail-under 0.9   # 召回低于 90% 退出码 1
```

评测集直接复用 `simulate` 的假群聊，每条消息自带**意图标注**（该不该推 / 该不该建待办 /
有没有截止时间），跑完真实链路后输出召回、误报、待办判定、截止时间、跨群去重、延迟、成本
与置信度分布。当前基线：**召回 100%、误报 0.5%、待办与截止时间 100%、去重 100%、
延迟中位 0.0 / P95 31.6 分钟**。同样不联网、不碰 `data/`、同 seed 必得同数字，
`tests/test_benchmark.py` 把它锁进了 CI。详见 [评测集与基线数字](docs/BENCHMARK.md)。

### 全链路自检 `main.py doctor`

`doctor` 会逐项检查 Python 版本、配置文件、群白名单、推送通道、OneBot 接收器、大模型、
模型用量、候选置信度、本地存储、NapCat、接收服务、待办台和访问层，每项给出 `OK / WARN / FAIL`
和一句可执行的建议：

```text
[OK  ] Python 版本   3.12.4
[OK  ] 群白名单        6 个群：95***96、10***22、55***18 等 6 个（已脱敏）
[FAIL] 推送通道        未配置任何可用通道
                   → 至少配置 WxPusher / Server酱 / PushPlus / Webhook 之一
[WARN] NapCat        get_status 请求失败: refused
                   → 确认 NapCat 与 QQ 已启动并登录（实时接收与历史补采都依赖它）
```

- 退出码：有任何 `FAIL` 返回 1，否则 0，方便写进脚本或计划任务。
- `--json`：输出同上内容的 JSON，便于自动化处理。
- `--online`：额外在线校验 QQ 官方机器人凭证（默认不联网校验）。
- 自检**只读**：不会发送消息、不改配置；输出已脱敏，不含 token、`.env` 全文和真实群号，可直接贴到 Issue 里。

## 模型精炼（推荐开启）

在 `.env` 填入阿里云百炼的 `DASHSCOPE_API_KEY`，保持 `QQ_DIGEST_LLM=1`。推送会调用
`QQ_DIGEST_LLM_MODEL` 指定的模型把通知改写成短摘要，默认不再附带原文：

- `QQ_DIGEST_LLM_MODEL`：当前部署使用 `qwen3.8-max`，优先准确率；如果更在意成本，
  可改回 `qwen-plus`，改完重启 `QQ-Live-Digest` 任务即可。
- `summary` 不超过 40 个汉字，只保留对象、事项、时间或行动。
- `action` 不超过 20 个汉字，没有明确行动就留空。
- `QQ_DIGEST_INCLUDE_RAW=0`：推送只显示精简摘要、截止时间、行动项和来源。
- 模型 API 失败时自动回退本地摘要，不会影响正常推送。

模型层是**可替换**的（Roadmap `A4`）：业务代码只依赖 `qq_live_digest/providers.py` 里的
`LLMProvider` 接口，不自己拼 HTTP 请求。任何 OpenAI 兼容端点（Ollama / vLLM / OpenAI / 其他厂商）
只要保持 `QQ_DIGEST_LLM_PROVIDER=openai-compat` 并改 `QQ_DIGEST_LLM_ENDPOINT` 就能接上；
非兼容协议如何接入见 `docs/PROVIDERS.md`。设成 `QQ_DIGEST_LLM_PROVIDER=none` 可彻底关闭模型调用。

想省成本可以开启**分级路由**（Roadmap `A6`）：填上 `QQ_DIGEST_LLM_MODEL_LIGHT`（例如
`qwen-turbo`）后，清晰的小批次走轻量模型，只有难例（候选偏多、分值贴着阈值、A7 判为低置信度）
才升级到 `QQ_DIGEST_LLM_MODEL` 指定的高能力模型；每次走了哪一档、为什么，都记进
`llm_calls` 并由 `main.py llm-stats` 的「路由分布」展示。留空 = 不启用，行为与之前一致；
详见 `docs/PROVIDERS.md`。

## 推送卡片与截止提醒

WxPusher 走 HTML 卡片（`QQ_DIGEST_PUSH_HTML=1`），通知栏和卡片标题用一句话摘要：

- 待办和紧急事项单独成色块：橙色待办、红色紧急，块里给截止时间、要做什么、来源和
  一句原文依据；普通通知压成一行“其余 N 条”，不再逐条铺开。
- 每条判断都能回溯：卡片里的“原文”来自通知原句，也可以在电脑上执行
  `python main.py show --limit 5` 查看最近几条摘要的判断依据。
- 截止提醒默认每天两次：早上 07:30 汇总今天到期的事，晚上 21:00 提前看明天到期的事；
  只提醒能识别出截止时间的通知。时间可改 `QQ_DIGEST_DEADLINE_MORNING`、
  `QQ_DIGEST_DEADLINE_EVENING`，总开关 `QQ_DIGEST_DEADLINE_REMINDERS`。

## 可靠性、降噪与自我纠错

AI 和推送都可能瞬时失败，这一层专门保证“不会静默丢事”：

- 大模型调用先原地重试（`QQ_DIGEST_LLM_MAX_RETRIES`，指数退避）：限流、5xx、超时和
  返回不是 JSON 都会重试；鉴权/参数错误直接降级，不会反复撞墙。
- 重试仍失败时，该批消息不会被标记为已处理，会推迟到下一个轮询周期再试；
  推迟上限由 `QQ_DIGEST_LLM_DEFER_MAX_ATTEMPTS`、`QQ_DIGEST_LLM_DEFER_WINDOW_MINUTES`
  控制，超过上限才用本地规则推送，并在日志和 `/health` 里记一次回退。
- 推送统一限流：`QQ_DIGEST_PUSH_DAILY_BUDGET` 限制每天的推送条数，
  `QQ_DIGEST_QUIET_HOURS`（默认 `23:00-07:00`）期间不打扰；紧急事项始终放行。
  被静默或超额的消息会留在队列，静默结束或第二天一起汇总，不会丢。
- 截止提醒只有真的投递成功才记“今天已提醒”；没有可用通道或处于静默时段时会
  保留状态，下一个轮询周期继续尝试。
- 待办台的“纠错”会记录原判断，并按群汇总成规则建议，出现在每周复盘和待办台
  设置页，用来发现“哪个群老是被误判成紧急/待办”。目前只给建议，不自动改配置。
- “标记重复”会把当前任务关联到原任务，并把两个来源群合并展示。
- `/health` 除了任务统计，还返回模型失败/推迟/回退次数、当日已推送条数、投递队列
  状态和最近一次失败原因；看门狗同时探活 8765 和 8766。

```ini
QQ_DIGEST_LLM_MAX_RETRIES=2
QQ_DIGEST_LLM_RETRY_BACKOFF=1.5
QQ_DIGEST_LLM_DEFER_MAX_ATTEMPTS=3
QQ_DIGEST_LLM_DEFER_WINDOW_MINUTES=15
QQ_DIGEST_PUSH_DAILY_BUDGET=12
QQ_DIGEST_QUIET_HOURS=23:00-07:00
```

## 手机待办台

服务启动时会同时开一个待办台（默认 `0.0.0.0:8766`），把摘要里的行动项单独存进 `tasks` 表：

- 首页先显示“待确认”，再按“今天 / 本周 / 以后 / 已完成”分组；今天和必做的排最上面。
- 模糊行动项不会直接混进正式待办：可以在待确认卡片里“确认待办 / 忽略 / 明天提醒”。
- “明天提醒”默认推迟到次日早上 07:30（跟随 `QQ_DIGEST_DEADLINE_MORNING`），任务卡上会
  显示“稍后 MM-DD HH:MM”小标签；这是真正的提醒时间，不再依赖任务是否逾期。
- 截止提醒只读取 `tasks` 表：完成和忽略的任务不再提醒；没有截止时间的任务不进入截止提醒。
- 同一任务同一天最多提醒一次；候选确认卡默认同一任务 24 小时最多一次，每天最多 4 张。
- 每周日 20:30 生成一次简短复盘：候选数、确认率、忽略率、完成率、提醒数和逾期未完成数。
- 访问地址和 token：`python main.py tasks --import-existing`，命令会打印可访问的 URL。
- 回填历史摘要：`python main.py tasks --reset --import-existing --days 7`。
- 关闭待办台：`QQ_DIGEST_WEB=0`；改端口 `QQ_DIGEST_WEB_PORT`。

事务闭环相关配置：

```ini
QQ_DIGEST_CANDIDATE_PUSH=1
QQ_DIGEST_CANDIDATE_MIN_CONFIDENCE=0.55
QQ_DIGEST_CANDIDATE_REMINDER_HOURS=24
QQ_DIGEST_CANDIDATE_MAX_PER_DAY=4
QQ_DIGEST_WEEKLY_REVIEW=1
QQ_DIGEST_WEEKLY_REVIEW_WEEKDAY=6
QQ_DIGEST_WEEKLY_REVIEW_TIME=20:30
```

手机不在同一网络时，需要一个公网入口（Tailscale、内网穿透或云服务器）。
推荐直接用 Tailscale：电脑和手机登录同一个 tailnet，然后在电脑上运行
`tailscale serve --bg 8766`，把本机待办台暴露为带 HTTPS 的 tailnet 地址。
把该地址写入 `QQ_DIGEST_WEB_BASE_URL` 后，推送里的确认链接会稳定指向 Tailscale，
不会再去猜测局域网 IP。页面支持 manifest 和 service worker，可以从手机浏览器
添加到桌面，作为轻量 PWA 使用。

## 群文件与图片（AI 读取）

群里有人发文件或截图，服务会自动下载、解析，和普通消息一起汇总，不额外刷屏：

- 文件：PDF、Word(.docx)、Excel(.xlsx)、PPT(.pptx)、txt/md/csv、zip（只解白名单文档，
  限 20 个文件、解压总量 5MB）。
- 文档正文会保留到单独字段，待办里的“查看完整原文”不再只看到摘要；
  提取出的正文交给 `qwen3.8-max` 做结构化提取，保留适用对象、条件、例外和截止时间。
- 图片：用视觉模型（`QQ_DIGEST_VL_MODEL`，默认 `qwen3-vl-plus`）读出截图里的文字；
  表情包和小图自动跳过。
- 扫描版 PDF 没有文本层时，前几页会转成图片交给 `qwen3-vl-plus` OCR。
- 单文件默认上限 10MB（`QQ_DIGEST_ATTACHMENT_MAX_MB`），每小时最多处理 30 个
  （`QQ_DIGEST_ATTACHMENT_MAX_PER_HOUR`）。
- 长文档默认保留前 30000 字用于提取和原文查看（`QQ_DIGEST_DOCUMENT_MAX_CHARS`）；
  扫描 PDF 默认最多 OCR 20 页（`QQ_DIGEST_PDF_OCR_MAX_PAGES`）。
- 加密文件或 OCR 仍读不出文字时，只记录文件名和大小。
- 文档缓存在 `data\files\`，默认保留 7 天后自动清理（`QQ_DIGEST_ATTACHMENT_RETENTION_DAYS`）；图片读完 OCR 即删除，不落盘。
- 只解析不执行。总开关 `QQ_DIGEST_ATTACHMENTS=0`，只关图片用 `QQ_DIGEST_VISION=0`。
- 手动解析单个文件：`python main.py attach-test <本地文件路径>`，或
  `python main.py attach-test --group <群号> --file-id <id> --busid <busid> --name <文件名>`。

## 重复通知去重

同一条通知被班级群和闲聊群各发一遍时，不会推两次：

- 同一批消息里内容相近的通知自动合并成一条，来源会列出多个群名并标注“跨群重复”。
- 默认 6 小时内推过的同内容通知不再重复推送（`QQ_DIGEST_DEDUPE_HOURS`，0=关闭）。

服务由 Windows 计划任务管理，登录后自动启动，窗口隐藏：

| 计划任务 | 作用 |
| --- | --- |
| `NapCat-QQ` | 隐藏启动 NapCat/QQ |
| `QQ-Live-Digest` | 隐藏启动摘要服务 |
| `NapCat-QQ-Watchdog` | 每 5 分钟检查一次并自动恢复 |

日志：

- 摘要服务：`<项目目录>\logs\qq-live-digest.log`
- 看门狗：`<NapCat目录>\watchdog.log`
- NapCat：`<NapCat目录>\shell\logs\`

## 历史补采

服务启动时会立即补采一次，之后每 30 分钟补采一次，默认回溯 24 小时、每个群最多 50 条。
补采只调用 `get_group_msg_history`，按 `msg_id` 去重入库，不会发送任何 QQ 消息。

如果启动时 NapCat 还没准备好，补采会失败并在约 5 分钟后自动重试；不会一直空等到下一个
30 分钟周期。相关配置：

```ini
QQ_DIGEST_CATCHUP_ENABLED=1
QQ_DIGEST_CATCHUP_HOURS=24
QQ_DIGEST_CATCHUP_COUNT=50
QQ_DIGEST_CATCHUP_INTERVAL_MINUTES=30
QQ_DIGEST_NAPCAT_API_URL=http://127.0.0.1:3000
QQ_DIGEST_NAPCAT_API_TOKEN=        # 留空时自动复用 OneBot token
```

电脑关机期间消息会断流。第二天开机启动后，补采会把最近 24 小时内错过的消息补回来；
如果关机超过 24 小时，超出窗口的历史消息不会被补采。

## 看门狗与告警

`watchdog.ps1` 每 5 分钟执行一次：

1. 检查 3000 端口、NapCat `get_status` 的 `online/good`、`get_login_info.user_id`。
2. 检查 8765 端口和 `/health` 返回的 `service=qq-live-digest`。
3. 发现异常先自动重启对应组件，重启后仍不健康就通过 WxPusher 发微信告警。
4. 同一个告警 1 小时内只发一次，状态保存在 `watchdog-alert-state.txt`。
5. 告警发送强制使用 TLS 1.2，避免 Windows PowerShell 默认 TLS 版本导致发送失败。

手动运行看门狗：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <NapCat目录>\watchdog.ps1
```

## 可选：QQ 官方机器人

默认 `QQ_DIGEST_OFFICIAL_BOT_ENABLED=0`。如需改回官方机器人，
在 `.env` 填 `QQ_BOT_APPID`、`QQ_BOT_SECRET`，并把开关设为 `1`。
官方机器人需要平台开放全量群消息权限；当前账号没有该权限，所以实际使用 NapCat。

## 隐私与安全

- 不要提交 `.env`、`.env.bak-*` 或其他包含 Token、UID、API Key 的文件；仓库只保留 `.env.example`。
- `data/`、`logs/`、SQLite 数据库和附件缓存会包含真实群消息、群号、文件名和推送地址，已被 `.gitignore` 排除。
- README、Issue、日志和截图在提交前请打码 QQ 号、群号、Tailscale 域名、本机路径和消息内容。
- NapCat 属于第三方 QQ 协议客户端，使用前请确认平台规则和账号风险；本项目不附带 QQ 账号、服务器或推送凭证。
- 如果不小心提交了密钥，应立即作废并重新生成，而不是只删除文件；Git 历史仍可能保留旧内容。

## 已知限制

- NapCat 属于第三方 QQ 协议客户端，登录主号存在账号风控风险。本项目只读群消息，
  代码不会调用 QQ 发送接口；是否更换为小号由用户自行取舍。
- 微信 WxPusher 通道可能受官方配额或风控影响（以官方文档为准）；实际使用中未见
  明确的日发送条数限制。发送失败时服务会重试，但不会无限刷屏。
- 电脑必须开机且 NapCat 保持登录，才能实时接收。关机期间依赖 24 小时补采窗口。
- 摘要由本地规则生成，可选大模型只做精炼；模型失败会自动回退本地规则。
- SQLite 默认保存最近 30 天消息（`QQ_DIGEST_RETENTION_DAYS`），`msg_id` 唯一约束保证重启不重复推送。

## 参与与文档

- [发展规划 Roadmap](docs/ROADMAP.md)：项目唯一路线图入口，含评分模型与分阶段排期。
- [常见问题 FAQ](docs/FAQ.md)：定位、模型、风控、部署等统一口径。
- [故障演练手册](docs/DR-DRILL.md)：NapCat 掉线、模型故障、推送失败、进程被杀等场景怎么验证「不丢事」。
- [评测集与基线数字](docs/BENCHMARK.md)：脱敏评测集的指标定义、第一版基线与 CI 门槛。
- [安全边界与数据流向](docs/SECURITY-BOUNDARY.md)：四条数据路径、配置加固清单、依赖供应链核查与第三方审计核实结果。
- [贡献指南](CONTRIBUTING.md)：分支、测试、代码风格、隐私与安全要求。
- [安全政策](SECURITY.md)：如何私密报告安全问题。
- [更新日志](CHANGELOG.md)：各版本变更记录。

## License

MIT License. See `LICENSE`.
