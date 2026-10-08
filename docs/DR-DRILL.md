# 故障演练手册（DR Drill）

> Roadmap A30。目标：在**真实故障**发生时，我们能确认「不会静默丢事」这件事真的成立，而不是只相信代码。
> 本文只做**操作层**演练：逻辑层（重试、推迟、回退、静默、预算、去重）已经有单元测试覆盖，见文末对照表。

---

## 一、演练的前提与原则

**三条判定原则**

1. **不丢**：故障恢复后，`/health` 的 `counts.unprocessed` 与投递队列最终收敛（重要消息都能推送）。
2. **不重复**：同一条消息不会被推送两次（`msg_id` 唯一约束 + `deliveries` 表去重）。
3. **可解释**：日志与 `/health` 里有对应的计数变化，而不是"看起来恢复了"。

**演练前必做**

- 记录基线快照（下节），演练后用同一命令对比。
- 备份 `.env`（演练要改配置）。**演练结束必须还原**，还原后重启服务。
- **不要拿真实班级群做实验**：造故障只动服务/通道，不要制造真实群消息；需要验证消息流时用 `main.py preview` 或测试推送。

**明确会丢消息的边界（不是 bug，是设计取舍）**

- 电脑关机超过 `QQ_DIGEST_CATCHUP_HOURS`（默认 24 小时）→ 超出窗口的历史消息不会被补采。
- 单次补采每群最多 `QQ_DIGEST_CATCHUP_COUNT`（默认 50）条。
- `QQ_DIGEST_RETENTION_DAYS`（默认 30 天）之外的消息会被清理。

---

## 二、基线快照（演练前后各记一次）

```powershell
cd <项目目录>

# 队列与计数：messages / unprocessed / digests / deliveries_sent / deliveries_failed
.\.venv\Scripts\python.exe main.py stats

# 全链路健康：含 llm.failures_total / deferred_total / fallbacks_total / last_error、
# push.sent_today / channels、counts.unprocessed、last_catchup_at
curl.exe -s http://127.0.0.1:8765/health

# 全链路自检（配置、NapCat、接收服务、待办台、访问层）
.\.venv\Scripts\python.exe main.py doctor
```

| 指标 | 看什么 | 期望 |
| --- | --- | --- |
| `counts.unprocessed` | 还没进入摘要的积压消息 | 故障恢复后回到 0（或稳态值） |
| `deliveries_failed` | 投递失败累计 | 不再增长；已有失败会被重试 |
| `llm.deferred_total` | 因模型故障推迟的批次 | 只在可重试失败时增长 |
| `llm.fallbacks_total` | 回退本地规则的批次 | 推迟用尽或不可重试时增长 |
| `push.sent_today` | 当日已推送条数 | 应大于 0；受静默/预算约束 |
| `last_catchup_at` | 最近一次补采成功时间 | 重启后应很快刷新 |

---

## 三、演练场景

每个场景按 **怎么造 → 预期现象 → 判定通过** 执行。

### S1 大模型不可用（可重试：超时 / 网络不通）

- **怎么造**：`.env` 里把 `QQ_DIGEST_LLM_ENDPOINT` 改成一个连不上的地址（例如 `http://127.0.0.1:1/v1/chat/completions`），重启服务，然后等下一个窗口。
- **预期现象**：日志出现「第 1/3 次失败，准备重试」（重试次数 = `QQ_DIGEST_LLM_MAX_RETRIES` + 1，退避 `QQ_DIGEST_LLM_RETRY_BACKOFF` 指数增长：0s / 1.5s / 3s）；批次被**推迟**而不是丢弃；在 `QQ_DIGEST_LLM_DEFER_WINDOW_MINUTES`（默认 15 分钟）内累计到 `QQ_DIGEST_LLM_DEFER_MAX_ATTEMPTS`（默认 3）次后**回退本地规则推送**。
- **判定通过**：`llm.deferred_total` 先增；随后 `llm.fallbacks_total` 增且**消息真的推了出来**；`counts.unprocessed` 回到 0；`llm.last_error` 有记录。
- **失败判据**：消息一直卡在 `unprocessed`（既不推也不回退），或延迟明显超过 15 分钟仍无推送。

### S2 大模型鉴权/参数错误（不可重试）

- **怎么造**：把 `DASHSCOPE_API_KEY` 改错（或换成已失效的 key），重启服务。
- **预期现象**：**不重试**（401/403/400 属不可恢复错误），直接一次性回退本地规则推送，日志是「大模型不可用…使用本地规则推送」。
- **判定通过**：`llm.failures_total` +1，`llm.fallbacks_total` +1，而 **`llm.deferred_total` 不增长**；消息正常推送。
- **失败判据**：出现连续重试日志，或批次被推迟（说明把配置错误当成了可恢复错误）。

### S3 推送通道失败

- **怎么造**：把 `WXPUSHER_APP_TOKEN` 改错（或临时把通道指向不可达地址），重启服务，然后 `main.py send-test`。
- **预期现象**：投递失败被记录并重试；已成功的通道不受影响（多通道时按 tier 回退）。
- **判定通过**：`deliveries_failed` 增长后**停止增长**；修回 token 后，积压的投递能被重试补发；同一条消息不重复推送。
- **失败判据**：消息静默消失（既没发出去也没进队列）。

### S4 NapCat / QQ 掉线

- **怎么造**：停掉 NapCat 计划任务（或直接结束 NapCat/QQ 进程）。
- **预期现象**：看门狗（`watchdog.ps1`，部署在 NapCat 目录，**不在本仓库**）每 5 分钟检查一次并自动重启：检查 3000 端口、`get_status` 的 `online/good`、`get_login_info`、以及 8765 `/health`；重启后仍不健康才通过 WxPusher 发一次告警（**同一告警 1 小时内只发一次**，状态存 `watchdog-alert-state.txt`）。
- **判定通过**：NapCat 自动拉起，`doctor` 的 NapCat 项回到 `OK online/good`；微信收到至多一条告警；期间接收服务 8765 仍可用（只影响收消息，不影响已有数据）。
- **失败判据**：反复告警刷屏；或 NapCat 未拉起且没有任何提示。

### S5 服务进程被杀

- **怎么造**：结束 `main.py run` 的 Python 进程（保留计划任务）。
- **预期现象**：`QQ-Live-Digest` 计划任务重新拉起服务；启动时**立即补采一次**最近 `QQ_DIGEST_CATCHUP_HOURS` 的历史消息，之后每 `QQ_DIGEST_CATCHUP_INTERVAL_MINUTES`（默认 30）分钟补一次。
- **判定通过**：`last_catchup_at` 很快刷新；`counts.unprocessed` 回落；停服期间的消息被补回且**没有重复推送**。
- **失败判据**：服务没有自动起来；或补采把已推送过的消息又推一遍。

### S6 关机超过补采窗口

- **怎么造**：停机 > `QQ_DIGEST_CATCHUP_HOURS` 后再启动。
- **预期现象**：窗口内的消息补回，窗口外的**不会**补回（设计取舍）。
- **判定通过**：日志里能看出补采的起止范围；`last_catchup_at` 正常更新。
- **注意**：这是**已知边界**，不是缺陷。需要更长回溯就调大 `QQ_DIGEST_CATCHUP_HOURS`（NapCat 侧能力有限）。

### S7 访问层不可用（Tailscale / 隧道断开）

- **怎么造**：断开 Tailscale（或停掉隧道）。
- **预期现象**：**核心消息处理完全不受影响**（访问层与消息链路是两层）；本机 8765 / 8766 仍正常。
- **判定通过**：`doctor` 的接收服务、待办台、NapCat 仍为 `OK`；`curl http://127.0.0.1:8765/health` 正常；`/health` 中 `last_tick_at` 持续更新。
- **失败判据**：消息处理也跟着停了（说明访问层与核心链路被错误耦合）。

### S8 夜间静默与推送预算

- **怎么造**：把 `QQ_DIGEST_QUIET_HOURS` 临时设成覆盖当前时间的区间（例如当前是 15:00 就设 `14:00-16:00`），重启服务，制造一条非紧急通知。
- **预期现象**：非紧急批次被**留在队列**，日志「本批 N 条消息暂不推送（quiet_hours），留到下次窗口」；紧急事项（urgent）**照常放行**。
- **判定通过**：静默期间不推送但 `unprocessed` 不丢；静默结束后（或还原配置重启后）消息被推送；当日推送数不超过 `QQ_DIGEST_PUSH_DAILY_BUDGET`。
- **失败判据**：静默期间消息被标记为已处理却没推送（真丢）；或紧急事项被压住。

### S9 数据目录不可写

- **怎么造**：给 `data/` 目录加只读（或占满磁盘）。
- **预期现象**：`doctor` 本地存储项 `FAIL` 并给出「检查 data/ 目录权限与磁盘剩余空间」；服务日志有明确异常。
- **判定通过**：故障原因一眼可见；恢复写权限后服务能继续，且不重复推送已有消息。

---

## 四、记录模板

| 场景 | 开始时间 | 基线（unprocessed / sent_today） | 造故障方式 | 观察到的现象 | 恢复时间 | 判定 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| S1 模型不可用 | | | | | | ☐通过 ☐失败 | |
| S3 推送失败 | | | | | | ☐通过 ☐失败 | |
| S4 NapCat 掉线 | | | | | | ☐通过 ☐失败 | |
| S5 进程被杀 | | | | | | ☐通过 ☐失败 | |
| S8 静默/预算 | | | | | | ☐通过 ☐失败 | |

**判定为失败时**：单独开一个 Bug Issue，附上脱敏后的日志片段、`/health` JSON 和上面的记录行（注意去掉 token、群号、本机路径）。

---

## 五、逻辑层已有测试覆盖（不需要手工演练）

| 行为 | 用例 |
| --- | --- |
| 可恢复错误重试、指数退避、重试耗尽 | `tests/test_retry.py` |
| 鉴权错误不重试 | `tests/test_retry.py::test_auth_error_is_not_retried` |
| 模型失败先推迟、恢复后继续 | `tests/test_service.py::test_retryable_llm_failure_defers_batch_then_recovers` |
| 不可重试失败直接回退且不阻塞 | `tests/test_service.py::test_non_retryable_llm_failure_falls_back_without_blocking` |
| 夜间静默留队列、紧急放行 | `tests/test_service.py::test_quiet_hours_hold_batch_until_morning`、`::test_urgent_bypasses_quiet_hours` |
| 当日推送预算 | `tests/test_service.py::test_daily_push_budget_holds_extra_batches` |
| 无可用通道时保留提醒重试键 | `tests/test_service.py::test_deadline_reminder_without_channel_keeps_retry_key` |
| 推送主通道失败回退备用通道 | `tests/test_push.py::test_primary_failure_uses_fallback` |
| 投递失败可重试发送 | `tests/test_push.py::test_retry_pending_resends_after_failure` |
| 补采按 `msg_id` 去重 | `tests/test_catchup.py::test_backfill_inserts_recent_messages_and_dedupes` |
| 消息/投递去重与重试 | `tests/test_store.py` |
| 安全边界（鉴权、SSRF、压缩炸弹等） | `tests/test_security.py` |
| 全链路自检本身 | `tests/test_doctor.py` |

---

## 六、演练时不要做的事

- 不要用真实班级群 / 真实通知做破坏性实验，也不要为了"制造故障"而删库或删附件。
- 不要为了让演练"通过"而临时关掉重试/静默/预算，然后忘记还原。
- 不要把带 token、群号、本机路径的原始日志直接贴进 Issue（见 `CONTRIBUTING.md`）。
