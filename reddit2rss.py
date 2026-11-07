
#!/usr/bin/env python3
"""
Reddit RSS -> per-subreddit RSS feeds.

Source: Reddit's public Atom (.rss) feeds; no OAuth or API key.
Selection: first 2 entries from Reddit's top-of-day feed.
Comments: first 2 comments returned by each post's comment feed.
"""

# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///

import argparse
import email.utils
import html
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests


BASE_URL = "https://www.reddit.com"
OUTPUT_DIR = Path("docs")
REQUEST_INTERVAL_SECONDS = 65
MAX_POSTS = 2
MAX_COMMENTS = 2

USER_AGENT = (
    "Mozilla/5.0 (compatible; reddit2rss/1.0; "
    "+https://github.com/pdotxyz/reddit2rss)"
)

ATOM_NS = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"

ET.register_namespace("content", CONTENT_NS[1:-1])
ET.register_namespace("media", MEDIA_NS[1:-1])


class RedditFeedError(Exception):
    pass


class RedditRSSClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/atom+xml, application/xml, text/xml",
        })
        self.last_request = 0.0

    def get_feed(self, url):
        elapsed = time.monotonic() - self.last_request
        if self.last_request and elapsed < REQUEST_INTERVAL_SECONDS:
            time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)

        response = self.session.get(url, timeout=30)
        self.last_request = time.monotonic()

        if response.status_code in (403, 429):
            raise RedditFeedError(
                f"Reddit returned HTTP {response.status_code} for {url}"
            )

        response.raise_for_status()

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise RedditFeedError(
                f"Reddit did not return valid Atom XML for {url}"
            ) from exc

        return root


def child_text(element, tag):
    node = element.find(ATOM_NS + tag)
    return node.text.strip() if node is not None and node.text else ""


def entry_link(entry):
    for link in entry.findall(ATOM_NS + "link"):
        if link.get("rel", "alternate") == "alternate":
            return link.get("href", "")
    return ""


def entry_html(entry):
    # Reddit may put HTML in either content or summary.
    for tag in ("content", "summary"):
        node = entry.find(ATOM_NS + tag)
        if node is not None and node.text:
            return node.text

    return ""


def entry_images(entry, body):
    images = []

    # Images embedded in the HTML body.
    for match in re.findall(
        r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""",
        body,
        flags=re.IGNORECASE,
    ):
        if match.startswith("http"):
            images.append(match)

    # Atom media:thumbnail elements.
    for node in entry.iter():
        if node.tag.endswith("thumbnail") or node.tag.endswith("content"):
            url = node.get("url")
            if url and re.search(r"\.(jpg|jpeg|png|gif|webp)(\?|$)", url, re.I):
                images.append(url)

    # Remove duplicates while preserving order.
    return list(dict.fromkeys(images))


def parse_entry(entry):
    body = entry_html(entry)
    images = entry_images(entry, body)

    published = (
        child_text(entry, "published")
        or child_text(entry, "updated")
    )

    return {
        "id": child_text(entry, "id"),
        "title": child_text(entry, "title") or "(untitled)",
        "link": entry_link(entry),
        "author": child_text(entry, "author"),
        "published": published,
        "body": body,
        "images": images,
    }


def parse_datetime(value):
    if not value:
        return datetime.now(timezone.utc)

    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        return datetime.now(timezone.utc)


def get_entries(root):
    return root.findall(ATOM_NS + "entry")


def fetch_subreddit_posts(client, subreddit):
    url = f"{BASE_URL}/r/{subreddit}/top/.rss?t=day&limit=100"
    root = client.get_feed(url)

    entries = [parse_entry(e) for e in get_entries(root)]
    entries = [e for e in entries if e["link"]]

    # Reddit's top/day feed is already ordered by its ranking.
    return entries[:MAX_POSTS]


def fetch_top_comments(client, post):
    permalink = post["link"].rstrip("/") + "/.rss"
    root = client.get_feed(permalink)

    comments = []
    for entry in get_entries(root):
        item = parse_entry(entry)

        # Skip the submission itself; keep comment entries.
        if item["id"].startswith("t1_") or "/comments/" in item["link"]:
            if item["link"] and item["link"] != post["link"]:
                comments.append(item)

        if len(comments) >= MAX_COMMENTS:
            break

    return comments[:MAX_COMMENTS]


def add_text(parent, tag, value):
    node = ET.SubElement(parent, tag)
    node.text = value or ""
    return node


def add_cdata_html(parent, tag, value):
    # ElementTree escapes the HTML as XML text. RSS readers decode it.
    node = ET.SubElement(parent, tag)
    node.text = value or ""
    return node


def build_item(parent, item, item_type):
    node = ET.SubElement(parent, "item")

    title_prefix = "💬 " if item_type == "comment" else "🧵 "
    add_text(node, "title", title_prefix + item["title"])
    add_text(node, "link", item["link"])
    add_text(node, "guid", item["id"] or item["link"])

    published = parse_datetime(item["published"])
    add_text(
        node,
        "pubDate",
        email.utils.format_datetime(published),
    )

    if item["author"]:
        add_text(node, "author", item["author"])

    body = item["body"] or ""
    if item["images"]:
        image_html = "".join(
            f'<p><img src="{html.escape(url, quote=True)}" '
            f'alt="Reddit image" /></p>'
            for url in item["images"]
        )
        body += image_html

    add_cdata_html(node, CONTENT_NS + "encoded", body)

    for image_url in item["images"]:
        media = ET.SubElement(node, MEDIA_NS + "content")
        media.set("url", image_url)
        media.set("medium", "image")

    return node


def write_feed(subreddit, posts, comments_by_post):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"{subreddit}.xml"

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    add_text(channel, "title", f"Reddit: r/{subreddit}")
    add_text(
        channel,
        "link",
        f"{BASE_URL}/r/{subreddit}/",
    )
    add_text(
        channel,
        "description",
        f"Top posts of the day from r/{subreddit}, with selected comments.",
    )
    add_text(channel, "lastBuildDate", email.utils.format_datetime(
        datetime.now(timezone.utc)
    ))

    for post in posts:
        build_item(channel, post, "post")

        for comment in comments_by_post.get(post["id"], []):
            build_item(channel, comment, "comment")

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")

    temp_path = output_path.with_suffix(".xml.tmp")
    tree.write(temp_path, encoding="utf-8", xml_declaration=True)
    temp_path.replace(output_path)


def process_subreddit(client, subreddit):
    posts = fetch_subreddit_posts(client, subreddit)

    comments_by_post = {}
    for post in posts:
        comments_by_post[post["id"]] = fetch_top_comments(client, post)

    write_feed(subreddit, posts, comments_by_post)
    print(
        f"{subreddit}: OK; {len(posts)} posts, "
        f"{sum(map(len, comments_by_post.values()))} comments"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("subreddits", nargs="*")
    parser.add_argument(
        "--file",
        default="subreddits.txt",
        help="File containing one subreddit name per line",
    )
    args = parser.parse_args()

    subreddits = args.subreddits
    if not subreddits:
        path = Path(args.file)
        if path.exists():
            subreddits = [
                line.strip().removeprefix("r/")
                for line in path.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]

    if not subreddits:
        print("No subreddits configured.", file=sys.stderr)
        return 1

    client = RedditRSSClient()
    failed = []

    for subreddit in subreddits:
        subreddit = subreddit.strip().removeprefix("r/")
        if not subreddit:
            continue

        try:
            process_subreddit(client, subreddit)
        except Exception as exc:
            failed.append(subreddit)
            print(f"{subreddit}: FAILED; existing feed kept. {exc}")

    succeeded = len(subreddits) - len(failed)
    print(f"SUMMARY: {succeeded}/{len(subreddits)} subreddit feeds updated.")

    if failed:
        print("Failed: " + ", ".join(failed))
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
