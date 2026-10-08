"""QQ 群通知实时监听与摘要推送服务。

模块划分：
    config      环境变量/.env 配置
    store       SQLite 消息、摘要、投递去重与恢复
    summarizer  复用 qq_digest 规则与模型精炼，生成推送文本
    providers   模型 Provider 抽象（换供应商/注入假实现都只动这一层）
    push        QQ 机器人私聊 / WxPusher / Server酱 / PushPlus / Webhook
    bot         官方 QQ 机器人 WebSocket 常驻客户端
    receiver    NapCat(OneBot v11) 兜底 HTTP 事件接收
    service     滚动窗口调度与投递重试
"""

__all__ = [
    "config",
    "store",
    "summarizer",
    "providers",
    "push",
    "bot",
    "receiver",
    "service",
]
