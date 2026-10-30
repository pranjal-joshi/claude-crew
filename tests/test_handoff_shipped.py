"""A Fixer card handed its commit to a Ship card, which opened a PR. The Fixer card stayed in Review looking unshipped,
was assigned to Shipper, and Shipper opened a second PR for the same change: its brief said nothing of the first.
A card's first message now names the cards it handed on to and the open PRs on its hand-off family and AB# item."""
from tests.helpers import TempCrewCase

PR1, PR2, PR3 = "https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2", "https://github.com/o/r/pull/3"


class HandoffShipped(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.crew._start = lambda ag: True
        self.sent = []
        self.crew._send = lambda ag, text, images=None: self.sent.append(text) or True
        self.a = next(iter(self.crew.agents.values()))

    def card(self, tid, **kw):
        t = {"id": tid, "title": f"Card {tid}", "col": "done", "created": tid} | kw
        self.crew.tasks.append(t)
        return t

    def test_a_handed_off_card_names_its_childs_pr_when_picked_up_again(self):
        fix = self.card(271, title="Record failure detail", col="review", details="Follow-up to AB#123.")
        self.card(274, title="Ship telemetry PR", handoff={"from": "Fixer", "task": 271}, who="Shipper", prs=[PR1])
        self.crew._begin(self.a, fix)
        self.assertIn("handed its work on to #274", self.sent[-1])
        self.assertIn(PR1, self.sent[-1])

    def test_open_prs_on_the_same_item_and_family_are_listed_closed_ones_not(self):
        self.card(268, title="Ship: AB#123 compaction", prs=[PR1])
        self.card(276, title="Open PR", handoff={"from": "Fixer", "task": 272}, prs=[PR2])
        self.card(280, title="Old", details="AB#123", prs=[PR3])
        self.crew._pr_closed.add(PR3)
        t = self.card(272, title="Skip compact", col="backlog", details="Follow-up to AB#123.")
        self.crew._begin(self.a, t)
        self.assertIn(PR1, self.sent[-1])
        self.assertIn(PR2, self.sent[-1])
        self.assertNotIn(PR3, self.sent[-1])

    def test_a_child_sees_its_siblings_prs(self):
        self.card(10, title="Fix")
        self.card(11, title="Ship one", handoff={"from": "Fixer", "task": 10}, prs=[PR1])
        t = self.card(12, title="Ship again", col="backlog", handoff={"from": "Fixer", "task": 10})
        self.crew._begin(self.a, t)
        self.assertIn(PR1, self.sent[-1])

    def test_nothing_known_adds_nothing(self):
        t = self.card(5, title="Fresh work", col="backlog")
        self.crew._begin(self.a, t)
        self.assertNotIn("Already done on this work", self.sent[-1])
