"""The feature delivery workflow template."""

ORDER = 10
WORKFLOW = {"id": "w1", "name": "Feature delivery", "description": "Plan it, build it, test it, review it, write it up.",
            "steps": [{"agent": "planner", "title": "Plan: {input}", "details": "", "gate": "approve"},
                      {"agent": "builder", "title": "Build: {input}", "details": "Implement the plan from step 1.", "gate": "approve"},
                      {"agent": "tester", "title": "Test: {input}", "details": "Cover the new behaviour and its edge cases.", "gate": "auto"},
                      {"agent": "reviewer", "title": "Review: {input}", "details": "Review the builder's and tester's commits.", "gate": "approve"},
                      {"agent": "scribe", "title": "Write up: {input}", "details": "PR description and release note.", "gate": "auto"}]}
