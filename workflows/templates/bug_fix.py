"""The bug fix workflow template."""

ORDER = 20
WORKFLOW = {"id": "w2", "name": "Bug fix", "description": "Reproduce with a failing test, fix, review.",
            "steps": [{"agent": "tester", "title": "Reproduce: {input}", "details": "Write a failing test that shows the bug. Do not fix it.", "gate": "auto"},
                      {"agent": "builder", "title": "Fix: {input}", "details": "Make the failing test from step 1 pass.", "gate": "approve"},
                      {"agent": "reviewer", "title": "Review fix: {input}", "details": "", "gate": "approve"}]}
