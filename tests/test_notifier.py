"""
Unit tests for the interactive corrupt-review Discord notification.

Verifies the link-button payload is built correctly (and the
?with_components=true query param is used) when a web URL is configured, and
that it gracefully degrades to an embed-only message when it isn't. The HTTP
call is mocked, so no network is touched.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notifier  # noqa: E402


_WEBHOOK = "https://discord.com/api/webhooks/123/abc"


class DownloadReviewNotificationTests(unittest.TestCase):
    def _capture(self, web_url):
        n = notifier.Notifier(discord_webhook=_WEBHOOK, ntfy_topic="")
        with mock.patch.object(notifier.requests, "post") as post:
            post.return_value = mock.Mock(raise_for_status=lambda: None)
            n.download_review(
                broken=2, checked=10,
                items=["**Burn Notice** — S06E14.mkv (decode error)"],
                web_url=web_url,
            )
        return post

    def test_buttons_present_with_web_url(self):
        post = self._capture("http://192.168.0.50:5000")
        self.assertEqual(post.call_count, 1)
        url = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertIn("with_components=true", url)
        rows = payload["components"]
        buttons = rows[0]["components"]
        self.assertEqual(len(buttons), 3)
        # All link buttons (style 5) with absolute URLs.
        self.assertTrue(all(b["style"] == 5 and b["url"].startswith("http") for b in buttons))
        urls = [b["url"] for b in buttons]
        self.assertIn("http://192.168.0.50:5000/review/redownload", urls)
        self.assertIn("http://192.168.0.50:5000/review/keep", urls)

    def test_trailing_slash_normalised(self):
        post = self._capture("http://host:5000/")
        urls = [b["url"] for b in post.call_args.kwargs["json"]["components"][0]["components"]]
        self.assertIn("http://host:5000/review/keep", urls)

    def test_no_buttons_without_web_url(self):
        post = self._capture("")
        self.assertEqual(post.call_count, 1)
        url = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("with_components=true", url)
        self.assertNotIn("components", payload)
        self.assertIn("embeds", payload)

    def test_non_http_url_ignored(self):
        post = self._capture("192.168.0.50:5000")  # missing scheme
        payload = post.call_args.kwargs["json"]
        self.assertNotIn("components", payload)


if __name__ == "__main__":
    unittest.main()
