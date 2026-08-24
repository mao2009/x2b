#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
BASE_DIR.mkdir(parents=True, exist_ok=True)

ENV_FILE = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / "seen.json"

load_dotenv(ENV_FILE)

LIST_ID = os.getenv("X_LIST_ID")

if not LIST_ID:
    print("ERROR: X_LIST_ID is not set.")
    sys.exit(1)


def main():

    print("Fetching current X list...")

    result = subprocess.run(
        [
            "twitter",
            "list",
            LIST_ID,
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    data = json.loads(result.stdout)

    if not data.get("ok"):
        raise RuntimeError(
            "twitter-cli returned ok=false"
        )

    posts = data.get("data", [])

    # リツイートは記録対象から除外
    posts = [
        post
        for post in posts
        if not post.get(
            "isRetweet",
            False
        )
    ]

    ids = [
        str(post["id"])
        for post in posts
        if post.get("id")
    ]

    STATE_FILE.write_text(
        json.dumps(
            ids,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    print()
    print(f"Initialized: {len(ids)} posts")
    print(f"State file: {STATE_FILE}")
    print()
    print(
        "These posts will NOT be sent to Bluesky."
    )
    print(
        "Only posts appearing after this "
        "initialization will be processed."
    )


if __name__ == "__main__":
    main()
