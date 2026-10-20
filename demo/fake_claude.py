"""Stand-in for `claude -p --input-format stream-json --output-format stream-json`.

Used by `python server.py --demo`: plays a short, scripted session per task and makes a
real file change in its worktree, so the board, transcript and review diff all work
without the Claude CLI or any tokens.
"""
import json, os, random, sys, time, uuid

sid = str(uuid.uuid4())
cost = 0.0
n = 0
arg = lambda flag, default: sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default
mode, model = arg("--permission-mode", "acceptEdits"), arg("--model", "opus")
pace = float(os.environ.get("CREW_DEMO_PACE", "1"))


def out(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()


def say(*blocks, tin=1400, tout=120, parent=None):
    global n
    n += 1
    out({"type": "assistant", "session_id": sid, "parent_tool_use_id": parent, "message": {"id": f"msg_{sid[:6]}_{n}", "content": list(blocks), "usage": {
        "input_tokens": tin, "cache_read_input_tokens": tin * 9, "cache_creation_input_tokens": 0, "output_tokens": tout}}})
    time.sleep(random.uniform(1.2, 2.6) * pace)


text = lambda s: {"type": "text", "text": s}
tool = lambda name, **inp: {"type": "tool_use", "id": f"tu_{uuid.uuid4().hex[:8]}", "name": name, "input": inp}


def subagents(*jobs, lead=""):
    """Start subagents in one message, interleave their work, then return their results.
    jobs: (description, subagent_type, [blocks...]) - mirrors how Claude Code streams them."""
    calls = [tool("Agent", description=d, subagent_type=k, prompt=d) for d, k, _ in jobs]
    say(*([text(lead)] if lead else []), *calls)
    queues = [list(steps) for _, _, steps in jobs]
    while any(queues):
        for call, q in zip(calls, queues):
            if q:
                say(q.pop(0), parent=call["id"], tin=900, tout=90)
    for call, (d, _, _) in zip(calls, jobs):
        out({"type": "user", "session_id": sid, "parent_tool_use_id": None, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call["id"], "content": f"{d}: done"}]}})


def write(path, body, append=False):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8") as f:
        f.write(body)


RATE_LIMIT = '''"""Token-bucket rate limiting for the login endpoint."""
import time
from collections import defaultdict

CAPACITY = 5          # attempts
REFILL_PER_SEC = 1 / 12  # one attempt every 12 s

_buckets: dict[str, tuple[float, float]] = defaultdict(lambda: (CAPACITY, time.monotonic()))


def allow(key: str) -> bool:
    """Return True if `key` (ip or username) may attempt a login now."""
    tokens, last = _buckets[key]
    now = time.monotonic()
    tokens = min(CAPACITY, tokens + (now - last) * REFILL_PER_SEC)
    if tokens < 1:
        _buckets[key] = (tokens, now)
        return False
    _buckets[key] = (tokens - 1, now)
    return True
'''

TESTS = '''from app.ratelimit import allow, CAPACITY


def test_allows_up_to_capacity():
    assert all(allow("1.2.3.4") for _ in range(CAPACITY))


def test_blocks_after_capacity():
    for _ in range(CAPACITY):
        allow("5.6.7.8")
    assert not allow("5.6.7.8")


def test_keys_are_independent():
    for _ in range(CAPACITY):
        allow("a")
    assert allow("b")
'''

SCRIPTS = {
    "rate": ("builder", [
        lambda: say(text("I'll add a token bucket in front of the login handler. Checking how requests reach it first."), tool("Grep", pattern="def login", path="app")),
        lambda: say(tool("Read", file_path="app/auth.py")),
        lambda: subagents(("Find existing middleware", "Explore", [tool("Glob", pattern="app/**/*.py"), tool("Grep", pattern="middleware|before_request"), text("No limiter in place; requests hit auth.login directly.")]),
                          lead="Splitting the work: a subagent maps the middleware while I design the limiter."),
        lambda: (write("app/ratelimit.py", RATE_LIMIT), say(tool("Write", file_path="app/ratelimit.py"), tout=900)),
        lambda: (write("app/auth.py", "\n\ndef guard(ip: str) -> None:\n    from app.ratelimit import allow\n    if not allow(ip):\n        raise TooManyAttempts(retry_after=12)\n", True), say(tool("Edit", file_path="app/auth.py"))),
        lambda: say(tool("Bash", command="python -m pytest -q", run_in_background=True)),
    ], "Added a token-bucket limiter (5 attempts, refills one per 12 s) and wired it into login via `guard()`.\n\n- app/ratelimit.py: new\n- app/auth.py: raises TooManyAttempts with Retry-After\n\nTests pass. Limits are per IP; per-username limiting is a follow-up."),
    "test": ("tester", [
        lambda: say(text("Reading the limiter to cover its edges."), tool("Read", file_path="app/ratelimit.py")),
        lambda: (write("tests/test_ratelimit.py", TESTS), say(tool("Write", file_path="tests/test_ratelimit.py"), tout=700)),
        lambda: say(tool("Bash", command="python -m pytest tests/test_ratelimit.py -q")),
        lambda: say(text("3 passed in 0.04s")),
    ], "Added 3 tests for the limiter: capacity, blocking, and key isolation. All pass.\n\nNot covered: refill timing (needs a fake clock)."),
    "research": ("researcher", [
        lambda: say(text("Comparing the three main options for background jobs."), tool("WebSearch", query="python background job queue comparison 2026")),
        lambda: say(tool("WebFetch", url="https://docs.celeryq.dev")),
        lambda: subagents(("Benchmark RQ vs Dramatiq", "general-purpose", [tool("WebSearch", query="rq vs dramatiq throughput"), tool("WebFetch", url="https://dramatiq.io/motivation.html"), text("Dramatiq ~3x RQ on retries-heavy loads.")]),
                          ("Check Celery ops cost", "general-purpose", [tool("WebSearch", query="celery production pitfalls"), text("Celery needs beat + flower + broker tuning.")]),
                          lead="Running two research subagents in parallel."),
        lambda: (write("notes/job-queues.md", "# Background jobs: options\n\n| | Celery | RQ | Dramatiq |\n|---|---|---|---|\n| Broker | Redis/RabbitMQ | Redis | Redis/RabbitMQ |\n| Retries | yes | basic | yes |\n| Ops weight | heavy | light | light |\n\n**Pick: Dramatiq.** Retries and rate limits built in, a fraction of Celery's setup.\n"),
         say(tool("Write", file_path="notes/job-queues.md"), tout=800)),
    ], "Recommend **Dramatiq**: built-in retries and rate limits, much lighter than Celery, sturdier than RQ.\n\nFull comparison in notes/job-queues.md."),
    "review": ("reviewer", [
        lambda: say(text("Reviewing the auth changes."), tool("Bash", command="git log --oneline -5")),
        lambda: subagents(("Scan for timing-unsafe comparisons", "Explore", [tool("Grep", pattern="== token|== secret"), text("1 hit: app/auth.py reset token check.")]),
                          ("Check dependency advisories", "general-purpose", [tool("Bash", command="pip-audit -r requirements.txt"), text("No known vulnerabilities.")]),
                          lead="Fanning out two reviewers: one for code patterns, one for dependencies."),
        lambda: say(tool("Read", file_path="app/auth.py")),
        lambda: (write("app/auth.py", "\n# reviewed: compare tokens with hmac.compare_digest to avoid timing leaks\n", True), say(tool("Edit", file_path="app/auth.py"))),
    ], "One real issue: password reset tokens were compared with `==`. Switched to `hmac.compare_digest`.\n\nEverything else looks fine."),
    "docs": ("docs", [
        lambda: say(text("Updating the README for the new login limits."), tool("Read", file_path="README.md")),
        lambda: (write("README.md", "\n## Login limits\n\nFive attempts per IP, then one more every 12 seconds. Clients get `429` with `Retry-After`.\n", True), say(tool("Edit", file_path="README.md"))),
    ], "Documented the login limits in README.md (attempts, refill rate, 429 + Retry-After)."),
}


def pick(title):
    t = title.lower()
    for key, words in (("test", ("test",)), ("review", ("review", "audit")), ("research", ("research", "compare", "evaluate", "queue")),
                       ("docs", ("doc", "readme")), ("rate", ("rate", "limit", "login", "auth"))):
        if any(w in t for w in words):
            return SCRIPTS[key]
    return SCRIPTS["docs"]


out({"type": "system", "subtype": "init", "session_id": sid, "model": model, "permissionMode": mode, "cwd": os.getcwd(),
     "tools": ["Read", "Edit", "Write", "Bash", "Grep", "Agent", "WebSearch", "mcp__docs__search"],
     "skills": ["ship-it", "deep-research", "code-review"],
     "mcp_servers": [{"name": "docs", "status": "connected"}, {"name": "linear", "status": "connected"}]})

for line in sys.stdin:
    title = json.loads(line)["message"]["content"].splitlines()[0]
    if not title.lower().startswith("task #"):  # a message from you mid-run or a send-back
        say(text("Got it, adjusting."))
        out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "total_cost_usd": round(cost, 4), "result": "Adjusted as asked."})
        continue
    _, steps, summary = pick(title)
    for step in steps:
        step()
    cost += random.uniform(0.08, 0.32)
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "total_cost_usd": round(cost, 4), "result": summary})
