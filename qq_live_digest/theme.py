"""界面配色的唯一来源（single source of truth）。

待办台页面（`webapp.PAGE_HTML` 里的 CSS 变量）和演示短片（`demo.py` 的调色板）都从这里取色。
这两处以前各写一份颜色常量，改主题时容易只改一处、两边配色悄悄漂移，现在收成一份。

`DARK` / `LIGHT` 是有序的 ``(变量名, 值)`` 列表，两套的 key 与顺序完全一致，浅色只覆盖同名值。
``color-scheme`` 不是 CSS 变量（写法不带 ``--``），但它跟变量同处一个声明块，所以也放在列表末尾。
"""

from __future__ import annotations

DARK: tuple[tuple[str, str], ...] = (
    ("--bg", "#0E1117"),
    ("--bg-top", "#151A23"),
    ("--glass-1", "rgba(14,17,23,.94)"),
    ("--glass-2", "rgba(14,17,23,.78)"),
    ("--card", "#171C25"),
    ("--card-2", "#202733"),
    ("--line", "#2A313D"),
    ("--line-strong", "#3A4351"),
    ("--text", "#E9EDF4"),
    ("--muted", "#A6B0C0"),
    ("--dim", "#8A94A2"),
    ("--accent", "#3A6BE0"),
    ("--accent-ink", "#FFFFFF"),
    ("--accent-soft", "#4C82FF"),
    ("--urgent-soft", "#FF6B72"),
    ("--urgent-ink", "#FFB3B7"),
    ("--action", "#F0A63C"),
    ("--action-ink", "#FFC87C"),
    ("--academic", "#4C82FF"),
    ("--academic-ink", "#9CBBFF"),
    ("--info", "#8E99A8"),
    ("--tint-urgent", "rgba(255,107,114,.13)"),
    ("--line-urgent", "rgba(255,107,114,.30)"),
    ("--tint-action", "rgba(240,166,60,.13)"),
    ("--line-action", "rgba(240,166,60,.30)"),
    ("--tint-academic", "rgba(76,130,255,.15)"),
    ("--line-academic", "rgba(76,130,255,.32)"),
    ("--hero-1", "#1B2536"),
    ("--hero-2", "#171D28"),
    ("--hero-3", "#151A23"),
    ("--overdue-1", "rgba(255,107,114,.12)"),
    ("--shadow", "rgba(0,0,0,.40)"),
    ("--bar", "rgba(255,255,255,.08)"),
    ("--check-line", "#4A5464"),
    ("--nav-bg", "rgba(23,28,37,.88)"),
    ("--nav-active", "linear-gradient(135deg,rgba(58,107,224,.95),rgba(58,107,224,.55))"),
    ("--offline-bg", "#3A2A12"),
    ("--offline-ink", "#FFCF7A"),
    ("color-scheme", "dark"),
)

LIGHT: tuple[tuple[str, str], ...] = (
    ("--bg", "#F5F7FA"),
    ("--bg-top", "#FFFFFF"),
    ("--glass-1", "rgba(255,255,255,.95)"),
    ("--glass-2", "rgba(255,255,255,.82)"),
    ("--card", "#FFFFFF"),
    ("--card-2", "#F1F4F8"),
    ("--line", "#E3E7ED"),
    ("--line-strong", "#C9D1DC"),
    ("--text", "#191D24"),
    ("--muted", "#5B6472"),
    ("--dim", "#666F80"),
    ("--accent", "#2F6BE0"),
    ("--accent-ink", "#FFFFFF"),
    ("--accent-soft", "#2F6BE0"),
    ("--urgent-soft", "#D93A42"),
    ("--urgent-ink", "#B32B33"),
    ("--action", "#B87514"),
    ("--action-ink", "#8F5A0A"),
    ("--academic", "#2F6BE0"),
    ("--academic-ink", "#2A5FCB"),
    ("--info", "#6B7585"),
    ("--tint-urgent", "rgba(217,58,66,.08)"),
    ("--line-urgent", "rgba(217,58,66,.22)"),
    ("--tint-action", "rgba(184,117,20,.10)"),
    ("--line-action", "rgba(184,117,20,.24)"),
    ("--tint-academic", "rgba(47,107,224,.10)"),
    ("--line-academic", "rgba(47,107,224,.26)"),
    ("--hero-1", "#EDF2FE"),
    ("--hero-2", "#F7F9FC"),
    ("--hero-3", "#FFFFFF"),
    ("--overdue-1", "rgba(217,58,66,.07)"),
    ("--shadow", "rgba(23,32,54,.10)"),
    ("--bar", "rgba(25,29,36,.08)"),
    ("--check-line", "#B9C2CE"),
    ("--nav-bg", "rgba(255,255,255,.92)"),
    ("--nav-active", "linear-gradient(135deg,#DCE7FF,#EAF0FF)"),
    ("--offline-bg", "#FDF3E0"),
    ("--offline-ink", "#8A5A10"),
    ("color-scheme", "light"),
)

SCHEMES: dict[str, tuple[tuple[str, str], ...]] = {"dark": DARK, "light": LIGHT}


def pairs(scheme: str = "dark") -> tuple[tuple[str, str], ...]:
    """按源码顺序返回某套主题的 ``(变量名, 值)`` 列表。"""
    try:
        return SCHEMES[scheme]
    except KeyError:
        raise ValueError(f"未知主题 {scheme!r}：可选 dark / light") from None


def tokens(scheme: str = "dark") -> dict[str, str]:
    """``{变量名: 值}``，顺序与源码一致。"""
    return dict(pairs(scheme))


def css_block(scheme: str = "dark", indent: str = "  ") -> str:
    """拼成 CSS 声明块（每行一条），供 ``:root{...}`` / ``[data-theme="light"]{...}`` 使用。"""
    return "\n".join(f"{indent}{name}:{value};" for name, value in pairs(scheme))


def hex_rgb(name: str, scheme: str = "dark") -> tuple[int, int, int]:
    """把 ``#RRGGBB`` 形式的 token 解成 PIL 用的 RGB 三元组（演示片调色板用）。"""
    value = tokens(scheme)[name]
    if len(value) != 7 or not value.startswith("#"):
        raise ValueError(f"{name} 不是 #RRGGBB 颜色：{value!r}")
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
