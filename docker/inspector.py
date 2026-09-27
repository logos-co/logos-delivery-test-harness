#!/usr/bin/env python3
"""Client for the QML inspector that logos-standalone-app embeds (logos-qt-mcp).

The inspector takes newline-delimited JSON, `{"id", "command", "params"}`, on
QML_INSPECTOR_PORT and answers one line per request. `evaluate` runs a JS
expression against the plugin's root QML object, which is how the harness
drives the demo: the same functions its buttons call.

  inspector.py eval '<expression>'   print the reply
  inspector.py wait '<expression>' <seconds>
                                     exit 0 once the expression is true
  inspector.py screenshot <file.png>
"""
import base64
import json
import os
import socket
import sys
import time

PORT = int(os.environ.get("QML_INSPECTOR_PORT", "3768"))


def call(command, params, timeout=20):
    with socket.create_connection(("127.0.0.1", PORT), timeout=timeout) as s:
        s.sendall((json.dumps({"id": 1, "command": command, "params": params}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.split(b"\n")[0])


def evaluate(expression):
    return call("evaluate", {"expression": expression})


def main(argv):
    cmd = argv[1]
    if cmd == "eval":
        print(json.dumps(evaluate(argv[2])))
        return 0
    if cmd == "wait":
        deadline = time.time() + float(argv[3])
        while time.time() < deadline:
            try:
                if evaluate(argv[2]).get("result") is True:
                    return 0
            except OSError:
                pass  # the inspector is not listening yet
            time.sleep(1)
        return 1
    if cmd == "screenshot":
        reply = call("screenshot", {})
        with open(argv[2], "wb") as f:
            f.write(base64.b64decode(reply.get("image", "")))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
