#!/usr/bin/env python3
"""生成 A27 演示短片（20–30 秒）：一串假群聊 → 一条通知。

    python tools/make_demo.py                    # 生成 docs/demo/demo.gif
    python tools/make_demo.py --mp4               # 顺便导出 mp4（需要 ffmpeg）
    python tools/make_demo.py --count 800 --budget 0

片子里的每个数字都来自刚刚那一次真实回放（走的还是生产同一条链路），
数据与画面全部是程序生成的虚构示例，可以放心公开。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import demo  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 A27 演示短片（假数据，可公开）")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "docs" / "demo"), help="输出目录")
    parser.add_argument("--count", type=int, default=500, help="模拟多少条群消息")
    parser.add_argument("--seed", type=int, default=20261008, help="随机种子（同种子必得同一份片）")
    parser.add_argument("--hours", type=float, default=16.0, help="消息铺开多少小时")
    parser.add_argument("--window", type=int, default=30, help="合并窗口分钟数")
    parser.add_argument("--budget", type=int, default=12, help="每日推送额度（0 = 不限）")
    parser.add_argument("--scale", type=float, default=0.72, help="画面缩放（越小文件越小）")
    parser.add_argument("--fps", type=int, default=5, help="帧率")
    parser.add_argument("--colors", type=int, default=48, help="GIF 调色板颜色数")
    parser.add_argument("--mp4", action="store_true", help="额外导出 mp4（需要 ffmpeg）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    print(f"回放假群聊：{args.count} 条消息 / seed={args.seed} / 窗口 {args.window} 分钟 …")
    data, gif, mp4 = demo.build(
        args.out,
        count=args.count,
        seed=args.seed,
        span_hours=args.hours,
        window_minutes=args.window,
        daily_budget=args.budget,
        scale=args.scale,
        fps=args.fps,
        colors=args.colors,
        make_mp4=args.mp4,
    )
    size_mb = gif.stat().st_size / 1e6
    print(f"片长 {demo.total_seconds():.1f} 秒 · {len(demo.frames(data, fps=args.fps))} 帧 · {size_mb:.2f} MB")
    print(
        f"素材：{data.message_count} 条消息 → 过滤 {data.filtered} + 重复 {data.deduped}"
        f" → {data.push_items} 条要点 → {data.pushed} 次通知 → {data.tasks} 项待办"
    )
    print(f"GIF：{gif}")
    if mp4:
        print(f"MP4：{mp4 or '未生成（没找到 ffmpeg）'}")
    print("下一步：把 gif 写进 README，或作为 Release 附件发布。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
