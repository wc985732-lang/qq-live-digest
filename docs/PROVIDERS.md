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

`python main.py doctor` 会校验 Provider 名字，写错会直接 `FAIL` 并列出可选值。

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
  这是后续成本统计（Roadmap `A5`）的数据来源。新写 Provider 时请别丢这些字段。
- **没有密钥就是不可用**：`build_provider()` 会返回 `NullProvider`，调用即失败并回退本地规则，
  不会静默返回空摘要。
