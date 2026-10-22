import unittest

from triggers.pipeline import pipe_results, pipe_spec, pipe_wants

BASE = {"pipelines": ["Api.Release"], "org": "https://dev.azure.com/acme", "project": "Proj"}


class PipelineResults(unittest.TestCase):
    """A pipeline trigger fires on any of the results you pick."""

    def test_several_results_are_kept_in_order(self):
        err, sp = pipe_spec(BASE | {"result": ["failed", "partial", "failed", "bogus"]})
        self.assertIsNone(err)
        self.assertEqual(sp["result"], ["failed", "partial"])

    def test_any_of_them_matches(self):
        sp = {"result": ["failed", "canceled"]}
        self.assertTrue(pipe_wants(sp, "failed"))
        self.assertTrue(pipe_wants(sp, "canceled"))
        self.assertFalse(pipe_wants(sp, "succeeded"))
        self.assertFalse(pipe_wants(sp, "partiallySucceeded"))

    def test_nothing_or_any_means_every_finished_run(self):
        for raw in (None, [], ["any", "failed"], "nonsense"):
            self.assertEqual(pipe_results(raw), ["any"])
        self.assertTrue(pipe_wants({"result": []}, "canceled"))

    def test_an_older_single_result_still_works(self):
        self.assertEqual(pipe_spec(BASE | {"result": "succeeded"})[1]["result"], ["succeeded"])
        self.assertTrue(pipe_wants({"result": "partial"}, "partiallySucceeded"))


if __name__ == "__main__":
    unittest.main()
