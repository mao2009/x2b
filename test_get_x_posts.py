"""
x2b X list fetching tests (Issue #8).

get_x_posts()が1 runにつきtwitter-cli listを正確に1回だけ呼び出し、
要求件数がMAX_POSTS_PER_RUN（=100）であることを保証する回帰テスト。

背景: twitter-cli list -n N の複数回呼び出しは「次ページ」を保証せず、
同じ最新投稿を再取得して二重投稿する危険があったため、
pagination自体を廃止した（docs/adr/0001-disable-pagination-for-x-list-fetching.md 参照）。

mockしたsubprocess / twitter-cliを使用し、実際のX APIには一切接しない。
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import x2b
from x2b import MAX_POSTS_PER_RUN, get_x_posts

# ============================================================
# ヘルパー
# ============================================================

def make_cli_result(posts=None, ok=True, returncode=0, stdout="", stderr=""):
    """subprocess.runの戻り値として動作するtwitter-cliレスポンス。"""
    response = MagicMock()
    response.returncode = returncode
    response.stdout = (
        stdout if stdout else json.dumps({"ok": ok, "data": posts or []})
    )
    response.stderr = stderr
    return response


def make_post(post_id, created="2026-08-01T00:00:00Z"):
    return {
        "id": str(post_id),
        "text": f"post {post_id}",
        "isRetweet": False,
        "createdAtISO": created,
        "author": {"screenName": "testuser"},
    }


def invoked_commands(mock_run):
    """patch済みsubprocess.runに渡されたコマンド引数リストの一覧。"""
    return [call.args[0] for call in mock_run.call_args_list]


@pytest.fixture
def pipeline_env(tmp_path, monkeypatch):
    """main()を外部依存なしで実行するための最小環境。"""
    monkeypatch.setattr(x2b, "STATE_FILE", tmp_path / "seen.json")
    monkeypatch.setattr(x2b, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(x2b, "LOCK_FILE", tmp_path / ".x2b.lock")

    # キャッシュヒットさせ、twitter-cliの起動（user取得）を防ぐ
    users_payload = {
        "testuser": {
            "name": "Test User",
            "screenName": "testuser",
            "updatedAt": x2b.iso_now(),
        },
    }
    monkeypatch.setattr(x2b, "load_users", lambda: dict(users_payload))
    monkeypatch.setattr(x2b.time, "sleep", lambda s: None)

    holder = {}

    def fake_client_factory():
        client = MagicMock()
        client.send_post.return_value = MagicMock(uri="at://did:x/post")
        holder["client"] = client
        return client

    monkeypatch.setattr(x2b, "Client", fake_client_factory)
    return holder


OGP_STUB = {"title": "T", "description": "D", "image": None}


# ============================================================
# 単一fetch保証（get_x_posts単体）
# ============================================================

class TestSingleFetchPerRun:

    def test_list_command_invoked_exactly_once(self):
        posts = [make_post(i) for i in range(5)]

        with patch(
            "x2b.subprocess.run", return_value=make_cli_result(posts)
        ) as mock_run:
            result = get_x_posts()

        assert mock_run.call_count == 1
        assert len(result) == 5

    def test_requested_maximum_is_100(self):
        with patch(
            "x2b.subprocess.run", return_value=make_cli_result([])
        ) as mock_run:
            get_x_posts()

        command = invoked_commands(mock_run)[0]
        assert command[-2:] == ["-n", str(MAX_POSTS_PER_RUN)]
        assert MAX_POSTS_PER_RUN == 100

    def test_no_second_call_when_full_page_of_100_returned(self):
        # 100件返っても「続きがある」とみなして再取得しない
        full_page = [make_post(i) for i in range(MAX_POSTS_PER_RUN)]

        with patch(
            "x2b.subprocess.run", return_value=make_cli_result(full_page)
        ) as mock_run:
            result = get_x_posts()

        assert mock_run.call_count == 1
        assert len(result) == MAX_POSTS_PER_RUN

    def test_returns_all_posts_from_single_call_in_order(self):
        posts = [make_post("a"), make_post("b"), make_post("c")]

        with patch(
            "x2b.subprocess.run", return_value=make_cli_result(posts)
        ):
            result = get_x_posts()

        assert [post["id"] for post in result] == ["a", "b", "c"]

    def test_empty_result_returns_empty_list(self):
        with patch(
            "x2b.subprocess.run", return_value=make_cli_result([])
        ) as mock_run:
            result = get_x_posts()

        assert mock_run.call_count == 1
        assert result == []


class TestFetchErrorHandling:

    def test_nonzero_exit_raises_and_never_retries_fetch(self):
        with patch(
            "x2b.subprocess.run",
            return_value=make_cli_result(returncode=1, stdout="", stderr="boom"),
        ) as mock_run, pytest.raises(RuntimeError, match="twitter-cli failed"):
            get_x_posts()

        assert mock_run.call_count == 1

    def test_ok_false_raises_runtime_error(self):
        with patch(
            "x2b.subprocess.run", return_value=make_cli_result(ok=False)
        ) as mock_run, pytest.raises(
            RuntimeError, match="twitter-cli returned ok=false"
        ):
            get_x_posts()

        assert mock_run.call_count == 1

    def test_invalid_json_raises_json_decode_error(self):
        with patch(
            "x2b.subprocess.run",
            return_value=make_cli_result(stdout="not-json"),
        ) as mock_run, pytest.raises(json.JSONDecodeError):
            get_x_posts()

        assert mock_run.call_count == 1


# ============================================================
# パイプライン全体での単一fetch保証（main経由）
# ============================================================

class TestPipelineSingleFetch:

    def test_normal_run_invokes_list_once_and_seen_filter_still_works(
        self, pipeline_env, capsys, monkeypatch
    ):
        state_file = x2b.STATE_FILE
        state_file.write_text(json.dumps(["111"]), encoding="utf-8")

        posts = [
            make_post("111", "2026-08-01T00:00:00Z"),
            make_post("222", "2026-08-02T00:00:00Z"),
        ]

        with patch(
            "x2b.subprocess.run", return_value=make_cli_result(posts)
        ) as mock_run, \
             patch("x2b.get_ogp", return_value=dict(OGP_STUB)):
            x2b.main([])

        captured = capsys.readouterr()

        assert mock_run.call_count == 1
        assert "Fetched: 2, Skipped (seen): 1" in captured.out
        assert "Summary: Success: 1" in captured.out
        assert pipeline_env["client"].send_post.call_count == 1

    def test_dry_run_invokes_list_exactly_once(
        self, pipeline_env, capsys, monkeypatch
    ):
        posts = [
            make_post("300", "2026-08-03T00:00:00Z"),
            make_post("301", "2026-08-04T00:00:00Z"),
        ]

        with patch(
            "x2b.subprocess.run", return_value=make_cli_result(posts)
        ) as mock_run, \
             patch("x2b.get_ogp", return_value=dict(OGP_STUB)):
            x2b.main(["--dry-run"])

        captured = capsys.readouterr()
        out = captured.out

        assert mock_run.call_count == 1
        assert "DRY-RUN SUMMARY" in out
        assert "would_post: 2" in out
        pipeline_env["client"].send_post.assert_not_called()
