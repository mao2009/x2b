"""
x2b dry-run tests (Issue #3).

Dry-runモードが「実際にBlueskyへ投稿せず、本番のseen stateも
変更せずに」パイプラインを最後まで実行できることを保証する。

- send_post / upload_blob（Blueskyへの書き込み）は絶対に呼ばれない
- seen fileは1バイトも変更されない
- build/validation/OGP/サムネイルサイズ検証などread-only処理は実行される
- retry経路・エラー分類は通常実行と同一ロジックで動作する

実際のBlueskyアカウント・X API・外部ネットワークには一切接しない。
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import x2b
from x2b import (
    BSKY_MAX_TEXT_GRAPHEMES,
    BSKY_MAX_THUMBNAIL_BYTES,
    MAX_RETRIES,
    RETRY_BASE_DELAY,
    PermanentError,
    TransientError,
    UnknownError,
    count_graphemes,
    parse_args,
    post_with_retry,
    process_single_post,
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


def make_post(post_id="1234567890", text="hello", screen="testuser",
              **extra):
    post = {
        "id": post_id,
        "text": text,
        "isRetweet": False,
        "author": {"screenName": screen},
    }
    post.update(extra)
    return post


@pytest.fixture(autouse=True)
def clear_dry_run_results():
    """モジュールレベルのDry-run結果がテスト間で漏れないようにする。"""
    x2b.DRY_RUN_RESULTS.clear()
    yield
    x2b.DRY_RUN_RESULTS.clear()


@pytest.fixture(autouse=True)
def isolate_state_file(tmp_path, monkeypatch):
    """本番のseen.json / users.jsonへ絶対に触れないようにする。"""
    monkeypatch.setattr(x2b, "STATE_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(x2b, "USERS_FILE", tmp_path / "users.json")


@pytest.fixture
def users():
    return {
        "testuser": fresh_user(),
        "hugeuser": fresh_user("H" * 300, "h" * 45),
    }


@pytest.fixture
def seen_env(tmp_path, monkeypatch):
    """STATE_FILEをtmpへ向け、save_seenの呼び出しを記録する。"""
    state_file = tmp_path / "seen.json"
    monkeypatch.setattr(x2b, "STATE_FILE", state_file)

    saves = []
    original_save_seen = x2b.save_seen

    def tracking_save(seen_set):
        original_save_seen(seen_set)
        saves.append(set(seen_set))

    monkeypatch.setattr(x2b, "save_seen", tracking_save)
    return {"file": state_file, "saves": saves}


def run_pipeline(post, seen, users, *, dry_run, ogp_image=None,
                 thumb_content=b"x" * 100):
    """
    OGP取得をstubしてprocess_single_postを実行する。

    ogp_imageを指定した場合のみサムネイルダウンロードが発生する
    （ダウンロード先はpatch済みのrequests.get）。
    """
    client = MagicMock()
    client.send_post.return_value = MagicMock(uri="at://real/post")

    ogp = {
        "title": "T",
        "description": "D",
        "image": ogp_image,
    }
    thumb_response = MagicMock()
    thumb_response.content = thumb_content
    thumb_response.raise_for_status.return_value = None

    with patch("x2b.get_ogp", return_value=ogp), \
         patch("x2b.requests.get", return_value=thumb_response):
        outcome = process_single_post(
            client, post, users, seen, dry_run=dry_run
        )

    return client, outcome


# ============================================================
# CLI (--dry-run)
# ============================================================

class TestCliParsing:

    def test_dry_run_flag_is_recognized(self):
        args = parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_default_run_is_not_dry_run(self):
        # 通常実行とDry-runを明確に区別できる
        args = parse_args([])
        assert args.dry_run is False

    def test_help_describes_dry_run(self, capsys):
        with pytest.raises(SystemExit):
            parse_args(["--help"])

        out = capsys.readouterr().out
        assert "--dry-run" in out
        assert "without publishing to Bluesky" in out
        assert "seen state" in out


# ============================================================
# 投稿の抑止（send_postは絶対に呼ばれない）
# ============================================================

class TestDryRunDoesNotPost:

    def test_send_post_and_upload_blob_never_called_in_dry_run(
        self, seen_env, users
    ):
        client, outcome = run_pipeline(
            make_post("111"), set(), users, dry_run=True
        )

        assert outcome == "would_post"
        client.send_post.assert_not_called()
        client.upload_blob.assert_not_called()

    def test_normal_run_still_calls_send_post(self, seen_env, users):
        # 対偶: 通常実行では投稿される（既存挙動の回帰防止）
        seen = set()
        client, outcome = run_pipeline(
            make_post("111"), seen, users, dry_run=False
        )

        assert outcome == "success"
        client.send_post.assert_called_once()
        assert seen == {"111"}
        assert len(seen_env["saves"]) == 1

    def test_build_and_validation_execute_in_dry_run(self, users):
        """
        Dry-runでもbuild→validateが実行され、制限超過本文は
        送信前にPermanentErrorとして検出される。
        （OGP取得等の後続API操作は一切発生しない）
        """
        fake_builder = MagicMock()
        fake_builder.build_text.return_value = (
            "a" * (BSKY_MAX_TEXT_GRAPHEMES + 1)
        )
        fake_builder.build_facets.return_value = []

        client = MagicMock()

        with patch("x2b.build_post", return_value=fake_builder), \
             patch("x2b.get_ogp") as mock_get_ogp:
            outcome = process_single_post(
                client,
                make_post("555000222"),
                users,
                set(),
                dry_run=True,
            )

        assert outcome == "permanent"
        mock_get_ogp.assert_not_called()
        client.send_post.assert_not_called()
        client.upload_blob.assert_not_called()

    def test_would_post_reports_text_and_grapheme_count(
        self, users
    ):
        _, _ = run_pipeline(
            make_post("123", text="hello world"),
            set(),
            users,
            dry_run=True,
        )

        result = x2b.DRY_RUN_RESULTS[-1]
        assert result.post_id == "123"
        assert result.text.endswith("hello world")
        assert result.text.startswith("📢 ")
        assert (
            result.grapheme_count
            == count_graphemes(result.text)
        )
        assert result.grapheme_count <= BSKY_MAX_TEXT_GRAPHEMES
        # 擬似URIは実投稿と誤認できない形
        assert "dry-run" in result.uri


# ============================================================
# blob uploadの抑止とローカル検証
# ============================================================

class TestDryRunThumbnailLocalValidation:

    def test_valid_thumbnail_is_validated_without_upload(
        self, seen_env, users
    ):
        client, outcome = run_pipeline(
            make_post("111"),
            set(),
            users,
            dry_run=True,
            ogp_image="https://img.example/t.jpg",
            thumb_content=b"a" * (BSKY_MAX_THUMBNAIL_BYTES - 1),
        )
        # ダウンロードとサイズ検証は行われるがuploadはしない
        client.upload_blob.assert_not_called()
        assert outcome == "would_post"

        result = x2b.DRY_RUN_RESULTS[-1]
        assert result.thumbnail_status == "ok"
        assert result.has_thumbnail is True
        assert result.thumbnail_skipped is False

    def test_oversized_thumbnail_detected_without_upload(
        self, users
    ):
        oversized_content = b"a" * (BSKY_MAX_THUMBNAIL_BYTES + 1)
        client = MagicMock()

        ogp = {
            "title": "T",
            "description": "D",
            "image": "https://img.example/huge.jpg",
        }
        response = MagicMock()
        response.content = oversized_content
        response.raise_for_status.return_value = None

        with patch("x2b.get_ogp", return_value=ogp), \
             patch("x2b.requests.get", return_value=response):
            outcome = process_single_post(
                client, make_post("111"), users, set(), dry_run=True
            )

        client.upload_blob.assert_not_called()
        client.send_post.assert_not_called()
        assert outcome == "would_post"

        result = x2b.DRY_RUN_RESULTS[-1]
        assert result.thumbnail_status == "too_large"
        assert result.thumbnail_skipped is True
        assert result.has_thumbnail is False

    def test_failed_download_continues_without_upload(self, users):
        client = MagicMock()

        ogp = {
            "title": "T",
            "description": "D",
            "image": "https://img.example/broken.jpg",
        }

        with patch("x2b.get_ogp", return_value=ogp), \
             patch(
                 "x2b.requests.get",
                 side_effect=requests.ConnectionError("boom"),
             ):
            outcome = process_single_post(
                client, make_post("111"), users, set(), dry_run=True
            )

        client.upload_blob.assert_not_called()
        # サムネイルなしで解析は継続する（既存ポリシー通り）
        assert outcome == "would_post"
        assert x2b.DRY_RUN_RESULTS[-1].thumbnail_status == "failed"


# ============================================================
# seen state（本番stateを一切変更しない）
# ============================================================

class TestDryRunSeenState:

    def test_seen_never_updated_even_on_permanent_failure(
        self, seen_env, users
    ):
        """
        Dry-run前後でseen stateが完全一致すること。
        would_post・permanent のどちらでもseenには入らない。
        """
        seen = {"already"}

        _, outcome_ok = run_pipeline(
            make_post("222"), seen, users, dry_run=True
        )
        assert outcome_ok == "would_post"

        _, outcome_perm = run_pipeline(
            make_post("333", screen="hugeuser"), seen, users,
            dry_run=True,
        )
        assert outcome_perm == "permanent"

        assert seen == {"already"}
        assert seen_env["saves"] == []

    def test_seen_file_bytes_and_mtime_unchanged(
        self, seen_env, tmp_path, users
    ):
        seen_env["file"].write_text(
            json.dumps(["111"]),
            encoding="utf-8",
        )
        before_bytes = seen_env["file"].read_bytes()
        before_mtime = seen_env["file"].stat().st_mtime_ns

        for post_id in ("111", "222"):
            run_pipeline(
                make_post(post_id),
                x2b.load_seen(),
                users,
                dry_run=True,
            )

        assert seen_env["file"].read_bytes() == before_bytes
        assert (
            seen_env["file"].stat().st_mtime_ns
            == before_mtime
        )
        assert x2b.load_seen() == {"111"}
        assert seen_env["saves"] == []


# ============================================================
# retry / backoff（Dry-runでは実時間待機しない）
# ============================================================

class TestDryRunRetryBackoff:

    def test_transient_retries_finitely_without_real_sleep(
        self, monkeypatch, capsys
    ):
        sleeps = []
        monkeypatch.setattr(
            x2b.time, "sleep", lambda s: sleeps.append(s)
        )

        attempts = []

        def always_transient(*args, **kwargs):
            attempts.append(1)
            raise TransientError("network down")

        with patch(
            "x2b._post_to_bluesky", side_effect=always_transient
        ), pytest.raises(TransientError):
            post_with_retry(
                MagicMock(), make_post(), {}, dry_run=True
            )

        # リトライ回数は通常実行と同一（有限回で必ず終了）
        assert len(attempts) == MAX_RETRIES + 1
        # 実時間待機は発生しない
        assert sleeps == []
        out = capsys.readouterr().out
        assert "[DRY-RUN] Would retry in" in out

    def test_transient_retry_eventually_succeeds_in_dry_run(
        self, monkeypatch, capsys
    ):
        sleeps = []
        monkeypatch.setattr(
            x2b.time, "sleep", lambda s: sleeps.append(s)
        )

        responses = iter([
            TransientError("flaky"),
            TransientError("still flaky"),
            "ok-response",
        ])

        def flaky(*args, **kwargs):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item

        with patch("x2b._post_to_bluesky", side_effect=flaky):
            result = post_with_retry(
                MagicMock(), make_post(), {}, dry_run=True
            )

        assert result == "ok-response"
        assert sleeps == []
        out = capsys.readouterr().out
        # 待機予定だったバックオフ時間が記録されるだけ
        assert f"{RETRY_BASE_DELAY} seconds" in out
        assert f"{RETRY_BASE_DELAY * 2} seconds" in out

    def test_normal_run_retry_behavior_unchanged(self, monkeypatch):
        # 対偶: 通常実行の指数バックオフは変更されていない
        sleeps = []
        monkeypatch.setattr(
            x2b.time, "sleep", lambda s: sleeps.append(s)
        )

        responses = iter([
            TransientError("flaky"),
            TransientError("still flaky"),
            "ok-response",
        ])

        def flaky(*args, **kwargs):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item

        with patch("x2b._post_to_bluesky", side_effect=flaky):
            result = post_with_retry(
                MagicMock(), make_post(), {}
            )

        assert result == "ok-response"
        assert sleeps == [
            RETRY_BASE_DELAY,
            RETRY_BASE_DELAY * 2,
        ]


# ============================================================
# エラー分類（Issue #1の分類をDry-runでも透過利用）
# ============================================================

class TestDryRunErrorClassification:

    @pytest.mark.parametrize("error,outcome", [
        (PermanentError("Post text too long"), "permanent"),
        (TransientError("Network error"), "transient"),
        (UnknownError("Unclassified"), "unknown"),
    ])
    def test_classified_errors_keep_outcome_and_seen_intact(
        self, seen_env, error, outcome
    ):
        client = MagicMock()

        with patch(
            "x2b.post_with_retry", side_effect=error
        ) as mock_retry:
            result = process_single_post(
                client,
                make_post("444"),
                {},
                set(),
                dry_run=True,
            )

        assert result == outcome
        mock_retry.assert_called_once()
        client.send_post.assert_not_called()
        # Dry-runでは分類に関わらずseenは変更されない
        assert seen_env["saves"] == []

    def test_permanent_failure_message_notes_dry_run(
        self, seen_env, capsys
    ):
        client = MagicMock()

        with patch(
            "x2b.post_with_retry",
            side_effect=PermanentError("Post text too long"),
        ):
            process_single_post(
                client, make_post("444"), {}, set(), dry_run=True
            )

        err = capsys.readouterr().err
        assert "PERMANENT FAILURE" in err
        assert "(not marked as seen in dry-run)" in err


# ============================================================
# ユーザーキャッシュの永続化抑止
# ============================================================

class TestDryRunUserCache:

    def make_fake_twitter(self):
        payload = json.dumps({
            "data": {"name": "New Name", "screenName": "newguy"},
        })
        return MagicMock(returncode=0, stdout=payload)

    def test_dry_run_does_not_persist_user_cache(
        self, tmp_path, monkeypatch
    ):
        users_file = tmp_path / "users.json"
        monkeypatch.setattr(x2b, "USERS_FILE", users_file)

        saved = []
        monkeypatch.setattr(
            x2b, "save_users", lambda u: saved.append(dict(u))
        )

        with patch(
            "x2b.subprocess.run", return_value=self.make_fake_twitter()
        ):
            users = {}
            entry = x2b.get_x_user("newguy", users, dry_run=True)

        assert entry["screenName"] == "newguy"
        # 実行中の出力は通常実行と同一になるようメモリ上は更新される
        assert "newguy" in users
        # 永続化はskipされ、ファイルは変更されない
        assert saved == []
        assert not users_file.exists()

    def test_normal_run_persists_user_cache(
        self, tmp_path, monkeypatch
    ):
        users_file = tmp_path / "users.json"
        monkeypatch.setattr(x2b, "USERS_FILE", users_file)

        with patch(
            "x2b.subprocess.run", return_value=self.make_fake_twitter()
        ):
            entry = x2b.get_x_user("newguy", {})

        assert entry["screenName"] == "newguy"
        assert users_file.exists()


# ============================================================
# E2E: main() を通したパイプライン全体
# ============================================================

def e2e_post(post_id, created, text="hello", screen="testuser"):
    return {
        "id": post_id,
        "text": text,
        "isRetweet": False,
        "createdAtISO": created,
        "author": {"screenName": screen},
    }


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    """main()を外部依存なしで実行するための環境。"""
    state_file = tmp_path / "seen.json"
    users_file = tmp_path / "users.json"
    lock_file = tmp_path / ".x2b.lock"

    monkeypatch.setattr(x2b, "STATE_FILE", state_file)
    monkeypatch.setattr(x2b, "USERS_FILE", users_file)
    monkeypatch.setattr(x2b, "LOCK_FILE", lock_file)

    # キャッシュヒットさせ、twitter-cliの起動を防ぐ
    users_payload = {
        "testuser": fresh_user(),
        "hugeuser": fresh_user("H" * 300, "h" * 45),
    }
    monkeypatch.setattr(
        x2b, "load_users", lambda: dict(users_payload)
    )

    sleeps = []
    monkeypatch.setattr(
        x2b.time, "sleep", lambda s: sleeps.append(s)
    )

    saves = []
    original_save_seen = x2b.save_seen

    def tracking_save(seen_set):
        original_save_seen(seen_set)
        saves.append(set(seen_set))

    monkeypatch.setattr(x2b, "save_seen", tracking_save)

    holder = {}

    def fake_client_factory():
        client = MagicMock()
        client.send_post.return_value = MagicMock(
            uri="at://did:x/post"
        )
        holder["client"] = client
        return client

    monkeypatch.setattr(x2b, "Client", fake_client_factory)

    return {
        "state_file": state_file,
        "sleeps": sleeps,
        "saves": saves,
        "holder": holder,
    }


OGP_STUB = {"title": "T", "description": "D", "image": None}


class TestDryRunEndToEnd:

    def test_full_pipeline_publishes_nothing_and_changes_no_state(
        self, main_env, capsys, monkeypatch
    ):
        env = main_env
        env["state_file"].write_text(
            json.dumps(["111", "222"]), encoding="utf-8"
        )
        before_bytes = env["state_file"].read_bytes()
        before_mtime = env["state_file"].stat().st_mtime_ns

        posts = [
            e2e_post("111", "2026-08-01T00:00:00Z"),
            e2e_post("222", "2026-08-02T00:00:00Z"),
            e2e_post(
                "300", "2026-08-03T00:00:00Z", text="fresh one"
            ),
            e2e_post(
                "301", "2026-08-04T00:00:00Z", screen="hugeuser"
            ),
        ]
        monkeypatch.setattr(x2b, "get_x_posts", lambda: posts)

        with patch("x2b.get_ogp", return_value=dict(OGP_STUB)):
            x2b.main(["--dry-run"])

        captured = capsys.readouterr()
        out = captured.out
        err = captured.err

        # --- ログがDry-runであることを明示する ---
        assert "Mode: DRY-RUN" in out
        assert "[DRY-RUN] Would post: X post ID: 300" in out
        assert "DRY-RUN SUMMARY" in out

        # --- summary ---
        assert "fetched: 4" in out
        assert "skipped_seen: 2" in out
        assert "new: 2" in out
        assert "would_post: 1" in out
        assert "permanent_failures: 1" in out
        assert "transient_failures: 0" in out
        assert "unknown_failures: 0" in out
        assert "thumbnail_skipped: 0" in out

        # --- 実投稿との誤認を防ぐ文言 ---
        assert "nothing was published" in out
        assert "seen.json was not modified" in out
        # 通常実行のsummary形式は出ない
        assert "Summary: Success:" not in out

        # --- Bluesky書き込み系APIは一切呼ばれない ---
        client = env["holder"]["client"]
        client.login.assert_called_once()  # 読み取り専用のログインは通る
        client.send_post.assert_not_called()
        client.upload_blob.assert_not_called()

        # --- permanent失敗もseenに反映されない ---
        assert "PERMANENT FAILURE" in err
        assert "(not marked as seen in dry-run)" in err

        # --- seen fileは1バイトも変更されない ---
        assert env["state_file"].read_bytes() == before_bytes
        assert (
            env["state_file"].stat().st_mtime_ns == before_mtime
        )
        assert env["saves"] == []

        # --- 投稿間隔の実待機も発生しない ---
        assert env["sleeps"] == []

    def test_normal_run_publishes_and_updates_seen(
        self, main_env, capsys, monkeypatch
    ):
        # 対偶: 同じ入力での通常実行は投稿しseenを更新する
        env = main_env
        env["state_file"].write_text(
            json.dumps(["111", "222"]), encoding="utf-8"
        )

        posts = [
            e2e_post("111", "2026-08-01T00:00:00Z"),
            e2e_post("222", "2026-08-02T00:00:00Z"),
            e2e_post("300", "2026-08-03T00:00:00Z"),
            e2e_post("301", "2026-08-04T00:00:00Z"),
        ]
        monkeypatch.setattr(x2b, "get_x_posts", lambda: posts)

        with patch("x2b.get_ogp", return_value=dict(OGP_STUB)):
            x2b.main([])

        captured = capsys.readouterr()
        client = env["holder"]["client"]

        # permanent(301)を含め、新規2件とも送信される
        assert client.send_post.call_count == 2
        assert "Posted:" in captured.out
        assert "Summary: Success: 2" in captured.out
        assert "DRY-RUN" not in captured.out

        # seenは新規2件+恒久失敗1件で更新される
        assert x2b.load_seen() == {"111", "222", "300", "301"}
        assert len(env["saves"]) >= 1

        # 投稿間隔の待機は通常実行では発生する
        assert env["sleeps"] == [x2b.POST_INTERVAL]

    def test_dry_run_summary_counts_transient_failures(
        self, main_env, capsys, monkeypatch
    ):
        env = main_env

        posts = [
            e2e_post("400", "2026-08-05T00:00:00Z"),
            e2e_post("401", "2026-08-06T00:00:00Z"),
        ]
        monkeypatch.setattr(x2b, "get_x_posts", lambda: posts)

        def always_transient(*args, **kwargs):
            raise TransientError("network down")

        with patch(
            "x2b._post_to_bluesky", side_effect=always_transient
        ):
            x2b.main(["--dry-run"])

        captured = capsys.readouterr()
        out = captured.out

        assert "transient_failures: 2" in out
        assert "would_post: 0" in out
        assert "DRY-RUN SUMMARY" in out

        # リトライは走るが実時間待機しない
        assert env["sleeps"] == []
        client = env["holder"]["client"]
        client.send_post.assert_not_called()

    def test_nothing_new_prints_no_summary_but_posts_nothing(
        self, main_env, capsys, monkeypatch
    ):
        env = main_env
        env["state_file"].write_text(
            json.dumps(["111"]), encoding="utf-8"
        )

        posts = [
            e2e_post("111", "2026-08-01T00:00:00Z"),
        ]
        monkeypatch.setattr(x2b, "get_x_posts", lambda: posts)

        x2b.main(["--dry-run"])

        captured = capsys.readouterr()
        client = env["holder"]["client"]

        assert "Nothing to post." in captured.out
        client.send_post.assert_not_called()
