# 更新日志（Changelog）

本文件记录每个版本的新增、修复、破坏性变更与迁移说明。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [Unreleased]

## [0.4.1] - 2026-10-09

### 安全
- 修复 `EmailPusher` 走 587 + STARTTLS 时未传 TLS 上下文的问题：`smtplib` 的默认上下文是
  `CERT_NONE`（既不校验证书也不校验主机名），SMTP 账号口令与摘要正文存在被中间人窃听的风险；
  现在显式传 `ssl.create_default_context()`，`SMTP_SSL` 与 `starttls()` 两条路径都覆盖。
- 修复 **dismissed / expired 待办泄露**：`conflicts`、ICS 导出、REST `/api/deadlines`、`/api/todos`
  过去只排除 `done`，被忽略 / 过期归档的待办仍会出现在冲突提醒与日历里；新增
  `qq_live_digest/taskstatus.py` 统一「完成 / 归档 / 仍开放」口径，日历与冲突一律跳过归档态，
  `/api/todos` 与 `/api/deadlines` 的开放态口径也随之对齐（不再一个含 `candidate`、一个不含）。
- 修复**待办台鉴权反转 + 缺 CSRF 校验**：过去 `web_host` 是回环且未配 token 时**不生成 token**，
  等于本机部署谁都能改待办；现在无论监听回环还是对外都会生成 token 并落库。`do_POST` 现在拒绝
  跨站写请求（`Origin` 与 `Host` 不一致直接 403），页面拿到 token 后立即 `history.replaceState`
  把它从地址栏 / 历史 / Referer 里抹掉，不再常驻 URL。
- 修复 OneBot 接收器**未设 token 仍监听非回环地址**的口子：这种配置等于局域网内任何人都能伪造
  群消息，现在 `OneBotReceiver.start()` 直接拒绝启动并打印原因（只监听 `127.0.0.1` / `localhost` /
  `::1` 才允许空 token）。
- 修复 Web / OneBot 的 token 比较为**恒定时间**（`hmac.compare_digest`），避免逐字符比较的计时侧信道。
- 修复 `store.search_messages` 的 **LIKE 通配符注入**：查询串里的 `%` / `_` / `\` 未转义，用户传
  `%` 就等于扫全库；现在转义并显式 `ESCAPE '\'`。
- 修复推送 token 泄露：Telegram 出错信息、Ntfy 明文 HTTP 场景下的 `NTFY_TOKEN` 现在会被遮蔽 /
  丢弃，避免写进日志或发送到非 https 端点。

### 修复
- 修复 `mcp.call_tool` 遇到未知 Tool 名称直接抛 `KeyError` 的问题，改为返回 `isError` 结果。
- 修复 `webapp.do_POST` 对非法 `Content-Length` 直接 `int()` 抛异常的问题。
- 修复 `ics.fold_line` 续行长度：RFC 5545 的 75 字节上限**包含**续行前导空格，过去续行按 75 字节切
  会超 1 字节，现在续行只放 74 字节。
- 收紧事件合并判据：两侧都没有日期 / 来源 / 截止这类硬锚点时，要求**更多**对象词重叠才判定为同一
  事件，减少「换个说法的闲聊」被误合并。
- 收敛重复实现：新增 `qq_live_digest/redact.py`（`observe.mask_id` 与 `doctor._mask_id` 共用）、
  `qq_live_digest/taskstatus.py`（状态口径）、`restapi.clamp_int`（`mcp._int_arg` / `webapp._query_int`
  共用）；REST 的 `importance` / `id` 解析改为防御式，坏参数不再 500。
- 让 `doctor` 的口径与本次政策对齐：「待办台」对外监听但未显式配 token 由 FAIL 降为 WARN
  （启动时总会自动生成 token，运行期并非无鉴权）；「OneBot 接收器」对外监听且无 token
  由 WARN 升为 FAIL（按新政策接收器会直接拒绝启动，等于收不到任何群消息）。

### 破坏性 / 迁移
- **待办台现在总是需要 token**：过去 `QQ_DIGEST_WEB_HOST` 是回环地址且未设 `QQ_DIGEST_WEB_TOKEN`
  时页面不鉴权，v0.4.1 起无论监听哪里都会生成 token 并存入 SQLite `meta` 表；用
  `python main.py tasks` 打印带 token 的访问地址。
- **OneBot 未设 token 时不再监听非回环地址**：`QQ_DIGEST_ONEBOT_HOST` 不是 `127.0.0.1` /
  `localhost` / `::1` 且 `QQ_DIGEST_ONEBOT_TOKEN` 为空时，接收器会拒绝启动；请补 token，或改回
  只监听回环。
- **`doctor` 的状态级别有调整**（如上「修复」所述）：待办台对外 + 未显式配 token 不再算 FAIL，
  OneBot 对外 + 无 token 改为 FAIL；若有脚本依赖 `doctor` 的退出码，请相应调整。

## [0.4.0] - 2026-10-09

Phase 2「信息中枢」全部交付：事件级跨群聚合（`A11`）、只读 REST API（`A15`）、MCP 接口（`A1` 只读 +
`A2` 确认后可写待办）、ICS / 日历导出（`A13`）、时间冲突检测（`A12`）、多端推送扩展（`A14`）、
PWA 离线（`A16`）与 Prompt / 模型 A-B 对比（`A22`），外加一处时间窗脆弱测试的修复。

### 新增
- 新增**只读 REST API**（Roadmap `A15`）：待办台服务同端口对外提供自描述的只读接口
  （`/api`、`/api/notifications`、`/api/tasks`、`/api/deadlines`、`/api/panel`、`/health`），
  `X-Token` 头或 `?token=` 查询参数鉴权，`/health` 免 token。新增 `qq_live_digest/restapi.py`
  （纯读、只发 SELECT），网页与 CLI 共用同一份字段口径；`/api/notices` 保留为 `/api/notifications`
  的历史别名；新增 `main.py api`（别名 `rest`）打印接口目录与访问地址；字段与边界写进 `docs/API.md`。
  **只读**：不写库、不联网、不推送，也不开放 QQ 发送；随后的 `A1`（MCP 只读）直接复用这份口径。
- 新增**只读 MCP 接口**（Roadmap `A1`）：把上面的只读口径包成 MCP Tool，`main.py mcp`（别名
  `mcp-serve`）以标准 **stdio** 传输对外服务（一行一条 JSON-RPC 2.0，**stdout 只放协议消息**），
  供 Cursor / Claude 等 MCP 客户端安全查询。**只暴露 4 个只读 Tool**——`recent_notifications` /
  `todos` / `deadlines` / `search_messages`（关键词搜历史消息）；`main.py mcp --list-tools` 可离线打印
  Tool 定义。新增 `qq_live_digest/mcp.py`（协议层纯函数，可脱离进程单测）；`search_messages` 的本地库
  查询落在 `store.search_messages`（`A2` 在这个只读底座上加了受确认约束的待办写入，见下条）。
- 新增**可写 MCP 待办**（Roadmap `A2`）：在 A1 的只读 MCP 上再加 `set_task_done`（完成 / 重开）与
  `add_task`（新建待办）两个写 Tool，以及只读的 `recent_audit`（看审计）。写操作**只碰 `tasks` 表**，
  且**必须显式 `confirm=true`**，否则拒绝；每次调用（含被拒 / 找不到 / 参数错）都写 `audit_log`
  （新增 `store.record_audit` / `recent_audit` 与 `audit_log` 表）。**没有任何 QQ 发送 / 删群 / 改设置
  的入口**；`add_task` 用 `summary|deadline` 的 SHA-1 生成稳定 `task_key`，并把 `source` 标成 `mcp`。
  MCP 工具目录现为 **5 只读 + 2 可写**，只读 Tool 带 `readOnlyHint` 标注。
- 新增**ICS / 日历导出**（Roadmap `A13`）：把「有明确时间的待办」导成 iCalendar（RFC 5545），
  供手机 / 桌面日历下载或订阅。新增 `qq_live_digest/ics.py`（纯字符串处理，无 I/O）；`main.py ics`
  （别名 `calendar`）写文件或 `--print` 到标准输出，待办台新增 `GET /api/calendar.ics`（token 鉴权，
  `text/calendar`）。只有日期没有时刻的截止按**全天事件**（`VALUE=DATE`）导出，没有截止时间的待办
  不导出；时间以浮动本地时间写出，不引入时区依赖。**只读**：只读 `tasks` 表，不写库、不联网、不改任务。
- 新增**时间冲突检测**（Roadmap `A12`）：新增 `qq_live_digest/conflicts.py`（纯函数）；`main.py conflicts`
  （别名 `conflict`，`--window` 分钟）与待办台 `GET /api/conflicts` 共用同一份报告。口径：把**截止时间**
  落在同一时间窗（默认 30 分钟）内的多个待办当作潜在冲突，按组返回、按时间升序，只提醒——
  **不擅自改任务**（不改时间、不合并不删除）；已完成与没有截止时间的待办默认不参与。
- 新增**多端推送扩展**（Roadmap `A14`）：在既有通道之外补上 **ntfy / Telegram / Discord /
  企业微信（群机器人）/ 邮件（SMTP）**，与已有通道共用同一套「主通道失败 → 回退其它通道」派发与
  `deliveries` 去重；配了就自动装配，不配则与以前完全一致。`Settings.push_channels()` 与 `doctor`
  的口径同步覆盖这些通道，`.env.example` 补齐对应变量。
- 新增**PWA 离线能力**（Roadmap `A16`）：待办台补齐 manifest（可安装）、service worker 缓存
  **应用壳 + 只读数据**——断网时页面仍能打开并回放最近一次的通知 / 待办（**离线只读**），
  写操作（完成 / 忽略 / 纠错）在离线时直接拦下并提示；页面新增离线横幅与「安装到桌面」引导。
  service worker 现在在 `localhost / 127.0.0.1`（http）也能注册，方便本机调试。
- 新增**Prompt / 模型 A/B 对比**（Roadmap `A22`）：新增 `qq_live_digest/abtest.py`（纯函数）；
  `main.py ab`（别名 `abtest`）在**同一份固定评测集**上跑两套配置，比较准确率（召回 / 误报 / 待办 /
  截止 / 去重）、成本与延迟并给出胜者。质量分 =（召回 + 待办 + 截止 + 去重 − 误报率）/ 5，等价于给误报
  负权重。配置可用内置预设（`default/strict/loose/wide/narrow/terse/detailed`）或直接给 JSON；其中
  `prompt_profile`（新增 `Settings.prompt_profile`，`terse/detailed`）与 `model` 只在配了 LLM Provider
  时才真正影响输出，离线时报告里会如实标注「不影响数字」。
- 新增**事件级跨群聚合**（Roadmap `A11`）：同一个事件被多个群先后转发时，不再各推一条，
  而是合成一条并标注来源群。判据不是字面相似，而是结构化的**事件键**——
  **对象 + 动作 + 时间 + 截止 + 来源**：对象取正文双字词，动作按行动词归一，
  时间取正文里的日期，截止取解析出的 deadline，来源取【学院通知】这类抬头（自动剥掉「通知 / 公告」后缀）。
  「同一事件」用各要素的**结构化相容**判定（动作 / 截止一致，时间 / 来源至少一方为空或相交，
  对象词重叠达标），所以「换个说法」合得起来、「同一天的两件不同事」不会误合。合并沿用既有
  `duplicate_groups` 口径，推送里显示「跨群重复」，决策日志记 `merged`（阶段 `event`）。
  新增 `qq_live_digest/events.py`（纯函数，无 I/O）；`main.py events`（别名 `aggregate`）查看开关 /
  最近合并 / 拆分覆盖。**默认关闭**（`QQ_DIGEST_EVENT_MERGE=0`），打开后才合并，避免改变既有推送口径。
- 新增**拆分覆盖**：判定误合并时，`main.py events --split <事件键>` 把该事件记进 `event_splits` 表，
  之后不再自动合并，`--unsplit` 撤销；事件键人可读、可复制，由 CLI 直接给出。

### 修复
- 修复 `Store.deferred_decisions` / `deferred_count` / `decision_counts` 的时间窗**绑定真实墙钟**导致
  的脆弱测试：三个方法新增可选 `now=` 参数（默认仍是当前时间），测试用冻结时刻调用，不再随运行日期漂移。
- 修复 `tests/test_ics.py` 里 `DTSTAMP` 断言**依赖运行机器时区**的问题（在 UTC 的 CI runner 上会失败）：
  测试改用带显式 `+08:00` 偏移的时刻，断言与机器无关（导出行为本身不变——`DTSTAMP` 始终按 UTC 写出）。

## [0.3.1] - 2026-10-09

Phase 1「AI 可控、可测、可替换」的**收尾**：补上群级个性化策略（`A9`）与消息处理可观测面板（`A32`），Phase 1 验收项至此全部交付。无破坏性变更。

### 新增
- 新增**消息处理可观测面板**（Roadmap `A32`）：一条命令看清消息是怎么被处理的——收了多少条
  （含出现过的群数与最活跃的几个群）、判了多少条与过滤率、攒出多少候选（推送 / 保留 / 待确认）、
  调了几次模型与 token 用量、推送成功率、候选待办的人工确认情况。**默认脱敏**：群号一律掩码
  （`123456789` → `12***89`），不带消息正文、发送者或任何密钥，输出可直接贴 Issue。
  新增 `qq_live_digest/observe.py`（纯聚合，只发 SELECT，不写库、不联网）；
  `main.py observe`（别名 `panel`，`--days` / `--json`）给终端视图，待办台服务同端口提供网页版
  `/panel` 与 `/api/panel`（token 与待办台相同，不新起端口），`doctor` 新增「可观测面板」一行。
  口径写进 README：过滤率分母只算已决状态（待投递 / 延后未决是过程态）、推送成功率分母不算待发、
  数据不足时给 `0.0` 而不是假装 100%；`store` 新增 `message_metrics` / `delivery_metrics` 两个只读聚合；
  新增 `tests/test_observe.py`（21 个用例，含脱敏断言，全程不联网）。
- 新增**群级个性化策略**（Roadmap `A9`）：每个群可以在全局配置之上覆盖少量开关，
  **没写的字段一律继承全局**，所以只改一个群不会牵连别的群。可覆盖：安静群（`quiet`）、
  本群额外关键词（`keywords`，命中即按明确通知处理，安静群 / 免打扰时段也会放行）、
  进摘要最低分（`min_score`）、模型档（`model`：`rule`/`light`/`strong`/`default`，对接 A6 分级路由）、
  免打扰时段（`quiet_hours`，按**消息时间**算，支持跨零点）。
  配置写在 `QQ_DIGEST_GROUP_POLICIES`（JSON，键可以是群号或群名）；新增
  `qq_live_digest/grouppolicy.py`（纯函数：坏 JSON / 认不出的字段 / 非法时段只丢自己不抛异常）、
  `main.py groups`（别名 `policies`，`--json`）逐群打印最终生效的开关与本群覆盖了哪些字段，
  `doctor` 的「群白名单」一行提示其中几个群配了策略。原有的 `QQ_DIGEST_QUIET_GROUPS` 仍然生效，
  只有被群策略显式写成 `"quiet": false` 时才让位；`min_score` 被覆盖后判定原因会改写成「本群阈值」。
  模型档只对单个群的批次生效，混群批次或没配 `QQ_DIGEST_LLM_MODEL_LIGHT` 时回落默认判据。
  *推送渠道的每群覆盖尚未纳入本项，留待后续。*
  新增 `tests/test_grouppolicy.py`（40 个用例，全程不联网）。

## [0.3.0] - 2026-10-09

Phase 1「AI 可控、可测、可替换」的**主体交付**（Roadmap `A4` / `A5` / `A6` / `A7` / `A21`）：
模型 Provider 抽象与可替换、Token/成本统计、每条候选的置信度与依据、模型分级路由，
以及第一版脱敏评测集与可复现指标。Phase 1 另两条验收项（`A8` 低置信度人工确认、
`A9` 群级策略）仍为部分完成，随后续小版本交付。

### 新增
- 新增**低置信度人工确认与反馈回收**（Roadmap `A8`）：待办分类改用 A7 的置信度打分，
  低于 `QQ_DIGEST_CANDIDATE_MIN_CONFIDENCE`（默认 0.55）的候选被**硬挡住**，绝不绕过确认
  直接进正式待办——阈值来自配置，判定原因里写明「置信度 X% 低于确认阈值 Y%」。
  新增 `main.py feedback`（别名 `review`，`--days` / `--json`）：汇总最近 N 天的确认率 /
  忽略率 / 纠错类型与按群分布，并把纠错样本转成**可读的规则建议**（只提建议，不自动改配置）。
  `doctor` 新增「反馈闭环」一行（候选 / 确认 / 忽略 / 纠错），「候选置信度」一行改用配置阈值。
  新增 `tests/test_hitl.py`（14 个用例：阈值分诊、反馈汇总、CLI 与 doctor，全程不联网）。
- 新增**模型分级路由**（Roadmap `A6`）：`qq_live_digest/routing.py` 在调用模型前先决定这一批
  走哪一档——本地规则 / 轻量模型 / 高能力模型——并把**原因**写进 `llm_calls` 新增的
  `route` / `route_reason` 两列（老库自动补列）。难例判据只用确定性信号：候选超过
  `QQ_DIGEST_LLM_ROUTE_MAX_LIGHT_ITEMS`（默认 6）条、有候选分值贴着入摘要阈值、或 A7 判为低置信度；
  配了 `QQ_DIGEST_LLM_MODEL_LIGHT` 才启用（留空 = 行为与之前完全一致），
  `QQ_DIGEST_LLM_ROUTE_EASY_LOCAL=1` 时条数很少且都已高置信的批次干脆不调模型（记一条「跳过」）。
  `main.py llm-stats` 新增「路由分布」一行，`--recent` 明细标注档位；
  新增 `tests/test_routing.py`（17 个用例：路由判据、用量表往返、老库迁移、真实链路接线，全程不联网）。
- 新增**脱敏评测集与可复现指标**（Roadmap `A21`）：`simulator.generate()` 给每条消息附一个
  `_expect` 意图标注（该不该推 / 该不该建待办 / 文本里有没有截止时间），新增
  `qq_live_digest/benchmark.py` 把它当 ground truth，跑真实链路回放后输出
  **召回 / 误报 / 待办判定 / 截止时间 / 跨群去重 / 延迟 / 成本 / 置信度分布**。
  新增 `main.py benchmark`（别名 `bench`，`--count/--seed/--fixture/--json/--fail-under`）：
  评测集与 `simulate` 是同一份假数据，离线、确定性（同 seed 同数字），可进 CI 当回归门槛。
  第一版基线（500 条 / seed=20261008）：召回 100%、误报 0.5%、待办与截止时间 100%、
  跨群去重 100%、延迟中位 0.0 / P95 31.6 分钟，详见新增的 `docs/BENCHMARK.md`；
  新增 `tests/test_benchmark.py`（14 个用例，含 150 条小评测集的召回 / 误报 / 待办 / 截止下限）。
- 新增**置信度与可解释性**（Roadmap `A7`）：每条候选都给出 0–1 的 `confidence` 与一组人话化的
  **触发规则**（例如 `+0.18 分值 10，高出阈值 3 两分以上`、`-0.24 原话有“记得”等不确定措辞`），
  权重就写在解释里，改规则即改解释。
  摘要候选与待办分类各用一套尺度（`assess_notice` / `assess_task`），待办那套与 `A8` 的数值逐项一致，
  只是把每一步都变成了可解释的依据。新增 `qq_live_digest/confidence.py`（纯函数，不碰数据库与网络）：
  推送正文多一行「为什么：…（把握较高 82%）」、待办台候选卡片多一行「依据：…」、
  `main.py show` 逐条打印置信度与「为什么」、`doctor` 新增「候选置信度」一行（把握较高 / 建议人工确认各几条）；
  老摘要没有这条记录时如实说明，不假装算过 0 分。
  新增 `tests/test_confidence.py`（38 个用例，含「触发规则求和 == 分数」与 A8 旧公式逐项对比，全程不联网）。
- 新增**Token / 成本统计**（Roadmap `A5`）：新增 `llm_calls` 表与 `qq_live_digest/llmstats.py`，
  每次模型调用（候选精炼 / 图片识别 / 文档理解）都落一行，记录 provider、模型、输入输出 token、
  耗时、失败原因，以及**是否重试过 / 是否降级回本地规则**；没配密钥或 `QQ_DIGEST_LLM_PROVIDER=none`
  时记一行「跳过」，让成本面板能解释「为什么一条都没有」。
  新增 `main.py llm-stats [--period day|week|month] [--buckets N] [--recent N] [--json]`
  （别名 `cost`）提供日 / 周 / 月视图，按 `QQ_DIGEST_LLM_PRICE_IN` / `QQ_DIGEST_LLM_PRICE_OUT`
  （元 / 百万 token，默认 0）**在展示时**折算费用，改价不用重写历史；`doctor` 新增「模型用量」
  一行（最近 24 小时调用 / token / 费用 / 失败）。新增 `tests/test_llmstats.py`（36 个用例，全程不联网）。
- 新增**模型 Provider 抽象**（Roadmap `A4`）：`qq_live_digest/providers.py` 定义
  `LLMProvider` / `LLMResult` / `NullProvider` / `OpenAICompatProvider` 与注册表；
  `qq_digest`（候选精炼）与 `attachments`（文档/图片理解）不再自己拼 HTTP 请求，
  换供应商只改配置。新增 `QQ_DIGEST_LLM_PROVIDER`（默认 `openai-compat`，`none` 可彻底关闭模型调用）、
  `docs/PROVIDERS.md`、`tests/test_providers.py`（30 个用例，全程不联网），
  `doctor` 新增 Provider 名校验。每次调用回传 model 与 token 用量，为 `A5` 成本统计留好接口。

## [0.2.0] - 2026-10-08

Phase 0「可解释 + 可贡献」全部交付：安全边界文档与回归测试、决策日志、故障演练、
全链路 `doctor`、测试数据模拟器、脱敏演示短片、FAQ 与 Issue/PR 模板。

### 新增
- 新增 `docs/ROADMAP.md`：项目唯一路线图入口，含 6 维加权评分与 Phase 0–3 排期。
- 新增 `docs/FAQ.md`：常见问题与对外统一口径。
- 新增 `CONTRIBUTING.md`、`SECURITY.md`、Issue 模板与 PR 模板。
- 新增 `tests/test_security.py`：安全边界回归测试（接收端请求体上限、鉴权失败不进入处理链路、
  `/health` 免鉴权但不泄露、SSRF 补充分支、压缩炸弹限制、移动端待办台鉴权），并在 CI 中单独成步运行。
- `main.py doctor` 重写为**全链路自检**（新增 `qq_live_digest/doctor.py`）：逐项检查 Python 版本、配置文件、
  群白名单、推送通道、OneBot 接收器、大模型、本地存储、NapCat、接收服务、待办台与访问层，
  每项给出 `OK/WARN/FAIL` 与一句可执行建议；新增 `--json`；输出脱敏，不含 token、`.env` 全文与真实群号。
- 新增 `tests/test_doctor.py`（29 个用例，全部离线，不依赖 NapCat / 网络 / 真实群号）。

- 新增 `docs/DR-DRILL.md` 故障演练手册：NapCat 掉线、模型可重试/不可重试故障、推送失败、
  进程被杀、超补采窗口、访问层断开、夜间静默与预算、数据目录不可写等场景的造障方式、预期现象与判定标准，
  附逻辑层已有测试用例对照表。

- 新增 `docs/SECURITY-BOUNDARY.md`：四条数据路径（QQ 入站 / AI 出站 / 推送出站 / 待办台入站）、
  逐项配置加固清单、依赖供应链核查（OSV.dev + PyPI）与第三方安全审计的逐条核实结果。
- 新增 `tests/test_optional_deps.py`：用子进程 + 导入拦截器模拟「未安装 qq-botpy」的环境，
  锁定「缺可选依赖时主程序与测试套件仍可用」这条边界。
- `docs/FAQ.md` 新增「装完之后它会不会一直在后台跑？怎么彻底卸载？」。

- 新增**决策日志**（Roadmap `A33`）：新增 `decisions` 表与 `qq_live_digest/decisions.py`，
  把「入口 → 筛选 → 去重 → 投递」的结论按 `msg_id` 结构化落库（结论 / 原因 / 分值 / 当时阈值 /
  命中规则 / 去重对照文本），`is_focus()` 拆出 `focus_reason()` 供日志与判定共用同一处逻辑。
  新增 CLI `main.py decisions`（别名 `why`，支持 `--msg-id` / `--outcome` / `--hours`）。
  `prune()` 与 `stats` 计数同步纳入 `decisions`。
- 新增 `tests/test_decisions.py`（23 个用例）：判定与原因一致性、批内/跨窗口去重、超限截断、
  入口拒绝（含每群每天只记一条的限流）、无通道时记 `held`、以及 CLI 渲染。
- 决策日志补齐 **`deferred`（延后未决）过程结论**：被夜间静默 / 当日推送额度 / 大模型失败挡住的消息
  以前整段不留痕——而夜间恰恰是最常见的场景，于是日志在夜里看着像"什么都没发生"。现在会沿 `msg_id`
  **就地更新**一条过程行（夜里 tick 几百次也只有一行），等消息真的推出去（`pushed` / `held`）或被最终
  挡下（`filtered` 等）时自动被覆盖，因此既不涨行、也不会出现"同时又延后又已推送"的自相矛盾。
  `main.py decisions` 抬头区分「已决分布」与「延后未决 N 条」，并支持 `--outcome deferred`；
  `doctor` 的本地存储一行新增 `decisions_deferred`。新增 6 个用例：就地更新、被最终结论清理、
  静默与额度两条端到端路径、以及"静默结束后真的推出去"。
- 新增**假群聊生成器**（Roadmap `A20`）：`qq_live_digest/simulator.py` + `main.py simulate`。
  生成一天的高校群消息流（闲聊 / 通知 / 作业 / 考试 / 报名 / 广告 / 图片 / 文件 / 跨群重复转发），
  字段与 OneBot 上报一致，可直接回放整条链路并输出漏斗（多少条消息 → 几次推送 → 几项待办 →
  决策分布），用来验证「500 条群聊最后剩几条通知」。**离线**：内存假通道 + 关闭大模型，
  配置里没有任何真实凭证，默认在临时目录里跑，不读 `.env`、不碰 `data/`。
  **确定性**：`--seed` 相同必得同一份数据，可导出 JSONL fixture 反复回放（也是后续 A21 评测集的数据来源）。
  新增 `tests/test_simulator.py`（11 个用例：同种子一致、字段合规、闲聊与通知都有、fixture 往返、
  离线承诺、回放可复现、额度用尽留下「延后未决」）。
- 新增**脱敏演示短片**（Roadmap `A27`）：`qq_live_digest/demo.py` + `tools/make_demo.py`，
  产出 26 秒的 `docs/demo/demo.gif`（≈3.9 MB，README 直接播放）与 `docs/demo/demo.mp4`（≈0.5 MB）。
  片子讲的是：一天 500 条假群消息 → 本地过滤 377 条 + 去重 54 条 → 68 条要点 → 23 次通知 → 63 项待办。
  **用生成代替录屏**：每个数字都取自那一次真实回放（生产同一条链路），改了参数或判定逻辑重跑一次
  片子就跟着变，不会烂成过期素材；输入是 `simulator` 的虚构群聊且每帧盖「示例数据」水印，
  因此可公开、无需打码。新增 `tests/test_demo.py`（6 个用例：数字与真实回放一致、理由去重、
  水印、片长落在 20–30 秒、GIF 可渲染、空数据不崩），详见 `docs/DEMO.md`。

### 修复

- **可选依赖不再拖垮整个进程**：`qq_live_digest/bot.py` 原先在 import 阶段 `raise SystemExit`，而
  `service.py` 又在模块级导入它，于是「未安装 qq-botpy」被放大成「整个程序与测试套件直接退出」
  （2026-10-08 第三方安全审计在 133 项测试中观察到的 1 个错误即源于此）。现在 qq-botpy 改为延迟导入，
  缺失时抛 `BotpyNotInstalled`：只有「QQ 官方机器人」这一个通道降级，其余功能照常。
- `main.py doctor` 新增判定：已配置 `QQ_BOT_APPID/QQ_BOT_SECRET` 但当前环境无法导入 qq-botpy 时报 FAIL
  并给出安装提示；未启用官方机器人时不误报。

- 附件下载在服务端返回的 `Content-Length` 与实际字节数不符（连接被截断）时会静默保存不完整文件，
  现改为拒绝并清理临时文件。

## [0.1.0] - 2026-10-06

首个开源版本。

### 新增
- NapCat / OneBot v11 只读接收，支持 chunked 上报与 `X-Signature` 鉴权，群白名单 + 安静群降噪。
- 本地规则打分 + 可选百炼 LLM 两级处理，LLM 失败自动回退本地摘要。
- 群文件与图片解析（PDF / Word / Excel / PPT / zip / 图片 OCR，OCR 不落盘）。
- 跨群同通知去重与最近推送窗口二次去重。
- SQLite 结构化存储（messages / processed / digests / deliveries / tasks / task_events）。
- 多通道推送与失败回退：WxPusher / Server酱 / PushPlus / Webhook / QQ 私聊。
- 手机待办台：分组、完成 / 忽略 / 纠错 / 稍后提醒、候选确认、每周复盘。
- 截止提醒（07:30 / 21:00）、推送预算与免打扰时段。
- 重启后 24 小时历史补采与定时补偿；看门狗自动重启与微信告警。
- CLI：run / tick / preview / stats / show / tasks / catchup / doctor / send-test / attach-test。
- GitHub Actions 单元测试；MIT 许可证。
