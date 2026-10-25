"""A stand-in for `claude -p --output-format stream-json` that misbehaves on purpose. CREW_FAULT picks how:

  exit1    reads the task, then exits with status 1 without a word
  crash    says hello, reads the task, starts answering, then exits without a result event
  garbage  prints lines that aren't JSON (and a cut-off one) around an otherwise good turn
  error    ends the turn with an error result whose text is CREW_FAULT_MSG
  ok       a normal, good turn
"""
import json
import os
import sys

fault = os.environ.get("CREW_FAULT", "ok")
sid = "faulty-session"


def out(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


def result(ok=True, text="done"):
    out({"type": "result", "subtype": "success" if ok else "error_during_execution", "is_error": not ok,
         "session_id": sid, "total_cost_usd": 0.01, "result": text, "permission_denials": []})


if fault == "exit1":
    sys.stdin.readline()
    sys.exit(1)
out({"type": "system", "subtype": "init", "session_id": sid, "model": "test", "permissionMode": "acceptEdits", "tools": []})
for line in sys.stdin:
    if fault == "crash":
        out({"type": "assistant", "session_id": sid, "message": {"id": "m1", "content": [{"type": "text", "text": "Working on it"}]}})
        sys.exit(0)
    if fault == "garbage":
        print("Warning: something on stdout that is not JSON", flush=True)
        sys.stdout.write('{"type": "assistant", "message": {"id": "cut\n')
        sys.stdout.flush()
        result(True, "done despite the noise")
    elif fault == "error":
        result(False, os.environ.get("CREW_FAULT_MSG", "boom"))
    else:
        result(True, "done")
