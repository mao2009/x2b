#!/usr/bin/env python3

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from atproto import Client, models, client_utils


# ============================================================
# 設定
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

ENV_FILE = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / "seen.json"
USERS_FILE = BASE_DIR / "users.json"

# Bluesky投稿間隔
POST_INTERVAL = 3

# ユーザー情報キャッシュの更新間隔
USER_CACHE_DAYS = 7

# Bluesky本文最大文字数
MAX_TEXT_LENGTH = 270

# cron対策
TWITTER_BIN = BASE_DIR / ".venv" / "bin" / "twitter"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/120 Safari/537.36"
)


# ============================================================
# 環境変数
# ============================================================

load_dotenv(ENV_FILE)

BSKY_HANDLE = os.getenv("BSKY_HANDLE")
BSKY_APP_PASSWORD = os.getenv("BSKY_APP_PASSWORD")

if not BSKY_HANDLE:
    print("ERROR: BSKY_HANDLE is not set.")
    sys.exit(1)

if not BSKY_APP_PASSWORD:
    print("ERROR: BSKY_APP_PASSWORD is not set.")
    sys.exit(1)

# アトラス系アカウントを登録しているXリスト
LIST_ID = os.getenv("X_LIST_ID")

if not LIST_ID:
    print("ERROR: X_LIST_ID is not set.")
    sys.exit(1)


# ============================================================
# 共通
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


# ============================================================
# 投稿済みID
# ============================================================

def load_seen():
    if not STATE_FILE.exists():
        return set()

    try:
        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(data, list):
            return set()

        return set(str(x) for x in data)

    except Exception as e:
        print(
            f"WARNING: failed to load seen.json: {e}"
        )
        return set()


def save_seen(seen):
    # 最大2000件
    data = list(seen)[-2000:]

    STATE_FILE.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


# ============================================================
# ユーザーキャッシュ
# ============================================================

def load_users():
    if not USERS_FILE.exists():
        return {}

    try:
        data = json.loads(
            USERS_FILE.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(data, dict):
            return data

    except Exception as e:
        print(
            f"WARNING: failed to load users.json: {e}"
        )

    return {}


def save_users(users):
    USERS_FILE.write_text(
        json.dumps(
            users,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )


def cache_is_fresh(entry):
    if not entry:
        return False

    updated_at = entry.get(
        "updatedAt"
    )

    if not updated_at:
        return False

    try:
        updated = datetime.fromisoformat(
            updated_at
        )

        if updated.tzinfo is None:
            updated = updated.replace(
                tzinfo=timezone.utc
            )

        return (
            now_utc() - updated
            < timedelta(days=USER_CACHE_DAYS)
        )

    except Exception:
        return False


def get_x_user(screen_name, users):
    """
    Xユーザー情報を取得。

    7日以内ならキャッシュを使用。
    7日を超えていればtwitter userで再取得。
    """

    if not screen_name:
        return {
            "name": "Unknown",
            "screenName": "",
        }

    key = screen_name.lower()

    cached = users.get(key)

    if cache_is_fresh(cached):
        print(
            f"User cache hit: @{screen_name}"
        )

        return cached

    print(
        f"Fetching X user: @{screen_name}"
    )

    if not TWITTER_BIN.exists():
        raise RuntimeError(
            f"twitter not found: {TWITTER_BIN}"
        )

    env = os.environ.copy()

    result = subprocess.run(
        [
            str(TWITTER_BIN),
            "user",
            screen_name,
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    if result.returncode != 0:
        print(
            result.stderr,
            file=sys.stderr
        )

        # 取得失敗時は古いキャッシュがあれば使用
        if cached:
            print(
                f"Using stale cache for "
                f"@{screen_name}"
            )
            return cached

        return {
            "name": screen_name,
            "screenName": screen_name,
        }

    try:
        data = json.loads(
            result.stdout
        )

    except json.JSONDecodeError:
        print(
            "WARNING: invalid user JSON",
            file=sys.stderr
        )

        if cached:
            return cached

        return {
            "name": screen_name,
            "screenName": screen_name,
        }

    # twitter-cliのレスポンス形式に対応
    user = data.get(
        "data",
        data
    )

    if isinstance(user, list):
        user = (
            user[0]
            if user
            else {}
        )

    name = (
        user.get("name")
        or screen_name
    )

    actual_screen_name = (
        user.get("screenName")
        or user.get("username")
        or screen_name
    )

    entry = {
        "name": name,
        "screenName": actual_screen_name,
        "updatedAt": iso_now(),
    }

    users[key] = entry
    save_users(users)

    return entry


# ============================================================
# Xリスト取得
# ============================================================

def get_x_posts():

    print("Fetching X list...")

    if not TWITTER_BIN.exists():
        raise RuntimeError(
            f"twitter not found: {TWITTER_BIN}"
        )

    env = os.environ.copy()

    result = subprocess.run(
        [
            str(TWITTER_BIN),
            "list",
            LIST_ID,
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    if result.returncode != 0:

        print(
            "twitter-cli error:",
            file=sys.stderr
        )

        print(
            result.stderr,
            file=sys.stderr
        )

        raise RuntimeError(
            "twitter-cli failed"
        )

    try:
        data = json.loads(
            result.stdout
        )

    except json.JSONDecodeError:
        print(
            result.stdout[:2000],
            file=sys.stderr
        )
        raise

    if not data.get("ok"):
        raise RuntimeError(
            "twitter-cli returned ok=false"
        )

    posts = data.get(
        "data",
        []
    )

    print(
        f"Fetched {len(posts)} posts"
    )

    return posts


# ============================================================
# X投稿URL
# ============================================================

def get_x_post_url(post):

    author = post.get(
        "author",
        {}
    )

    screen_name = author.get(
        "screenName"
    )

    post_id = post.get(
        "id"
    )

    if not screen_name or not post_id:
        return None

    return (
        f"https://x.com/"
        f"{screen_name}/status/"
        f"{post_id}"
    )


# ============================================================
# OGP取得
# ============================================================

def get_ogp(url):

    print(
        f"Fetching OGP: {url}"
    )

    try:

        response = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT
            },
            timeout=15,
            allow_redirects=True,
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        def meta(name):

            tag = soup.find(
                "meta",
                attrs={
                    "property": name
                }
            )

            if not tag:

                tag = soup.find(
                    "meta",
                    attrs={
                        "name": name
                    }
                )

            if tag:
                return tag.get(
                    "content",
                    ""
                ).strip()

            return ""

        title = meta(
            "og:title"
        )

        description = meta(
            "og:description"
        )

        image = meta(
            "og:image"
        )

        if not title and soup.title:
            title = soup.title.get_text(
                strip=True
            )

        if not description:
            description = meta(
                "description"
            )

        return {
            "title": title or url,
            "description": description,
            "image": image,
        }

    except Exception as e:

        print(
            f"OGP failed: {e}"
        )

        return {
            "title": url,
            "description": "",
            "image": None,
        }


# ============================================================
# X投稿画像
# ============================================================

def get_media_image(post):

    media = post.get(
        "media"
    ) or []

    for item in media:

        if item.get("type") != "photo":
            continue

        url = item.get(
            "url"
        )

        if url:
            return url

    return None


def get_thumbnail_url(post, ogp):

    # OGP画像を優先
    image = ogp.get(
        "image"
    )

    if image:
        return image

    # OGPが取れなければX画像
    return get_media_image(
        post
    )


# ============================================================
# URL / ハッシュタグ
# ============================================================

URL_PATTERN = re.compile(
    r"https?://[^\s<>\"]+"
)

HASHTAG_PATTERN = re.compile(
    r"#[^\s#]+"
)


def clean_url(url):
    """
    文末の日本語句読点などをURLから外す。
    """

    trailing = ".,!?。！？、）」』】〉》〉>"

    while url and url[-1] in trailing:
        url = url[:-1]

    return url


def add_text_with_facets(
    builder,
    text
):
    """
    本文中のURLとハッシュタグを
    Bluesky facetとして追加。
    """

    matches = []

    # URL
    for match in URL_PATTERN.finditer(text):

        raw_url = match.group()

        url = clean_url(
            raw_url
        )

        if not url:
            continue

        actual_end = (
            match.start()
            + len(url)
        )

        matches.append(
            (
                match.start(),
                actual_end,
                "url",
                url,
            )
        )

    # ハッシュタグ
    for match in HASHTAG_PATTERN.finditer(text):

        tag = match.group()

        matches.append(
            (
                match.start(),
                match.end(),
                "hashtag",
                tag[1:],
            )
        )

    # 開始位置順
    matches.sort(
        key=lambda x: x[0]
    )

    position = 0

    for start, end, kind, value in matches:

        # 重複範囲を無視
        if start < position:
            continue

        # facet前の通常文字
        if start > position:

            builder.text(
                text[position:start]
            )

        if kind == "url":

            builder.link(
                value,
                value
            )

        elif kind == "hashtag":

            builder.tag(
                f"#{value}",
                value
            )

        position = end

    # 残り
    if position < len(text):

        builder.text(
            text[position:]
        )


# ============================================================
# Bluesky用OGPカード
# ============================================================

def create_external_embed(
    client,
    url,
    ogp,
    thumbnail_url
):

    thumb_blob = None

    if thumbnail_url:

        print(
            f"Downloading thumbnail: "
            f"{thumbnail_url}"
        )

        try:

            response = requests.get(
                thumbnail_url,
                headers={
                    "User-Agent": USER_AGENT
                },
                timeout=20,
            )

            response.raise_for_status()

            upload = client.upload_blob(
                response.content
            )

            thumb_blob = upload.blob

            print(
                "Thumbnail uploaded."
            )

        except Exception as e:

            print(
                f"Thumbnail upload failed: "
                f"{e}"
            )

    external = (
        models.AppBskyEmbedExternal
        .External(
            uri=url,
            title=(
                ogp.get("title")
                or url
            ),
            description=(
                ogp.get("description")
                or ""
            ),
            thumb=thumb_blob,
        )
    )

    return (
        models.AppBskyEmbedExternal
        .Main(
            external=external
        )
    )


# ============================================================
# 投稿者表示
# ============================================================

def add_user_to_builder(
    builder,
    user,
):
    name = (
        user.get("name")
        or user.get("screenName")
        or "Unknown"
    )

    screen_name = (
        user.get("screenName")
        or ""
    )

    builder.text(
        name
    )

    if screen_name:

        profile_url = (
            f"https://x.com/"
            f"{screen_name}"
        )

        builder.text(
            " "
        )

        builder.link(
            f"@{screen_name}",
            profile_url
        )

    builder.text(
        "（X）"
    )


# ============================================================
# 本文生成
# ============================================================

def build_post(
    post,
    users
):

    author = post.get(
        "author",
        {}
    )

    author_screen_name = (
        author.get(
            "screenName"
        )
    )

    # 元投稿者
    author_user = get_x_user(
        author_screen_name,
        users
    )

    builder = (
        client_utils.TextBuilder()
    )

    is_retweet = post.get(
        "isRetweet",
        False
    )

    # --------------------------------------------------------
    # 通常投稿
    # --------------------------------------------------------

    if not is_retweet:

        builder.text(
            "📢 "
        )

        add_user_to_builder(
            builder,
            author_user
        )

    # --------------------------------------------------------
    # リポスト
    # --------------------------------------------------------

    else:

        builder.text(
            "🔁 リポスト"
        )

        builder.text(
            "\n\n元投稿："
        )

        add_user_to_builder(
            builder,
            author_user
        )

        retweeted_by = (
            post.get(
                "retweetedBy"
            )
            or ""
        )

        if retweeted_by:

            repost_user = get_x_user(
                retweeted_by,
                users
            )

            builder.text(
                "\nリポスト："
            )

            add_user_to_builder(
                builder,
                repost_user
            )

    builder.text(
        "\n\n"
    )

    # --------------------------------------------------------
    # X本文
    # --------------------------------------------------------

    text = (
        post.get(
            "text"
        )
        or ""
    ).strip()

    if len(text) > MAX_TEXT_LENGTH:

        text = (
            text[
                :MAX_TEXT_LENGTH - 1
            ]
            + "…"
        )

    add_text_with_facets(
        builder,
        text
    )

    return builder


# ============================================================
# Blueskyへ投稿
# ============================================================

def post_to_bluesky(
    client,
    post,
    users
):

    builder = build_post(
        post,
        users
    )

    x_post_url = get_x_post_url(
        post
    )

    if not x_post_url:

        raise RuntimeError(
            "Could not determine X post URL"
        )

    # --------------------------------------------------------
    # OGP
    # --------------------------------------------------------

    ogp = get_ogp(
        x_post_url
    )

    thumbnail_url = (
        get_thumbnail_url(
            post,
            ogp
        )
    )

    # --------------------------------------------------------
    # OGPカード
    # --------------------------------------------------------

    embed = create_external_embed(
        client,
        x_post_url,
        ogp,
        thumbnail_url
    )

    # --------------------------------------------------------
    # Bluesky投稿
    # --------------------------------------------------------

    response = client.send_post(
        text=builder.build_text(),
        facets=builder.build_facets(),
        embed=embed,
    )

    return response


# ============================================================
# メイン
# ============================================================

def main():

    print(
        "=========================================="
    )

    print(
        " X List → Bluesky"
    )

    print(
        f" List ID: {LIST_ID}"
    )

    print(
        "=========================================="
    )

    # --------------------------------------------------------
    # Blueskyログイン
    # --------------------------------------------------------

    print(
        "Logging into Bluesky..."
    )

    client = Client()

    client.login(
        BSKY_HANDLE,
        BSKY_APP_PASSWORD
    )

    print(
        "Bluesky login: OK"
    )

    # --------------------------------------------------------
    # ユーザーキャッシュ
    # --------------------------------------------------------

    users = load_users()

    # --------------------------------------------------------
    # X取得
    # --------------------------------------------------------

    posts = get_x_posts()

    # --------------------------------------------------------
    # リポストも含める
    # --------------------------------------------------------

    print(
        "Retweets are included."
    )

    # --------------------------------------------------------
    # 古い順
    # --------------------------------------------------------

    posts.sort(
        key=lambda post:
        post.get(
            "createdAtISO",
            ""
        )
    )

    # --------------------------------------------------------
    # 投稿済み確認
    # --------------------------------------------------------

    seen = load_seen()

    new_posts = []

    for post in posts:

        post_id = str(
            post.get(
                "id",
                ""
            )
        )

        if not post_id:
            continue

        if post_id in seen:
            continue

        new_posts.append(
            post
        )

    print(
        f"New posts: "
        f"{len(new_posts)}"
    )

    if not new_posts:

        print(
            "Nothing to post."
        )

        return

    # --------------------------------------------------------
    # 投稿
    # --------------------------------------------------------

    for index, post in enumerate(
        new_posts
    ):

        post_id = str(
            post["id"]
        )

        author = post.get(
            "author",
            {}
        )

        screen_name = (
            author.get(
                "screenName",
                "unknown"
            )
        )

        name = (
            author.get(
                "name",
                "unknown"
            )
        )

        is_retweet = post.get(
            "isRetweet",
            False
        )

        print()
        print(
            f"[{index + 1}/"
            f"{len(new_posts)}]"
        )

        if is_retweet:

            print(
                f"RETWEET: "
                f"{name} "
                f"@{screen_name}"
            )

            print(
                "retweetedBy: "
                f"{post.get('retweetedBy', '')}"
            )

        else:

            print(
                f"POST: "
                f"{name} "
                f"@{screen_name}"
            )

        print(
            f"X post ID: {post_id}"
        )

        try:

            response = post_to_bluesky(
                client,
                post,
                users
            )

            if response:

                print(
                    "Posted:"
                )

                print(
                    response.uri
                )

                # 成功した場合のみ既読
                seen.add(
                    post_id
                )

                save_seen(
                    seen
                )

        except Exception as e:

            print(
                f"POST ERROR: {e}",
                file=sys.stderr
            )

            # 失敗した投稿は
            # seenに入れない。
            # 次回実行時に再試行する。

        # ----------------------------------------------------
        # 次の投稿まで3秒
        # ----------------------------------------------------

        if (
            index
            < len(new_posts) - 1
        ):

            print(
                f"Waiting "
                f"{POST_INTERVAL} seconds..."
            )

            time.sleep(
                POST_INTERVAL
            )

    print()
    print(
        "Done."
    )


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "Stopped."
        )

    except Exception as e:

        print(
            f"FATAL ERROR: {e}",
            file=sys.stderr
        )

        sys.exit(1)
