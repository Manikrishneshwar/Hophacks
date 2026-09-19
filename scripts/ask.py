"""Ask the tablet a yes/no question and wait for the tap answer.

    python scripts/ask.py "Is this the right reading?"

This is the same call step 2 will make to confirm an API result. Needs the
server running and the tablet connected.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
TIMEOUT = 120


def main() -> int:
    question = " ".join(sys.argv[1:]) or "Is this correct?"

    request = urllib.request.Request(
        f"{BASE}/api/ask",
        data=json.dumps({"question": question, "timeout": TIMEOUT}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    print(f"asking the tablet: {question!r}")
    print("tap once for yes, twice for no ...")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT + 15) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = json.loads(exc.read() or b"{}").get("error", exc.reason)
        print(f"\nno answer: {detail}")
        return 1
    except urllib.error.URLError as exc:
        print(f"\ncannot reach the server at {BASE}: {exc.reason}")
        return 1

    print(f"\nanswer: {result['answer'].upper()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
