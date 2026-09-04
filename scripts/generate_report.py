#!/usr/bin/env python3
"""Canonical report generation CLI shim.

Delegates directly to claude_ads_core.reporting for deterministic
Markdown, HTML, and PDF rendering from validated ReportBundle JSON.
Free-form markdown regex parsing is retired under claude-ads-pc9.8.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from claude_ads_core.cli import main

if __name__ == "__main__":
    if len(sys.argv) > 1 and (sys.argv[1].endswith(".md") or sys.argv[1] in {"--help", "-h"} and "--format" not in sys.argv):
        if sys.argv[1].endswith(".md"):
            print(
                "Error: Free-form markdown report parsing is retired. Pass a validated ReportBundle JSON "
                "to 'claude-ads-core render <bundle.json>' or use 'python -m claude_ads_core.cli audit ...'",
                file=sys.stderr,
            )
            sys.exit(2)
    sys.exit(main(["render", *sys.argv[1:]]))
