# reddit2rss

A small Python + GitHub Actions project that creates one RSS feed per subreddit and publishes the XML files through GitHub Pages. It follows the lightweight structure of [`pdotxyz/tg2rss`](https://github.com/pdotxyz/tg2rss).

## MVP behavior

- Reads subreddit names from `subreddits.txt` (one per line).
- Requests Reddit's public `/.json` listing (`limit=100&sort=top&t=day`), following the endpoint pattern in the r/vending example.
- Keeps the two highest-scoring posts published within the previous rolling 24 hours.
- Fetches the comments for those two posts and keeps the two highest-scoring top-level comments per post.
- Writes one file per subreddit, for example `docs/vending.xml`.
- Embeds Reddit-hosted and supported direct image URLs in the HTML content, and emits `media:content` metadata.
- Runs daily at 08:15 America/Los_Angeles; `workflow_dispatch` supports manual runs.

## Important access limitation

This MVP makes ordinary, unauthenticated requests to Reddit `.json` URLs because that is the requested prototype source. It sends a clearly identified `reddit2rss` User-Agent and does **not** impersonate a browser, use proxies, retry around blocks, or bypass login/access controls.

Reddit's current Responsible Builder Policy says approval is required before accessing Reddit data through its API. Reddit's May 28, 2026 announcement also says it is deprecating unauthenticated `.json` access. Reddit may return HTTP 401/403/429 or a non-JSON block/login page, especially from GitHub-hosted runners. When that happens, the script reports the failure and keeps the prior feed file unchanged. This MVP is therefore an implementation prototype, not a promise that scheduled unauthenticated fetching will continue to work.

Before publishing Reddit content publicly or using this beyond a local experiment, review Reddit's current policies and obtain the required access/approval.

## Configure

Edit `subreddits.txt`:

```text
vending
overemployed
MachineLearning
```

Bare names, `r/name`, and `https://www.reddit.com/r/name/` are accepted. Lines beginning with `#` are comments.

## Run locally

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv run reddit2rss.py
```

Run selected subreddits:

```bash
uv run reddit2rss.py vending overemployed
```

The feed files are written to `docs/` by default. You can override the output directory:

```bash
uv run reddit2rss.py --subreddits-file subreddits.txt --output-dir docs
```

## Test

```bash
uv run --with requests python -m unittest discover -s tests -v
```

The tests use mocked Reddit responses and do not require Reddit network access.

## GitHub Pages setup

1. Create a new public GitHub repository named `reddit2rss` under your account.
2. Add these files to its `main` branch.
3. In **Settings → Pages**, choose **Deploy from a branch**, select `main` and `/docs`.
4. In **Actions**, enable workflows and manually run **reddit2rss** once.
5. After a successful run, subscribe to:

   `https://<github-username>.github.io/reddit2rss/vending.xml`

   Replace `vending` with any configured subreddit.

No secrets are required by this unauthenticated JSON MVP. If Reddit blocks the request, the workflow will show the HTTP status and will not attempt to evade it.

## Current limits

- Top posts are selected from Reddit's `top` listing for `t=day`, with an additional rolling 24-hour timestamp check. The source listing is capped at 100 posts.
- At most two comments are emitted per post; only top-level comments are considered.
- Image URLs are embedded remotely, not downloaded or mirrored. Whether they render depends on the RSS reader and Reddit's image-host availability.
- Video posts include an available thumbnail only; the video itself is linked, not re-hosted.
- Failed subreddit updates leave that subreddit’s previous XML file in place; other subreddits continue processing.
