"""把 A20 的假群聊回放渲染成 20–30 秒演示短片（Roadmap A27）。

为什么用代码生成而不是录屏：演示里的每个数字都必须是**真跑出来的**。这里直接复用
`simulator` 的那一次回放——消息条数、过滤条数、要点条数、待办条数、推送正文、决策日志，
全部取自真实运行结果。换了参数或改了判定逻辑，片子会跟着变，不会烂成过期的宣传素材。

它同时也天然脱敏：输入是 `simulator` 生成的虚构群聊，画面每帧都盖「示例数据」水印，
没有任何真实群号、昵称或 token 有机会进入渲染。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import decisions, simulator

LOGGER = logging.getLogger(__name__)

# 每帧都盖这个水印：看到片子的人第一眼就该知道数据是假的
WATERMARK = "示例数据 · 全部虚构"

WIDTH = 880
# 画布高度必须是偶数：libx264 + yuv420p 拒绝奇数尺寸，否则 mp4 导出会直接失败
HEIGHT = 496
FPS = 8

BG = (14, 17, 22)
PANEL = (22, 27, 34)
BORDER = (48, 54, 61)
FG = (230, 237, 243)
MUTED = (139, 148, 158)
ACCENT = (88, 166, 255)
GOOD = (63, 185, 80)
WARN = (210, 153, 34)
INK = (10, 12, 16)

# 场景时长（秒）：加起来就是片长，控制在 20–30 秒
SCENES: tuple[tuple[str, float], ...] = (
    ("消息洪流", 3.6),
    ("本地先筛", 4.4),
    ("合并推送", 4.0),
    ("一条通知", 6.0),
    ("决策日志", 4.5),
    ("落版", 3.5),
)

_FONT_CANDIDATES = (
    os.environ.get("QQ_DIGEST_DEMO_FONT", ""),
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


@dataclass
class DemoData:
    """演示要用到的全部素材，全部来自一次真实的假群聊回放。"""

    message_count: int = 0
    pushed: int = 0
    push_items: int = 0
    tasks: int = 0
    filtered: int = 0
    deduped: int = 0
    seed: int = 20261008
    window_minutes: int = 30
    daily_budget: int = 12
    groups: int = 5
    span_hours: float = 16.0
    stream: list[tuple[str, str, str]] = field(default_factory=list)  # (群名, 发言人, 内容)
    reasons: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    digest_lines: list[str] = field(default_factory=list)
    sample_msg_id: str = ""

    @property
    def candidates(self) -> int:
        """既没被规则挡下、也没被判重复的那部分——真正进入摘要的候选。"""
        return max(0, self.message_count - self.filtered - self.deduped)


def collect(
    workdir: Path | str,
    *,
    count: int = 500,
    seed: int = 20261008,
    span_hours: float = 16.0,
    window_minutes: int = 30,
    daily_budget: int = 12,
    step_minutes: int = 5,
) -> DemoData:
    """跑一次真实回放，把演示需要的数字与文本收集起来。"""
    workdir = Path(workdir)
    records = simulator.generate(count, seed=seed, span_hours=span_hours)
    service, channel = simulator.build_service(
        workdir, window_minutes=window_minutes, daily_budget=daily_budget
    )
    report = simulator.replay(
        records,
        data_dir=workdir,
        window_minutes=window_minutes,
        daily_budget=daily_budget,
        step_minutes=step_minutes,
        service=service,
        channel=channel,
    )
    data = DemoData(
        message_count=report.messages,
        pushed=report.pushed,
        push_items=report.push_items,
        tasks=report.tasks,
        filtered=int(report.decisions.get(decisions.FILTERED) or 0),
        deduped=int(report.decisions.get(decisions.DEDUPED) or 0),
        seed=seed,
        window_minutes=window_minutes,
        daily_budget=daily_budget,
        groups=len(simulator.GROUPS),
        span_hours=span_hours,
        stream=[
            (
                str(record.get("group_name") or ""),
                str(record.get("sender_name") or ""),
                str(record.get("content") or ""),
            )
            for record in records[:80]
        ],
        digest_lines=[],
    )
    # 取信息量最大的那条推送来展示：只挑最短的一条，面板会空得难看
    best = ""
    for _title, body in channel.sent[:8]:
        if len(body) > len(best):
            best = body
    data.digest_lines = [line for line in (best or report.first_body).splitlines() if line.strip()]
    seen: set[str] = set()
    for row in service.store.recent_decisions(outcome=decisions.FILTERED, limit=60):
        reason = str(row.get("reason") or "").strip()
        if reason and reason not in seen:
            seen.add(reason)
            data.reasons.append(reason)
        if len(data.reasons) >= 8:
            break
    pushed_rows = service.store.recent_decisions(outcome=decisions.PUSHED, limit=2)
    if pushed_rows:
        data.sample_msg_id = str(dict(pushed_rows[0]).get("msg_id") or "")
    else:
        recent = service.store.recent_decisions(limit=1)
        data.sample_msg_id = str(dict(recent[0]).get("msg_id") or "") if recent else ""
    # 日志场景刻意混三类结论：一眼看出「推 / 不推 / 推迟」都留了痕
    data.log_lines = decisions.describe_rows(
        pushed_rows
        + service.store.recent_decisions(outcome=decisions.FILTERED, limit=2)
        + service.store.recent_decisions(outcome=decisions.DEDUPED, limit=2)
    )
    return data


# ------------------------------------------------------------------ 绘图小工具
def _font(size: int):
    from PIL import ImageFont

    for candidate in _FONT_CANDIDATES:
        if candidate and Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:  # pragma: no cover - 字体文件损坏时试下一个
                continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_px: float) -> list[str]:
    """按像素宽度折行；中英混排下比按字符数折行靠得住。"""
    lines: list[str] = []
    current = ""
    for char in str(text):
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        if draw.textlength(current + char, font=font) > max_px and current:
            lines.append(current)
            current = char
        else:
            current += char
    if current or not lines:
        lines.append(current)
    return lines


def _panel(draw, box: tuple[float, float, float, float]) -> None:
    draw.rounded_rectangle(box, radius=10, fill=PANEL, outline=BORDER, width=1)


def _clip(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ease(value: float) -> float:
    return max(0.0, min(1.0, value))


def banner_text(data: DemoData) -> str:
    """顶部信息条：一眼看出这是假数据回放、以及跑的是哪套参数。"""
    return (
        f"qq-live-digest  ·  假群聊回放  ·  seed={data.seed}  ·  "
        f"窗口 {data.window_minutes} 分钟  ·  额度 {data.daily_budget}/天  ·  {WATERMARK}"
    )


def total_seconds() -> float:
    return sum(duration for _name, duration in SCENES)


# ------------------------------------------------------------------ 各场景
def _scene_stream(draw, data: DemoData, progress: float) -> None:
    title = _font(30)
    body = _font(16)
    small = _font(14)
    draw.text((48, 74), f"一天，{data.message_count} 条群消息", font=title, fill=FG)
    draw.text(
        (48, 118),
        f"{data.groups} 个群 · {data.span_hours:g} 小时 · 发言、通知、作业、广告、图片、文件混在一起",
        font=small,
        fill=MUTED,
    )
    box = (48, 152, WIDTH - 48, HEIGHT - 74)
    _panel(draw, box)
    lines = data.stream or [("群名", "某人", "……")]
    visible = int((box[3] - box[1] - 24) // 26)
    offset = int(progress * max(1, len(lines) - visible))
    y = box[1] + 14
    for group, sender, content in lines[offset : offset + visible]:
        draw.text((box[0] + 16, y), _clip(group, 10), font=small, fill=ACCENT)
        draw.text((box[0] + 136, y), _clip(sender, 8), font=small, fill=WARN)
        draw.text((box[0] + 256, y), _clip(content, 42), font=body, fill=FG)
        y += 26


def _scene_filter(draw, data: DemoData, progress: float) -> None:
    title = _font(24)
    huge = _font(76)
    body = _font(17)
    small = _font(14)
    draw.text((48, 74), "先过本地规则：不值得打扰的，一条都不推", font=title, fill=FG)
    value = int(data.filtered * _ease(progress * 1.7))
    draw.text((48, 126), str(value), font=huge, fill=GOOD)
    draw.text((300, 166), "条被判定为「不值得打扰」", font=body, fill=FG)
    draw.text(
        (300, 194),
        f"另有 {data.deduped} 条与已推内容重复，一并跳过",
        font=small,
        fill=MUTED,
    )
    box = (48, 232, WIDTH - 48, HEIGHT - 74)
    _panel(draw, box)
    draw.text((box[0] + 16, box[1] + 12), "判定理由（取自真实决策日志）", font=small, fill=MUTED)
    reasons = data.reasons or ["分值 2 < 阈值 3"]
    index = min(len(reasons) - 1, int(progress * len(reasons)))
    y = box[1] + 42
    for offset, reason in enumerate(reasons[index : index + 6]):
        color = FG if offset == 0 else MUTED
        prefix = "> " if offset == 0 else "· "
        for line in _wrap(draw, prefix + reason, small, box[2] - box[0] - 32)[:2]:
            draw.text((box[0] + 16, y), line, font=small, fill=color)
            y += 22


def _scene_funnel(draw, data: DemoData, progress: float) -> None:
    title = _font(24)
    body = _font(18)
    small = _font(14)
    draw.text((48, 74), "去重、合并，再按额度排队", font=title, fill=FG)
    steps = (
        ("原始消息", data.message_count, ACCENT),
        ("规则过滤 + 重复", data.filtered + data.deduped, MUTED),
        ("进入摘要的要点", data.push_items, GOOD),
        ("真正打扰你的通知", data.pushed, WARN),
    )
    span = WIDTH - 96
    widest = max(1, data.message_count)
    for index, (label, value, color) in enumerate(steps):
        ratio = value / widest
        shown = ratio * _ease(progress * 1.5 - index * 0.16)
        y = 130 + index * 72
        draw.text((48, y), label, font=small, fill=MUTED)
        draw.rectangle((48, y + 22, 48 + span, y + 44), fill=PANEL)
        draw.rectangle((48, y + 22, 48 + max(2.0, span * shown), y + 44), fill=color)
        draw.text((56, y + 24), str(value), font=body, fill=INK)
    draw.text(
        (48, HEIGHT - 96),
        f"额度 {data.daily_budget}/天兜底、紧急事项例外；被挡下的留到下一个窗口，不会丢",
        font=small,
        fill=MUTED,
    )


def _scene_digest(draw, data: DemoData, progress: float) -> None:
    title = _font(24)
    small = _font(14)
    mono = _font(15)
    draw.text((48, 74), "手机上真正收到的内容", font=title, fill=FG)
    draw.text((48, 108), "（标题、要点、截止时间都是刚跑出来的，不是截图）", font=small, fill=MUTED)
    box = (48, 136, WIDTH - 48, HEIGHT - 74)
    _panel(draw, box)
    lines = data.digest_lines or ["（本次运行没有产生推送）"]
    visible = int((box[3] - box[1] - 24) // 24)
    offset = int(progress * max(1, len(lines) - visible + 1))
    y = box[1] + 14
    for index, line in enumerate(lines[offset : offset + visible]):
        text = _clip(line, 56)
        if index == 0:
            color = FG
        elif text[:1].isdigit():
            color = ACCENT
        else:
            color = MUTED
        draw.text((box[0] + 16, y), text, font=mono, fill=color)
        y += 24


def _scene_log(draw, data: DemoData, progress: float) -> None:
    title = _font(24)
    mono = _font(15)
    draw.text((48, 74), "想追问「为什么没推给我」？日志里都有", font=title, fill=FG)
    command = f"$ python main.py decisions --msg-id {data.sample_msg_id or 'sim-20261008-00001'}"
    draw.text((48, 108), _clip(command, 70), font=mono, fill=GOOD)
    box = (48, 140, WIDTH - 48, HEIGHT - 74)
    _panel(draw, box)
    lines = data.log_lines or ["（没有决策记录）"]
    visible = int((box[3] - box[1] - 24) // 22)
    count = max(1, int(_ease(progress * 1.6) * len(lines)))
    y = box[1] + 14
    for line in lines[:count][:visible]:
        color = ACCENT if ("已推送" in line or "延后" in line) else MUTED
        draw.text((box[0] + 16, y), _clip(line, 58), font=mono, fill=color)
        y += 22


def _scene_outro(draw, data: DemoData, progress: float) -> None:
    title = _font(44)
    body = _font(18)
    small = _font(14)
    mono = _font(16)
    reveal = _ease(progress * 1.6)
    draw.text((48, 140), "qq-live-digest", font=title, fill=FG)
    draw.text((48, 200), "单用户自托管 · 把 QQ 群消息实时聚合成通知与待办", font=body, fill=ACCENT)
    if reveal > 0.35:
        draw.text(
            (48, 250),
            f"{data.message_count} 条消息 → {data.pushed} 次通知 → {data.tasks} 项待办",
            font=body,
            fill=FG,
        )
    if reveal > 0.55:
        draw.text(
            (48, 296),
            f"$ python main.py simulate --count {data.message_count} --seed {data.seed}",
            font=mono,
            fill=GOOD,
        )
    if reveal > 0.75:
        draw.text(
            (48, 332),
            "上面每个数字都能用这条命令复现；画面里的群、人、消息全部是虚构示例",
            font=small,
            fill=MUTED,
        )


_SCENE_RENDERERS = (
    _scene_stream,
    _scene_filter,
    _scene_funnel,
    _scene_digest,
    _scene_log,
    _scene_outro,
)


def _chrome(draw, data: DemoData, scene_index: int, total_progress: float) -> None:
    small = _font(14)
    tiny = _font(13)
    draw.text((48, 20), banner_text(data), font=small, fill=MUTED)
    label = f"{scene_index + 1}/{len(SCENES)}  {SCENES[scene_index][0]}"
    draw.text((WIDTH - 48 - draw.textlength(label, font=small), 20), label, font=small, fill=ACCENT)
    draw.rectangle((48, HEIGHT - 42, WIDTH - 48, HEIGHT - 36), fill=PANEL)
    draw.rectangle(
        (48, HEIGHT - 42, 48 + (WIDTH - 96) * _ease(total_progress), HEIGHT - 36), fill=ACCENT
    )
    draw.text((48, HEIGHT - 30), WATERMARK, font=tiny, fill=MUTED)
    note = "数据与截图均为程序生成的虚构示例"
    draw.text(
        (WIDTH - 48 - draw.textlength(note, font=tiny), HEIGHT - 30), note, font=tiny, fill=MUTED
    )


def frames(data: DemoData, *, fps: int = FPS) -> list[Any]:
    """按时间线渲染全部帧（RGB）。"""
    from PIL import Image, ImageDraw

    plan: list[tuple[int, float]] = []
    for index, (_name, duration) in enumerate(SCENES):
        ticks = max(1, int(round(duration * fps)))
        for tick in range(ticks):
            plan.append((index, (tick + 1) / ticks))
    total = len(plan)
    out: list[Any] = []
    for position, (index, progress) in enumerate(plan):
        image = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(image)
        _SCENE_RENDERERS[index](draw, data, progress)
        _chrome(draw, data, index, (position + 1) / total)
        out.append(image)
    return out


def render(
    data: DemoData,
    path: Path | str,
    *,
    scale: float = 1.0,
    fps: int = FPS,
    colors: int = 128,
) -> Path:
    """渲染成 GIF（README 里能直接自动播放那种）。"""
    from PIL import Image

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    size = (int(WIDTH * scale), int(HEIGHT * scale))
    palette: list[Any] = []
    for image in frames(data, fps=fps):
        if size != image.size:
            image = image.resize(size, Image.LANCZOS)
        palette.append(image.quantize(colors=colors, dither=Image.Dither.NONE))
    palette[0].save(
        target,
        save_all=True,
        append_images=palette[1:],
        duration=int(1000 / fps),
        loop=0,
        optimize=True,
        disposal=2,
    )
    return target


def render_mp4(
    data: DemoData,
    path: Path | str,
    *,
    fps: int = FPS,
    ffmpeg: str = "",
    crf: int = 22,
) -> Path | None:
    """顺便导出 mp4（用于发布）；找不到 ffmpeg 就返回 None，不报错。"""
    binary = ffmpeg or shutil.which("ffmpeg") or ""
    if not binary:
        LOGGER.warning("没有找到 ffmpeg，跳过 mp4 导出（GIF 已经生成好了）")
        return None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        frame_dir = Path(tmp)
        for index, image in enumerate(frames(data, fps=fps)):
            image.save(frame_dir / f"frame_{index:05d}.png")
        subprocess.run(
            [
                binary,
                "-y",
                "-loglevel",
                "error",
                "-framerate",
                str(fps),
                "-i",
                str(frame_dir / "frame_%05d.png"),
                # 调用方可能传了会算出奇数尺寸的 scale，这里兜一层，别让导出莫名其妙失败
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                str(crf),
                str(target),
            ],
            check=True,
        )
    return target


def build(
    out_dir: Path | str,
    *,
    count: int = 500,
    seed: int = 20261008,
    span_hours: float = 16.0,
    window_minutes: int = 30,
    daily_budget: int = 12,
    scale: float = 1.0,
    fps: int = FPS,
    colors: int = 96,
    make_mp4: bool = False,
    workdir: Path | str | None = None,
) -> tuple[DemoData, Path, Path | None]:
    """一条命令产出演示素材：跑回放 → 渲染 GIF（可选 mp4）。"""
    target_dir = Path(out_dir)
    temporary = workdir is None
    root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="qq-digest-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    data = collect(
        root,
        count=count,
        seed=seed,
        span_hours=span_hours,
        window_minutes=window_minutes,
        daily_budget=daily_budget,
    )
    gif = render(data, target_dir / "demo.gif", scale=scale, fps=fps, colors=colors)
    mp4 = render_mp4(data, target_dir / "demo.mp4", fps=fps) if make_mp4 else None
    if temporary:
        shutil.rmtree(root, ignore_errors=True)
    LOGGER.info("演示已生成：%s（%s 条消息 → %s 次通知）", gif, data.message_count, data.pushed)
    return data, gif, mp4
