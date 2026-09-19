#!/usr/bin/env python3
"""
Keeps the Threads access token alive.

A long-lived token lasts 60 days and can be swapped for a fresh 60 days at any
point. This runs weekly, so the token is never anywhere near expiry, and Julia
never has to think about it.

It writes the new token straight back into the repository's secrets using the
GitHub API, which needs a personal access token in the GH_PAT secret with
permission to update secrets on this repository.

If GH_PAT is missing it still refreshes and prints when the new token expires,
so a human can paste it in. It never prints the token itself.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode

API = "https://graph.threads.net/v1.0"


def refresh(token: str) -> dict:
    url = f"{API}/refresh_access_token?" + urllib.parse.urlencode(
        {"grant_type": "th_refresh_token", "access_token": token}
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


def store(new_token: str, repo: str, gh_token: str) -> None:
    """Write the token back into the repo's Actions secrets, encrypted."""
    try:
        from nacl import encoding, public  # type: ignore
    except ImportError:
        print("pynacl not installed, cannot write the secret back", file=sys.stderr)
        raise

    def gh(method, path, body=None):
        req = urllib.request.Request(
            f"https://api.github.com{path}",
            data=json.dumps(body).encode() if body else None,
            method=method,
            headers={
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}

    key = gh("GET", f"/repos/{repo}/actions/secrets/public-key")
    sealed = public.SealedBox(
        public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    ).encrypt(new_token.encode())

    gh("PUT", f"/repos/{repo}/actions/secrets/THREADS_ACCESS_TOKEN", {
        "encrypted_value": b64encode(sealed).decode(),
        "key_id": key["key_id"],
    })
    print("token refreshed and stored")


def main() -> int:
    token = os.environ.get("THREADS_ACCESS_TOKEN", "").strip()
    if not token:
        print("THREADS_ACCESS_TOKEN is not set", file=sys.stderr)
        return 2

    try:
        result = refresh(token)
    except urllib.error.HTTPError as exc:
        print(f"refresh failed: {exc.code} {exc.read().decode(errors='replace')}",
              file=sys.stderr)
        return 1

    new_token = result.get("access_token")
    expires_in = int(result.get("expires_in", 0))
    if not new_token:
        print(f"no token came back: {result}", file=sys.stderr)
        return 1

    print(f"new token is good for {expires_in // 86400} days")

    gh_token = os.environ.get("GH_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if gh_token and repo:
        store(new_token, repo, gh_token)
    else:
        print("no GH_PAT set, so the new token was not saved. "
              "Paste it into the THREADS_ACCESS_TOKEN secret by hand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
