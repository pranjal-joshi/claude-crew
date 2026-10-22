"""The model change workflow template."""

ORDER = 40
WORKFLOW = {"id": "w4", "name": "Model change", "description": "Baseline eval, change, re-eval against the baseline.",
            "steps": [{"agent": "evaluator", "title": "Baseline: {input}", "details": "Measure the current behaviour first.", "gate": "auto"},
                      {"agent": "builder", "title": "Change: {input}", "details": "", "gate": "approve"},
                      {"agent": "evaluator", "title": "Re-evaluate: {input}", "details": "Same dataset and settings as the baseline; report the delta.", "gate": "approve"}]}
