# 只读 REST + MCP 接口（Roadmap A15 / A1）

待办台服务（`qq_live_digest/webapp.py`，默认 `0.0.0.0:8766`）同时对外提供一组**只读** REST 接口，
给脚本、手机快捷指令，以及 MCP 只读接口（`A1`）用。

**它只读**：只发 SELECT，不写库、不联网、不推送，也不开放任何 QQ 发送能力。所有 `/api/*` 都要 token。

## 鉴权

- 请求头：`X-Token: <token>`
- 或查询参数：`?token=<token>`
- token 来自 `QQ_DIGEST_WEB_TOKEN`；没配且监听非回环地址时会自动生成并落库到 `meta`（`web_token`）。
- `/health` 与 `/api/health` 免 token（给存活探针用）。

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | 免 | 存活探针 |
| GET | `/api` | 需要 | 接口目录与版本（`read_only: true`） |
| GET | `/api/notifications` | 需要 | 最近的群通知摘要（正文截断到 600 字、不含发送者） |
| GET | `/api/tasks` | 需要 | 待办清单（今天 / 本周 / 以后 / 已完成 / 待确认分组） |
| GET | `/api/deadlines` | 需要 | 带截止时间的待办，按时间升序，标出逾期 |
| GET | `/api/calendar.ics` | 需要 | 有明确时间的待办导出为 iCalendar（`text/calendar`） |
| GET | `/api/conflicts` | 需要 | 潜在时间冲突：截止时间相近的待办按组返回 |
| GET | `/api/panel` | 需要 | 消息处理可观测面板（脱敏聚合） |

`/api/notices` 作为 `/api/notifications` 的历史别名保留，网页待办台仍在用。

`/api/calendar.ics` 把有明确时间的待办导成 iCalendar（RFC 5545），可直接导入或订阅日历：
时间以浮动本地时间写出，只有日期没有时刻的截止按**全天事件**（`VALUE=DATE`）导出，
没有截止时间的待办不导出；`?include_done=1` 可把已完成待办也带上。同样是**只读**的。

`/api/conflicts` 检测潜在时间冲突：把**截止时间**落在同一时间窗（`?window=` 分钟，默认 30）内的
多个待办按组返回（`conflicts[].items`），只提醒、**不擅自改任务**；`?include_done=1` 可带上已完成。

## 查询参数

- `/api/notifications?limit=8`：1–50，默认 8。
- `/api/deadlines?limit=200&include_done=1`：`limit` 1–1000 默认 200；`include_done` 传 `1/true/yes/on` 时带上已完成。
- `/api/panel?days=7`：1–365，默认 7。

## 例子

```bash
TOKEN=$(grep -m1 '^QQ_DIGEST_WEB_TOKEN=' .env | cut -d= -f2-)
curl -s -H "X-Token: $TOKEN" http://127.0.0.1:8766/api
curl -s -H "X-Token: $TOKEN" http://127.0.0.1:8766/api/deadlines | jq '.items[].summary'
curl -s "http://127.0.0.1:8766/api/notifications?token=$TOKEN&limit=3"
```

```powershell
.\\.venv\\Scripts\\python.exe main.py api          # 看接口目录 / 鉴权状态 / 访问地址
.\\.venv\\Scripts\\python.exe main.py api --json   # 机器可读
```

## `GET /api/deadlines` 字段

| 字段 | 说明 |
| --- | --- |
| `id` / `summary` / `action` / `category` / `importance` | 待办本身 |
| `status` / `done` | `open` / `candidate` / `done` |
| `deadline` | ISO 时间 |
| `overdue` | 未完成且已过截止时间 |
| `hours_left` | 距截止的小时数（可为负） |
| `groups` | 这条待办来自哪些群（群名） |

顶层还有 `as_of`（参考时刻）、`count` / `total`（返回条数 / 命中的总条数）、`overdue`（逾期条数）。

## 边界

- **只读**：没有写接口。写待办（完成 / 忽略 / 纠错）由网页待办台的 `POST /api/tasks/<id>` 负责，属于用户交互，不在本节范围内。
- **不开放 QQ 发送**：这套接口不会、也不能往群里发消息。
- **脱敏**：`/api/panel` 的群号一律掩码；`/api/notifications` 只给摘要与截断正文，不带发送者。
- `A1`（MCP 只读）已直接复用这里的 `qq_live_digest/restapi.py` 数据口径，不再另起一套（见下一节）。

## MCP 只读接口（Roadmap A1）

`main.py mcp`（别名 `mcp-serve`）以 MCP 的 **stdio** 传输对外服务：一行一条 JSON-RPC 2.0 消息，
**stdout 只放协议消息**（日志走 stderr，避免污染协议流）。它复用上面同一份只读口径，同样只发 SELECT、
不写库、不联网、不推送，也不开放任何 QQ 发送能力。

| Tool | 参数 | 说明 |
| --- | --- | --- |
| `recent_notifications` | `limit`（1–50，默认 8） | 最近的通知摘要（正文截断 600 字，不带发送者） |
| `todos` | `include_done`、`limit`（1–1000，默认 200） | 待办清单（未完成在前、逾期优先、越重要越靠前） |
| `deadlines` | `include_done`、`limit`（1–1000，默认 200） | 带截止时间的待办，按截止升序并标出逾期 |
| `search_messages` | `query`（必填）、`limit`（1–200，默认 20）、`hours`（0–8760，0=不限） | 按关键词搜历史群消息（本地库） |

只暴露这 **4 个只读 Tool**，没有任何写入、删除或发送能力；`main.py mcp --list-tools` 可离线打印 Tool 定义。
客户端配置示例（Cursor / Claude 等 MCP 客户端）：

```json
{ "mcpServers": { "qq-live-digest": { "command": "python", "args": ["main.py", "mcp"] } } }
```
