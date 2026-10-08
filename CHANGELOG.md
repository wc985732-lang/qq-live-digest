# 更新日志（Changelog）

本文件记录每个版本的新增、修复、破坏性变更与迁移说明。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [Unreleased]

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
