#!/usr/bin/env python3
"""
Flying Dutchman Furniture - Threads keyword auto-reply.

What it does, in plain terms:
  Every few minutes it looks at Julia's recent Threads posts, finds comments
  that are just one of her offer keywords (GRIT, ORDER, ...), and replies to
  that person with the matching free link.

Two things worth knowing:
  * Threads has no DM API. Replies are PUBLIC, underneath the person's comment.
  * It is stateless. Before replying it checks whether Julia has already
    replied to that comment, so nobody gets answered twice even if the
    script restarts or runs twice at once.

Environment variables:
  THREADS_USER_ID       Julia's Threads user id (numeric)
  THREADS_ACCESS_TOKEN  Long-lived access token
  THREADS_DRY_RUN       "1" to log what it would send without sending
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://graph.threads.net/v1.0"
CONFIG_PATH = Path(__file__).with_name("config.json")

log = logging.getLogger("threads-autoreply")


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class ThreadsError(RuntimeError):
    pass


def _request(method: str, path: str, params: dict, token: str, retries: int = 3):
    """One Threads API call, with retries on transient failures."""
    params = {**params, "access_token": token}
    url = f"{API}{path}"
    data = None

    if method == "GET":
        url = f"{url}?{urllib.parse.urlencode(params)}"
    else:
        data = urllib.parse.urlencode(params).encode()

    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            # 4xx other than rate-limiting will not get better by retrying.
            if 400 <= exc.code < 500 and exc.code != 429:
                raise ThreadsError(f"{method} {path} -> {exc.code}: {body}") from exc
            last = ThreadsError(f"{method} {path} -> {exc.code}: {body}")
        except (urllib.error.URLError, TimeoutError) as exc:
            last = ThreadsError(f"{method} {path} -> {exc}")

        if attempt < retries - 1:
            time.sleep(2 ** attempt)

    raise last if last else ThreadsError("request failed")


def get(path, params, token):
    return _request("GET", path, params, token)


def post(path, params, token):
    return _request("POST", path, params, token)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def normalise(text: str) -> str:
    """Lowercase, strip emoji/punctuation, collapse whitespace."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def match_keyword(text: str, keywords: dict) -> str | None:
    """
    Return the keyword this comment was aiming at, or None.

    Deliberately conservative. It fires on a bare keyword, or a keyword with
    filler around it ("grit please", "Grit. Thanks", "grit 🌹"), but NOT on a
    keyword buried in a real sentence, because a real sentence is a question
    for a human and should not get a canned link.
    """
    words = normalise(text).split()
    if not words or len(words) > MAX_WORDS:
        return None

    filler = {
        "please", "pls", "plz", "thanks", "thank", "you", "ty", "yes",
        "me", "i", "would", "like", "want", "need", "the", "a", "my",
        "send", "info", "hi", "hey", "hello", "gracias", "merci",
    }

    for keyword, entry in keywords.items():
        variants = {normalise(keyword)} | {normalise(v) for v in entry.get("variants", [])}
        variants.discard("")
        if not variants & set(words):
            continue
        # Everything else in the comment must be filler.
        leftovers = [w for w in words if w not in variants and w not in filler]
        if not leftovers:
            return keyword
    return None


MAX_WORDS = 6  # a comment longer than this is a person talking, not a keyword


# --------------------------------------------------------------------------
# Threads calls
# --------------------------------------------------------------------------

def recent_posts(user_id: str, token: str, limit: int) -> list[dict]:
    resp = get(f"/{user_id}/threads",
               {"fields": "id,permalink,timestamp,text", "limit": limit},
               token)
    return resp.get("data", [])


def conversation(post_id: str, token: str) -> list[dict]:
    """Every reply under a post, at any depth."""
    out, path = [], f"/{post_id}/conversation"
    params = {"fields": "id,text,username,timestamp,replied_to,is_reply", "limit": 100}
    while True:
        resp = get(path, params, token)
        out.extend(resp.get("data", []))
        nxt = resp.get("paging", {}).get("cursors", {}).get("after")
        if not nxt or len(out) > 1000:
            return out
        params = {**params, "after": nxt}


def already_answered(replies: list[dict], me: str) -> set[str]:
    """Comment ids that Julia has already replied to."""
    answered = set()
    for r in replies:
        if (r.get("username") or "").lower() != me.lower():
            continue
        parent = (r.get("replied_to") or {}).get("id")
        if parent:
            answered.add(parent)
    return answered


def send_reply(user_id: str, reply_to_id: str, text: str, token: str) -> str:
    """Two steps: build the container, then publish it."""
    container = post(f"/{user_id}/threads",
                     {"media_type": "TEXT", "text": text, "reply_to_id": reply_to_id},
                     token)
    creation_id = container.get("id")
    if not creation_id:
        raise ThreadsError(f"no creation id: {container}")
    time.sleep(1)  # Meta asks for a moment between create and publish
    published = post(f"/{user_id}/threads_publish", {"creation_id": creation_id}, token)
    return published.get("id", "")


def refresh_token(token: str) -> dict:
    return get("/refresh_access_token", {"grant_type": "th_refresh_token"}, token)


def whoami(token: str) -> str:
    """The token already knows whose it is, so we never have to be told."""
    return get("/me", {"fields": "id,username"}, token)["id"]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("THREADS_USER_ID", "").strip()
    dry_run = os.environ.get("THREADS_DRY_RUN", "") == "1"

    if not token:
        log.error("THREADS_ACCESS_TOKEN is not set")
        return 2

    if not user_id:
        # One less thing for anyone to copy and paste wrongly.
        try:
            user_id = whoami(token)
            log.info("resolved Threads user id from the token")
        except ThreadsError as exc:
            log.error("could not work out who this token belongs to: %s", exc)
            return 1

    cfg = json.loads(CONFIG_PATH.read_text())
    per_post = {k: v for k, v in cfg.get("posts", {}).items() if not k.startswith("_")}
    evergreen = {k: v for k, v in cfg.get("evergreen_keywords", {}).items()
                 if not k.startswith("_")}
    me = cfg["username"]
    pilot = cfg.get("pilot_post_ids") or []
    look_back = int(cfg.get("posts_to_check", 10))
    sign_off = cfg.get("reply_template", "Here you go: {link} See you in the garage! Julia")

    if dry_run:
        log.info("DRY RUN - nothing will actually be sent")
    if pilot:
        log.info("PILOT MODE - only these posts: %s", ", ".join(pilot))

    try:
        posts = recent_posts(user_id, token, look_back)
    except ThreadsError as exc:
        log.error("could not list posts: %s", exc)
        return 1

    if pilot:
        posts = [p for p in posts if p["id"] in pilot]

    sent = skipped = unmapped = 0

    for p in posts:
        # Which keywords does THIS reel use today? LinkDM is set up per reel,
        # so the same word means different things on different days. If the
        # sweep has not written this reel's entry yet, leave the reel alone
        # rather than guessing - a wrong link is worse than a late one.
        entry = per_post.get(p["id"])
        keywords = {k: v for k, v in (entry or {}).get("keywords", {}).items()
                    if isinstance(v, dict)}
        if not keywords:
            keywords = evergreen
        if not keywords:
            unmapped += 1
            log.info("post %s has no keyword mapping yet - skipping (%s)",
                     p["id"], (p.get("text") or "")[:60].replace("\n", " "))
            continue

        try:
            replies = conversation(p["id"], token)
        except ThreadsError as exc:
            log.error("post %s: could not read replies: %s", p["id"], exc)
            continue

        answered = already_answered(replies, me)

        for r in replies:
            if (r.get("username") or "").lower() == me.lower():
                continue
            if r["id"] in answered:
                continue

            keyword = match_keyword(r.get("text") or "", keywords)
            if not keyword:
                continue

            link = keywords[keyword]["link"]
            if not link:
                log.warning("%s asked for %s but no link is configured - skipping",
                            r.get("username"), keyword)
                skipped += 1
                continue

            text = sign_off.format(link=link)

            if dry_run:
                log.info("WOULD REPLY to @%s (%s): %s", r.get("username"), keyword, text)
                sent += 1
                continue

            try:
                new_id = send_reply(user_id, r["id"], text, token)
                log.info("replied to @%s (%s) -> %s", r.get("username"), keyword, new_id)
                sent += 1
                answered.add(r["id"])
                time.sleep(2)  # be gentle
            except ThreadsError as exc:
                log.error("could not reply to @%s: %s", r.get("username"), exc)
                skipped += 1

    log.info("done: %d replied, %d skipped, %d reels with no mapping yet, "
             "%d reels checked", sent, skipped, unmapped, len(posts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
