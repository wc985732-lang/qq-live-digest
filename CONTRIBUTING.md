# 参与贡献（Contributing）

感谢你愿意为 QQ-Live-Digest 做贡献。本文说明**怎么提交一个能被顺利合并的改动**。

## 一、先看看要做什么

- 路线图与优先级：`docs/ROADMAP.md`（评论区提到的想法一律先进候选池，按评分排期）
- 常见问题与统一口径：`docs/FAQ.md`
- 明确暂缓 / 不做的事项写在 ROADMAP 第六节，**不要**在没有讨论前就去实现它们

## 二、提 Issue

- Bug / 功能建议 / 提问请使用对应的 Issue 模板，按模板填写，**每个 Issue 写清验收条件**。
- 安全类问题**不要**开公开 Issue，请按 `SECURITY.md` 的方式私密报告。
- 提交前先搜索是否已有同类 Issue。

## 三、开发流程

```powershell
git clone https://github.com/wc985732-lang/qq-live-digest.git
cd qq-live-digest
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 改动前后都要跑测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

1. 从 `main` 拉出分支，命名建议：`fix/...`、`feat/...`、`docs/...`。
2. 小步提交，提交信息用祈使句、说明**为什么**（例如 `Fix stdout log encoding on Windows`）。
3. 保持 `main` 可运行：提交前必须本地跑通全部单元测试。
4. 直接向 `main` 提 PR（仓库 `protect-main` 规则禁止强推和删除默认分支）。

## 四、代码风格

- Python 3.12，遵循现有模块划分：接收 `receiver.py` / 调度 `service.py` / 存储 `store.py` / 摘要 `summarizer.py` / 推送 `push.py` / Web `webapp.py`。
- 保持改动**最小且聚焦**：不要顺手重构无关代码，不要引入与任务无关的依赖。
- 新增配置项请在 `.env.example` 与 README 中同步说明。
- 本项目**没有**配置额外的格式检查工具，请与周围代码风格保持一致即可。

## 五、测试要求

- 新功能 / 修复请补充对应单元测试（`tests/`）。
- 涉及消息筛选、去重、截止时间解析、推送回退等核心逻辑的改动，**必须**有测试覆盖。
- 依赖外部服务的改动请用 mock，不要在 CI 里发起真实网络请求。
- CI（`.github/workflows/tests.yml`）会在 `windows-latest` + Python 3.12 上跑全量测试。

## 六、隐私与安全（重要）

这是本项目最容易被忽略、但最不能出错的地方：

- **不要**提交 `.env`、`.env.bak-*`、Token、UID、API Key 或任何凭证。
- `data/`、`logs/`、SQLite 数据库、附件缓存已被 `.gitignore` 排除，**不要**为了调试把它们加进来。
- Issue、PR、日志、截图、测试数据里必须**打码**：QQ 号、群号、宿舍号、真实姓名、Tailscale 域名、本机绝对路径、真实消息内容。
- 如果需要真实数据结构做复现，请自己脱敏后提供最小样例。
- 不要新增调用 QQ 发送接口、绕过平台检测或提高账号风控风险的代码。

## 七、依赖变更

- 新增依赖请在 PR 里说明**用途、许可证、体积和维护状态**，并评估是否真的必要。
- 优先使用标准库；能不加依赖就不加。

## 八、PR 检查清单

- [ ] 关联了对应 Issue（如有）
- [ ] 本地全量单元测试通过
- [ ] 新增 / 修改的行为有测试覆盖
- [ ] 无凭证、无隐私数据、无真实群号 / 路径
- [ ] 配置项已同步 `.env.example` 与 README
- [ ] 改动聚焦，未夹带无关重构
