"""The scribe preset."""

ORDER = 90
AGENT = {"name": "scribe", "tags": ["docs", "report"], "model": "sonnet", "effort": "low",
         "role": "Writes for people: daily standup reports, PR descriptions, release notes, work item comments and docs. "
                 "Short, factual, linked to the PR or work item, no filler."}
