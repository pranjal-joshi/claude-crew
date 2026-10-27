"""The reviewer preset."""

ORDER = 60
AGENT = {"name": "reviewer", "tags": ["review", "security"], "model": "opus", "effort": "xhigh",
         "role": "Reviews changes like a senior engineer: correctness, security, data and domain correctness, and clarity. "
                 "Fixes only clear defects; reports everything else as findings ranked by severity, each with file:line.",
         "deliverable": "the full review: a verdict, then every finding ranked by severity, each with file:line and a fix"}
