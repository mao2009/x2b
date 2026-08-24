#!/usr/bin/env python3

import fcntl
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

try:
    import grapheme
except ImportError:
    grapheme = None


# ============================================================
# 設定
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

ENV_FILE = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / "seen.json"
USERS_FILE = BASE_DIR / "users.json"
LOCK_FILE = BASE_DIR / ".x2b.lock"

# Bluesky投稿間隔
POST_INTERVAL = 3

# ユーザー情報キャッシュの更新間隔
USER_CACHE_DAYS = 7

# Bluesky本文最大文字数（グラフェム数）
# プレフィックス分を考慮して余裕を持たせる
MAX_TEXT_LENGTH = 270

# Xリスト取得の最大件数（0で無制限）
MAX_POSTS_PER_RUN = 200

# twitter-cli 1回あたりの取得件数
TWITTER_CLI_MAX = 100

# リトライ設定
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds

# ロック取得待機時間
LOCK_TIMEOUT = 30  # seconds

# cron対策
TWITTER_BIN = BASE_DIR / ".venv" / "bin" / "twitter"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/120 Safari/537.36"
)

# Bluesky制限
BSKY_MAX_TEXT_GRAPHEMES = 300
BSKY_MAX_THUMBNAIL_BYTES = 1_000_000


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
# ロック機能
# ============================================================

def acquire_lock():
    """
    ファイルベースのロックを取得。
    既にロックされている場合は待機する。
    """
    lock_file = LOCK_FILE.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another instance is running. Waiting for lock...")
        start = time.time()
        while time.time() - start < LOCK_TIMEOUT:
            time.sleep(1)
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                print("Lock acquired.")
                break
            except BlockingIOError:
                continue
        else:
            lock_file.close()
            raise RuntimeError(f"Could not acquire lock within {LOCK_TIMEOUT} seconds")
    
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


def release_lock(lock_file):
    """ロックを解放"""
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()


# ============================================================
# 共通
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def count_graphemes(text):
    """グラフェム数をカウント（graphemeライブラリがあれば使用、なければ文字数）"""
    if grapheme:
        return grapheme.length(text)
    return len(text)


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
    """
    Xリストから全投稿を取得（ページネーション対応）。
    twitter-cliは-n/--maxで最大取得件数を指定可能。
    """
    print("Fetching X list...")

    if not TWITTER_BIN.exists():
        raise RuntimeError(
            f"twitter not found: {TWITTER_BIN}"
        )

    env = os.environ.copy()

    all_posts = []
    max_posts = MAX_POSTS_PER_RUN if MAX_POSTS_PER_RUN > 0 else TWITTER_CLI_MAX * 10
    remaining = max_posts

    while remaining > 0:
        fetch_count = min(TWITTER_CLI_MAX, remaining)

        print(f"Fetching page (max {fetch_count} posts)...")

        result = subprocess.run(
            [
                str(TWITTER_BIN),
                "list",
                LIST_ID,
                "--json",
                "-n", str(fetch_count),
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
            raise RuntimeError("twitter-cli failed")

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(result.stdout[:2000], file=sys.stderr)
            raise

        if not data.get("ok"):
            raise RuntimeError("twitter-cli returned ok=false")

        posts = data.get("data", [])
        
        if not posts:
            print("No more posts.")
            break

        all_posts.extend(posts)
        print(f"Fetched {len(posts)} posts (total: {len(all_posts)})")

        if len(posts) < fetch_count:
            break

        remaining -= len(posts)

    print(f"Total fetched: {len(all_posts)} posts")
    return all_posts


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

            # 画像サイズチェック（Bluesky制限: 1MB）
            image_size = len(response.content)
            if image_size > BSKY_MAX_THUMBNAIL_BYTES:
                print(
                    f"Thumbnail too large: {image_size} bytes "
                    f"(max {BSKY_MAX_THUMBNAIL_BYTES}), skipping thumbnail"
                )
                thumbnail_url = None
            else:
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
    # プレフィックス構築（文字数計算用）
    # --------------------------------------------------------
    
    def build_prefix_text(is_retweet, author_user, retweeted_by_user=None):
        """プレフィックス部分のテキストを構築して返す（グラフェム数計算用）"""
        temp_builder = client_utils.TextBuilder()
        if not is_retweet:
            temp_builder.text("📢 ")
            name = author_user.get("name") or author_user.get("screenName") or "Unknown"
            screen_name = author_user.get("screenName") or ""
            temp_builder.text(name)
            if screen_name:
                temp_builder.text(" ")
                temp_builder.text(f"@{screen_name}")
            temp_builder.text("（X）")
        else:
            temp_builder.text("🔁 リポスト")
            temp_builder.text("\n\n元投稿：")
            name = author_user.get("name") or author_user.get("screenName") or "Unknown"
            screen_name = author_user.get("screenName") or ""
            temp_builder.text(name)
            if screen_name:
                temp_builder.text(" ")
                temp_builder.text(f"@{screen_name}")
            if retweeted_by_user:
                temp_builder.text("\nリポスト：")
                name = retweeted_by_user.get("name") or retweeted_by_user.get("screenName") or "Unknown"
                screen_name = retweeted_by_user.get("screenName") or ""
                temp_builder.text(name)
                if screen_name:
                    temp_builder.text(" ")
                    temp_builder.text(f"@{screen_name}")
        temp_builder.text("\n\n")
        return temp_builder.build_text()

    # --------------------------------------------------------
    # 通常投稿
    # --------------------------------------------------------

    retweeted_by_user = None
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
            retweeted_by_user = repost_user

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
    # X本文（プレフィックス長を考慮して切り詰め）
    # --------------------------------------------------------

    text = (
        post.get(
            "text"
        )
        or ""
    ).strip()

    # プレフィックスのグラフェム数を計算
    prefix_text = build_prefix_text(is_retweet, author_user, retweeted_by_user)
    prefix_length = count_graphemes(prefix_text)
    
    # 利用可能な文字数 = 最大 - プレフィックス - 余裕(10)
    available_length = BSKY_MAX_TEXT_GRAPHEMES - prefix_length - 10
    
    if available_length < 50:
        available_length = 50  # 最小保証

    if count_graphemes(text) > available_length:
        if grapheme:
            # グラフェム単位で切り詰め
            text = grapheme.slice(text, 0, available_length - 1) + "…"
        else:
            text = text[:available_length - 1] + "…"

    add_text_with_facets(
        builder,
        text
    )

    return builder


# ============================================================
# エラー分類とリトライ
# ============================================================

class PermanentError(Exception):
    """永続的なエラー（リトライしても解決しない）"""
    pass


class TransientError(Exception):
    """一時的なエラー（リトライで解決する可能性がある）"""
    pass


def classify_error(e):
    """
    エラーを永続的/一時的に分類。
    - 400: PermanentError（不正なリクエスト）
    - 429: TransientError（レートリミット）
    - 5xx: TransientError（サーバーエラー）
    - ネットワークエラー: TransientError
    - その他: TransientError（安全側）
    """
    error_str = str(e)
    
    # atprotoのエラーレスポンスからステータスコードを抽出
    if "status_code=400" in error_str or "status_code=400" in error_str:
        return PermanentError(f"Bad request (400): {e}")
    
    if "status_code=429" in error_str:
        return TransientError(f"Rate limited (429): {e}")
    
    if "status_code=5" in error_str and len(error_str.split("status_code=")) > 1:
        try:
            code_part = error_str.split("status_code=")[1]
            status_code = int(code_part[:3])
            if 500 <= status_code < 600:
                return TransientError(f"Server error ({status_code}): {e}")
        except (ValueError, IndexError):
            pass
    
    # ネットワーク系エラー
    if isinstance(e, (requests.RequestException, ConnectionError, TimeoutError)):
        return TransientError(f"Network error: {e}")
    
    # その他は一時的エラーとして扱う（安全側）
    return TransientError(f"Unknown error: {e}")


def post_with_retry(client, post, users, max_retries=MAX_RETRIES):
    """
    リトライ付きでBlueskyに投稿。
    一時的エラーは指数バックオフでリトライ。
    永続的エラーは即座にraise。
    """
    last_error = None
    
    for attempt in range(max_retries + 1):
        try:
            return _post_to_bluesky(client, post, users)
        except Exception as e:
            classified = classify_error(e)
            
            if isinstance(classified, PermanentError):
                print(f"PERMANENT ERROR (not retrying): {classified}")
                raise classified
            
            last_error = classified
            
            if attempt < max_retries:
                delay = RETRY_BASE_DELAY * (2 ** attempt)  # 指数バックオフ
                print(f"TRANSIENT ERROR (attempt {attempt + 1}/{max_retries + 1}): {classified}")
                print(f"Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                print(f"TRANSIENT ERROR (max retries exceeded): {classified}")
    
    raise last_error


# ============================================================
# Blueskyへ投稿（内部関数）
# ============================================================

def _post_to_bluesky(
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
    lock_file = None
    
    try:
        # ロック取得
        lock_file = acquire_lock()
        
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
        skipped_seen = 0
        skipped_no_id = 0

        for post in posts:

            post_id = str(
                post.get(
                    "id",
                    ""
                )
            )

            if not post_id:
                skipped_no_id += 1
                continue

            if post_id in seen:
                skipped_seen += 1
                continue

            new_posts.append(
                post
            )

        print(
            f"Fetched: {len(posts)}, "
            f"Skipped (seen): {skipped_seen}, "
            f"Skipped (no ID): {skipped_no_id}, "
            f"New: {len(new_posts)}"
        )

        if not new_posts:

            print(
                "Nothing to post."
            )

            return

        # --------------------------------------------------------
        # 投稿
        # --------------------------------------------------------

        success_count = 0
        permanent_fail_count = 0
        transient_fail_count = 0

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

                response = post_with_retry(
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
                    success_count += 1

            except PermanentError as e:

                print(
                    f"PERMANENT FAILURE (will not retry): {e}",
                    file=sys.stderr
                )
                permanent_fail_count += 1
                # 永続的エラーの場合はseenに追加して次回スキップ
                # （同じエラーで永遠にリトライしないため）
                seen.add(post_id)
                save_seen(seen)

            except TransientError as e:

                print(
                    f"TRANSIENT FAILURE (will retry next run): {e}",
                    file=sys.stderr
                )
                transient_fail_count += 1
                # 一時的エラーはseenに入れない（次回リトライ）

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
            f"Summary: Success: {success_count}, "
            f"Permanent failures: {permanent_fail_count}, "
            f"Transient failures: {transient_fail_count}"
        )
        print(
            "Done."
        )
    
    finally:
        if lock_file:
            release_lock(lock_file)


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
