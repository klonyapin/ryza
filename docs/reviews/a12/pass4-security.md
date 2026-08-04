# Ryza 自動運用システム 第1回フル実装監査(A-12) 監査報告書

**監査人**: 独立監査AI(GLM-4系・実装者Claude系とは別系統)
**監査対象**: 外部入力境界、デプロイスクリプト、依存関係、認可・統制機構
**日付**: 2026-08-05

---

## 検査項目 ⑥: デプロイスクリプトの安全性

### 所見 1: [重大] SQL内のロール名に対するSQLインジェクション可能性(deploy-dashboard.sh)

**根拠**:
`ops/deploy-dashboard.sh` の Python ヒアドキュメント内で生成される SQL。環境変数を用いて SQL 文を組み立てていますが、`.replace()` による単純文字列置換を行っているため、外部から与えられたロール名等に SQL のメタ文字(`"`)が含まれている場合、SQLインジェクションが成立します。

```python
# deploy-dashboard.sh 内 Python ヒアドキュクト
sql = (
    SQL.replace("__DASH_VERIFIER__", scram_verifier(os.environ["RYZA_DASH_PW"]))
    .replace("__BR_VERIFIER__", scram_verifier(os.environ["RYZA_BR_PW"]))
    .replace("__DASH_ROLE__", os.environ["RYZA_DASH_ROLE"])  # <-- 検証無しでSQL文字列に直接埋め込み
    .replace("__BR_ROLE__", os.environ["RYZA_BR_ROLE"])
    .replace("__OWNER__", os.environ["RYZA_OWNER"])
    .replace("__DB__", os.environ["RYZA_DB"])
)
```
SQL 内のプレースホルダ(例: `__DASH_ROLE__`)はダブルクォーテーション `"` で囲まれた識別子として展開されますが、`RYZA_DASH_ROLE` 等の環境変数の値に対するバリデーションやエスケープ処理が存在しません。

**推奨是正**:
スクリプト内の変数(`DB_ROLE`等)について、英数字とアンダースコアのみからなる識別子としてバリデーションを追加するか、PostgreSQL 側で `quote_ident()` / `quote_literal()` を用いた安全なSQL組み立てを行ってください。

---

## 検査項目 ①: インジェクション(SQL・コマンド)

### 所見 2: [中] ingest モジュール群における外部データ取り込み時のメタデータ取り扱い

**根拠**:
`src/ryza/ingest/base.py` の `upsert_document` や `ingest/jquants.py` 等で外部 API から取得したレスポンスを `meta` フィールド等に格納しています。
```python
# base.py
def upsert_document(..., meta: dict[str, Any] | None = None, ...) -> DocResult:
    ...
    cur.execute(
        """
        INSERT INTO docs.documents
            ...
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ...
        """,
        (
            ..., Jsonb(meta) if meta is not None else None, ...
        ),
    )
```
psycopg のパラメータクエリ(`%s`)を正しく使用しているため SQL インジェクションのリスクは低いです。しかし、外部 API の生レスポンス(例えば EDINET, e-Stat 等)を `meta` や各種 JSONB カラムに直接シリアライズして保存しています。外部 API が異常なサイズや巨大なネストを持つ JSON を返した場合、DB 側のリソースを枯渇させる(DoS)可能性があります。

**推奨是正**:
外部 API からのレスポンスを `meta` 等に格納する際、最大サイズや構造の検証(最大バイト数制限等)を導入することを推奨します。

### 所見 3: [軽微] ingest モジュールにおける XML パース(XXE)

**根拠**:
`src/ryza/ingest/base.py` の `parse_feed` メソッドで `xml.etree.ElementTree` を用いて RSS/Atom フィードをパースしています。
```python
def parse_feed(xml_bytes: bytes) -> list[FeedItem]:
    root = ET.fromstring(xml_bytes)
```
Python 3.12 の `ElementTree.fromstring` はデフォルトで外部エンティティ(XXE)を解決しないため安全ですが、将来の Python バージョン変更等の際に念のため `defusedxml` 等の利用を考慮しても良い領域です。

---

## 検査項目 ④: Discord からの指示の認可

### 所見 4: 検査したが所見なし(Kill Switch・承認ボタンの認可)

**根拠**:
`src/ryza/bot/killswitch.py` および `src/ryza/bot/approvals.py` を検査しました。

```python
# killswitch.py
def _require_owner(command: str, actor: str, owner_ids: Iterable[str]) -> None:
    if not is_owner(actor, owner_ids):
        raise NotOwnerError(f"非オーナーの /{command} を拒否: user={actor}")
```
```python
# approvals.py
def record_decision(
    conn: psycopg.Connection, ...
    owner_ids: Iterable[str], ...
) -> Decision:
    ...
    if not is_owner(decided_by, owner_ids):
        raise NotOwnerError(f"非オーナーの承認操作を拒否: user={decided_by}")
```
`/kill` や `/winddown` 等の Kill Switch 操作、および承認/却下ボタン(`ApprovalView`)の押下において、確実にオーナー検証(`_require_owner` / `is_owner`)が行われています。Discord の `interaction.user.id` を用いて文字列比較しており、認可バイパスの経路は見つかりませんでした。正常に統制されています。

---

## 検査項目 ②: 秘密情報の扱い

### 所見 5: [重要] Bot 配送時の Webhook URL マスクはしているが、チャネル ID 等は露出する点

**根拠**:
`src/ryza/bot/webhooks.py` および `src/ryza/bot/main.py` の配送ループ。

```python
# webhooks.py
def mask_url(webhook_url: str) -> str:
    ...
    return f"{m.group('prefix')}/***" if m else "<webhook url masked>"
```
Webhook URL 自体は `mask_url` でトークン部分を伏せてログ・例外に出力する設計は非常に優秀です。しかし、`main.py` 内でチャネルの解決に失敗した際、以下のように Discord の内部チャネル ID を例外メッセージに出力しています。
```python
# main.py
if channel_id is None:
    raise RuntimeError(f"チャンネル未解決(ensure 前?): {msg.channel}")
...
if channel is None:
    raise RuntimeError(f"チャンネル取得失敗: {channel_id}")
```
これは Discord サーバーの内部 ID であるためシステム上の致命的な秘密ではありませんが、ログ等に流出する点は留意が必要です。

### 所見 6: 検査したが所見なし(デプロイスクリプトのパスワード扱い)

**根拠**:
`ops/deploy-dashboard.sh` におけるデータベースのパスワード設定を検査しました。
```bash
# deploy-dashboard.sh
ensure_password_secret() { ... }
DASH_PW="$(ensure_password_secret "${DB_PASSWORD_SECRET}")"
...
ROLE_SQL_B64="$(
  RYZA_DASH_PW="${DASH_PW}" RYZA_BR_PW="${BR_PW}" \
  ...
  python3 - <<'PY'
```
DB のパスワードをクライアント側で SCRAM-SHA-256 検証子としてハッシュ化し、VM 側には平文を渡さず(検証子のみ渡す)デプロイする設計は非常に強力な秘密保護統制です。デプロイスクリプト内でパスワードの平文がファイル等に残留しないよう適切に処理されています。

---

## 検査項目 ③: 外部 API 応答の検証

### 所見 7: [中] 各 ingest モジュールの外部 API 応答に対する型検証の不足

**根拠**:
`src/ryza/ingest/jquants.py` の `ingest_daily_quotes` や、`src/ryza/ingest/base.py` の `upsert_document` 等。

```python
# jquants.py
def _num(rec: dict[str, Any], key: str) -> float | None:
    v = rec.get(key)
    return float(v) if v is not None else None
```
J-Quants 等の外部 API レスポンスから値を取得する際、キーが存在しなかった場合や `None` だった場合は安全にパスしますが、例えば文字列型で数値以外が返された場合 `float(v)` で `ValueError` が発生します。`main` ループや `ingest_all` で `except Exception` によって握りつぶされリトライ対象から外れるためクリティカルではありませんが、異常応答時の耐性がやや弱いです。

### 所見 8: [重要] ingest.jquants の DAILY ジョブが autocommit モードであること

**根拠**:
`src/ryza/ingest/jquants.py` の `main` 関数。
```python
def main(argv: list[str] | None = None) -> int:
    ...
    conn = connect(autocommit=True)
    try:
        ...
        with run_ctx("ingest.jquants.daily", params, conn=conn) as r:
            result = run_daily(
                conn, r, store, fetcher,
                quote_date=quote_date, with_instruments=not args.no_instruments,
            )
```
`autocommit=True` で DB 接続を使用しています。途中で API 通信が途絶えた場合や、一部のデータ保存に失敗した場合、トランザクションがロールバックされずに「途中までのデータが確定」してしまいます。リネージ(`meta.lineage_edges`)の記録途中で失敗した場合、証憑とのリンクが切れたレコードが残る可能性があります。他の ingest モジュール(`edinet.py`, `news_rss.py` 等)でも同様です。

**推奨是正**:
ジョブ全体を1トランザクション(`with conn:`)で囲み、ジョブ単位でコミット・ロールバックを行うべきです。`Run` の終了ステータスとデータの原子性を一致させる必要があります。

---

## 検査項目 ⑤: 依存パッケージの既知の懸念

### 所見 9: [軽微] 外部依存ライブラリのバージョン指定方針

**根拠**:
`pyproject.toml`
```toml
dependencies = [
    "psycopg[binary]>=3.2",
    "pandas>=2.0",
    "pyyaml>=6.0.3",
]
```
`>=` による緩いバージョン指定が多用されています。`requirements.txt` 等でロックファイルを提供しない限り、ビルドのたびに最新のパッケージが導入され、予期せぬ破壊的変更が本番環境に入り込む(Supply Chain Attack 含む)リスクがあります。

**推奨是正**:
`uv.lock` 等のロックファイルをデプロイ資材に含め、CI/CD で再現性のあるビルドを保証することを推奨します。

---

以上、監査報告を完了します。