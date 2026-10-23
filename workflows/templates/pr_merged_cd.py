"""The PR merged → CD workflow template: follow a merged PR through its deployment. A PR trigger on "merged" starts it."""

ORDER = 80
WORKFLOW = {"id": "w8", "name": "PR merged: CD", "description": "Follow a merged PR through its deployment and check it landed.",
            "steps": [{"agent": "ops", "title": "Ship it: {input}",
                       "details": "This PR just merged. Find the CD or release run its merge started (`gh run list --commit <sha>` "
                                  "or `az pipelines runs list --branch <base>`) and watch it. If no run starts on its own and the "
                                  "repo documents how to deploy, start that and say so. When it fails, find the failing step and "
                                  "the cause in its log and report it; never retry a production deploy or roll back on your own. "
                                  "Once it succeeds, smoke-check what the PR changed where it was deployed. Call decide with "
                                  "continue when it is deployed and checked, with stop when the deployment failed; otherwise end "
                                  "with one line of status and the run's URL.",
                       "gate": "until", "every": 10, "checks": 72}]}
