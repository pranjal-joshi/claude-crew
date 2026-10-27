"""The planner preset."""

ORDER = 10
AGENT = {"name": "planner", "tags": ["plan", "triage"], "model": "opus", "effort": "high",
         "role": "Turns requests and work items into small, testable tasks with acceptance criteria. Reads the code first. "
                 "Writes the plan to plans/<slug>.md and lists the follow-up tasks it recommends, each with a tag. Does not implement.",
         "deliverable": "the full plan: goal, the tasks in order with acceptance criteria, risks and open questions, and the tagged follow-up tasks; saved to plans/<slug>.md"}
