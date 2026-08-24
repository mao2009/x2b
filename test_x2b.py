"""
x2b regression tests (Issue #2).

既知のBluesky投稿エラー（本文300グラフェム制限、
サムネイル1,000,000バイト制限）が、API送信前に防止されることを保証する。

実際のBlueskyアカウントには一切投稿しない。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import grapheme
import pytest
import requests
from atproto import models
from atproto_client.models.blob_ref import IpldLink

sys.path.insert(0, str(Path(__file__).resolve().parent))

import x2b
from x2b import (
    BSKY_MAX_TEXT_GRAPHEMES,
    BSKY_MAX_THUMBNAIL_BYTES,
    MIN_BODY_GRAPHEMES,
    TEXT_LENGTH_MARGIN,
    PermanentError,
    TransientError,
    _post_to_bluesky,
    build_post,
    classify_error,
    count_graphemes,
    create_external_embed,
    post_with_retry,
    truncate_text_to_graphemes,
    validate_post_text,
)

# ============================================================
# ヘルパー
# ============================================================

def fresh_user(name="Test User", screen_name="testuser"):
    """キャッシュヒットする（= twitter-cliを起動しない）ユーザーエントリ。"""
    return {
        "name": name,
        "screenName": screen_name,
        "updatedAt": x2b.iso_now(),
    }


@pytest.fixture
def users():
    return {
        "testuser": fresh_user(),
        "reposter": fresh_user("Reposter User", "reposter"),
    }


def make_post(text="hello", is_retweet=False, post_id="1234567890", **extra):
    post = {
        "id": post_id,
        "text": text,
        "isRetweet": is_retweet,
        "author": {"screenName": "testuser"},
    }
    post.update(extra)
    return post


def get_prefix(users, is_retweet=False, retweeted_by=None,
               screen_name="testuser"):
    """
    空本文でbuild_postを実行し、プレフィックス部分のみを取り出す。

    「入力本文 → prefix付与 → 最終本文」の関係テストに使用する。
    """
    post = make_post(
        text="",
        is_retweet=is_retweet,
    )
    post["author"] = {"screenName": screen_name}

    if retweeted_by:
        post["retweetedBy"] = retweeted_by

    return build_post(post, users).build_text()


def make_upload_response(content):
    """upload_blobが返す実物モデルのレスポンス（pydantic検証を通るBlobRef）。"""
    blob_ref = models.ComAtprotoRepoDefs.BlobRef(
        ref=IpldLink(**{"$link": "bafkreitest"}),
        mime_type="image/png",
        size=len(content),
    )
    return models.ComAtprotoRepoUploadBlob.Response(blob=blob_ref)


# ============================================================
# グラフェムカウント
# ============================================================

class TestCountGraphemes:

    def test_ascii(self):
        assert count_graphemes("abc") == 3

    def test_multi_codepoint_family_emoji_is_one_grapheme(self):
        assert count_graphemes("\U0001F468\u200D\U0001F469"
                               "\u200D\U0001F467"
                               "\u200D\U0001F466") == 1

    def test_flag_emoji_is_one_grapheme(self):
        assert count_graphemes("\U0001F1EF\U0001F1F5") == 1

    def test_skin_tone_modifier_is_part_of_grapheme(self):
        assert count_graphemes("\U0001F44B\U0001F3FD") == 1

    def test_combining_character(self):
        # e + U+0301 は1グラフェム（コードポイントは2）
        text = "cafe\u0301"
        assert len(text) == 5
        assert count_graphemes(text) == 4


# ============================================================
# 切り詰め（truncate_text_to_graphemes）
# ============================================================

class TestTruncateTextToGraphemes:

    def test_exact_limit_returns_unchanged(self):
        text = "a" * BSKY_MAX_TEXT_GRAPHEMES
        assert truncate_text_to_graphemes(
            text, BSKY_MAX_TEXT_GRAPHEMES
        ) == text

    def test_over_limit_produces_exact_size(self):
        out = truncate_text_to_graphemes(
            "a" * (BSKY_MAX_TEXT_GRAPHEMES + 1),
            BSKY_MAX_TEXT_GRAPHEMES,
        )
        assert count_graphemes(out) == BSKY_MAX_TEXT_GRAPHEMES
        assert out.endswith("…")
        # 元の本文が可能な限り保持される
        assert out.startswith("a" * (BSKY_MAX_TEXT_GRAPHEMES - 1))

    def test_within_limit_returns_unchanged(self):
        text = "a" * 100
        assert truncate_text_to_graphemes(text, 300) == text

    def test_zero_max_returns_empty(self):
        assert truncate_text_to_graphemes("abc", 0) == ""

    def test_never_exceeds_limit_for_various_inputs(self):
        samples = [
            "x" * 1000,
            "\U0001F468\u200D\U0001F469\u200D\U0001F467"
            "\u200D\U0001F466" * 400,
            "e\u0301" * 500,
            "日本語のテキストです。" * 100,
            "\U0001F1EF\U0001F1F5 mixed café \u00E8" * 50,
        ]
        for sample in samples:
            for max_g in (1, MIN_BODY_GRAPHEMES, 299, 300):
                out = truncate_text_to_graphemes(sample, max_g)
                assert count_graphemes(out) <= max_g

    def test_multi_codepoint_emoji_not_broken_in_half(self):
        units = [
            "\U0001F468\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466",
            "\U0001F1EF\U0001F1F5",
            "\U0001F44B\U0001F3FD",
        ]
        text = "".join(units * 50)
        out = truncate_text_to_graphemes(text, 30)

        assert count_graphemes(out) == 30
        # 各グラフェムは元の絵文字の完全なものか、省略記号
        for g in grapheme.graphemes(out):
            assert g in set(units) | {"…"}

    def test_combining_characters_not_split(self):
        unit = "e\u0301"  # é（結合文字）
        text = unit * 100
        out = truncate_text_to_graphemes(text, 40)

        assert count_graphemes(out) == 40
        for g in grapheme.graphemes(out):
            assert g in {unit, "…"}


# ============================================================
# 送信前バリデーション（validate_post_text）
# ============================================================

class TestValidatePostText:

    def test_exactly_300_graphemes_passes(self):
        validate_post_text("a" * 300)

    def test_exactly_300_emoji_graphemes_passes(self):
        validate_post_text(
            "\U0001F468\u200D\U0001F469"
            "\u200D\U0001F467\u200D\U0001F466" * 300
        )

    def test_301_graphemes_raises_permanent_error(self):
        with pytest.raises(PermanentError) as excinfo:
            validate_post_text("a" * 301)
        message = str(excinfo.value)
        assert "301" in message
        assert "300" in message

    def test_emoji_boundary_raises_permanent_error(self):
        family = ("\U0001F468\u200D\U0001F469"
                  "\u200D\U0001F467\u200D\U0001F466")
        with pytest.raises(PermanentError):
            # 300グラフェム + 1 ASCII = 301
            validate_post_text(family * 300 + "a")

    def test_combining_character_counting_in_validation(self):
        # e+U+0301 x300 = 300グラフェム（コードポイントは600）なので通過
        validate_post_text("e\u0301" * 300)
        with pytest.raises(PermanentError):
            validate_post_text("e\u0301" * 301)


# ============================================================
# build_post: 入力本文 → prefix付与 → 最終本文 の関係
# ============================================================

class TestBuildPostTextLength:

    def test_short_body_kept_verbatim_after_prefix(self, users):
        prefix = get_prefix(users)
        final = build_post(
            make_post(text="Hello world"),
            users,
        ).build_text()

        assert final.startswith(prefix)
        assert final[len(prefix):] == "Hello world"
        assert count_graphemes(final) < BSKY_MAX_TEXT_GRAPHEMES

    def test_empty_body_produces_prefix_only(self, users):
        prefix = get_prefix(users)
        final = build_post(make_post(text=""), users).build_text()
        assert final == prefix

    def test_long_body_truncated_and_under_limit(self, users):
        prefix = get_prefix(users)

        final = build_post(
            make_post(text="x" * 10000),
            users,
        ).build_text()

        assert final.startswith(prefix)
        assert final.endswith("…")
        total = count_graphemes(final)
        assert total <= BSKY_MAX_TEXT_GRAPHEMES
        # 通常のプレフィックスでは マージン込みで 300-10=290 まで保持される
        # （total = prefix + (300 - prefix - margin) = 300 - margin）
        assert total == BSKY_MAX_TEXT_GRAPHEMES - TEXT_LENGTH_MARGIN

    def test_prefix_plus_body_just_over_limit(self, users):
        prefix = get_prefix(users)
        prefix_length = count_graphemes(prefix)

        # prefix + 本文 がちょうど301グラフェムになる入力
        body_length = BSKY_MAX_TEXT_GRAPHEMES + 1 - prefix_length
        body = "y" * body_length

        final = build_post(
            make_post(text=body),
            users,
        ).build_text()

        assert count_graphemes(final) <= BSKY_MAX_TEXT_GRAPHEMES
        assert final.endswith("…")
        validate_post_text(final)

    def test_emoji_body_truncated_without_corruption(self, users):
        family = ("\U0001F468\u200D\U0001F469"
                  "\u200D\U0001F467\u200D\U0001F466")
        prefix = get_prefix(users)
        final = build_post(
            make_post(text=family * 500),
            users,
        ).build_text()

        assert count_graphemes(final) <= BSKY_MAX_TEXT_GRAPHEMES
        # 本文部分のみ検証（プレフィックスには別の絵文字が含まれる）
        body = final[len(prefix):]
        for g in grapheme.graphemes(body):
            assert g == family or g == "…"

    def test_non_ascii_japanese_body_truncated(self, users):
        final = build_post(
            make_post(text="日本語の投稿です。" * 200),
            users,
        ).build_text()

        assert count_graphemes(final) <= BSKY_MAX_TEXT_GRAPHEMES
        assert final.endswith("…")
        validate_post_text(final)

    def test_facets_built_from_truncated_text(self, users):
        body = ("https://example.com/very/long/url "
                + "q" * 600)
        builder = build_post(
            make_post(text=body),
            users,
        )

        facets = builder.build_facets()
        assert facets  # URL facetが生成されている

        text = builder.build_text()
        byte_length = len(text.encode("utf-8"))
        for facet in facets:
            start, end = facet.index.byte_start, facet.index.byte_end
            assert 0 <= start < end <= byte_length

    def test_retweet_prefix_consistent_and_truncated(self, users):
        prefix = get_prefix(
            users,
            is_retweet=True,
            retweeted_by="reposter",
        )

        final = build_post(
            make_post(
                text="r" * 5000,
                is_retweet=True,
                retweetedBy="reposter",
            ),
            users,
        ).build_text()

        assert final.startswith(prefix)
        assert count_graphemes(final) <= BSKY_MAX_TEXT_GRAPHEMES
        validate_post_text(final)


class TestBuildPostLongPrefix:

    @pytest.fixture
    def long_prefix_users(self):
        # プレフィックス合計 ≒ 294グラフェム（マージン分より長い）
        return {
            "biguser": fresh_user(
                name="N" * 240,
                screen_name="s" * 45,
            ),
        }

    def test_long_prefix_clamped_so_total_never_exceeds_limit(
        self, long_prefix_users
    ):
        prefix = get_prefix(long_prefix_users, screen_name="biguser")
        prefix_length = count_graphemes(prefix)

        # 通常の余裕(10)を引くと最小保証(50)を下回るプレフィックス
        assert (
            BSKY_MAX_TEXT_GRAPHEMES - prefix_length
            < MIN_BODY_GRAPHEMES
        )

        final = build_post(
            make_post(
                text="z" * 100,
                post_id="777000111",
                author={"screenName": "biguser"},
            ),
            long_prefix_users,
        ).build_text()

        total = count_graphemes(final)
        assert total <= BSKY_MAX_TEXT_GRAPHEMES
        validate_post_text(final)
        # 可能な限り本文を保持する（空にはしない）
        assert total > prefix_length

    def test_prefix_longer_than_limit_raises_permanent_error(self, users):
        huge_users = {
            "hugeuser": fresh_user(
                name="H" * 300,
                screen_name="h" * 45,
            ),
        }

        with pytest.raises(PermanentError) as excinfo:
            build_post(
                make_post(
                    text="hi",
                    post_id="999888777",
                    author={"screenName": "hugeuser"},
                ),
                huge_users,
            )

        # X post IDと原因がログ（例外メッセージ）に残る
        assert "999888777" in str(excinfo.value)
        assert "Prefix too long" in str(excinfo.value)


# ============================================================
# サムネイルサイズ制限（create_external_embed）
# ============================================================

def run_create_embed(content=None, download_exc=None,
                     thumbnail_url="https://img.example/t.jpg"):
    client = MagicMock()
    ogp = {"title": "T", "description": "D", "image": None}

    kwargs = {}
    if content is not None:
        response = MagicMock()
        response.content = content
        response.raise_for_status.return_value = None
        kwargs["return_value"] = response
        # upload_blobは実物モデルを返す（pydanticの検証を通すため）
    else:
        kwargs["side_effect"] = download_exc

    if content is not None:
        client.upload_blob.return_value = make_upload_response(content)

    with patch("x2b.requests.get", **kwargs):
        embed = create_external_embed(
            client,
            "https://example.com/post",
            ogp,
            thumbnail_url,
        )

    return client, embed


class TestCreateExternalEmbedThumbnail:

    def test_below_limit_uploaded(self):
        content = b"a" * (BSKY_MAX_THUMBNAIL_BYTES - 1)
        client, embed = run_create_embed(content)

        client.upload_blob.assert_called_once_with(content)
        assert embed.external.thumb is not None
        assert embed.external.thumb.size == len(content)

    def test_at_limit_is_accepted(self):
        # Blueskyの上限は1,000,000バイトちょうどまで許容（>で比較）
        content = b"a" * BSKY_MAX_THUMBNAIL_BYTES
        client, embed = run_create_embed(content)

        client.upload_blob.assert_called_once_with(content)
        assert embed.external.thumb is not None
        assert embed.external.thumb.size == BSKY_MAX_THUMBNAIL_BYTES

    def test_above_limit_skipped_without_upload(self):
        content = b"a" * (BSKY_MAX_THUMBNAIL_BYTES + 1)
        client, embed = run_create_embed(content)

        # upload前に検証されるため、upload_blobは呼ばれない
        client.upload_blob.assert_not_called()
        assert embed.external.thumb is None

    def test_no_thumbnail_url_no_download_no_upload(self):
        client = MagicMock()
        client.upload_blob.return_value = make_upload_response(b"x" * 10)
        ogp = {"title": "T", "description": "D", "image": None}

        with patch("x2b.requests.get") as mock_get:
            embed = create_external_embed(
                client,
                "https://example.com/post",
                ogp,
                None,
            )

        # ダウンロードもアップロードも発生しない
        mock_get.assert_not_called()
        client.upload_blob.assert_not_called()
        assert embed.external.thumb is None

    def test_download_failure_does_not_raise(self):
        client, embed = run_create_embed(
            download_exc=requests.ConnectionError("boom"),
        )

        client.upload_blob.assert_not_called()
        # サムネイルなしでembedを作成し、投稿継続できる
        assert embed.external.thumb is None

    def test_upload_failure_does_not_raise(self):
        client = MagicMock()
        client.upload_blob.side_effect = RuntimeError("upload failed")
        ogp = {"title": "T", "description": "D", "image": None}

        with patch(
            "x2b.requests.get",
            return_value=MagicMock(
                content=b"a" * 100,
                raise_for_status=lambda: None,
            ),
        ):
            embed = create_external_embed(
                client,
                "https://example.com/post",
                ogp,
                "https://img.example/t.jpg",
            )

        # アップロード失敗してもサムネイルなしで投稿継続
        assert embed.external.thumb is None


# ============================================================
# API送信前バリデーションの順序
# （X post → build/transform → validate → Bluesky API）
# ============================================================

class TestPostToBlueskyValidationOrder:

    def test_success_path_without_thumbnail(self, users):
        client = MagicMock()
        ogp = {"title": "T", "description": "D", "image": None}

        with patch("x2b.get_ogp", return_value=ogp):
            response = _post_to_bluesky(
                client,
                make_post(text="hi there"),
                users,
            )

        assert response is client.send_post.return_value
        client.upload_blob.assert_not_called()
        client.send_post.assert_called_once()

        sent_text = client.send_post.call_args.kwargs["text"]
        assert sent_text.startswith("📢 ")
        # 送信する本文は必ず制限内
        validate_post_text(sent_text)

    def test_oversized_thumbnail_still_posts_text(self, users):
        client = MagicMock()
        ogp = {
            "title": "T",
            "description": "D",
            "image": "https://img.example/huge.jpg",
        }
        oversized = MagicMock(
            content=b"a" * (BSKY_MAX_THUMBNAIL_BYTES + 1)
        )
        oversized.raise_for_status.return_value = None

        with patch("x2b.get_ogp", return_value=ogp), \
             patch("x2b.requests.get", return_value=oversized):
            response = _post_to_bluesky(
                client,
                make_post(text="hello"),
                users,
            )

        # 投稿全体が失われず、本文だけでも送信される
        assert response is client.send_post.return_value
        client.send_post.assert_called_once()

        embed = client.send_post.call_args.kwargs["embed"]
        assert embed.external.thumb is None
        client.upload_blob.assert_not_called()

    def test_invalid_payload_blocked_before_any_api_operation(self, users):
        client = MagicMock()

        # build_postが制限超過の本文を返したケースをシミュレート
        fake_builder = MagicMock()
        fake_builder.build_text.return_value = (
            "a" * (BSKY_MAX_TEXT_GRAPHEMES + 1)
        )
        fake_builder.build_facets.return_value = []

        with patch("x2b.build_post", return_value=fake_builder), \
             patch("x2b.get_ogp") as mock_get_ogp, \
             pytest.raises(PermanentError) as excinfo:
            _post_to_bluesky(
                client,
                make_post(post_id="555000222"),
                users,
            )

        # Bluesky API操作（OGP取得・blob upload・send_post）は一切発生しない
        client.send_post.assert_not_called()
        client.upload_blob.assert_not_called()
        mock_get_ogp.assert_not_called()
        assert "graphemes" in str(excinfo.value)


# ============================================================
# エラー分類・リトライとの整合性
# ============================================================

class TestErrorHandlingIntegration:

    def test_classify_error_passes_through_permanent_error(self):
        error = PermanentError(
            "Post text too long: 301 graphemes (max 300)"
        )
        assert classify_error(error) is error

    def test_classify_error_passes_through_transient_error(self):
        error = TransientError("Network error: timeout")
        classified = classify_error(error)
        assert isinstance(classified, TransientError)

    def test_permanent_validation_error_is_not_retried(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(
            x2b.time, "sleep", lambda seconds: sleeps.append(seconds)
        )

        attempts = []

        def failing_post(*args, **kwargs):
            attempts.append(1)
            raise PermanentError(
                "Post text too long: 301 graphemes (max 300)"
            )

        with patch("x2b._post_to_bluesky", side_effect=failing_post), \
             pytest.raises(PermanentError):
            post_with_retry(MagicMock(), make_post(), {})

        # 無限リトライ・不要なsleepなし（1回だけで即raise）
        assert len(attempts) == 1
        assert sleeps == []

    def test_transient_errors_are_retried_then_succeed(self, monkeypatch):
        delays = []
        monkeypatch.setattr(
            x2b.time, "sleep", lambda seconds: delays.append(seconds)
        )

        responses = iter([
            TransientError("Network error"),
            TransientError("Rate limited"),
            "ok-response",
        ])

        def flaky_post(*args, **kwargs):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item

        with patch("x2b._post_to_bluesky", side_effect=flaky_post):
            result = post_with_retry(MagicMock(), make_post(), {})

        assert result == "ok-response"
        assert len(delays) == 2  # 指数バックオフで2回待機


# ============================================================
# seen状態
# ============================================================

class TestSeenState:

    def test_seen_roundtrip(self, tmp_path, monkeypatch):
        state_file = tmp_path / "seen.json"
        monkeypatch.setattr(x2b, "STATE_FILE", state_file)

        seen = {"111", "222"}
        x2b.save_seen(seen)
        assert x2b.load_seen() == seen

    def test_thumbnail_skip_success_would_be_marked_seen(self, users):
        """
        oversized thumbnailをskipして本文投稿が成功した場合、
        send_postが正常レスポンスを返すためseen登録対象になることを確認。
        （main loopはresponseがtruthyな場合のみseen.addする）
        """
        client = MagicMock()
        client.send_post.return_value = MagicMock(uri="at://did:ok")
        ogp = {
            "title": "T",
            "description": "D",
            "image": "https://img.example/huge.jpg",
        }
        oversized = MagicMock(
            content=b"a" * (BSKY_MAX_THUMBNAIL_BYTES + 1)
        )
        oversized.raise_for_status.return_value = None

        with patch("x2b.get_ogp", return_value=ogp), \
             patch("x2b.requests.get", return_value=oversized):
            response = _post_to_bluesky(
                client,
                make_post(post_id="333444555"),
                users,
            )

        assert response is not None
        assert response.uri
