"""End-to-end check: post a synthetic capture and read it back.

    python scripts/smoke_test.py [base_url]

Verifies that an upload lands on disk, gets indexed, is served back over HTTP,
and reaches a connected viewer over the WebSocket.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import urllib.request
import uuid

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

# Smallest valid PNG: a single white pixel.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def post_capture() -> dict:
    boundary = uuid.uuid4().hex
    strokes = {
        "strokes": [
            {"tool": "pen", "color": "#111318", "width": 2.6,
             "points": [[10, 10, 0.5, 0], [60, 40, 0.7, 120], [110, 20, 0.6, 240]]}
        ]
    }
    meta = {
        "session_id": "smoke-test",
        "trigger": "manual",
        "width": 1180,
        "height": 820,
        "dpr": 2,
        "duration_ms": 240,
        "idle_timeout_ms": 20000,
    }

    body = io.BytesIO()

    def field(name: str, value: str) -> None:
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.write(value.encode() + b"\r\n")

    body.write(f"--{boundary}\r\n".encode())
    body.write(b'Content-Disposition: form-data; name="image"; filename="capture.png"\r\n')
    body.write(b"Content-Type: image/png\r\n\r\n")
    body.write(PNG + b"\r\n")
    field("strokes", json.dumps(strokes))
    field("meta", json.dumps(meta))
    body.write(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        f"{BASE}/api/capture",
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def get(path: str) -> bytes:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=15) as response:
        return response.read()


def main() -> int:
    failures = []

    settings = json.loads(get("/api/config"))
    print(f"config           idle_timeout_ms={settings['idle_timeout_ms']}")

    result = post_capture()
    record = result["record"]
    print(f"upload           {record['id']}  strokes={record['stroke_count']} points={record['point_count']}")

    served = get(f"/captures/{record['png']}")
    print(f"png served back  {len(served)} bytes")
    if served != PNG:
        failures.append("served PNG does not match what was uploaded")

    strokes_file = json.loads(get(f"/captures/{record['strokes']}"))
    print(f"stroke file      {len(strokes_file['strokes'])} stroke(s), analysis={strokes_file['analysis']}")
    if strokes_file["strokes"][0]["points"][1] != [60, 40, 0.7, 120]:
        failures.append("stroke points did not round-trip")

    listing = json.loads(get("/api/captures?limit=5"))["captures"]
    print(f"index            {len(listing)} recent record(s), newest={listing[0]['id']}")
    if listing[0]["id"] != record["id"]:
        failures.append("new capture is not at the top of the index")

    for page in ("/canvas", "/viewer", "/static/canvas.js", "/static/viewer.js", "/static/style.css"):
        if not get(page):
            failures.append(f"{page} returned nothing")
    print("pages            canvas, viewer and assets all serve")

    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
