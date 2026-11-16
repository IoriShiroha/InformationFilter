#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目入口。

原来的「抓首页第一条 + 与 last_content.txt 比对 + 直接推送」旧爬虫已移除，
全部能力收敛到 pipeline（详见 Doc.md）：

    爬虫 → 预处理/双层去重 → AI 按【自然语言意图 + Tag】筛选 → 结构化解析 → 幂等推送

常用命令：
    python main.py --check-config                 # 只校验配置
    python main.py --dry-run --limit 5            # 只跑到粗筛，不调用 AI、不推送
    python main.py                                # 正式运行
    python main.py --setup-source <列表页URL>      # 只给 URL，让 AI 生成并缓存解析规则
"""

from __future__ import annotations

import sys

from pipeline.runner import main

if __name__ == "__main__":
    sys.exit(main())
