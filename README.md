# x2b

X（Twitter）リストの投稿をBlueskyへクロスポストするスクリプト。

## セットアップ

```bash
python3 -m venv .venv
.venv/bin/pip install -r requests.txt
cp .env.example .env   # BSKY_HANDLE / BSKY_APP_PASSWORD / X_LIST_ID を設定
```

## 使い方

```bash
# 通常実行（新しいX投稿をBlueskyへ投稿し、seen.jsonを更新する）
.venv/bin/python x2b.py

# Dry-run
.venv/bin/python x2b.py --dry-run
```

X posts are fetched once per run, with a maximum of 100 posts
（X投稿は1回の実行につき1回だけ取得し、最大100件です）。

### Dry-runモード

`--dry-run` を付けると、実際にはBlueskyへ何も投稿せず、
seen state（seen.json）も変更せずに、パイプラインを最後まで実行できる。

- X取得 / フィルタ / 本文生成 / グラフェム検証 / OGP・サムネイルサイズ検証 /
  エラー分類 / リトライ経路は通常実行と同じ経路で動作する
- Blueskyへの投稿（send_post）・blob uploadは一切行われない
- seen.jsonへの書き込みは発生しない
- 投稿されるはずだった内容は `[DRY-RUN] Would post:` として出力され、
  最後に `DRY-RUN SUMMARY`（would_post等の集計）が表示される

「この実行なら何が投稿されるか」を安全に確認するためのモードであり、
Dry-runの実行結果は実投稿として記録されない。

## テスト

```bash
pytest
```

テストは実際のBlueskyアカウント・X API・外部ネットワークを使用しない。
