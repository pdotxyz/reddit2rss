
#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
#   "beautifulsoup4",
# ]
# ///

import email.utils
import html
import re
import time
import xml.etree.ElementTree as ET

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ============================================================
# Configuration
# ============================================================

BASE_URL = "https://www.reddit.com"
OUTPUT_DIR = Path("docs")
SUBREDDITS_FILE = Path("subreddits.txt")

POST_LIMIT = 2
COMMENT_LIMIT = 2
REQUEST_DELAY = 65
TIME_PERIOD = "day"

USER_AGENT = "Mozilla/5.0 (compatible; Reddit2RSS/1.0)"

ATOM_NS = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"

ET.register_namespace("content", CONTENT_NS[1:-1])
ET.register_namespace("media", MEDIA_NS[1:-1])

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


# ============================================================
# XML helpers
# ============================================================

def add_text(parent, tag, value):
    element = ET.SubElement(parent, tag)
    element.text = str(value or "")
    return element


def add_html(parent, tag, value):
    element = ET.SubElement(parent, tag)
    element.text = value or ""
    return element


def safe_html(value):
    return html.escape(str(value or ""), quote=True)


# ============================================================
# Date and Atom parsing
# ============================================================

def parse_datetime(value):
    if not value:
        return datetime.now(timezone.utc)

    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except Exception:
        pass

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except Exception:
        return datetime.now(timezone.utc)


def entry_html(entry):
    for key in ("content", "summary"):
        element = entry.find(f"{ATOM_NS}{key}")

        if element is not None:
            return "".join(element.itertext()).strip()

    return ""


def entry_images(entry, body=""):
    images = []

    soup = BeautifulSoup(body, "html.parser")

    for image in soup.find_all("img"):
        src = image.get("src")

        if src:
            images.append(urljoin(BASE_URL, src))

    for element in entry.iter():
        if element.tag.endswith(("content", "thumbnail")):
            url = element.attrib.get("url")

            if url and re.search(
                r"\.(jpg|jpeg|png|gif|webp)(\?|$)",
                url,
                re.I,
            ):
                images.append(url)

    return list(dict.fromkeys(images))


def parse_entry(entry):
    title = entry.findtext(
        f"{ATOM_NS}title",
        default="",
    ).strip()

    link = ""

    for element in entry.findall(f"{ATOM_NS}link"):
        if element.attrib.get("rel", "alternate") == "alternate":
            link = element.attrib.get("href", "")
            break

    if not link:
        element = entry.find(f"{ATOM_NS}link")

        if element is not None:
            link = element.attrib.get("href", "")

    link = urljoin(BASE_URL, link)

    author_element = entry.find(f"{ATOM_NS}author")
    author = ""

    if author_element is not None:
        author = author_element.findtext(
            f"{ATOM_NS}name",
            default="",
        ).strip()

    published = (
        entry.findtext(f"{ATOM_NS}published")
        or entry.findtext(f"{ATOM_NS}updated")
        or ""
    )

    body = entry_html(entry)

    return {
        "id": entry.findtext(
            f"{ATOM_NS}id",
            default="",
        ).strip(),
        "title": title,
        "link": link,
        "author": author,
        "published": published,
        "body": body,
        "images": entry_images(entry, body),
    }


def fetch_feed(url):
    response = session.get(url, timeout=30)
    response.raise_for_status()

    root = ET.fromstring(response.content)

    entries = root.findall(f"{ATOM_NS}entry")

    return [parse_entry(entry) for entry in entries]


# ============================================================
# Post-level statistics
# ============================================================

def empty_post_stats():
    return {
        "score": None,
        "upvote_ratio": None,
        "num_comments": None,
    }


def fetch_post_stats(post):
    """
    Retrieve actual post-level statistics from Reddit metadata.

    RSS does not expose score, upvote ratio, or comment count.
    If Reddit blocks JSON, return N/A values.
    """
    stats = empty_post_stats()

    try:
        permalink = post["link"].rstrip("/")

        if permalink.endswith(".rss"):
            permalink = permalink[:-4]

        url = permalink + ".json"

        response = session.get(
            url,
            headers={"Accept": "application/json"},
            params={"raw_json": 1},
            timeout=20,
        )

        response.raise_for_status()

        data = response.json()

        post_data = data[0]["data"]["children"][0]["data"]

        stats["score"] = post_data.get("score")
        stats["upvote_ratio"] = post_data.get("upvote_ratio")
        stats["num_comments"] = post_data.get("num_comments")

    except Exception as exc:
        print(f"    Post stats unavailable: {exc}")

    return stats


# ============================================================
# Reddit fetching
# ============================================================

def fetch_posts(subreddit):
    url = (
        f"{BASE_URL}/r/{subreddit}/top/.rss"
        f"?t={TIME_PERIOD}&limit=100"
    )

    print(f"Fetching posts: r/{subreddit}")

    entries = fetch_feed(url)

    posts = [
        entry for entry in entries
        if entry["link"] and "/comments/" in entry["link"]
    ]

    return posts[:POST_LIMIT]


def fetch_top_comments(post):
    """
    Fetch comments from the post's RSS feed.

    Reddit RSS does not guarantee score ordering.
    The first returned comments are used.
    """
    post_url = post["link"].rstrip("/")
    comment_url = post_url + ".rss"

    try:
        response_entries = fetch_feed(comment_url)

        comments = [
            entry for entry in response_entries
            if entry["link"]
            and entry["link"] != post["link"]
            and "/comments/" in entry["link"]
        ]

        return comments[:COMMENT_LIMIT]

    except Exception as exc:
        print(f"    Comment fetch failed: {exc}")
        return []


# ============================================================
# HTML rendering
# ============================================================

def render_post_stats(post, stats, comments):
    score = (
        f'{stats["score"]:,}'
        if stats["score"] is not None
        else "N/A"
    )

    ratio = (
        f'{stats["upvote_ratio"] * 100:.0f}%'
        if stats["upvote_ratio"] is not None
        else "N/A"
    )

    comment_count = (
        f'{stats["num_comments"]:,}'
        if stats["num_comments"] is not None
        else "N/A"
    )

    return f"""
    <div class="post-stats">
      <h3>📊 Post statistics</h3>
      <ul>
        <li><b>Author:</b> {safe_html(post["author"] or "N/A")}</li>
        <li><b>Score:</b> {score}</li>
        <li><b>Upvote ratio:</b> {ratio}</li>
        <li><b>Total Reddit comments:</b> {comment_count}</li>
        <li><b>Replies fetched:</b> {len(comments)}</li>
        <li><b>Replies published:</b> {len(comments)}</li>
      </ul>
    </div>
    <hr/>
    """


def render_comments(comments):
    if not comments:
        return "<h3>💬 Replies</h3><p>No replies available.</p>"

    body = f"<h3>💬 Replies ({len(comments)})</h3>"

    for index, comment in enumerate(comments, start=1):
        author = safe_html(
            comment["author"] or "Reddit user"
        )

        link = safe_html(comment["link"])

        body += f"""
        <div class="reddit-comment">
          <h4>Reply {index} — {author}</h4>
          <p><a href="{link}">View reply on Reddit</a></p>
          <blockquote>{comment["body"] or ""}</blockquote>
        """

        for image_url in comment["images"]:
            safe_url = safe_html(image_url)

            body += (
                f'<p><img src="{safe_url}" '
                f'alt="Reddit reply image" /></p>'
            )

        body += "</div><hr/>"

    return body


# ============================================================
# RSS item generation
# ============================================================

def build_item(parent, post, stats, comments):
    node = ET.SubElement(parent, "item")

    add_text(node, "title", "🍿 " + post["title"])
    add_text(node, "link", post["link"])
    add_text(node, "guid", post["id"] or post["link"])

    add_text(
        node,
        "pubDate",
        email.utils.format_datetime(
            parse_datetime(post["published"])
        ),
    )

    body = post["body"] or ""

    # Post images.
    for image_url in post["images"]:
        safe_url = safe_html(image_url)

        body += (
            f'<p><img src="{safe_url}" '
            f'alt="Reddit post image" /></p>'
        )

    # Post-level statistics.
    body += render_post_stats(post, stats, comments)

    # Nested replies.
    body += render_comments(comments)

    add_html(node, CONTENT_NS + "encoded", body)

    # Media RSS metadata.
    all_images = list(post["images"])

    for comment in comments:
        all_images.extend(comment["images"])

    for image_url in dict.fromkeys(all_images):
        media = ET.SubElement(
            node,
            MEDIA_NS + "content",
        )

        media.set("url", image_url)
        media.set("medium", "image")

    return node


# ============================================================
# Feed generation
# ============================================================

def write_feed(subreddit, posts, post_data):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    output_path = OUTPUT_DIR / f"{subreddit}.xml"

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    add_text(
        channel,
        "title",
        f"🍿 Reddit: r/{subreddit}",
    )

    add_text(
        channel,
        "link",
        f"{BASE_URL}/r/{subreddit}/",
    )

    add_text(
        channel,
        "description",
        f"Top {len(posts)} daily posts from r/{subreddit}, "
        "with individual post statistics and nested replies.",
    )

    add_text(
        channel,
        "lastBuildDate",
        email.utils.format_datetime(
            datetime.now(timezone.utc)
        ),
    )

    for post in posts:
        data = post_data[post["id"]]

        build_item(
            parent=channel,
            post=post,
            stats=data["stats"],
            comments=data["comments"],
        )

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")

    temp_path = output_path.with_suffix(".xml.tmp")

    tree.write(
        temp_path,
        encoding="utf-8",
        xml_declaration=True,
    )

    temp_path.replace(output_path)

    print(f"Feed written: {output_path}")


# ============================================================
# Subreddit processing
# ============================================================

def load_subreddits():
    if not SUBREDDITS_FILE.exists():
        raise FileNotFoundError(
            f"Missing input file: {SUBREDDITS_FILE}"
        )

    subreddits = []

    for line in SUBREDDITS_FILE.read_text().splitlines():
        value = line.strip()

        if not value or value.startswith("#"):
            continue

        value = value.removeprefix("r/").strip("/")

        if value:
            subreddits.append(value)

    return list(dict.fromkeys(subreddits))


def process_subreddit(subreddit):
    print(f"\n{'=' * 50}")
    print(f"Processing r/{subreddit}")
    print(f"{'=' * 50}")

    posts = fetch_posts(subreddit)

    if not posts:
        print("No posts found.")
        return

    post_data = {}

    for index, post in enumerate(posts):
        if index > 0:
            time.sleep(REQUEST_DELAY)

        print(f"\nPost {index + 1}: {post['title']}")

        # Fetch statistics for this individual post.
        stats = fetch_post_stats(post)

        # Fetch replies for this individual post.
        time.sleep(REQUEST_DELAY)
        comments = fetch_top_comments(post)

        post_data[post["id"]] = {
            "stats": stats,
            "comments": comments,
        }

        print(f"  Score: {stats['score']}")
        print(f"  Upvote ratio: {stats['upvote_ratio']}")
        print(f"  Total Reddit comments: {stats['num_comments']}")
        print(f"  Replies published: {len(comments)}")

    write_feed(
        subreddit=subreddit,
        posts=posts,
        post_data=post_data,
    )


# ============================================================
# Main
# ============================================================

def main():
    subreddits = load_subreddits()

    if not subreddits:
        print("No subreddits configured.")
        return

    print(f"Subreddits: {subreddits}")

    failed = []

    for index, subreddit in enumerate(subreddits):
        if index > 0:
            time.sleep(REQUEST_DELAY)

        try:
            process_subreddit(subreddit)

        except Exception as exc:
            print(f"ERROR processing r/{subreddit}: {exc}")
            failed.append(subreddit)

    print("\n" + "=" * 50)
    print("RUN SUMMARY")
    print("=" * 50)
    print(f"Subreddits processed: {len(subreddits) - len(failed)}")
    print(f"Subreddits failed: {len(failed)}")

    if failed:
        print(f"Failed: {', '.join(failed)}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
