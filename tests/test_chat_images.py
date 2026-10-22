import json
import unittest

import agents
from tests.helpers import TempCrewCase
from triggers.teams import teams_prompt, teams_spec, thread_id

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


class ChatImages(TempCrewCase):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.crew._start = lambda a: True
        self.crew._send = lambda a, text, images=None: self.sent.append((text, images)) or True

    def test_screenshot_goes_with_the_message(self):
        r = self.crew.act("tell", {"agent": "boss", "text": "", "images": [{"media_type": "image/png", "data": PNG}]})
        self.assertFalse(r.get("error"), r)
        text, images = self.sent[-1]
        self.assertEqual(text, "See the attached screenshot.")
        self.assertEqual(images[0]["source"], {"type": "base64", "media_type": "image/png", "data": PNG})
        self.assertEqual(self.crew.boss["log"][-1]["images"], 1)

    def test_bad_images_are_refused(self):
        self.assertIn("PNG", self.crew.act("tell", {"agent": "boss", "text": "x", "images": [{"media_type": "image/svg+xml", "data": "x"}]})["error"])
        many = [{"media_type": "image/png", "data": PNG}] * 5
        self.assertIn("at most", self.crew.act("tell", {"agent": "boss", "text": "x", "images": many})["error"])

    def test_agents_cannot_send_images_over_mcp(self):
        self.crew.act("tell", {"agent": "boss", "text": "hi", "images": [{"media_type": "image/png", "data": PNG}]}, via="mcp")
        self.assertEqual(self.sent[-1], ("hi", []))

    def test_stdin_line_carries_image_blocks(self):
        line = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"},
                           *agents.launch.clean_images([{"media_type": "image/png", "data": PNG}])[1]]}})
        self.assertIn('"type": "image"', line)


class TeamsLinks(unittest.TestCase):
    def test_thread_id_from_message_and_channel_links(self):
        self.assertEqual(thread_id("https://teams.microsoft.com/l/message/19%3aAbC-1_2%40thread.skype/1792670176559"), "19:AbC-1_2@thread.skype")
        self.assertEqual(thread_id("https://teams.microsoft.com/l/channel/19%3a8e87%40thread.tacv2/Dev?groupId=x"), "19:8e87@thread.tacv2")
        self.assertEqual(thread_id("Platform > Incidents"), "")

    def test_prompt_searches_by_thread_id(self):
        _, sp = teams_spec({"where": "https://teams.microsoft.com/l/message/19%3aAbC%40thread.skype/1", "name": "Feedback"})
        p = teams_prompt(sp, "2026-01-01T00:00:00Z")
        self.assertIn("chatId is 19:AbC@thread.skype", p)
        self.assertIn("Don't list teams", p)

    def test_plain_link_and_channel_name(self):
        link = ("https://teams.microsoft.com/l/message/19:AbC-1@thread.skype/1792673067294?tenantId=t&groupId=g"
                "&teamName=Team%20A&channelName=Internal%20Feedback&createdTime=1")
        _, sp = teams_spec({"where": link})
        self.assertEqual((sp["thread"], sp["name"]), ("19:AbC-1@thread.skype", "Internal Feedback"))


if __name__ == "__main__":
    unittest.main()
