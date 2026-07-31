#!/usr/bin/env python3
"""最小 call/check 样例 bot（紧凑 JSON 协议）。开发期可直接当「二进制」用：
python3 -c "..." 或 chmod +x 本脚本后上传（需为 ELF 时请用 samples/build_sample.sh）。

本文件也可被 samples/callbot.py 以解释器方式本地测试。
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"a": "f"}), flush=True)
            continue
        to_call = int(req.get("to", 0) or 0)
        if to_call > 0:
            print(json.dumps({"a": "c"}), flush=True)
        else:
            print(json.dumps({"a": "k"}), flush=True)


if __name__ == "__main__":
    main()
