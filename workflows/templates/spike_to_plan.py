"""The spike to plan workflow template."""

ORDER = 30
WORKFLOW = {"id": "w3", "name": "Spike to plan", "description": "Research the options, then turn the pick into a plan.",
            "steps": [{"agent": "researcher", "title": "Research: {input}", "details": "", "gate": "approve"},
                      {"agent": "planner", "title": "Plan: {input}", "details": "Plan around the recommendation from step 1.", "gate": "approve"}]}
