# 更新日志（Changelog）

本文件记录每个版本的新增、修复、破坏性变更与迁移说明。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [Unreleased]

### 新增
- 新增 `docs/ROADMAP.md`：项目唯一路线图入口，含 6 维加权评分与 Phase 0–3 排期。
- 新增 `docs/FAQ.md`：常见问题与对外统一口径。
- 新增 `CONTRIBUTING.md`、`SECURITY.md`、Issue 模板与 PR 模板。

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
