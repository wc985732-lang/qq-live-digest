# 模型 Provider：怎么换、怎么加

`QQ-Live-Digest` 的业务代码（候选精炼、文档/图片理解）**不直接发 HTTP 请求**，只依赖
`qq_live_digest/providers.py` 里的 `LLMProvider` 接口。换供应商、接本地模型、注入假实现，
都只动这一层（Roadmap `A4`）。

## 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `QQ_DIGEST_LLM_PROVIDER` | `openai-compat` | Provider 名字；填 `none` 彻底关闭模型调用 |
| `DASHSCOPE_API_KEY` | 空 | 密钥；为空时自动退化成「不可用」（不会静默编造结果） |
| `QQ_DIGEST_LLM_ENDPOINT` | 百炼兼容端点 | 任何 OpenAI 兼容的 `/chat/completions` |
| `QQ_DIGEST_LLM_MODEL` | `qwen-plus` | 文本模型 |
| `QQ_DIGEST_VL_MODEL` | `qwen3-vl-plus` | 视觉模型（走同一条通道，只是换模型名） |
| `QQ_DIGEST_LLM_PRICE_IN` | `0` | 输入 token 单价，**元 / 百万 token**；只用于成本折算 |
| `QQ_DIGEST_LLM_PRICE_OUT` | `0` | 输出 token 单价，**元 / 百万 token**；只用于成本折算 |

`python main.py doctor` 会校验 Provider 名字，写错会直接 `FAIL` 并列出可选值。

## 用量与成本（Roadmap A5）

每次调用都会由 `providers.complete*_with_retries` 汇总成一条 `LLMCall`，交给
`DigestService` 挂上的记录器写进 SQLite 的 `llm_calls` 表（不挂记录器就完全不写库，
所以库外调用和单元测试不受影响）：

| 字段 | 含义 |
| --- | --- |
| `purpose` | 用途：`refine`（候选精炼）/ `vision`（图片识别）/ `document`（文档理解）/ `chat` |
| `provider` / `model` | 实际应答的 Provider 与模型名（取 `LLMResult`，失败时取 Provider 自述） |
| `prompt_tokens` / `completion_tokens` / `latency_ms` | 用量与耗时 |
| `status` | `ok` / `error` / `skipped`（本该调用却跳过了，例如没配密钥） |
| `attempts` / `retried` | 一次逻辑调用尝试了几次；是否发生过重试 |
| `fallback` | 失败后调用方是否降级回本地规则（不阻塞推送） |
| `error` | 失败原因（含异常类型） |

日 / 周 / 月视图与费用折算：

```powershell
.\.venv\Scripts\python.exe main.py llm-stats --period day     # 默认：最近 14 天
.\.venv\Scripts\python.exe main.py llm-stats --period week    # 最近 8 周
.\.venv\Scripts\python.exe main.py llm-stats --period month --recent 10
```

费用按上面两个单价**在展示时**折算，不写进数据库，所以换价目表可以重算全部历史；
单价留空时只统计 token（`doctor` 会提示）。

## 分级路由：规则 → 轻量 → 高能力（Roadmap A6）

不是每一批候选都值得动用贵的模型。填上 `QQ_DIGEST_LLM_MODEL_LIGHT` 之后，系统会先路由再调用：

| 档位 | 什么时候走 | 落库的 `route` |
| --- | --- | --- |
| 本地规则 | 只有打开 `QQ_DIGEST_LLM_ROUTE_EASY_LOCAL=1` 才会走：条数很少（≤2）且每条都已是高置信度 | `rule`（记一条「跳过」，不产生 token 成本） |
| 轻量模型 | 其余情况的默认档：候选条数不多、分值不贴阈值、没有低置信度候选 | `light` |
| 高能力模型 | 难例：候选超过 `QQ_DIGEST_LLM_ROUTE_MAX_LIGHT_ITEMS` 条、有候选分值贴着入摘要阈值、或 A7 判为低置信度 | `strong` |

三条刻意的保守设定：

1. **不填 `QQ_DIGEST_LLM_MODEL_LIGHT` 就等于没启用**——全部走高能力模型，行为和 A6 之前完全一致；
2. 「本地规则直接交付」要显式打开 `QQ_DIGEST_LLM_ROUTE_EASY_LOCAL=1`：不声不响地少给模型干活是不行的；
3. 难例判据只用确定性信号（条数、分值、A7 置信度），不做概率猜测。

每次决定连同原因一起写进 `llm_calls.route` / `route_reason`，`main.py llm-stats` 会多打一行
「路由分布：轻量模型 12、高能力模型 3」；`--recent` 的每条明细也会标出走的是哪一档。

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `QQ_DIGEST_LLM_MODEL_LIGHT` | 空 | 轻量模型名；留空 = 不启用分级路由 |
| `QQ_DIGEST_LLM_ROUTE_MAX_LIGHT_ITEMS` | 6 | 候选超过这个条数算难例，直接交给高能力模型 |
| `QQ_DIGEST_LLM_ROUTE_EASY_LOCAL` | 0 | 打开后：条数很少且都已高置信的批次干脆不调模型 |

## 接别的 OpenAI 兼容端点（最常见）

绝大多数场景不用写代码，改两个变量即可，例如本地 Ollama：

```dotenv
QQ_DIGEST_LLM_PROVIDER=openai-compat
QQ_DIGEST_LLM_ENDPOINT=http://127.0.0.1:11434/v1/chat/completions
QQ_DIGEST_LLM_MODEL=qwen2.5:7b
DASHSCOPE_API_KEY=ollama          # 兼容端点通常要求非空，随便填一个
```

视觉模型（`QQ_DIGEST_VL_MODEL`）走的是同一条通道；如果该端点没有视觉模型，把
`QQ_DIGEST_VISION=0` 关掉图片识别即可，其余功能不受影响。

## 接非兼容协议（需要写一个 Provider）

实现 `LLMProvider.complete()` 就够了，重试策略由调用方负责，不用自己实现：

```python
from qq_live_digest import providers


class MyProvider(providers.LLMProvider):
    name = "my-vendor"

    def __init__(self, *, api_key: str, model: str = "", timeout: int = 60) -> None:
        super().__init__(model=model, timeout=timeout)
        self.api_key = api_key

    def complete(self, messages, *, temperature=0.1, max_tokens=3000, timeout=None):
        text, usage = call_my_sdk(messages, model=self.model)   # 你自己的 SDK 调用
        return providers.LLMResult(
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )


def factory(settings, *, model: str = ""):
    return MyProvider(api_key=settings.dashscope_api_key, model=model or settings.dashscope_model)


providers.register_provider("my-vendor", factory)
```

然后 `QQ_DIGEST_LLM_PROVIDER=my-vendor`。

## 设计约定

- **Provider 只负责「发出去、拿回来」**：不重试、不降级、不做业务判断。失败统一抛
  `LLMError` 子类（`LLMRequestError` 带 HTTP `status`，网络错误 `status=0`）。
- **重试策略在调用方**：`providers.complete_with_retries` / `complete_json_with_retries`
  按 `llm_should_retry` 判定——限流、5xx、超时、返回内容不是 JSON 才重试；鉴权/参数错误立刻放弃。
- **用量必须回传**：`LLMResult` 带 `model` 与 `prompt_tokens` / `completion_tokens` / `latency_ms`，
  成本统计（Roadmap `A5`）直接读它落库，新写 Provider 时请别丢这些字段。只要实现 `complete()`，
  基类的 `complete_json_result()` 会顺带把用量交出来；如果覆写了 `complete_json()`，请让它继续
  走 `complete_json_result()`（或自己保证用量不丢），否则「要 JSON」的那条路径统计不到。
- **没有密钥就是不可用**：`build_provider()` 会返回 `NullProvider`，调用即失败并回退本地规则，
  不会静默返回空摘要。
