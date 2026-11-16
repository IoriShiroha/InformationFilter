#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流水线入口（与 python main.py 等价）

用法：
    python run_pipeline.py --check-config          # 只校验配置
    python run_pipeline.py --setup-source URL      # 只给 URL，生成并缓存解析规则
    python run_pipeline.py --dry-run               # 只跑到粗筛，不调用 AI、不推送
    python run_pipeline.py                         # 正式跑
    python run_pipeline.py --source auto-news --limit 5
"""

from __future__ import annotations

import sys

from pipeline.runner import main

if __name__ == "__main__":
    sys.exit(main())
