"""
x2b error classification tests (Issue #1).

atproto SDKの例外型と構造化情報（status_code / error識別子）に基づく
エラー分類を検証する。例外文字列の部分一致に依存しないことを保証する。

実際のBlueskyアカウントには一切投稿しない。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atproto_client import exceptions as atproto_exceptions
from atproto_client.models.common import XrpcError
from atproto_client.models.utils import get_or_create
from atproto_client.request import Response as SdkResponse

import x2b
from x2b import (
    MAX_RETRIES,
    RETRY_BASE_DELAY,
    PermanentError,
    TransientError,
    UnknownError,
    classify_error,
    create_external_embed,
    format_error_for_log,
    post_with_retry,
    process_single_post,
)

# ============================================================
# テストdouble: SDKと同じ構造のAPIエラーを構築する
# ============================================================

# SDK内部（_handle_response）と同一のステータス→例外型マッピング
STATUS_TO_EXCEPTION = {
    400: atproto_exceptions.BadRequestError,
    401: atproto_exceptions.UnauthorizedError,
    403: atproto_exceptions.UnauthorizedError,
    404: atproto_exceptions.RequestException,
    409: atproto_exceptions.NetworkError,
    413: atproto_exceptions.NetworkError,
    429: atproto_exceptions.RequestException,
    500: atproto_exceptions.RequestException,
    502: atproto_exceptions.NetworkError,
    503: atproto_exceptions.RequestException,
}


def make_api_error(status_code, error="InvalidRequest",
                   message="something went wrong"):
    """
    atproto SDKが実際に上げる例外と同じ構造を持つAPIエラーを作る。

    （atproto_client.request._handle_responseと同じ構築手順）
    """
    content = get_or_create(
        {"error": error, "message": message},
        XrpcError,
        strict=False,
    )
    response = SdkResponse(
        success=False,
        status_code=status_code,
        content=content,
        headers={},
    )
    exception_class = STATUS_TO_EXCEPTION.get(
        status_code,
        atproto_exceptions.RequestException,
    )
    return exception_class(response)


@pytest.fixture
def seen(tmp_path, monkeypatch):
    """STATE_FILEをtmpへ向け、save_seenの呼び出しを記録する。"""
    state_file = tmp_path / "seen.json"
    monkeypatch.setattr(x2b, "STATE_FILE", state_file)

    saves = []
    original_save_seen = x2b.save_seen

    def tracking_save(seen_set):
        original_save_seen(seen_set)
        saves.append(set(seen_set))

    monkeypatch.setattr(x2b, "save_seen", tracking_save)
    return {"set": set(), "saves": saves}


def make_post(post_id="1234567890"):
    return {
        "id": post_id,
        "text": "hello",
        "isRetweet": False,
        "author": {"screenName": "testuser"},
    }


# ============================================================
# 既存分類済みエラーの透過（再分類防止）
# ============================================================

class TestAlreadyClassifiedPassthrough:

    @pytest.mark.parametrize("error_class", [
        PermanentError,
        TransientError,
        UnknownError,
    ])
    def test_classified_errors_returned_as_is(self, error_class):
        error = error_class("already classified")

        assert classify_error(error) is error

    def test_permanent_not_rewrapped_as_transient(self):
        # Issue #2で発見された問題の回帰防止:
        # PermanentErrorが"Unknown error"としてTransientに包まれないこと
        error = PermanentError("Post text too long: 301 graphemes (max 300)")

        classified = classify_error(error)

        assert type(classified) is PermanentError


# ============================================================
# 構造化情報による分類（SDK例外型 + status_code + error識別子）
# ============================================================

class TestStructuredApiClassification:

    # ---- Transient ----

    @pytest.mark.parametrize("status", [429])
    def test_rate_limit_is_transient(self, status):
        classified = classify_error(make_api_error(status))

        assert isinstance(classified, TransientError)

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_server_errors_are_transient(self, status):
        classified = classify_error(make_api_error(status))

        assert isinstance(classified, TransientError)

    # ---- Permanent（既知のpayload validation問題）----

    def test_known_text_length_violation_is_permanent(self):
        error = make_api_error(
            400,
            message="Post text must not exceed 300 graphemes",
        )

        classified = classify_error(error)

        assert isinstance(classified, PermanentError)

    def test_payload_too_large_error_id_is_permanent(self):
        error = make_api_error(
            400,
            error="PayloadTooLarge",
            message="This file is too large.",
        )

        classified = classify_error(error)

        assert isinstance(classified, PermanentError)

    def test_http_413_is_permanent_regardless_of_message(self):
        # payloadサイズ超過は再送でも解決しないため恒久扱い
        error = make_api_error(413, message="unexpected message")

        classified = classify_error(error)

        assert isinstance(classified, PermanentError)

    # ---- Unknown（勝手に確定分類へ変換しない）----

    def test_unknown_400_is_unknown_not_permanent(self):
        # 内容を確認できない400をPermanent扱いすると
        # 投稿が黙って失われるためUnknownとして扱う
        error = make_api_error(
            400,
            message="mysterious failure xyz",
        )

        classified = classify_error(error)

        assert isinstance(classified, UnknownError)
        assert not isinstance(classified, PermanentError)
        assert not isinstance(classified, TransientError)

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_errors_are_unknown(self, status):
        # 認証情報の問題は単一投稿の再試行では解決しない
        error = make_api_error(status, error="AuthenticationRequired")

        classified = classify_error(error)

        assert isinstance(classified, UnknownError)

    def test_unexpected_status_is_unknown(self):
        error = make_api_error(404, error="NotFound")

        classified = classify_error(error)

        assert isinstance(classified, UnknownError)

    def test_conflict_status_is_unknown(self):
        error = make_api_error(409, error="Conflict")

        classified = classify_error(error)

        assert isinstance(classified, UnknownError)


# ============================================================
# トランスポート層・ローカルネットワーク系の分類
# ============================================================

class TestTransportAndNetworkClassification:

    def test_sdk_timeout_is_transient(self):
        error = atproto_exceptions.InvokeTimeoutError()

        assert isinstance(classify_error(error), TransientError)

    def test_sdk_transport_network_error_is_transient(self):
        # responseなしのNetworkError = トランスポート層の失敗
        error = atproto_exceptions.NetworkError()

        assert isinstance(classify_error(error), TransientError)

    @pytest.mark.parametrize("error", [
        requests.ConnectionError("connection refused"),
        requests.Timeout("timed out"),
        requests.RequestException("request failed"),
        ConnectionError("broken pipe"),
        TimeoutError("operation timed out"),
    ])
    def test_local_network_errors_are_transient(self, error):
        assert isinstance(classify_error(error), TransientError)

    def test_login_required_is_unknown(self):
        error = atproto_exceptions.LoginRequiredError()

        assert isinstance(classify_error(error), UnknownError)


# ============================================================
# Unknownの扱い（握り潰さない）
# ============================================================

class TestUnknownErrors:

    def test_generic_exception_is_unknown_not_transient(self):
        # 旧実装は未知の例外をTransientErrorに包んでリトライしていた。
        # 分類できないものはUnknownErrorとして調査可能な形で残す。
        classified = classify_error(ValueError("totally unexpected"))

        assert isinstance(classified, UnknownError)

    def test_exception_type_name_survives_in_log_format(self):
        formatted = format_error_for_log(ValueError("boom"))

        assert "ValueError" in formatted
        assert "boom" in formatted


# ============================================================
# 例外文字列への依存がないことの回帰テスト
# ============================================================

class TestNoStringParsing:

    def test_decoy_string_does_not_force_permanent(self):
        # 旧実装はstr()に"status_code=400"が含まれるだけで
        # Permanent扱いできてしまう（dataclass repr由来の偶然一致）。
        # 構造化情報を持たない例外はUnknownであるべき。
        decoy = ValueError(
            "Response(success=False, status_code=400, "
            "content=XrpcError(...))"
        )

        classified = classify_error(decoy)

        assert isinstance(classified, UnknownError)
        assert not isinstance(classified, PermanentError)

    def test_message_decoy_cannot_override_status_classification(self):
        # status_code（構造化）が優先され、message中の文字列は
        # 5xx/429の分類を変えない
        error = make_api_error(
            500,
            message="status_code=400 pretend to be bad request",
        )

        classified = classify_error(error)

        assert isinstance(classified, TransientError)

    def test_structured_status_is_used_not_repr(self):
        # BadRequestErrorのreprに"status_code=400"が含まれることは
        # あるが、分類は例外型とresponse.status_codeから決まる
        error = make_api_error(400, message="mysterious")

        classified = classify_error(error)

        # 文字列照合ならPermanentになっていたケース
        assert isinstance(classified, UnknownError)


# ============================================================
# ログ整形（調査可能性 & secrets非含有）
# ============================================================

class TestErrorLogging:

    def test_api_error_log_contains_structured_fields(self):
        error = make_api_error(
            400,
            error="InvalidRequest",
            message="Input must be less than 300 graphemes",
        )

        formatted = format_error_for_log(error)

        assert "BadRequestError" in formatted
        assert "status=400" in formatted
        assert "api_error=InvalidRequest" in formatted
        assert "300 graphemes" in formatted

    def test_long_api_message_is_truncated(self):
        error = make_api_error(400, message="x" * 10000)

        formatted = format_error_for_log(error)

        assert len(formatted) < 500

    def test_no_credentials_in_log_output(self):
        # headersに認証情報があってもログには現れないこと
        error = make_api_error(401)
        error.response.headers = {
            "authorization": "Bearer super-secret-token",
            "cookie": "session=abc123",
        }

        formatted = format_error_for_log(error)

        assert "super-secret-token" not in formatted
        assert "abc123" not in formatted
        assert "Bearer" not in formatted

    def test_plain_exception_log_contains_type_and_message(self):
        formatted = format_error_for_log(RuntimeError("plain failure"))

        assert "RuntimeError" in formatted
        assert "plain failure" in formatted


# ============================================================
# リトライ挙動（有限性の保証）
# ============================================================

class TestRetryBehavior:

    def test_transient_error_retries_finitely(self, monkeypatch):
        delays = []
        monkeypatch.setattr(
            x2b.time, "sleep", lambda seconds: delays.append(seconds)
        )

        attempts = []

        def always_transient(*args, **kwargs):
            attempts.append(1)
            raise make_api_error(503, message="service unavailable")

        with patch("x2b._post_to_bluesky", side_effect=always_transient), \
                pytest.raises(TransientError):
            post_with_retry(MagicMock(), make_post(), {})

        # 初回 + MAX_RETRIES回のリトライ = 有限回で必ず終了する
        assert len(attempts) == MAX_RETRIES + 1
        assert len(delays) == MAX_RETRIES
        # 指数バックオフ
        assert (
            delays
            == [RETRY_BASE_DELAY * (2 ** i) for i in range(MAX_RETRIES)]
        )

    def test_unknown_error_is_not_retried(self, monkeypatch):
        delays = []
        monkeypatch.setattr(
            x2b.time, "sleep", lambda seconds: delays.append(seconds)
        )

        attempts = []

        def always_unknown(*args, **kwargs):
            attempts.append(1)
            raise ValueError("unexpected internal error")

        with patch("x2b._post_to_bluesky", side_effect=always_unknown), \
                pytest.raises(UnknownError):
            post_with_retry(MagicMock(), make_post(), {})

        assert len(attempts) == 1  # リトライせず即raise
        assert delays == []

    def test_permanent_error_is_not_retried(self, monkeypatch):
        delays = []
        monkeypatch.setattr(
            x2b.time, "sleep", lambda seconds: delays.append(seconds)
        )

        attempts = []

        def always_permanent(*args, **kwargs):
            attempts.append(1)
            raise make_api_error(
                400,
                message="Post text must not exceed 300 graphemes",
            )

        with patch("x2b._post_to_bluesky", side_effect=always_permanent), \
                pytest.raises(PermanentError):
            post_with_retry(MagicMock(), make_post(), {})

        assert len(attempts) == 1
        assert delays == []


# ============================================================
# seen状態との整合性（process_single_post の結果別の振る舞い）
# ============================================================

class TestSeenDispositions:

    def test_successful_post_marks_seen(self, seen):
        client = MagicMock()
        client_send = MagicMock(return_value=MagicMock(uri="at://did:x"))

        with patch("x2b.post_with_retry", return_value=client_send()):
            outcome = process_single_post(
                client, make_post("111"), {}, seen["set"]
            )

        assert outcome == "success"
        assert seen["set"] == {"111"}
        assert len(seen["saves"]) == 1  # save_seenが呼ばれ永続化される

    def test_permanent_failure_marks_seen(self, seen):
        client = MagicMock()

        def permanent(*args, **kwargs):
            raise PermanentError("Post text too long")

        with patch("x2b.post_with_retry", side_effect=permanent):
            outcome = process_single_post(
                client, make_post("222"), {}, seen["set"]
            )

        assert outcome == "permanent"
        # 既知の恒久失敗は毎回再処理しないようseenに入る
        # （stderrとサマリで可視化されるため黙って消えない）
        assert seen["set"] == {"222"}

    def test_transient_exhaustion_keeps_post_out_of_seen(self, seen):
        client = MagicMock()

        def transient(*args, **kwargs):
            raise TransientError("Network error")

        with patch("x2b.post_with_retry", side_effect=transient):
            outcome = process_single_post(
                client, make_post("333"), {}, seen["set"]
            )

        assert outcome == "transient"
        assert seen["set"] == set()  # 次回実行で自動的に再試行される

    def test_unknown_failure_keeps_post_out_of_seen(self, seen):
        client = MagicMock()

        def unknown(*args, **kwargs):
            raise UnknownError("Unclassified exception")

        with patch("x2b.post_with_retry", side_effect=unknown):
            outcome = process_single_post(
                client, make_post("444"), {}, seen["set"]
            )

        assert outcome == "unknown"
        # 原因調査まで次回実行で再表面化させる（黙って消えない）
        assert seen["set"] == set()

    def test_falsy_response_changes_nothing(self, seen):
        client = MagicMock()

        with patch("x2b.post_with_retry", return_value=None):
            outcome = process_single_post(
                client, make_post("555"), {}, seen["set"]
            )

        assert outcome == "no_response"
        assert seen["set"] == set()


# ============================================================
# thumbnail障害時も投稿全体を維持する仕様（Issue #2からの継続）
# ============================================================

class TestThumbnailFailurePolicy:

    def run_embed_with_upload_error(self, upload_exc):
        client = MagicMock()
        client.upload_blob.side_effect = upload_exc
        ogp = {"title": "T", "description": "D", "image": None}

        response = MagicMock()
        response.content = b"a" * 100
        response.raise_for_status.return_value = None

        with patch("x2b.requests.get", return_value=response):
            embed = create_external_embed(
                client,
                "https://example.com/post",
                ogp,
                "https://img.example/t.jpg",
            )

        return embed

    def test_blob_api_rate_limit_continues_without_thumbnail(self):
        embed = self.run_embed_with_upload_error(make_api_error(429))

        assert embed.external.thumb is None

    def test_blob_api_server_error_continues_without_thumbnail(self):
        embed = self.run_embed_with_upload_error(make_api_error(500))

        assert embed.external.thumb is None

    def test_blob_permanent_validation_error_continues_without_thumbnail(self):
        embed = self.run_embed_with_upload_error(
            make_api_error(400, error="PayloadTooLarge")
        )

        assert embed.external.thumb is None
