#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///

"""Build one RSS feed per subreddit from Reddit's public JSON listing.

This MVP makes ordinary unauthenticated GET requests to Reddit's .json
endpoints. Reddit may block these requests. It does not attempt to bypass
blocks, login walls, rate limits, or access controls.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

APP_VERSION = "0.1.0"
BASE_URL = "https://www.reddit.com"
USER_AGENT = f"reddit2rss/{APP_VERSION} (Python requests; unauthenticated JSON MVP)"
POST_LIMIT = 100
COMMENT_LIMIT = 100
REQUEST_TIMEOUT = 20

NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
NS_MEDIA = "http://search.yahoo.com/mrss/"
ET.register_namespace("content", NS_CONTENT)
ET.register_namespace("media", NS_MEDIA)


class RedditFetchError(RuntimeError):
    """A Reddit endpoint could not be read without bypassing its restrictions."""


def normalize_subreddit(value: str) -> str:
    """Accept `subreddit`, `r/subreddit`, or a subreddit URL."""
    candidate = value.strip()
    candidate = re.sub(r"^https?://(?:www\.)?reddit\.com/r/", "", candidate)
    candidate = re.sub(r"^r/", "", candidate, flags=re.IGNORECASE)
    candidate = candidate.strip("/ ")
    if not re.fullmatch(r"[A-Za-z0-9_]{2,21}", candidate):
        raise argparse.ArgumentTypeError(f"Invalid subreddit name: {value!r}")
    return candidate


def read_subreddits(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Subreddit list not found: {path}")
    names: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = normalize_subreddit(line)
        key = name.casefold()
        if key not in seen:
            names.append(name)
            seen.add(key)
    return names


def _get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    """Make one transparent request; do not retry or disguise blocked requests."""
    try:
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RedditFetchError(f"Request failed for {url}: {exc}") from exc

    if response.status_code in (401, 403):
        raise RedditFetchError(
            f"Reddit returned HTTP {response.status_code} for {url}. "
            "The unauthenticated .json endpoint is not available to this run; "
            "the script will not try to bypass the restriction."
        )
    if response.status_code == 429:
        raise RedditFetchError(
            f"Reddit returned HTTP 429 for {url}. Rate/access limit reached; "
            "this MVP does not retry or work around it."
        )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RedditFetchError(f"Reddit returned {response.status_code} for {url}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise RedditFetchError(
            f"Reddit did not return JSON for {url} (possibly a login or block page)."
        ) from exc

    if not isinstance(payload, (dict, list)):
        raise RedditFetchError(f"Unexpected JSON response from {url}")
    return payload


def fetch_top_posts(subreddit: str, now: datetime, window_hours: int = 24) -> list[dict[str, Any]]:
    """Fetch top-of-day posts and enforce a rolling 24-hour cutoff locally."""
    url = f"{BASE_URL}/r/{quote(subreddit, safe='')}/.json"
    payload = _get_json(
        url,
        params={"limit": POST_LIMIT, "sort": "top", "t": "day", "raw_json": 1},
    )
    try:
        children = payload["data"]["children"]
    except (KeyError, TypeError) as exc:
        raise RedditFetchError(f"Unexpected post listing structure for r/{subreddit}") from exc

    cutoff = now.timestamp() - window_hours * 3600
    posts: list[dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t3":
            continue
        data = child.get("data") or {}
        created = data.get("created_utc")
        if not isinstance(created, (int, float)) or created < cutoff:
            continue
        if data.get("removed_by_category") or data.get("selftext") == "[removed]":
            continue
        posts.append(data)

    posts.sort(key=lambda item: (int(item.get("score") or 0), float(item.get("created_utc") or 0)), reverse=True)
    return posts[:2]


def fetch_top_comments(subreddit: str, post_id: str) -> list[dict[str, Any]]:
    url = f"{BASE_URL}/comments/{quote(post_id, safe='')}.json"
    payload = _get_json(
        url,
        params={"limit": COMMENT_LIMIT, "sort": "top", "raw_json": 1},
    )
    if not isinstance(payload, list) or len(payload) < 2:
        raise RedditFetchError(f"Unexpected comments response for r/{subreddit} post {post_id}")

    listing = payload[1]
    try:
        children = listing["data"]["children"]
    except (KeyError, TypeError) as exc:
        raise RedditFetchError(f"Unexpected comments listing for post {post_id}") from exc

    comments: list[dict[str, Any]] = []
    for child in children:
        if not isinstance(child, dict) or child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        # Only top-level comments; ignore nested replies and deleted placeholders.
        if int(data.get("depth", 0) or 0) != 0:
            continue
        body = data.get("body") or ""
        if not body or body in ("[deleted]", "[removed]"):
            continue
        comments.append(data)

    comments.sort(key=lambda item: (int(item.get("score") or 0), float(item.get("created_utc") or 0)), reverse=True)
    return comments[:2]


def _clean_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = html.unescape(value.strip())
    if value.startswith("https://") or value.startswith("http://"):
        return value
    return None


def extract_image_urls(post: dict[str, Any]) -> list[str]:
    """Collect direct, preview, and gallery image URLs without downloading them."""
    urls: list[str] = []

    def add(value: Any) -> None:
        url = _clean_url(value)
        if url and url not in urls:
            urls.append(url)

    # Reddit gallery image URLs, generally ordered according to gallery_data.items.
    metadata = post.get("media_metadata") or {}
    gallery = post.get("gallery_data") or {}
    for item in gallery.get("items", []) if isinstance(gallery, dict) else []:
        media_id = item.get("media_id") if isinstance(item, dict) else None
        media = metadata.get(media_id, {}) if isinstance(metadata, dict) else {}
        source = media.get("s", {}) if isinstance(media, dict) else {}
        add(source.get("u") if isinstance(source, dict) else None)
        if not urls and isinstance(media, dict):
            previews = media.get("p") or []
            if previews:
                add(previews[-1].get("u"))

    # Standard Reddit preview images.
    preview = post.get("preview") or {}
    for image in preview.get("images", []) if isinstance(preview, dict) else []:
        source = image.get("source", {}) if isinstance(image, dict) else {}
        add(source.get("url") if isinstance(source, dict) else None)

    # Direct image URLs (i.redd.it, preview.redd.it, or external image files).
    direct = _clean_url(post.get("url_overridden_by_dest") or post.get("url"))
    if direct:
        path = direct.split("?", 1)[0].lower()
        if "i.redd.it/" in direct or "preview.redd.it/" in direct or path.endswith(
            (".jpg", ".jpeg", ".png", ".gif", ".webp")
        ):
            add(direct)

    # Reddit's thumbnail is useful for video posts when no preview is exposed.
    if not urls and post.get("is_video"):
        add(post.get("thumbnail"))

    return urls


def _reddit_html(raw_html: str | None, fallback_text: str | None) -> str:
    if raw_html:
        # Reddit's `*_html` fields are HTML-escaped inside JSON.
        return html.unescape(raw_html)
    if fallback_text:
        return "<p>" + html.escape(fallback_text).replace("\n", "<br/>") + "</p>"
    return ""


def _post_permalink(subreddit: str, post: dict[str, Any]) -> str:
    permalink = post.get("permalink")
    if isinstance(permalink, str) and permalink.startswith("/"):
        return BASE_URL + permalink
    post_id = post.get("id", "")
    return f"{BASE_URL}/r/{quote(subreddit)}/comments/{quote(str(post_id))}/"


def _format_post_html(subreddit: str, post: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    body = _reddit_html(post.get("selftext_html"), post.get("selftext"))
    if body:
        parts.append(body)

    for image_url in extract_image_urls(post):
        safe_url = html.escape(image_url, quote=True)
        parts.append(
            f'<p><a href="{safe_url}"><img src="{safe_url}" '
            'alt="Reddit post image" style="max-width:100%;height:auto;" /></a></p>'
        )

    destination = post.get("url_overridden_by_dest") or post.get("url")
    destination_url = _clean_url(destination)
    if destination_url and destination_url != _post_permalink(subreddit, post):
        safe_destination = html.escape(destination_url, quote=True)
        parts.append(f'<p>Link: <a href="{safe_destination}">{safe_destination}</a></p>')

    post_url = html.escape(_post_permalink(subreddit, post), quote=True)
    author = html.escape(str(post.get("author") or "[deleted]"))
    score = int(post.get("score") or 0)
    parts.append(
        f'<p><strong>r/{html.escape(subreddit)} · {score} points · '
        f'u/{author}</strong> · <a href="{post_url}">View post on Reddit</a></p>'
    )

    parts.append("<h3>Top comments</h3>")
    if not comments:
        parts.append("<p>No visible top-level comments.</p>")
    for index, comment in enumerate(comments, start=1):
        comment_body = _reddit_html(comment.get("body_html"), comment.get("body"))
        commenter = html.escape(str(comment.get("author") or "[deleted]"))
        comment_score = int(comment.get("score") or 0)
        comment_id = str(comment.get("id") or "")
        comment_url = _post_permalink(subreddit, post)
        if comment_id:
            comment_url = comment_url.rstrip("/") + "/" + quote(comment_id) + "/"
        safe_comment_url = html.escape(comment_url, quote=True)
        parts.append(
            f'<section><p><strong>Comment #{index} · {comment_score} points · '
            f'u/{commenter}</strong></p>{comment_body}'
            f'<p><a href="{safe_comment_url}">View comment</a></p></section>'
        )

    return "\n".join(parts)


def _rss_date(timestamp: float) -> str:
    return format_datetime(datetime.fromtimestamp(timestamp, tz=timezone.utc))


def build_feed_xml(subreddit: str, posts_with_comments: list[tuple[dict[str, Any], list[dict[str, Any]]]], now: datetime) -> bytes:
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = f"r/{subreddit} — Reddit2RSS"
    ET.SubElement(channel, "link").text = f"{BASE_URL}/r/{subreddit}/"
    ET.SubElement(channel, "description").text = (
        f"Top 2 posts from r/{subreddit} in the previous 24 hours, "
        "including up to two top-level comments per post."
    )
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now.astimezone(timezone.utc))

    for post, comments in posts_with_comments:
        permalink = _post_permalink(subreddit, post)
        title = str(post.get("title") or "(untitled Reddit post)")
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = permalink
        guid = ET.SubElement(item, "guid", {"isPermaLink": "true"})
        guid.text = permalink
        created = post.get("created_utc")
        if isinstance(created, (int, float)):
            ET.SubElement(item, "pubDate").text = _rss_date(float(created))

        content_html = _format_post_html(subreddit, post, comments)
        ET.SubElement(item, "description").text = content_html
        ET.SubElement(item, f"{{{NS_CONTENT}}}encoded").text = content_html

        for image_url in extract_image_urls(post):
            ET.SubElement(
                item,
                f"{{{NS_MEDIA}}}content",
                {"url": image_url, "medium": "image"},
            )

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def update_subreddit(subreddit: str, output_dir: Path, now: datetime, window_hours: int = 24) -> int:
    posts = fetch_top_posts(subreddit, now=now, window_hours=window_hours)
    posts_with_comments = [(post, fetch_top_comments(subreddit, str(post.get("id", "")))) for post in posts]

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{subreddit.lower()}.xml"
    xml_bytes = build_feed_xml(subreddit, posts_with_comments, now=now)
    output_path.write_bytes(xml_bytes)
    return len(posts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subreddits", nargs="*", help="Optional subreddit names; defaults to subreddits.txt")
    parser.add_argument("--subreddits-file", type=Path, default=Path("subreddits.txt"))
    parser.add_argument("--output-dir", type=Path, default=Path("docs"))
    parser.add_argument("--window-hours", type=int, default=24)
    args = parser.parse_args(argv)

    if args.window_hours <= 0:
        parser.error("--window-hours must be positive")

    try:
        names = [normalize_subreddit(name) for name in args.subreddits] if args.subreddits else read_subreddits(args.subreddits_file)
    except (OSError, argparse.ArgumentTypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if not names:
        print("No subreddits configured.", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    failures: list[str] = []
    success_count = 0
    for subreddit in names:
        try:
            count = update_subreddit(subreddit, args.output_dir, now, args.window_hours)
            print(f"{subreddit}: wrote {count} posts to {args.output_dir / (subreddit.lower() + '.xml')}", flush=True)
            success_count += 1
        except Exception as exc:  # keep processing other configured subreddits
            failures.append(subreddit)
            print(f"{subreddit}: FAILED; existing feed kept. {exc}", file=sys.stderr, flush=True)
        # Small, fixed pause between subreddit jobs; no retry on blocked requests.
        time.sleep(1)

    print(f"SUMMARY: {success_count}/{len(names)} subreddit feeds updated.")
    if failures:
        print(f"Failed: {', '.join(failures)}", file=sys.stderr)
    return 0 if success_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
