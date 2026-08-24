# ADR 0001: X list取得におけるpaginationの禁止

- Status: Accepted
- Date: 2026-08-25
- Related: Issue #8（過去の二重投稿インシデント調査に関連）

## Context

x2bはX（Twitter）リストの投稿をBlueskyへ自動クロスポストするツールであり、
自動実行されるため、同一投稿の二重公開は重大なcorrectness failureである。

かつての `get_x_posts()` 実装は、最大100件を上限とする
`twitter-cli list ... -n N` の呼び出しを複数回繰り返すことで
最大200件まで取得する「ページング」構造になっていた。
しかし `-n/--max` は結果件数の上限指定であり、「次ページ」を保証する
cursorではなく、呼び出しが前回の続きから始まることを示す根拠もない。

つまり複数回呼び出しは、

    call #1: latest 100 posts
    call #2: latest 100 posts

のように同じ最新投稿を再取得する可能性がある。実際に過去に
二重投稿が発生しており、これは効率問題ではなく
クロスポストの安全性・正確性の問題である。当時の実装には
page間のpost-ID deduplicationも存在しなかった。

## Decision

- 現時点ではpaginationを使用しない（禁止する）
- 1 runにつきX list fetchは1回のみ
- 取得上限は `MAX_POSTS_PER_RUN = 100` を単一の上限（SSOT）とする
- 明示的なcursor/token/page identifierを持たない複数回取得は行わない

## Rationale

**correctness / safety > candidate volume**

「一度に100件しか確認できない」という候補量の制約より、
「同じ投稿を再取得して二重投稿する」リスクの方がはるかに重大である。
決定的な100件の小さい候補集合を毎回安全に処理する方が、
重複混入の可能性がある200件を処理することより優れている。

## Consequences

### Merits（メリット）

- 同一run内のpagination由来の重複を排除できる
- X取得挙動が決定的になる（同じコマンドを正確に1回だけ実行）
- Dry-runと通常実行の挙動が単純で一致する
- 二重投稿リスクを低減する

### Drawbacks（デメリット）

- 1 runで取得できる候補は最大100件
- 100件を超える新規投稿を取り込むには次回runを待つ必要がある

## Future reconsideration

将来paginationを再導入する場合は、最低限以下をすべて満たすこと:

1. upstream側（X API / twitter-cli）の正式なpagination仕様の文書化
2. cursor/token/page identifierの存在
3. page間で確実に前進する保証（forward progress guarantee）
4. page間のpost-ID deduplication
5. runあたりのハードな上限（hard per-run upper bound）の維持
6. 重複するpage（同一内容が返るpage）への耐性
7. Dry-runと通常実行でcandidate selectionが一致すること
8. paginationに関する十分な自動テスト

「単純に `-n 100` を複数回呼ぶだけ」の実装には戻さないこと。
