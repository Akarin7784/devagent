"""控制台输出编码。

## 解决的问题

Windows 上 Python 的 ``stdout`` 默认使用**本地 ANSI 代码页**（英文系统是
cp1252，中文系统是 cp936）。含中文的输出在 cp1252 控制台上会直接抛
``UnicodeEncodeError: 'charmap' codec can't encode characters`` —— 整个命令
以非零码退出，而失败原因看起来像是"程序崩了"，实际只是打印了一行中文。

这个坑在 CI 上尤其刺眼：GitHub Actions 的 ``windows-latest`` 是英文环境，
于是 ``python scripts/demo_smoke.py`` 在 ubuntu/macOS 上全绿、在 Windows 上
必红，而本地中文 Windows 反而看不出问题。

## 取舍

统一改成 UTF-8，并把编码错误降级为 ``replace``：宁可在无法渲染中文的
终端上看到问号，也不要因为"打印不出来"而崩溃。重定向到文件/CI 日志时
得到的是正确的 UTF-8，正是我们想要的结果。
"""

from __future__ import annotations

import sys
from typing import Any

_DONE = False


def force_utf8_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8（幂等，且永不抛异常）。

    只对支持 ``reconfigure`` 的文本流生效；被重定向到不支持的文件对象时
    静默跳过 —— 这只是尽力而为的兼容层，不该成为新的失败点。
    """
    global _DONE
    if _DONE:
        return
    _DONE = True
    for stream in (sys.stdout, sys.stderr):
        reconfigure: Any = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 取决于宿主环境
            continue


__all__ = ["force_utf8_stdio"]
