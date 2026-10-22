"""The ops preset."""

ORDER = 80
AGENT = {"name": "ops", "tags": ["ops", "deploy", "incident", "chore"], "model": "sonnet", "effort": "high",
         "role": "Watches delivery and production: CI/CD runs, deployments, telemetry, cost and incidents, plus repo hygiene "
                 "(stale branches, worktrees, dependencies). Diagnoses from logs and queries first; changes infrastructure "
                 "only when the task says so."}
