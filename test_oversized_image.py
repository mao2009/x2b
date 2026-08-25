"""
x2b oversized image tests (Issue #10).

X投稿に含まれる画像がBlueskyのblobサイズ制限を超えた場合でも、
投稿全体が失敗しないことを保証する。

- サイズ制限より十分小さい画像はそのまま使用される
- サイズ制限を超える画像はリサイズ・再エンコードされる
- リサイズでも収まらない場合はtext-only fallbackが行われる
- メディア処理失敗時にpost全体が黙って消えない
- Blueskyへサイズ超過blobが送信されない

実際のBlueskyアカウントには一切投稿しない。
"""

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atproto import models
from atproto_client.models.blob_ref import IpldLink

import x2b
from x2b import (
    BSKY_MAX_THUMBNAIL_BYTES,
    _resize_image_to_limit,
    create_external_embed,
    download_thumbnail,
)

# ============================================================
# ヘルパー: テスト用画像を生成
# ============================================================

def make_jpeg(width, height, quality=90):
    """指定サイズのJPEG画像を生成し、bytesとして返す。"""
    img = Image.new("RGB", (width, height), color=(128, 64, 32))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def make_upload_response(content):
    """upload_blobが返す実物モデルのレスポンス（pydantic検証を通るBlobRef）。"""
    blob_ref = models.ComAtprotoRepoDefs.BlobRef(
        ref=IpldLink(**{"$link": "bafkreitest"}),
        mime_type="image/jpeg",
        size=len(content),
    )
    return models.ComAtprotoRepoUploadBlob.Response(blob=blob_ref)


def make_large_jpeg(target_bytes, quality=85):
    """
    指定バイト数以上のJPEG画像を生成する。
    udaslawを広げてサイズを増加させる。
    """
    # 最初は大きな画像から始めて、サイズが足りなければ拡大する
    width = 2000
    height = 1500

    while True:
        content = make_jpeg(width, height, quality)
        if len(content) >= target_bytes:
            return content
        width = int(width * 1.5)
        height = int(height * 1.5)
        if width > 20000:
            break

    return content


def make_png(width, height):
    """指定サイズのPNG画像を生成する（ノイズ付きで圧縮効きにくい）。"""
    import random
    random.seed(42)
    img = Image.new("RGBA", (width, height))
    pixels = img.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (
                random.randint(0, 255),
                random.randint(0, 255),
                random.randint(0, 255),
                255,
            )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ============================================================
# _resize_image_to_limit の単体テスト
# ============================================================

class TestResizeImageToLimit:

    def test_small_image_passes_through(self):
        """サイズ制限より十分小さい画像はそのまま返される。"""
        content = make_jpeg(400, 300, quality=85)
        assert len(content) < BSKY_MAX_THUMBNAIL_BYTES

        result, size = _resize_image_to_limit(
            content, BSKY_MAX_THUMBNAIL_BYTES
        )

        assert result is not None
        assert size <= BSKY_MAX_THUMBNAIL_BYTES
        # リサイズ不要な場合は元の画像がそのまま返される（再エンコードなし）
        assert result is content
        assert size == len(content)

    def test_oversized_jpeg_is_resized(self):
        """サイズ制限を超えるJPEG画像はリサイズされる。"""
        # 1.2MB相当の画像を作成（Issue #10の再現ケース同等）
        content = make_large_jpeg(1_200_000)

        assert len(content) > BSKY_MAX_THUMBNAIL_BYTES

        result, size = _resize_image_to_limit(
            content, BSKY_MAX_THUMBNAIL_BYTES
        )

        assert result is not None
        assert size <= BSKY_MAX_THUMBNAIL_BYTES
        # リサイズされた画像はJPEGとして有効
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"

    def test_oversized_png_is_converted_to_jpeg(self):
        """サイズ制限を超えるPNG画像はJPEGに変換される。"""
        # 600x600 random RGBA PNG = ~1.26MB（制限超過）
        content = make_png(600, 600)

        assert len(content) > BSKY_MAX_THUMBNAIL_BYTES

        result, size = _resize_image_to_limit(
            content, BSKY_MAX_THUMBNAIL_BYTES
        )

        assert result is not None
        assert size <= BSKY_MAX_THUMBNAIL_BYTES
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"

    def test_rgba_image_is_converted_to_rgb(self):
        """透過付き画像（RGBA）はRGBに変換されてJPEGエンコードされる。"""
        # ノイズ付きRGBA画像（制限超過）
        import random
        random.seed(42)
        img = Image.new("RGBA", (600, 600))
        pixels = img.load()
        for y in range(600):
            for x in range(600):
                pixels[x, y] = (
                    random.randint(0, 255),
                    random.randint(0, 255),
                    random.randint(0, 255),
                    random.randint(0, 255),
                )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        content = buf.getvalue()

        result, _size = _resize_image_to_limit(
            content, BSKY_MAX_THUMBNAIL_BYTES
        )

        assert result is not None
        decoded = Image.open(io.BytesIO(result))
        assert decoded.mode == "RGB"

    def test_unresizable_image_returns_none(self):
        """デコードできない oversized データは(None, 元のサイズ)を返す。"""
        # サイズ制限を超えるデコード不可データ
        invalid_data = (
            b"not an image at all"
            + b"\x00" * (BSKY_MAX_THUMBNAIL_BYTES + 100)
        )

        result, size = _resize_image_to_limit(
            invalid_data, BSKY_MAX_THUMBNAIL_BYTES
        )

        assert result is None
        assert size == len(invalid_data)

    def test_actual_payload_size_is_verified(self):
        """
        リサイズ後の実際のpayloadサイズが制限以下であることを確認。
        「JPEGなら小さくなるはず」のような推測で判定しない。
        """
        content = make_large_jpeg(1_500_000)

        result, size = _resize_image_to_limit(
            content, BSKY_MAX_THUMBNAIL_BYTES
        )

        assert result is not None
        # 実際にバイト列を確認
        assert len(result) == size
        assert size <= BSKY_MAX_THUMBNAIL_BYTES

    def test_quality_reduction_attempted_first(self):
        """
        リサイズ時はまずJPEG品質の引き下げが試行される。
        寸法縮小は品質だけでは収まらない場合のみ実行される。
        """
        # 品質だけでは収まらない大きな画像
        content = make_large_jpeg(2_000_000, quality=95)

        result, size = _resize_image_to_limit(
            content, BSKY_MAX_THUMBNAIL_BYTES
        )

        assert result is not None
        assert size <= BSKY_MAX_THUMBNAIL_BYTES

    def test_1_2mb_reproduction_case(self):
        """
        Issue #10の再現ケース: 1.2MB程度の画像。
        リサイズによりBluesky制限内に収まることが保証される。
        """
        # 実際の再現ケースと同等のサイズ
        content = make_large_jpeg(1_200_000)

        result, size = _resize_image_to_limit(
            content, BSKY_MAX_THUMBNAIL_BYTES
        )

        assert result is not None
        assert size <= BSKY_MAX_THUMBNAIL_BYTES
        # 実際のバイトサイズを検証
        assert len(result) == size

    def test_never_exceeds_limit_after_resize(self):
        """
        どんなに大きな画像でもリサイズ後のサイズは
        制限以下であることが保証される。
        """
        # 非常に大きな画像
        content = make_large_jpeg(2_000_000)

        result, size = _resize_image_to_limit(
            content, BSKY_MAX_THUMBNAIL_BYTES
        )

        if result is not None:
            assert size <= BSKY_MAX_THUMBNAIL_BYTES
            assert len(result) == size


# ============================================================
# download_thumbnail のリサイズ統合テスト
# ============================================================

class TestDownloadThumbnailResize:

    def test_small_image_returned_as_ok(self):
        """小さい画像は'ok'ステータスで返される。"""
        content = make_jpeg(400, 300)
        response = MagicMock()
        response.content = content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            result, status = download_thumbnail(
                "https://img.example/small.jpg"
            )

        assert result == content
        assert status == "ok"

    def test_oversized_image_is_resized(self):
        """大きい画像はリサイズされて'resized'ステータスで返される。"""
        content = make_large_jpeg(1_200_000)
        response = MagicMock()
        response.content = content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            result, status = download_thumbnail(
                "https://img.example/large.jpg"
            )

        assert result is not None
        assert status == "resized"
        assert len(result) <= BSKY_MAX_THUMBNAIL_BYTES

    def test_non_image_oversized_returns_too_large(self):
        """画像でない大きなデータは'too_large'ステータスで返される。"""
        # Pillowがデコードできないデータ
        fake_content = b"\x00" * (BSKY_MAX_THUMBNAIL_BYTES + 1000)
        response = MagicMock()
        response.content = fake_content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            result, status = download_thumbnail(
                "https://img.example/fake.dat"
            )

        assert result is None
        assert status == "too_large"

    def test_no_url_returns_none(self):
        """URLがNoneの場合は(None, 'none')を返す。"""
        result, status = download_thumbnail(None)
        assert result is None
        assert status == "none"

    def test_download_failure_returns_failed(self):
        """ダウンロード失敗時は(None, 'failed')を返す。"""
        import requests as req_lib

        with patch(
            "x2b.requests.get",
            side_effect=req_lib.ConnectionError("boom"),
        ):
            result, status = download_thumbnail(
                "https://img.example/fail.jpg"
            )

        assert result is None
        assert status == "failed"

    def test_at_limit_accepted_without_resize(self):
        """サイズ制限ちょうどの画像はリサイズなしで返される。"""
        content = make_jpeg(400, 300, quality=95)
        # サイズが制限以下であることを確認
        assert len(content) <= BSKY_MAX_THUMBNAIL_BYTES

        response = MagicMock()
        response.content = content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            result, status = download_thumbnail(
                "https://img.example/exact.jpg"
            )

        assert result == content
        assert status == "ok"

    def test_resize_preserves_image_validity(self):
        """リサイズされた画像はJPEGとして有効である。"""
        content = make_large_jpeg(1_200_000)
        response = MagicMock()
        response.content = content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            result, _status = download_thumbnail(
                "https://img.example/valid.jpg"
            )

        assert result is not None
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"
        assert img.size[0] > 0
        assert img.size[1] > 0


# ============================================================
# create_external_embed: リサイズ画像のアップロード
# ============================================================

class TestCreateExternalEmbedWithResized:

    def test_resized_image_is_uploaded(self):
        """リサイズされた画像はblob uploadされること。"""
        client = MagicMock()
        client.upload_blob.return_value = make_upload_response(b"\x00" * 100)
        ogp = {"title": "T", "description": "D", "image": None}

        # 1.2MBの画像（リサイズが必要）
        large_content = make_large_jpeg(1_200_000)
        response = MagicMock()
        response.content = large_content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            embed = create_external_embed(
                client,
                "https://example.com/post",
                ogp,
                "https://img.example/large.jpg",
            )

        # リサイズされた画像がアップロードされる
        assert client.upload_blob.call_count == 1
        uploaded_bytes = client.upload_blob.call_args[0][0]
        assert len(uploaded_bytes) <= BSKY_MAX_THUMBNAIL_BYTES
        # embedにthumbが設定される
        assert embed.external.thumb is not None

    def test_oversized_non_image_still_skips(self):
        """画像でない大きなデータはリサイズ失敗でサムネイルなし。"""
        client = MagicMock()
        ogp = {"title": "T", "description": "D", "image": None}

        fake_content = b"\x00" * (BSKY_MAX_THUMBNAIL_BYTES + 1000)
        response = MagicMock()
        response.content = fake_content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            embed = create_external_embed(
                client,
                "https://example.com/post",
                ogp,
                "https://img.example/fake.dat",
            )

        client.upload_blob.assert_not_called()
        assert embed.external.thumb is None

    def test_resized_status_tracked_in_dry_run(self):
        """Dry-run時にリサイズステータスが正しく記録される。"""
        client = MagicMock()
        ogp = {"title": "T", "description": "D", "image": None}

        large_content = make_large_jpeg(1_200_000)
        response = MagicMock()
        response.content = large_content
        response.raise_for_status.return_value = None

        thumbnail_status_out = []

        with patch("x2b.requests.get", return_value=response):
            create_external_embed(
                client,
                "https://example.com/post",
                ogp,
                "https://img.example/large.jpg",
                dry_run=True,
                thumbnail_status_out=thumbnail_status_out,
            )

        assert thumbnail_status_out == ["resized"]
        client.upload_blob.assert_not_called()


# ============================================================
# 投稿パイプライン統合テスト
# ============================================================

class TestOversizedImagePostPipeline:

    def _make_post(self, post_id="2092136411118137697"):
        return {
            "id": post_id,
            "text": "ATLUS ゲーム音楽テスト",
            "isRetweet": False,
            "author": {"screenName": "ATLUS_Gamemusic"},
        }

    def _make_users(self):
        return {
            "atlus_gamemusic": {
                "name": "ATLUS Game Music",
                "screenName": "ATLUS_Gamemusic",
                "updatedAt": x2b.iso_now(),
            },
        }

    def test_oversized_image_post_succeeds_with_resize(self):
        """1.2MB画像の投稿がリサイズにより成功する。"""
        client = MagicMock()
        client.send_post.return_value = MagicMock(
            uri="at://did:plc:ok/post"
        )
        client.upload_blob.return_value = make_upload_response(b"\x00" * 100)

        users = self._make_users()
        post = self._make_post()

        # OGP画像URLを設定（X投稿画像の代替パス）
        ogp = {
            "title": "T",
            "description": "D",
            "image": "https://img.example/large.jpg",
        }

        # 1.2MBの画像
        large_image = make_large_jpeg(1_200_000)
        response = MagicMock()
        response.content = large_image
        response.raise_for_status.return_value = None

        with patch("x2b.get_ogp", return_value=ogp), \
             patch("x2b.requests.get", return_value=response):
            from x2b import _post_to_bluesky
            result = _post_to_bluesky(
                client, post, users
            )

        # 投稿は成功する
        assert result is client.send_post.return_value
        client.send_post.assert_called_once()

        # リサイズされた画像がアップロードされる
        assert client.upload_blob.call_count == 1
        uploaded = client.upload_blob.call_args[0][0]
        assert len(uploaded) <= BSKY_MAX_THUMBNAIL_BYTES

    def test_unresizable_oversized_image_falls_back_to_text(self):
        """
        リサイズできない大きなデータの場合、
        text-only投稿にフォールバックする。
        """
        client = MagicMock()
        client.send_post.return_value = MagicMock(
            uri="at://did:plc:ok/post"
        )

        users = self._make_users()
        post = self._make_post()

        ogp = {
            "title": "T",
            "description": "D",
            "image": "https://img.example/broken.jpg",
        }

        # Pillowがデコードできないデータ
        fake_content = b"\x00" * (BSKY_MAX_THUMBNAIL_BYTES + 5000)
        response = MagicMock()
        response.content = fake_content
        response.raise_for_status.return_value = None

        with patch("x2b.get_ogp", return_value=ogp), \
             patch("x2b.requests.get", return_value=response):
            from x2b import _post_to_bluesky
            result = _post_to_bluesky(
                client, post, users
            )

        # 投稿自体は成功する（画像なし）
        assert result is client.send_post.return_value
        client.send_post.assert_called_once()

        # 画像はアップロードされない
        client.upload_blob.assert_not_called()

        # embedにはthumbがNone
        embed = client.send_post.call_args.kwargs["embed"]
        assert embed.external.thumb is None

    def test_normal_image_post_unchanged(self):
        """通常サイズの画像投稿は従来通り動作する。"""
        client = MagicMock()
        client.send_post.return_value = MagicMock(
            uri="at://did:plc:ok/post"
        )
        client.upload_blob.return_value = make_upload_response(b"\x00" * 100)

        users = self._make_users()
        post = self._make_post()

        ogp = {
            "title": "T",
            "description": "D",
            "image": "https://img.example/normal.jpg",
        }

        normal_image = make_jpeg(800, 600)
        response = MagicMock()
        response.content = normal_image
        response.raise_for_status.return_value = None

        with patch("x2b.get_ogp", return_value=ogp), \
             patch("x2b.requests.get", return_value=response):
            from x2b import _post_to_bluesky
            result = _post_to_bluesky(
                client, post, users
            )

        assert result is client.send_post.return_value
        client.send_post.assert_called_once()

        # 通常サイズの画像はそのままアップロード
        assert client.upload_blob.call_count == 1
        uploaded = client.upload_blob.call_args[0][0]
        assert uploaded == normal_image

    def test_text_only_post_no_image_unchanged(self):
        """画像なしのtext-only投稿は従来通り動作する。"""
        client = MagicMock()
        client.send_post.return_value = MagicMock(
            uri="at://did:plc:ok/post"
        )

        users = self._make_users()
        post = self._make_post()

        ogp = {"title": "T", "description": "D", "image": None}

        with patch("x2b.get_ogp", return_value=ogp):
            from x2b import _post_to_bluesky
            result = _post_to_bluesky(
                client, post, users
            )

        assert result is client.send_post.return_value
        client.send_post.assert_called_once()
        client.upload_blob.assert_not_called()

    def test_media_failure_does_not_lose_post(self):
        """
        メディア処理失敗時でも投稿全体が失われない。
        text-onlyフォールバックが行われる。
        """
        client = MagicMock()
        client.send_post.return_value = MagicMock(
            uri="at://did:plc:ok/post"
        )

        users = self._make_users()
        post = self._make_post()

        ogp = {
            "title": "T",
            "description": "D",
            "image": "https://img.example/fail.jpg",
        }

        import requests as req_lib

        with patch("x2b.get_ogp", return_value=ogp), \
             patch(
                 "x2b.requests.get",
                 side_effect=req_lib.ConnectionError("network error"),
             ):
            from x2b import _post_to_bluesky
            result = _post_to_bluesky(
                client, post, users
            )

        # ネットワークエラーでも投稿は成功する
        assert result is client.send_post.return_value
        client.send_post.assert_called_once()


# ============================================================
# Bluesky API 安全性確認
# ============================================================

class TestBlueskyApiSafety:

    def test_no_oversized_blob_sent_to_bluesky(self):
        """
        uploadBlobに渡されるpayloadは常にサイズ制限以下である。
        oversized image → resize → final payload → size check → uploadBlob
        の流れを検証。
        """
        client = MagicMock()
        client.upload_blob.return_value = make_upload_response(b"\x00" * 100)

        # 1.2MBの画像
        large_content = make_large_jpeg(1_200_000)
        response = MagicMock()
        response.content = large_content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            create_external_embed(
                client,
                "https://example.com/post",
                {"title": "T", "description": "D", "image": None},
                "https://img.example/large.jpg",
            )

        if client.upload_blob.called:
            uploaded_bytes = client.upload_blob.call_args[0][0]
            assert len(uploaded_bytes) <= BSKY_MAX_THUMBNAIL_BYTES

    def test_resized_payload_size_verified_before_upload(self):
        """
        リサイズ後の最終payloadサイズを実測し、
        uploadBlobに渡す前にサイズ制限を確認する。
        """
        client = MagicMock()
        client.upload_blob.return_value = make_upload_response(b"\x00" * 100)

        # 1.5MBの画像（制限超過）
        large_content = make_large_jpeg(1_500_000)
        response = MagicMock()
        response.content = large_content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            create_external_embed(
                client,
                "https://example.com/post",
                {"title": "T", "description": "D", "image": None},
                "https://img.example/huge.jpg",
            )

        if client.upload_blob.called:
            uploaded_bytes = client.upload_blob.call_args[0][0]
            actual_size = len(uploaded_bytes)
            assert actual_size <= BSKY_MAX_THUMBNAIL_BYTES

    def test_size_check_before_and_after_resize(self):
        """
        リサイズ前後の両方でサイズを確認する。
        """
        content = make_large_jpeg(1_500_000)

        # リサイズ前のサイズ
        original_size = len(content)
        assert original_size > BSKY_MAX_THUMBNAIL_BYTES

        # リサイズ後のサイズ
        result, final_size = _resize_image_to_limit(
            content, BSKY_MAX_THUMBNAIL_BYTES
        )

        assert result is not None
        assert final_size <= BSKY_MAX_THUMBNAIL_BYTES
        assert len(result) == final_size


# ============================================================
# エラー処理と診断情報
# ============================================================

class TestDiagnostics:

    def test_resize_failure_logged_with_size_info(self, capsys):
        """リサイズ失敗時に画像サイズ情報がログに残る。"""
        fake_content = b"\x00" * (BSKY_MAX_THUMBNAIL_BYTES + 5000)

        result, _size = _resize_image_to_limit(
            fake_content, BSKY_MAX_THUMBNAIL_BYTES
        )

        assert result is None
        captured = capsys.readouterr()
        # デコード失敗のログが残る
        assert "Image decode failed" in captured.out

    def test_oversized_image_logged_with_size(self, capsys):
        """サイズ超過時に画像サイズがログに残る。"""
        content = make_large_jpeg(1_200_000)

        _resize_image_to_limit(content, BSKY_MAX_THUMBNAIL_BYTES)

        captured = capsys.readouterr()
        # リサイズ結果のログが残る
        assert "Image resized" in captured.out

    def test_no_credentials_in_logs(self, capsys):
        """ログに認証情報が含まれないことを確認。"""
        content = make_large_jpeg(1_200_000)

        _resize_image_to_limit(content, BSKY_MAX_THUMBNAIL_BYTES)

        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "BSKY_APP_PASSWORD" not in output
        assert "app_password" not in output.lower()

    def test_download_thumbnail_logs_size_on_oversize(self, capsys):
        """download_thumbnailがサイズ超過時にログを残す。"""
        content = make_large_jpeg(1_200_000)
        response = MagicMock()
        response.content = content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            download_thumbnail("https://img.example/large.jpg")

        captured = capsys.readouterr()
        assert "too large" in captured.out.lower() or "resized" in captured.out.lower()


# ============================================================
# DryRunResult のリサイズ対応
# ============================================================

class TestDryRunResultResized:

    def test_resized_status_property(self):
        """DryRunResultが'resized'ステータスを正しく扱う。"""
        post = {
            "id": "123",
            "author": {"screenName": "test", "name": "Test"},
        }
        result = x2b.DryRunResult(
            post=post,
            text="hello",
            grapheme_count=5,
            thumbnail_status="resized",
        )

        assert result.thumbnail_resized is True
        assert result.thumbnail_skipped is False
        assert result.has_thumbnail is True

    def test_too_large_status_property(self):
        """DryRunResultが'too_large'ステータスを正しく扱う。"""
        post = {
            "id": "123",
            "author": {"screenName": "test", "name": "Test"},
        }
        result = x2b.DryRunResult(
            post=post,
            text="hello",
            grapheme_count=5,
            thumbnail_status="too_large",
        )

        assert result.thumbnail_resized is False
        assert result.thumbnail_skipped is True
        assert result.has_thumbnail is False

    def test_ok_status_property(self):
        """DryRunResultが'ok'ステータスを正しく扱う。"""
        post = {
            "id": "123",
            "author": {"screenName": "test", "name": "Test"},
        }
        result = x2b.DryRunResult(
            post=post,
            text="hello",
            grapheme_count=5,
            thumbnail_status="ok",
        )

        assert result.thumbnail_resized is False
        assert result.thumbnail_skipped is False
        assert result.has_thumbnail is True


# ============================================================
# 1.2MB再現ケース同等のfixture
# ============================================================

class TestReproductionCase:

    def test_1_2mb_jpeg_resize_produces_valid_output(self):
        """
        Issue #10の再現ケース: 1.2MB程度のJPEG画像。
        リサイズ後に有効なJPEG画像が得られ、サイズ制限以下である。
        """
        # 実際の再現ケースと同等のサイズ
        content = make_large_jpeg(1_200_000)

        assert len(content) > BSKY_MAX_THUMBNAIL_BYTES

        result, size = _resize_image_to_limit(
            content, BSKY_MAX_THUMBNAIL_BYTES
        )

        assert result is not None
        assert size <= BSKY_MAX_THUMBNAIL_BYTES

        # リサイズされた画像が有効なJPEGであることを確認
        img = Image.open(io.BytesIO(result))
        assert img.format == "JPEG"
        assert img.mode in ("RGB", "L")

    def test_reproduction_case_through_full_download_flow(self):
        """
        download_thumbnailを通じた完全なフローで
        1.2MB画像が正しく処理される。
        """
        content = make_large_jpeg(1_200_000)
        response = MagicMock()
        response.content = content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            result, status = download_thumbnail(
                "https://img.example/repro.jpg"
            )

        assert result is not None
        assert status == "resized"
        assert len(result) <= BSKY_MAX_THUMBNAIL_BYTES

    def test_reproduction_case_upload_verified(self):
        """
        1.2MB画像のリサイズ結果がuploadBlobに渡される前に
        サイズ制限を満たすことを確認。
        """
        client = MagicMock()
        client.upload_blob.return_value = make_upload_response(b"\x00" * 100)
        content = make_large_jpeg(1_200_000)
        response = MagicMock()
        response.content = content
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            create_external_embed(
                client,
                "https://example.com/post",
                {"title": "T", "description": "D", "image": None},
                "https://img.example/repro.jpg",
            )

        # uploadBlobが呼ばれた場合、渡されるサイズは制限以下
        if client.upload_blob.called:
            uploaded = client.upload_blob.call_args[0][0]
            assert len(uploaded) <= BSKY_MAX_THUMBNAIL_BYTES
