"""在调用 Promptfoo 前验证团队约定的 Python 与 Node 运行时。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

MIN_NODE = (22, 22, 0)
MIN_PYTHON = (3, 12)


def _node_version() -> tuple[int, int, int]:
    node = os.getenv("PROMPTFOO_NODE_BINARY") or shutil.which("node")
    if not node:
        raise RuntimeError("找不到 node；请先运行 nvm use 或设置 PROMPTFOO_NODE_BINARY")
    completed = subprocess.run([node, "--version"], check=True, capture_output=True, text=True)
    raw = completed.stdout.strip().removeprefix("v")
    parts = raw.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise RuntimeError(f"无法解析 Node 版本：{raw}")
    return tuple(int(part) for part in parts)


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        print(f"Python 必须 >= {'.'.join(map(str, MIN_PYTHON))}，当前为 {sys.version.split()[0]}")
        return 1
    try:
        node = _node_version()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Node 运行时检查失败：{error}")
        return 1
    if node < MIN_NODE:
        print(
            f"Node 必须 >= {'.'.join(map(str, MIN_NODE))}，当前为 {'.'.join(map(str, node))}。"
            "请在仓库根运行 nvm use。"
        )
        return 1
    print(
        f"运行时通过：Python {sys.version.split()[0]}，"
        f"Node {'.'.join(map(str, node))}。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
