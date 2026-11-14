
import os
import re
import html
import logging
import requests
import xml.etree.ElementTree as ET

from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from feedgen.feed import FeedGenerator
from bs4 import BeautifulSoup

# ============================================================
# CONFIGURATION
# ============================================================

BASE_URL = "https://www.reddit.com"
USER_AGENT = "reddit2rss/1.0 (RSS feed generator)"

POST_LIMIT = 2
COMMENT_LIMIT = 2
FETCH_LIMIT = 100

OUTPUT_DIR = "."

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"
})


# ============================================================
# HELPERS
# ============================================================

def safe_html(value):
    return html.escape(str(value or ""))


def clean_text(value):
    if not value:
        return ""

    return BeautifulSoup(value, "html.parser").get_text(
        " ", strip=True
    )


def load_subreddits():
    if not os.path.exists("subreddits.txt"):
        raise FileNotFoundError("subreddits.txt not found")

    subreddits = []

    with open("subreddits.txt", encoding="utf-8") as f:
        for line in f:
            name = line.strip()

            if not name or name.startswith("#"):
                continue

            name = re.sub(r"^r/", "", name)
            subreddits.append(name)

    return list(dict.fromkeys(subreddits))


# ============================================================
# REDDIT RSS FETCHING
# ============================================================

def fetch_posts(subreddit):
    url = (
        f"{BASE_URL}/r/{subreddit}/top/.rss"
        f"?t=day&limit={FETCH_LIMIT}"
    )

    logging.info("Fetching daily Top feed: %s", subreddit)

    response = session.get(url, timeout=30)
    response.raise_for_status()

    root = ET.fromstring(response.content)

    posts = []

    for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        title = entry.findtext(
            "{http://www.w3.org/2005/Atom}title", ""
        )

        link_element = entry.find(
            "{http://www.w3.org/2005/Atom}link"
        )

        link = (
            link_element.attrib.get("href", "")
            if link_element is not None else ""
        )

        author_element = entry.find(
            "{http://www.w3.org/2005/Atom}author/"
            "{http://www.w3.org/2005/Atom}name"
        )

        author = (
            author_element.text
            if author_element is not None else "Unknown"
        )

        content_element = entry.find(
            "{http://www.w3.org/2005/Atom}content"
        )

        content = (
            content_element.text
            if content_element is not None else ""
        )

        updated = entry.findtext(
            "{http://www.w3.org/2005/Atom}updated", ""
        )

        if not link:
            continue

        posts.append({
            "title": title,
            "link": link,
            "author": author,
            "content": content,
            "updated": updated
        })

    # Preserve Reddit's returned Top feed ordering.
    for rank, post in enumerate(posts, start=1):
        post["top_rank"] = rank

    return posts[:POST_LIMIT]


# ============================================================
# COMMENTS
# ============================================================

def fetch_comments(post):
    """
    Retrieve comments from the post's RSS endpoint.

    Note: RSS does not guarantee that comments are ordered
    by score. These are the first two available replies.
    """

    url = post["link"].rstrip("/") + ".rss"

    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()

        root = ET.fromstring(response.content)

        comments = []

        for entry in root.findall(
            ".//{http://www.w3.org/2005/Atom}entry"
        ):
            title = entry.findtext(
                "{http://www.w3.org/2005/Atom}title", ""
            )

            content_element = entry.find(
                "{http://www.w3.org/2005/Atom}content"
            )

            content = (
                content_element.text
                if content_element is not None else ""
            )

            link_element = entry.find(
                "{http://www.w3.org/2005/Atom}link"
            )

            link = (
                link_element.attrib.get("href", "")
                if link_element is not None else ""
            )

            author_element = entry.find(
                "{http://www.w3.org/2005/Atom}author/"
                "{http://www.w3.org/2005/Atom}name"
            )

            author = (
                author_element.text
                if author_element is not None else "Unknown"
            )

            comments.append({
                "title": title,
                "content": content,
                "link": link,
                "author": author
            })

            if len(comments) >= COMMENT_LIMIT:
                break

        return comments

    except requests.RequestException as exc:
        logging.warning(
            "Could not fetch comments for %s: %s",
            post["link"], exc
        )
        return []

    except ET.ParseError as exc:
        logging.warning("Invalid comment RSS: %s", exc)
        return []


# ============================================================
# IMAGE EXTRACTION
# ============================================================

def extract_images(content):
    if not content:
        return []

    soup = BeautifulSoup(content, "html.parser")

    images = []

    for img in soup.find_all("img"):
        src = img.get("src")

        if src and src.startswith("https://"):
            if src not in images:
                images.append(src)

    return images


def render_images(content):
    images = extract_images(content)

    if not images:
        return ""

    output = ['<div class="reddit-images">']

    for image in images:
        output.append(
            f'<p><img src="{html.escape(image, quote=True)}" '
            f'alt="Reddit image" style="max-width:100%;height:auto;"></p>'
        )

    output.append("</div>")

    return "".join(output)


# ============================================================
# POST STATISTICS
# ============================================================

def render_post_stats(post, comments):
    rank = post["top_rank"]

    if rank % 10 == 1 and rank % 100 != 11:
        suffix = "st"
    elif rank % 10 == 2 and rank % 100 != 12:
        suffix = "nd"
    elif rank % 10 == 3 and rank % 100 != 13:
        suffix = "rd"
    else:
        suffix = "th"

    return f"""
    <div class="post-stats">
      <h3>📊 Post statistics</h3>
      <ul>
        <li><b>Author:</b> {safe_html(post["author"])}</li>
        <li><b>Popularity:</b> Reddit Top (day) #{rank}</li>
        <li><b>Ranking:</b> {rank}{suffix} in daily Top feed</li>
        <li><b>Replies fetched:</b> {len(comments)}</li>
        <li><b>Replies published:</b> {len(comments)}</li>
      </ul>
    </div>
    <hr/>
    """


# ============================================================
# COMMENT RENDERING
# ============================================================

def render_comments(comments):
    if not comments:
        return "<p>No comments available.</p>"

    output = [
        f"<h3>💬 Replies ({len(comments)})</h3>"
    ]

    for index, comment in enumerate(comments, start=1):
        author = safe_html(comment["author"])
        link = html.escape(comment["link"], quote=True)

        content = clean_text(comment["content"])

        output.append(f"""
        <h4>Reply {index} — {author}</h4>
        <p>
          <a href="{link}">View reply on Reddit</a>
        </p>
        <blockquote>
          {safe_html(content)}
        </blockquote>
        <hr/>
        """)

    return "".join(output)


# ============================================================
# RSS GENERATION
# ============================================================

def generate_feed(subreddit, posts):
    fg = FeedGenerator()

    fg.id(f"{BASE_URL}/r/{subreddit}/")
    fg.title(f"🍿 Reddit - r/{subreddit}")
    fg.link(
        href=f"{BASE_URL}/r/{subreddit}/",
        rel="alternate"
    )
    fg.description(
        f"Daily top Reddit posts from r/{subreddit}"
    )
    fg.language("en")

    for post in posts:
        comments = fetch_comments(post)

        content = f"""
        <h2>{safe_html(post["title"])}</h2>

        <p>
          <b>Author:</b> {safe_html(post["author"])}
        </p>

        <p>
          <a href="{html.escape(post["link"], quote=True)}">
            View original Reddit post
          </a>
        </p>

        {render_post_stats(post, comments)}

        <div class="post-content">
          {post["content"]}
        </div>

        {render_images(post["content"])}

        {render_comments(comments)}
        """

        fe = fg.add_entry()

        fe.id(post["link"])
        fe.title(
            f"[#{post['top_rank']}] {post['title']}"
        )
        fe.link(href=post["link"])
        fe.author(name=post["author"])

        try:
            if post["updated"]:
                dt = datetime.fromisoformat(
                    post["updated"].replace("Z", "+00:00")
                )
                fe.pubDate(dt)
        except (ValueError, TypeError):
            pass

        fe.content(content, type="CDATA")

        logging.info(
            "Published %s | Top rank #%s | Comments: %s",
            post["title"],
            post["top_rank"],
            len(comments)
        )

    filename = os.path.join(
        OUTPUT_DIR, f"{subreddit}.xml"
    )

    fg.rss_file(filename, pretty=True)

    logging.info("Generated feed: %s", filename)


# ============================================================
# MAIN
# ============================================================

def process_subreddit(subreddit):
    logging.info("=" * 60)
    logging.info("Processing r/%s", subreddit)

    try:
        posts = fetch_posts(subreddit)

        if not posts:
            logging.warning(
                "No daily Top posts found for r/%s",
                subreddit
            )
            return

        generate_feed(subreddit, posts)

        logging.info(
            "Successfully processed r/%s: %s posts",
            subreddit, len(posts)
        )

    except requests.RequestException as exc:
        logging.error(
            "Reddit request failed for r/%s: %s",
            subreddit, exc
        )

    except Exception:
        logging.exception(
            "Unexpected error processing r/%s",
            subreddit
        )


def main():
    subreddits = load_subreddits()

    logging.info(
        "Starting Reddit2RSS for %s subreddits",
        len(subreddits)
    )

    for subreddit in subreddits:
        process_subreddit(subreddit)

    logging.info("Reddit2RSS completed.")


if __name__ == "__main__":
    main()
