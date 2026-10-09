# 只读 REST API（Roadmap A15）

待办台服务（`qq_live_digest/webapp.py`，默认 `0.0.0.0:8766`）同时对外提供一组**只读** REST 接口，
给脚本、手机快捷指令，以及后续的 MCP 只读接口（`A1`）用。

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
| GET | `/api/panel` | 需要 | 消息处理可观测面板（脱敏聚合） |

`/api/notices` 作为 `/api/notifications` 的历史别名保留，网页待办台仍在用。

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
- 计划中的 `A1`（MCP 只读）会直接复用这里的 `qq_live_digest/restapi.py` 数据口径，不再另起一套。

