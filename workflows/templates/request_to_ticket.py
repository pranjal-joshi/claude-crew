"""The request to ticket workflow template: file a work item from a request, then plan it."""

ORDER = 60
WORKFLOW = {"id": "w6", "name": "Request to ticket", "description": "File a Bug or PBI from a request, share its link, then plan it.",
            "steps": [{"agent": "ticketer", "title": "File: {input}", "details": "File a Bug or PBI for this request, or link an open duplicate, "
                                                                             "and share the link where the request came from.", "gate": "auto"},
                      {"agent": "planner", "title": "Plan: {input}", "details": "Plan the work item filed in step 1: small tasks with acceptance criteria.",
                       "gate": "approve"}]}
