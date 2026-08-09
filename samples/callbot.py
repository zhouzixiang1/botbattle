#!/usr/bin/env python3
import json
import sys

KEEP_RUNNING = ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"


def current_request(envelope):
    if "request" in envelope:
        request = envelope["request"]
    else:
        requests = envelope.get("requests")
        if not isinstance(requests, list) or not requests:
            raise ValueError("missing requests")
        request = requests[-1]
    if not isinstance(request, dict):
        raise ValueError("request payload must be an object")
    return request


def main():
    first_response = True
    for line in sys.stdin:
        if not line.strip():
            continue

        envelope = json.loads(line)
        current_request(envelope)

        # Holdem: response=0 表示 call/check。
        print(json.dumps({"response": 0}, separators=(",", ":")), flush=True)

        if first_response:
            print(KEEP_RUNNING, flush=True)
            first_response = False


if __name__ == "__main__":
    main()
