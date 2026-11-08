
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


def add_cdata_html(parent, tag, value):
    # ElementTree safely escapes HTML as XML text.
    element = ET.SubElement(parent, tag)
    element.text = value or ""
    return element


# ============================================================
# Date and Atom RSS parsing
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
    """Extract HTML content from an Atom entry."""
    ns = "{http://www.w3.org/2005/Atom}"

    for key in ("content", "summary"):
        value = entry.find(f"{ns}{key}")

        if value is not None:
            return "".join(value.itertext()).strip()

    return ""


def entry_images(entry, body=""):
    """Extract image URLs from an Atom entry."""
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
    """Convert one Atom entry into a dictionary."""
    ns = "{http://www.w3.org/2005/Atom}"

    title = entry.findtext(
        f"{ns}title",
        default="",
    ).strip()

    link = ""

    for element in entry.findall(f"{ns}link"):
        if element.attrib.get("rel", "alternate") == "alternate":
            link = element.attrib.get("href", "")
            break

    if not link:
        link_element = entry.find(f"{ns}link")

        if link_element is not None:
            link = link_element.attrib.get("href", "")

    link = urljoin(BASE_URL, link)

    author_element = entry.find(f"{ns}author")
    author = ""

    if author_element is not None:
        author = author_element.findtext(
            f"{ns}name",
            default="",
        ).strip()

    published = (
        entry.findtext(f"{ns}published")
        or entry.findtext(f"{ns}updated")
        or ""
    )

    body = entry_html(entry)

    return {
        "id": entry.findtext(
            f"{ns}id",
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
    """Fetch and parse a Reddit Atom RSS feed."""
    response = session.get(url, timeout=30)
    response.raise_for_status()

    root = ET.fromstring(response.content)

    ns = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(f"{ns}entry")

    return [parse_entry(entry) for entry in entries]


# ============================================================
# Reddit fetching
# ============================================================

def fetch_posts(subreddit):
    """Fetch top posts from the daily subreddit feed."""
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

    total_posts = len(posts)

    selected_posts = posts[:POST_LIMIT]

    print(f"  Posts found: {total_posts}")
    print(f"  Posts selected: {len(selected_posts)}")

    return selected_posts, total_posts


def fetch_top_comments(post):
    """
    Fetch comments from a post's RSS feed.

    Reddit RSS does not reliably expose comment scores.
    Therefore, these are the first comments returned,
    not guaranteed highest-scoring comments.
    """
    post_url = post["link"].rstrip("/")
    comment_url = post_url + ".rss"

    print(f"  Fetching comments: {post_url}")

    time.sleep(REQUEST_DELAY)

    try:
        entries = fetch_feed(comment_url)

        comments = [
            entry for entry in entries
            if entry["link"]
            and entry["link"] != post["link"]
            and "/comments/" in entry["link"]
        ]

        total_comments = len(comments)
        selected_comments = comments[:COMMENT_LIMIT]

        print(f"    Comments found: {total_comments}")
        print(f"    Comments selected: {len(selected_comments)}")

        return selected_comments, total_comments

    except Exception as exc:
        print(f"    Comment fetch failed: {exc}")
        return [], 0


# ============================================================
# RSS item generation
# ============================================================

def build_item(parent, post, comments=None):
    """Create one RSS item with nested comments."""
    comments = comments or []

    node = ET.SubElement(parent, "item")

    add_text(node, "title", "🍿 " + post["title"])
    add_text(node, "link", post["link"])
    add_text(node, "guid", post["id"] or post["link"])

    published = parse_datetime(post["published"])

    add_text(
        node,
        "pubDate",
        email.utils.format_datetime(published),
    )

    if post["author"]:
        add_text(node, "author", post["author"])

    body = post["body"] or ""

    # Post images.
    for image_url in post["images"]:
        safe_url = html.escape(image_url, quote=True)

        body += (
            f'<p><img src="{safe_url}" '
            f'alt="Reddit post image" /></p>'
        )

    # Nested comments.
    if comments:
        body += (
            "<hr/>"
            f"<h3>Comments ({len(comments)})</h3>"
        )

        for index, comment in enumerate(comments, start=1):
            author = html.escape(
                comment["author"] or "Reddit user"
            )

            link = html.escape(
                comment["link"],
                quote=True,
            )

            body += f"""
            <div class="reddit-comment">
              <h4>Comment {index} by {author}</h4>
              <p><a href="{link}">View comment on Reddit</a></p>
              <blockquote>{comment["body"] or ""}</blockquote>
            """

            for image_url in comment["images"]:
                safe_url = html.escape(
                    image_url,
                    quote=True,
                )

                body += (
                    f'<p><img src="{safe_url}" '
                    f'alt="Reddit comment image" /></p>'
                )

            body += "</div><hr/>"

    add_cdata_html(
        node,
        CONTENT_NS + "encoded",
        body,
    )

    # Media RSS image metadata.
    all_images = list(post["images"])

    for comment in comments:
        all_images.extend(comment["images"])

    all_images = list(dict.fromkeys(all_images))

    for image_url in all_images:
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

def write_feed(
    subreddit,
    posts,
    comments_by_post,
    post_stats,
    comment_stats,
):
    """Write one RSS feed for a subreddit."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    output_path = OUTPUT_DIR / f"{subreddit}.xml"

    total_comments_found = sum(comment_stats.values())
    total_comments_included = sum(
        len(comments)
        for comments in comments_by_post.values()
    )

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

    description = (
        f"Top posts of the day from r/{subreddit}. "
        f"Posts: {len(posts)}/{post_stats}. "
        f"Comments: {total_comments_included}/"
        f"{total_comments_found}."
    )

    add_text(channel, "description", description)

    add_text(
        channel,
        "lastBuildDate",
        email.utils.format_datetime(
            datetime.now(timezone.utc)
        ),
    )

    for post in posts:
        comments = comments_by_post.get(post["id"], [])
        build_item(channel, post, comments)

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")

    temp_path = output_path.with_suffix(".xml.tmp")

    tree.write(
        temp_path,
        encoding="utf-8",
        xml_declaration=True,
    )

    temp_path.replace(output_path)

    print(f"  Feed written: {output_path}")
    print(
        f"  Final stats: "
        f"{len(posts)} posts, "
        f"{total_comments_included} comments"
    )


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
    """Fetch posts/comments and update one subreddit feed."""
    print(f"\n{'=' * 50}")
    print(f"Processing r/{subreddit}")
    print(f"{'=' * 50}")

    posts, total_posts = fetch_posts(subreddit)

    if not posts:
        print(f"No posts found for r/{subreddit}")
        return

    comments_by_post = {}
    comment_stats = {}

    for index, post in enumerate(posts):
        if index > 0:
            time.sleep(REQUEST_DELAY)

        comments, total_comments = fetch_top_comments(post)

        comments_by_post[post["id"]] = comments
        comment_stats[post["id"]] = total_comments

    write_feed(
        subreddit=subreddit,
        posts=posts,
        comments_by_post=comments_by_post,
        post_stats=total_posts,
        comment_stats=comment_stats,
    )

    print(f"\nStatistics for r/{subreddit}:")
    print(f"  Posts found: {total_posts}")
    print(f"  Posts published: {len(posts)}")
    print(
        "  Comments found: "
        f"{sum(comment_stats.values())}"
    )
    print(
        "  Comments published: "
        f"{sum(len(c) for c in comments_by_post.values())}"
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
