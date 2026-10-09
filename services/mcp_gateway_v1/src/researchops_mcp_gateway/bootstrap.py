"""从仓库检出中的核心源码启动独立服务，不安装根依赖。"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    source_root = Path(__file__).resolve().parents[4] / "src"
    if not (source_root / "researchops" / "tool_runtime.py").is_file():
        print("MCP 网关须从 ResearchOps 仓库检出中安装并运行。", file=sys.stderr)
        return 2
    sys.path.insert(0, str(source_root))
    from .cli import main as cli_main

    return cli_main()
