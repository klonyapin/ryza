# T-021: リスク計測のポジション粒度是正(ポッド間ネットの解消 — A-12 是正 F-6・Issue #121)

- 起草: 2026-08-04 設計リード / 前提: T-015 統合済み main に対して作業
- 前提知識: CLAUDE.md、docs/reviews/t014-design-decisions.md 項目3(グロス計算の行単位化)、src/ryza/risk/{engine,daily}.py、migrations/0014(trading.positions)
- **保護領域**(リスクリミット)。統合は設計リードが独立役員審査+みなし承認手続で行う
- 本仕様書自体を実装ブランチの最初のコミットとして `docs/tasks/T-021-risk-position-granularity.md` に含めること

## 問題(F-6)

`load_positions`(src/ryza/risk/daily.py:126-198)は `GROUP BY p.instrument_id, p.asset_class HAVING sum(p.qty) <> 0` で**ポッド(fm)間をネット**してから時価評価する。`trading.positions` の PK は (book_id, fm, instrument_id)(0014:94-104)で qty は符号付き。ポッド A が +100、ポッド B が -100 を持つと、リスクエンジンには**保有ゼロ**として渡り:

1. **グロスレバ・資産クラスグロスが過小計上**される(gross_leverage / single_asset_class_gross は本来 Σ|行| で測るべき — ゲート側 G-4/G-8 は t014 設計判断3 で行単位 Σ|qty|×時価に是正済み。リスクレポート側だけポッド間ネットが残った)
2. ネットゼロ銘柄の時価欠落が**検出されない**(HAVING で行ごと消えるため Exclusion にも notes にも出ない)

## 是正方針(設計リード裁定 — 測度ごとに集計意味論を分ける)

ポジションは**行単位(fm × instrument)**でエンジンへ渡し、ネットするかどうかは**各測度の意味論に従って測度側で決める**:

| 測度 | 集計 | 理由 |
|---|---|---|
| gross_leverage / single_asset_class_gross | **行単位 Σ\|value\|**(ネットしない) | 建玉の総量を測る。ポッド間の相殺は執行上の相殺ではない(t014 設計判断3 と同じ理屈) |
| issuer_concentration | **銘柄単位でネット後 abs** | ゲートのファンド集中(compliance.py の abs(post_fund_qty))と同じ意味論。同一銘柄の両建ては発行体リスクとしては相殺される |
| ES(es95) | **銘柄単位でネット**(現行の weights 集計を維持) | 同一銘柄の逆ポジションの P&L は厳密に相殺する。engine.py:266-271 の weights は既に instrument_id ごとに符号付き加算しており、行単位入力にすればそのまま正しくネットされる |

## 実装

### 1. `daily.load_positions` — 行単位化

- SQL を `SELECT p.fm, p.instrument_id, p.asset_class, p.qty FROM trading.positions p WHERE p.book_id = %s AND p.qty <> 0` に変更(GROUP BY 廃止。行内 qty=0 のみ落とす — ポッド内の消滅ポジション)
- 時価・乗数の取得は現行どおり銘柄単位(ids は重複排除すること)
- **時価欠落の Exclusion / notes は銘柄単位で重複排除**(同一銘柄を複数ポッドが持つ場合に2重に出さない)
- docstring の「全ポッド合算・銘柄単位」を実態に合わせて更新

### 2. `engine.RiskPosition` — fm フィールド追加

```python
@dataclass(frozen=True)
class RiskPosition:
    instrument_id: int
    asset_class: str
    value: Decimal  # 符号付き JPY 時価総額(行=fm×instrument 単位)
    fm: str = ""    # 保有ポッド。集計はしないが由来の開示・デバッグ用
```

既存テストの構築箇所が壊れないようデフォルト値を持たせてよい(ただし daily 側は必ず実 fm を渡す)。

### 3. `engine.guardrail_usage` — issuer のみネット化

- `by_issuer` を2段集計に変更: まず instrument_id ごとに**符号付き** value を合算 → その後 abs
- `by_class` / `gross` は現行の行単位 Σ|value| のまま(入力が行単位になることで自動的にグロス計上になる — これが是正の本体)
- docstring に測度ごとの集計意味論(上表)を明記

### 4. `engine.es95` — ネットゼロの後処理

- weights 集計後、**ネット結果が 0 の銘柄を weights から落とす**(現行は行単位の `pos.value != 0` ガードのみで、合算後ゼロが `included`/除外判定・共通観測日の計算に混入し得る)
- それ以外のロジックは変更しない

## テスト(tests/risk/)

- 両建てシナリオ: 同一銘柄をポッド A +q・ポッド B -q で保有 → gross_leverage と single_asset_class_gross は 2|q×price|/nav、issuer_concentration は 0、es95 の weights から当該銘柄が消える(ネットゼロ)
- 部分相殺: +100/-40 → issuer は |60×price|、gross は 140×price
- 時価欠落×複数ポッド: 同一銘柄を2ポッドが保有し時価欠落 → Exclusion / notes は**1件**
- load_positions が fm 単位の行を返すこと(合算しないこと)の DB テスト
- **snapshot**: tests/risk/test_engine_invariance.py と engine_snapshot.json は保護されたリグレッション固定。両建てが無い入力では結果が不変であること(不変が期待値)。もし snapshot 更新が必要になった場合は、差分の数値的理由を PR 本文で説明すること(黙って更新しない)

## 受け入れ基準

全テスト+ruff 通過 / 両建てシナリオで上表どおりの数値(手計算固定値で検証)/ 両建て無しの既存入力で全測度の結果不変 / ips.yaml 値のハードコードなし / LLM 非関与 / コミットは日本語+Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>、push しない(統合は設計リードが行う)
