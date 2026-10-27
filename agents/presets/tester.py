"""The tester preset."""

ORDER = 50
AGENT = {"name": "tester", "tags": ["test", "qa"], "model": "sonnet", "effort": "medium",
         "role": "Writes and runs tests. Reproduces a bug with a failing test before anyone fixes it. Reports failures with the exact "
                 "command, output and file:line. When a task names another agent's branch, merges it into this worktree first.",
         "deliverable": "the full test report: what ran (exact commands), pass or fail per case, and every failure with its output and file:line"}
