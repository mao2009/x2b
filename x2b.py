#!/usr/bin/env python3

import argparse
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
from atproto_client import exceptions as atproto_exceptions

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

# Xリスト取得の最大件数（1 runあたりのハード上限）
# twitter-cli listは1 runにつき1回だけ実行し、要求件数はこの値で固定する。
# 明示的なpagination契約がないため複数回の取得は行わない
# （docs/adr/0001-disable-pagination-for-x-list-fetching.md 参照）
MAX_POSTS_PER_RUN = 100

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

# 本文長マージン（プレフィックス計算などの揺らぎに対する保険）
TEXT_LENGTH_MARGIN = 10

# 切り詰め時の本文最低保証グラフェム数
MIN_BODY_GRAPHEMES = 50


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


def truncate_text_to_graphemes(text, max_graphemes):
    """
    グラフェム単位で本文を切り詰める。

    - 絵文字・結合文字を壊さないようグラフェム単位で処理する
    - 切り詰め時は末尾を省略記号「…」に置き換える
      （結果は常にmax_graphemesグラフェム以下）
    """
    if max_graphemes <= 0:
        return ""

    if count_graphemes(text) <= max_graphemes:
        return text

    keep = max_graphemes - 1

    if grapheme:
        return grapheme.slice(text, 0, keep) + "…"

    return text[:keep] + "…"


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


def get_x_user(screen_name, users, dry_run=False):
    """
    Xユーザー情報を取得。

    7日以内ならキャッシュを使用。
    7日を超えていればtwitter userで再取得。

    Dry-run時は取得結果をメモリ上のキャッシュにのみ反映し、
    users.jsonへの永続化は行わない（副作用なしで解析するため）。
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

    if not dry_run:
        save_users(users)

    return entry


# ============================================================
# Xリスト取得
# ============================================================

def get_x_posts():
    """
    Xリストから最新投稿を1ページ分だけ取得する。

    twitter-cli list -n N には「次ページ」を保証するcursor/tokenがなく、
    複数回呼び出すと同じ最新投稿を再取得して二重投稿する危険があるため、
    paginationは禁止している。

    - 1 runにつきtwitter-cli listは正確に1回だけ実行する
    - 要求件数はMAX_POSTS_PER_RUN（=100）でハード上限とする

    詳細は docs/adr/0001-disable-pagination-for-x-list-fetching.md を参照。
    """
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
            "-n", str(MAX_POSTS_PER_RUN),
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

    # twitter-cliが要求件数を超えて返しても
    # runあたりのハード上限を守る（ADR 0001）
    posts = data.get("data", [])[:MAX_POSTS_PER_RUN]

    print(f"Fetched {len(posts)} posts")
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

def download_thumbnail(thumbnail_url):
    """
    サムネイル画像をダウンロードし、Blueskyのサイズ制限を検証する。

    戻り値は (content, status) のタプル:

    - content: 画像bytes（取得できない場合はNone）
    - status:  "ok" | "too_large" | "failed" | "none"

    サイズ超過・取得失敗時も例外を上げない
    （サムネイルなしで投稿を継続する既存ポリシー）。

    この関数は読み込みのみを行い、Blueskyへの書き込みは発生しないため、
    Dry-runでもそのまま実行できる。
    """
    if not thumbnail_url:
        return None, "none"

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

    except requests.RequestException as e:

        print(
            f"Thumbnail processing failed "
            f"(continuing without thumbnail): "
            f"{format_error_for_log(e)}"
        )

        return None, "failed"

    # 画像サイズチェック（Bluesky制限: 1MB）
    image_size = len(response.content)

    if image_size > BSKY_MAX_THUMBNAIL_BYTES:
        print(
            f"Thumbnail too large: {image_size} bytes "
            f"(max {BSKY_MAX_THUMBNAIL_BYTES}), skipping thumbnail"
        )
        return None, "too_large"

    return response.content, "ok"


def create_external_embed(
    client,
    url,
    ogp,
    thumbnail_url,
    dry_run=False,
    thumbnail_status_out=None,
):

    """
    OGPカード用のembedを構築する。

    Dry-run時はblob upload（Blueskyへの書き込み操作）を行わない。
    ただしサムネイルのダウンロードとサイズ検証は通常実行と同じく行い、
    結果をthumbnail_status_out（list）に報告する。

    thumbnail_status_outが渡された場合、最終ステータス文字列
    （"ok" / "too_large" / "failed" / "none" / "upload_failed"）を追加する。
    """

    thumb_blob = None

    content, thumb_status = download_thumbnail(
        thumbnail_url
    )

    if content is not None:

        if dry_run:
            print(
                "[DRY-RUN] Thumbnail valid "
                f"({len(content)} bytes); "
                "blob upload skipped"
            )
        else:
            try:
                upload = client.upload_blob(
                    content
                )

                thumb_blob = upload.blob

                print(
                    "Thumbnail uploaded."
                )

            except Exception as e:

                thumb_status = "upload_failed"

                print(
                    f"Thumbnail processing failed "
                    f"(continuing without thumbnail): "
                    f"{format_error_for_log(e)}"
                )

    if thumbnail_status_out is not None:
        thumbnail_status_out.append(thumb_status)

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
    users,
    dry_run=False,
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
        users,
        dry_run=dry_run,
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
                users,
                dry_run=dry_run,
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

    # 利用可能な文字数 = 最大 - プレフィックス - 余裕
    available_length = (
        BSKY_MAX_TEXT_GRAPHEMES
        - prefix_length
        - TEXT_LENGTH_MARGIN
    )

    if available_length < MIN_BODY_GRAPHEMES:
        # プレフィックスが極端に長い場合は余裕を削って
        # 最終本文が300グラフェムを超えないようにする
        # （通常のプレフィックスではこの分岐に入らない）
        available_length = BSKY_MAX_TEXT_GRAPHEMES - prefix_length

    if available_length <= 0:
        raise PermanentError(
            f"Prefix too long for X post "
            f"{post.get('id')}: {prefix_length} graphemes "
            f"(max {BSKY_MAX_TEXT_GRAPHEMES}), cannot build valid post"
        )

    original_length = count_graphemes(text)

    if original_length > available_length:
        text = truncate_text_to_graphemes(
            text,
            available_length
        )

        print(
            f"Text truncated for X post "
            f"{post.get('id')}: "
            f"{original_length} -> "
            f"{count_graphemes(text)} graphemes "
            f"(prefix: {prefix_length}, "
            f"limit: {BSKY_MAX_TEXT_GRAPHEMES})"
        )

    add_text_with_facets(
        builder,
        text
    )

    return builder


# ============================================================
# エラー分類とリトライ
# ============================================================

class PermanentError(Exception):
    """
    恒久的なエラー（同じpayloadを再送しても解決しない既知の失敗）。

    例:
    - 送信前payload validation failure（本文長制限違反など）
    - payload size violation（413 PayloadTooLargeなど）
    - 既知の恒久的なAPI validation error
    """
    pass


class TransientError(Exception):
    """
    一時的なエラー（時間を置いて再実行すれば成功する可能性がある）。

    例:
    - HTTP 429（レートリミット）
    - HTTP 5xx（サーバー側の一時的障害）
    - network failure / timeout / connection failure
    """
    pass


class UnknownError(Exception):
    """
    分類不能な予期しないエラー。

    PermanentError / TransientErrorへ勝手に変換して握り潰さない。
    - リトライはしない（同じ結果になる可能性が高く、原因調査が必要）
    - seenには登録しない（次回実行時に再表面化し、ログで追跡できる）
    """
    pass


# Bluesky APIが恒久的なpayload問題を示すと判明しているシグナル。
#
# XRPCエラーの`error`フィールドは"InvalidRequest"など粗い識別子しか
# 持たないため、既知の恒久原因の判定だけはAPI返却message内容を
# 参照する（優先順位ルール上の最終手段。SDK例外型とstatus_codeの
# 構造化情報を先に使う）。語録は保守的に最小限に留め、拡張は慎重に。

# 実際に確認されている恒久エラー識別子（小文字比較）
PERMANENT_PAYLOAD_ERROR_IDS = frozenset({
    "payloadtoolarge",  # blob等のサイズ超過（HTTP 400/413で返る）
})

PERMANENT_PAYLOAD_MESSAGE_KEYWORDS = (
    "text too long",   # 本文長制限違反
    "grapheme",        # Blueskyはグラフェム数で本文長を検証する
    "too large",       # サイズ超過一般
)


def _extract_api_error_info(e):
    """
    atproto SDK例外から構造化情報を取り出す。

    Returns:
        (status_code, error_id, message) のタプル。
        構造化情報を持たない例外の場合は None。
    """
    response = getattr(e, "response", None)

    if response is None:
        return None

    status_code = getattr(response, "status_code", None)

    error_id = None
    message = None

    # SDKはJSON bodyをXrpcErrorモデルへパース済み。
    # （atproto_client.request._handle_response参照）
    content = getattr(response, "content", None)

    if content is not None:
        error_id = getattr(content, "error", None)
        message = getattr(content, "message", None)

        if error_id is None and isinstance(content, dict):
            error_id = content.get("error")
            message = content.get("message")

    return (status_code, error_id, message)


def _is_known_permanent_payload_error(error_id, message):
    """既知の恒久的payloadエラーかを判定する。"""
    if error_id and error_id.lower() in PERMANENT_PAYLOAD_ERROR_IDS:
        return True

    if message:
        lowered = message.lower()

        if any(keyword in lowered for keyword in PERMANENT_PAYLOAD_MESSAGE_KEYWORDS):
            return True

    return False


def format_error_for_log(e):
    """
    例外を調査可能な1行ログへ整形する。

    記録する要素: 例外型名 / HTTP status / API error identifier /
    API error message（切り詰め）/ 例外メッセージ。

    認証情報（access token / app password / headers等）は
    構造化情報から意図的に除外しており、ログに出力されない。
    """
    parts = [type(e).__name__]

    info = _extract_api_error_info(e)

    if info is not None:
        status_code, error_id, message = info

        parts.append(f"status={status_code}")

        if error_id:
            parts.append(f"api_error={error_id}")

        if message:
            parts.append(f"api_message={str(message)[:200]}")
    else:
        detail = str(e).strip()

        if detail:
            parts.append(str(e)[:200])

    return " | ".join(parts)


def classify_error(e):
    """
    エラーを Permanent / Transient / Unknown に分類する。

    優先順位:
    1. 既に分類済みの例外（送信前validation等）はそのまま透過
    2. atproto SDKの例外型 + 構造化されたstatus_code / error識別子
    3. 自前ネットワーク処理（OGP・thumbnail取得）の例外型
    4. それ以外はUnknownError（握り潰さない）

    分類ポリシー:
    - 429, 5xx, transport timeout/network → TransientError
    - 400/413 のうち既知のpayload validation問題 → PermanentError
    - 未知の400 → UnknownError（勝手にPermanent扱いしない）
    - 401/403等のその他4xx → UnknownError（認証情報の見直しが必要）
    - 上記以外 → UnknownError

    注意: SDK v0.0.71は502をNetworkErrorとして上げるため、
    response付きの例外は例外型よりstatus_codeを優先して判断する。
    """
    # ---- 再分類防止: 送信前validation等で確定済みの分類は正とする ----
    if isinstance(e, (PermanentError, TransientError, UnknownError)):
        return e

    # ---- atproto SDKの例外（構造化情報あり）----
    if isinstance(e, atproto_exceptions.AtProtocolError):
        info = _extract_api_error_info(e)

        status_code = info[0] if info else None
        error_id = info[1] if info else None
        message = info[2] if info else None

        if status_code is not None:
            if status_code == 429 or (
                500 <= status_code < 600
            ):
                return TransientError(
                    f"Bluesky API temporarily unavailable "
                    f"(HTTP {status_code}): {error_id}"
                )

            if status_code in (400, 413):
                if _is_known_permanent_payload_error(
                    error_id,
                    message,
                ):
                    return PermanentError(
                        f"Bluesky rejected the payload permanently "
                        f"(HTTP {status_code}, {error_id}): {message}"
                    )

                if status_code == 413:
                    # payloadが大きすぎる問題は再送でも解決しない
                    return PermanentError(
                        f"Payload too large "
                        f"(HTTP {status_code}, {error_id}): {message}"
                    )

                # 内容を確認できない400をPermanent扱いにすると
                # 投稿が黙って失われるためUnknownとして調査対象にする
                return UnknownError(
                    f"Unrecognized client error from Bluesky API "
                    f"(HTTP {status_code}, {error_id}): {message}"
                )

            # 401/403（認証情報の問題）等、単一postの再試行では
            # 解決しないためUnknownとして扱う
            return UnknownError(
                f"Unexpected API response from Bluesky "
                f"(HTTP {status_code}, {error_id}): {message}"
            )

        # responseなし = トランスポート層の失敗
        if isinstance(
            e,
            (atproto_exceptions.InvokeTimeoutError,
             atproto_exceptions.NetworkError),
        ):
            return TransientError(
                f"Network failure while calling Bluesky API: "
                f"{format_error_for_log(e)}"
            )

        return UnknownError(
            f"Unexpected atproto client error: "
            f"{format_error_for_log(e)}"
        )

    # ---- 自前のネットワーク処理（OGP取得・thumbnail DL等）----
    if isinstance(
        e,
        (requests.RequestException, ConnectionError, TimeoutError),
    ):
        return TransientError(
            f"Network error: {format_error_for_log(e)}"
        )

    # ---- 分類不能 ----
    return UnknownError(
        f"Unclassified exception: {format_error_for_log(e)}"
    )


def post_with_retry(
    client,
    post,
    users,
    max_retries=MAX_RETRIES,
    dry_run=False,
):
    """
    リトライ付きでBlueskyに投稿する。

    - PermanentError: リトライせず即raise
      （同じpayloadの再送では解決しないため）
    - UnknownError:   リトライせず即raise
      （原因不明のため調査が必要。seenには入らないため
       次回実行時に再表面化する）
    - TransientError: 指数バックオフで最大max_retries回リトライし、
      尽きたらTransientErrorのままraise（有限回で必ず終了する）

    Dry-run時も分類・リトライ回数は通常実行と同一だが、
    実際の待機は行わない（テストを高速かつ安全にするため）。

    ログにはX post ID・例外型・HTTP status・API error識別子を含める。
    """
    post_id = (
        str(post.get("id"))
        if isinstance(post, dict)
        else None
    )

    last_error = None

    for attempt in range(max_retries + 1):

        try:
            return _post_to_bluesky(
                client,
                post,
                users,
                dry_run=dry_run,
            )

        except Exception as e:

            classified = classify_error(e)
            detail = format_error_for_log(e)

            if isinstance(classified, PermanentError):
                print(
                    f"PERMANENT ERROR "
                    f"(not retrying) "
                    f"X post ID: {post_id} | "
                    f"{detail}",
                    file=sys.stderr,
                )
                raise classified

            if isinstance(classified, UnknownError):
                print(
                    f"UNKNOWN ERROR "
                    f"(not retrying, requires investigation) "
                    f"X post ID: {post_id} | "
                    f"{detail}",
                    file=sys.stderr,
                )
                raise classified

            last_error = classified

            if attempt < max_retries:
                delay = RETRY_BASE_DELAY * (2 ** attempt)  # 指数バックオフ
                print(
                    f"TRANSIENT ERROR "
                    f"(attempt {attempt + 1}/{max_retries + 1}) "
                    f"X post ID: {post_id} | "
                    f"{detail}"
                )
                if dry_run:
                    # Dry-runでは実際の待機を行わない
                    # （リトライ経路自体は通常実行と同じ）
                    print(
                        f"[DRY-RUN] Would retry in {delay} seconds "
                        f"(sleep skipped)"
                    )
                else:
                    print(
                        f"Retrying in {delay} seconds..."
                    )
                    time.sleep(delay)
            else:
                print(
                    f"TRANSIENT ERROR "
                    f"(retry exhausted after {attempt + 1} attempts) "
                    f"X post ID: {post_id} | "
                    f"{detail}"
                )

    raise last_error


# ============================================================
# seen状態と投稿結果の処理
# ============================================================

def mark_seen(seen, post_id):
    """seenを更新して永続化する。"""
    seen.add(post_id)
    save_seen(seen)


def process_single_post(
    client,
    post,
    users,
    seen,
    dry_run=False,
):
    """
    1件のX投稿をBlueskyへ投稿し、結果に応じてseenを更新する。

    Dry-run時はBlueskyへの投稿・seenの更新を一切行わず、
    「投稿されるはずだった内容」を出力して結果を報告する
    （"would_post"は実投稿を意味しない）。

    戻り値（結果種別）とseenの扱い:

    - "success":     投稿成功 → seen登録
                     （Dry-runでは発生しない）
    - "would_post":  Dry-runで投稿可能だった → seen変更なし
    - "permanent":   既知の恒久的失敗 → seen登録
                     （Dry-runではseen登録をskip）
    - "transient":   一時的失敗（リトライ上限到達含む）→ seen未登録
                     （次回実行時に自動的に再試行される）
    - "unknown":     分類不能な予期しない失敗 → seen未登録
                     （原因調査が完了するまで次回実行で
                      再表面化させ、黙って消えないようにする）
    - "no_response": 投稿APIがfalsyな応答を返した
                     （従来実装と同様、seenにも計上にも入れない）
    """
    post_id = str(post["id"])

    try:
        response = post_with_retry(
            client,
            post,
            users,
            dry_run=dry_run,
        )

        if response:

            if dry_run:
                print_dry_run_result(response)
                return "would_post"

            print(
                "Posted:"
            )

            print(
                response.uri
            )

            # 成功した場合のみ既読
            mark_seen(seen, post_id)

            return "success"

        return "no_response"

    except PermanentError as e:

        seen_note = (
            "(not marked as seen in dry-run)"
            if dry_run
            else "(marked as seen)"
        )

        print(
            f"PERMANENT FAILURE "
            f"(will not retry; {seen_note}): "
            f"X post ID: {post_id} | "
            f"{format_error_for_log(e)}",
            file=sys.stderr,
        )

        # 既知の恒久的失敗は次回も同じ結果になるため
        # seenに追加して毎回の再処理・再通知を防ぐ
        # （失敗事実はstderrログとサマリ計上で可視化される）
        # Dry-run時は本番のseen stateを変更しない
        if not dry_run:
            mark_seen(seen, post_id)

        return "permanent"

    except TransientError as e:

        print(
            f"TRANSIENT FAILURE "
            f"(will retry next run): "
            f"X post ID: {post_id} | "
            f"{format_error_for_log(e)}",
            file=sys.stderr,
        )
        return "transient"

    except UnknownError as e:

        print(
            f"UNKNOWN FAILURE "
            f"(kept out of seen; will resurface next run): "
            f"X post ID: {post_id} | "
            f"{format_error_for_log(e)}",
            file=sys.stderr,
        )
        return "unknown"


def validate_post_text(text):
    """
    Bluesky API送信前の本文バリデーション。

    既知の制約（300グラフェム制限）を
    APIに送信する前に検出し、PermanentErrorとして扱う。
    """
    length = count_graphemes(text)

    if length > BSKY_MAX_TEXT_GRAPHEMES:
        raise PermanentError(
            f"Post text too long: {length} graphemes "
            f"(max {BSKY_MAX_TEXT_GRAPHEMES})"
        )


# ============================================================
# Blueskyへ投稿（内部関数）
# ============================================================

class DryRunResult:
    """
    Dry-run時の投稿結果。

    _post_to_blueskyが通常実行ではBluesky APIのレスポンスを返す位置で、
    Dry-runでは代わりにこのオブジェクトを返す。
    実際の投稿は存在しないため、uriは擬似的なもの
    （at://dry-run/...）であり、実投稿と誤認できない形にする。
    """

    def __init__(
        self,
        post,
        text,
        grapheme_count,
        thumbnail_status,
    ):
        author = post.get(
            "author",
            {}
        )

        self.post_id = str(post.get("id"))

        self.author_name = (
            author.get("name")
            or author.get("screenName")
            or "Unknown"
        )

        self.author_screen_name = (
            author.get("screenName")
            or ""
        )

        self.text = text
        self.grapheme_count = grapheme_count
        self.thumbnail_status = thumbnail_status

        # 実在しない投稿を示す擬似URI
        self.uri = f"at://dry-run/x2b/{self.post_id}"

    @property
    def thumbnail_skipped(self):
        """サイズ制限超過によりサムネイルが除外されたか。"""
        return self.thumbnail_status == "too_large"

    @property
    def has_thumbnail(self):
        """サイズ検証を通過したサムネイルが存在するか。"""
        return self.thumbnail_status == "ok"


# Dry-run実行中の結果（main()でサマリ集計するために使用）。
# 通常実行では決して追加されない。
DRY_RUN_RESULTS = []


def print_dry_run_result(result):
    """
    Dry-runで「投稿されるはずだった内容」を出力する。

    実際の投稿と誤認できないよう、すべての行に[DRY-RUN]を付ける。
    認証情報等は含まない（本文・作者・文字数・サムネイル判定のみ）。
    """

    preview = result.text.replace("\n", " ")

    if count_graphemes(preview) > 60:
        preview = truncate_text_to_graphemes(
            preview,
            60,
        )

    thumbnail_labels = {
        "ok": "valid (blob upload skipped)",
        "too_large": "skipped (exceeds size limit)",
        "failed": "download failed",
        "upload_failed": "not attempted (dry-run)",
        "none": "none",
    }

    label = thumbnail_labels.get(
        result.thumbnail_status,
        result.thumbnail_status,
    )

    print(
        f"[DRY-RUN] Would post: X post ID: {result.post_id}"
    )
    print(
        f"[DRY-RUN]   Author: "
        f"{result.author_name} "
        f"@{result.author_screen_name}"
    )
    print(
        f"[DRY-RUN]   Text "
        f"({result.grapheme_count} graphemes): "
        f"{preview}"
    )
    print(
        f"[DRY-RUN]   Embed: external card | "
        f"thumbnail: {label}"
    )


def _post_to_bluesky(
    client,
    post,
    users,
    dry_run=False,
):
    """
    1件のX投稿からBluesky投稿ペイロードを構築し、送信する。

    Dry-run時はbuild/validate/OGP取得/サムネイル検証まで
    通常実行と同一経路で実行した後、blob uploadとsend_postを
    行わずDryRunResultを返す（Blueskyへの書き込みは一切発生しない）。
    """

    builder = build_post(
        post,
        users,
        dry_run=dry_run,
    )

    text = builder.build_text()

    # --------------------------------------------------------
    # 送信前バリデーション
    # 既知の制約違反はBluesky APIに送る前に検出する
    # （OGP取得・blobアップロード等の無駄なAPI操作も発生させない）
    # --------------------------------------------------------

    validate_post_text(text)

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
    # （Dry-runでもダウンロード・サイズ検証は行い、
    #   blob uploadのみskipする）
    # --------------------------------------------------------

    thumbnail_status_out = (
        [] if dry_run else None
    )

    embed = create_external_embed(
        client,
        x_post_url,
        ogp,
        thumbnail_url,
        dry_run=dry_run,
        thumbnail_status_out=thumbnail_status_out,
    )

    # --------------------------------------------------------
    # Bluesky投稿（Dry-run時はここでSTOP）
    # --------------------------------------------------------

    if dry_run:
        result = DryRunResult(
            post=post,
            text=text,
            grapheme_count=count_graphemes(text),
            thumbnail_status=(
                thumbnail_status_out[-1]
                if thumbnail_status_out
                else "none"
            ),
        )

        DRY_RUN_RESULTS.append(result)

        return result

    response = client.send_post(
        text=text,
        facets=builder.build_facets(),
        embed=embed,
    )

    return response


# ============================================================
# メイン
# ============================================================

def parse_args(argv=None):
    """
    コマンドライン引数を解析する。
    """
    parser = argparse.ArgumentParser(
        prog="x2b",
        description=(
            "Cross-post X list posts to Bluesky."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run the posting pipeline without publishing to Bluesky "
            "or modifying seen state."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    dry_run = args.dry_run

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

        if dry_run:
            print(
                " Mode: DRY-RUN "
                "(nothing will be published;"
                " seen state will not change)"
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
        would_post_count = 0
        permanent_fail_count = 0
        transient_fail_count = 0
        unknown_fail_count = 0

        # Dry-run結果は実行ごとに集計する
        DRY_RUN_RESULTS.clear()

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

            outcome = process_single_post(
                client,
                post,
                users,
                seen,
                dry_run=dry_run,
            )

            if outcome == "success":
                success_count += 1
            elif outcome == "would_post":
                would_post_count += 1
            elif outcome == "permanent":
                permanent_fail_count += 1
            elif outcome == "transient":
                transient_fail_count += 1
            elif outcome == "unknown":
                unknown_fail_count += 1

            # ----------------------------------------------------
            # 次の投稿まで3秒
            # （Dry-runでは実投稿がないため待機しない）
            # ----------------------------------------------------

            if (
                index < len(new_posts) - 1
                and not dry_run
            ):

                print(
                    f"Waiting "
                    f"{POST_INTERVAL} seconds..."
                )

                time.sleep(
                    POST_INTERVAL
                )

        print()

        if dry_run:
            # Dry-runは「何も投稿していない」ことを
            # 明確に区別できるサマリで締める
            thumbnail_skipped = sum(
                1 for result in DRY_RUN_RESULTS
                if result.thumbnail_skipped
            )

            print(
                "=========================================="
            )
            print(
                " DRY-RUN SUMMARY "
                "(nothing was published;"
                " seen.json was not modified)"
            )
            print(
                "=========================================="
            )
            print(
                f"fetched: {len(posts)}"
            )
            print(
                f"skipped_seen: {skipped_seen}"
            )
            print(
                f"skipped_no_id: {skipped_no_id}"
            )
            print(
                f"new: {len(new_posts)}"
            )
            print(
                f"would_post: {would_post_count}"
            )
            print(
                f"permanent_failures: {permanent_fail_count}"
            )
            print(
                f"transient_failures: {transient_fail_count}"
            )
            print(
                f"unknown_failures: {unknown_fail_count}"
            )
            print(
                f"thumbnail_skipped: {thumbnail_skipped}"
            )
        else:
            print(
                f"Summary: Success: {success_count}, "
                f"Permanent failures: {permanent_fail_count}, "
                f"Transient failures: {transient_fail_count}, "
                f"Unknown failures: {unknown_fail_count}"
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
