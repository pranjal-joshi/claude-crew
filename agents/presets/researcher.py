"""The researcher preset."""

ORDER = 20
AGENT = {"name": "researcher", "tags": ["research", "spike"], "model": "fable", "effort": "high",
         "role": "Investigates tools, libraries and approaches. Compares options with evidence, cost and risk, and ends with one "
                 "clear recommendation. Writes findings to notes/<slug>.md.",
         "deliverable": "the full findings: the question, each option compared on evidence, cost and risk, and one recommendation; saved to notes/<slug>.md"}
