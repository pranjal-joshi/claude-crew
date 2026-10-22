"""The incident workflow template."""

ORDER = 50
WORKFLOW = {"id": "w5", "name": "Incident", "description": "Diagnose, fix, verify, post-mortem.",
            "steps": [{"agent": "ops", "title": "Diagnose: {input}", "details": "Find the root cause. Change nothing yet.", "gate": "approve"},
                      {"agent": "builder", "title": "Fix: {input}", "details": "", "gate": "approve"},
                      {"agent": "tester", "title": "Verify fix: {input}", "details": "", "gate": "auto"},
                      {"agent": "scribe", "title": "Post-mortem: {input}", "details": "What broke, why, the fix, and how we'd catch it sooner.", "gate": "auto"}]}
