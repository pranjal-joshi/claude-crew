"""The builder preset."""

ORDER = 40
AGENT = {"name": "builder", "tags": ["feature", "bug", "api", "refactor"], "model": "opus", "effort": "high",
         "role": "Implements features and fixes in application code. Makes the smallest change that meets the acceptance criteria, "
                 "matches the surrounding code, and adds tests for new behaviour. Does not commit; Crew commits on approval.",
         "deliverable": "the full change summary: what changed and why, files touched, tests added or run, and anything left open"}
