"""The PR CI monitor workflow template: keep a PR green until it merges. A PR trigger starts it for PRs opened outside Crew."""

ORDER = 70
WORKFLOW = {"id": "w7", "name": "PR CI monitor", "description": "Watch a PR's CI, fix what it broke, until it merges.",
            "steps": [{"agent": "ops", "title": "Keep green: {input}",
                       "details": "Watch this PR's CI checks (`gh pr checks <n>`). When a check fails because of this PR, find the cause in "
                                  "its log, fix it on the PR's branch and push; a failure that is already on the base branch is not "
                                  "this PR's: say so and leave it. Never merge the PR yourself. Call decide with continue once it is "
                                  "merged, with stop if it was closed without merging; otherwise end with one line of status and the "
                                  "PR's full URL.",
                       "gate": "until", "every": 10, "checks": 144}]}
