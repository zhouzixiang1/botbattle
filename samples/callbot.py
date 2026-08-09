#!/usr/bin/env python3
"""最小 call/check 样例 bot（Botzone 标准协议）。

Botzone 信封：平台每回合发一行
  - Traditional / LongRunning 首回合：{"requests":[...], "responses":[...], ...}
  - LongRunning 后续回合：{"request": <单条请求负载>}
Bot 输出：{"response": <裸整数>}（-1 fold / -2 allin / 0 call-check / >0 raise 额外量）。

callbot 策略：永远 call/check（response=0）。
"""
from __future__ import annotations

import json
import sys


def _extract_request(envelope: dict) -> dict:
    """从信封取当前回合的请求负载。"""
    if "request" in envelope:          # LongRunning 单条
        return envelope["request"]
    reqs = envelope.get("requests") or []  # Traditional / 首回合
    return reqs[-1] if reqs else {}


def main() -> None:
    first_response = True
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"response": -1}), flush=True)  # 出错 fold
            continue
        # callbot：永远 call/check（0）
        print(json.dumps({"response": 0}), flush=True)
        if first_response:
            print(">>>BOTZONE_REQUEST_KEEP_RUNNING<<<", flush=True)
            first_response = False


if __name__ == "__main__":
    main()
