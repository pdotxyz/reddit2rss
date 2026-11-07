import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import reddit2rss


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def post(post_id, title, score, created, image_url=None):
    data = {
        "id": post_id,
        "title": title,
        "score": score,
        "created_utc": created,
        "permalink": f"/r/vending/comments/{post_id}/slug/",
        "selftext": "Post body",
        "selftext_html": "&lt;div class=\"md\"&gt;&lt;p&gt;Post body&lt;/p&gt;&lt;/div&gt;",
        "author": "poster",
        "url": image_url or f"https://www.reddit.com/r/vending/comments/{post_id}/slug/",
        "url_overridden_by_dest": image_url,
        "preview": {"images": [{"source": {"url": image_url}}]} if image_url else {},
    }
    return {"kind": "t3", "data": data}


def comment(comment_id, score, body, depth=0):
    return {
        "kind": "t1",
        "data": {
            "id": comment_id,
            "score": score,
            "body": body,
            "body_html": f"&lt;div class=\"md\"&gt;&lt;p&gt;{body}&lt;/p&gt;&lt;/div&gt;",
            "depth": depth,
            "author": "commenter",
        },
    }


class Reddit2RSSTests(unittest.TestCase):
    def test_normalize_subreddit(self):
        self.assertEqual(reddit2rss.normalize_subreddit("r/overemployed"), "overemployed")
        self.assertEqual(
            reddit2rss.normalize_subreddit("https://www.reddit.com/r/vending/"),
            "vending",
        )

    def test_filter_and_rank_posts_last_24_hours(self):
        now = datetime.now(timezone.utc)
        ts = now.timestamp()
        payload = {
            "data": {
                "children": [
                    post("old", "Too old", 999, ts - 90000),
                    post("a", "Lower score", 20, ts - 1000),
                    post("b", "Highest score", 200, ts - 2000),
                    post("c", "Second score", 100, ts - 3000),
                ]
            }
        }
        with patch.object(reddit2rss, "_get_json", return_value=payload):
            selected = reddit2rss.fetch_top_posts("vending", now)
        self.assertEqual([p["id"] for p in selected], ["b", "c"])

    def test_select_top_level_comments_only(self):
        payload = [
            {"data": {"children": []}},
            {
                "data": {
                    "children": [
                        comment("low", 3, "Low"),
                        comment("reply", 900, "Reply", depth=1),
                        comment("high", 50, "High"),
                        comment("mid", 20, "Mid"),
                    ]
                }
            },
        ]
        with patch.object(reddit2rss, "_get_json", return_value=payload):
            selected = reddit2rss.fetch_top_comments("vending", "abc")
        self.assertEqual([c["id"] for c in selected], ["high", "mid"])

    def test_build_feed_contains_image_and_two_comments(self):
        now = datetime.now(timezone.utc)
        created = now.timestamp() - 100
        p = post("abc", "A test post", 321, created, "https://i.redd.it/example.jpg")
        comments = [comment("c1", 25, "First comment"), comment("c2", 12, "Second comment")]
        xml = reddit2rss.build_feed_xml("vending", [(p["data"], [c["data"] for c in comments])], now)
        root = ET.fromstring(xml)
        item = root.find("./channel/item")
        self.assertIsNotNone(item)
        encoded = item.find("{http://purl.org/rss/1.0/modules/content/}encoded").text
        self.assertIn("https://i.redd.it/example.jpg", encoded)
        self.assertIn("First comment", encoded)
        self.assertIn("Second comment", encoded)
        media = item.findall("{http://search.yahoo.com/mrss/}content")
        self.assertEqual(len(media), 1)

    def test_http_403_is_reported_without_retry(self):
        with patch.object(reddit2rss.requests, "get", return_value=FakeResponse({}, 403)) as get:
            with self.assertRaises(reddit2rss.RedditFetchError) as ctx:
                reddit2rss._get_json("https://www.reddit.com/r/vending/top.json")
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertEqual(get.call_count, 1)

    def test_update_writes_feed(self):
        now = datetime.now(timezone.utc)
        ts = now.timestamp() - 100
        listing = {"data": {"children": [post("abc", "A test post", 3, ts)]}}
        comments_payload = [{"data": {"children": []}}, {"data": {"children": [comment("c1", 3, "Good") ]}}]
        with patch.object(reddit2rss, "_get_json", side_effect=[listing, comments_payload]):
            with tempfile.TemporaryDirectory() as tmp:
                count = reddit2rss.update_subreddit("vending", Path(tmp), now)
                feed_path = Path(tmp) / "vending.xml"
                self.assertEqual(count, 1)
                self.assertTrue(feed_path.exists())
                ET.parse(feed_path)


if __name__ == "__main__":
    unittest.main()
