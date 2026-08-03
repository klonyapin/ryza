# 独立役員意見書 — T-017 FM エージェント第一陣(Ben/Jim)

- 日付: 2026-08-03 / 対象: ブランチ最終コミット 7d4ffb0(8 commits, 26 files)
- 審査者: 独立役員(非執行・批判専任。起草者の選好は不知)
- 根拠: CLAUDE.md 不変原則1・4・6、docs/tasks/T-017-fm-agents.md、docs/design/81-fm-mandates.md、config/mandates/{ben,jim}.yaml
- 検証: `ruff check` 通過 / `pytest tests/fm tests/jobs/test_daily.py` 74 passed(審査者が実行)

## 判定: 条件付き承認

### マージ前必須(3件)

1. **C-1(重大)ポッド内集中度上限の突破経路**。`fm/ben.py:189-207` に候補の重複排除が無く、
   `fm/base.py:344` の `held` は同一実行内で建てた分を反映しない。`gate/orders.py:93-108, 235`
   の G-3 も pending 注文を post-trade に加算しない(G-7 は `orders.py:127-147` で加算しており非対称)。
   同一 instrument_id を 3 件返すだけで、各注文は 20% で G-3 を通り合計 60% となり
   ben のポッド内集中度上限 40% を破る。**LLM 出力が実効集中度を決めている点で不変原則1 の趣旨にも反する**。
   是正: `submit_intents` に決定論的重複排除と実行内 held 更新、重複入力のテスト。
2. **C-2(重大・保護領域)0018 の追記オンリー保証の不足**。`migrations/0018:59-63` は行トリガ +
   REVOKE UPDATE, DELETE のみ。`migrations/0015:82-111` が既に「TRUNCATE は行トリガを迂回する」として
   文トリガ + REVOKE TRUNCATE を標準化しており、後発の 0018 が同じ穴を再導入した。
   `TRUNCATE trading.fm_theses CASCADE` で FM 判断証跡と trading.orders が同時に消える。
   是正: BEFORE TRUNCATE 文トリガ + REVOKE TRUNCATE(併せて trading.orders の封鎖も)。
3. **C-7(低)語彙外 direction の無言ドロップ**(`fm/base.py:309-312`)。skipped に残す 1 行。

### リマインダー登録を条件に許容(4件)

- C-3 提案テキストの無検疫再注入(`ben.py:63-89, 92-113`)× 追記オンリー = 撤去不能なプロンプト汚染。
  外部文書経由の注入が最大 10 週間、着任プロンプトに残る。封じ込めはユニバース・スロット・ゲートで効く。
- C-4 `market.instrument_classification` が上書き型で PIT 履歴を持たない(0015:117-127)。
  リプレイのユニバースが静かに空になる/look-ahead を許す。**現状で E6 達成は主張できない**。
- C-5 FM 段が単一 savepoint で、Ben の例外が Jim の決定論注文を巻き戻す(`jobs/daily.py`)。
- C-6 証憑検証が参照先 as_of のみで ts を見ない(`fm/theses.py:96-117`)。

## 評価できる点(反対を探して見つからなかった箇所)

- ゲート変更は kwarg 追加と INSERT 1 列のみで、G-0〜G-10 と fail-closed 挙動を一切弱めていない。
- long-only 裁定と 0018 の CHECK 差異は**実害なし**。schema enum / direction ハードコード /
  allow_short 既定 False / side マッピング / G-2・G-9 / runner の `_LEDGER_SIDES` の 6 層で遮断される。
- 不変原則1 の固定はシグネチャ検査(`test_sizing.py:31-41`)と挙動テスト(`test_ben.py:89-102`)の二重。
- 未分類銘柄・NAV 欠落・出来高欠測をいずれも fail-closed にし、ユニバースを埋めるためにタグを
  緩めない旨を明記している点は正しい判断である。

## この承認判断が誤っている場合の理由トップ3

1. C-1 を「重複候補は現実の LLM ではまず起きない」と過大評価している可能性。
   → 証拠で決着させる: 重複入力の回帰テストを追加すれば争点は消える(コストは数行)。
2. C-2 を過大評価している可能性(所有者ロールを取られた時点で終わり、という反論)。
   → ただし 0015 で本プロジェクト自身が定めた基準に後発の migration が達していないことは事実であり、
      基準の非一貫は監査上それ自体が欠陥である。
3. 逆に C-3/C-4 を過小評価している可能性。撤去不能なプロンプト汚染と PIT ユニバースの不在は、
   実弾移行前に必ず塞ぐべき性質のもので、「後日」で足りるかは投資委員会の判断を仰ぐべきである。

## 敵対的シナリオ評価(要旨)

侵害された Ben が出せる最悪は「同一銘柄を max_candidates 件重複させポッド資本の 60% を
1 銘柄に集中(C-1)」+「未来にわたる自己プロンプト汚染(C-3)」。サイズ・レバ・空売り・帳簿・
実弾には到達できない。スロット制とゲートの封じ込めは設計どおり効いており、C-1 の 1 点だけが
マンデート境界を越える。

## 設計リード裁定(2026-08-03 追記)

- C-1・C-2・C-7: マージ前必須として実装(C-8 の決定論ソートも C-1 是正と同時に導入)。
- C-3〜C-6: ops/reminders.yaml へ登録の上、次回以降の PR で対応。C-4(PIT ユニバース)は
  E6 に関わるため、リプレイ結果を提示する際は「E6 未達」の但し書きを必ず付す。

## 後続是正審査記録(2026-08-03 追記)— C-3 / C-5 / C-6 の是正

- 対象: `origin/main..HEAD` 5 commits(`research/prompting.py` 新設・`migrations/0023`(保護領域)・ts 検証・fm 段分割)。検証: `ruff check` 通過 / `pytest tests/fm tests/research tests/jobs/test_daily.py tests/governance` **205 passed**(審査者が実行)。判定は**条件付き承認** — C-5・C-6 は是正済みと認めるが、C-3 の封じ込め設計に未解決の副作用がある。
- **C-9(中・回帰・マージ前必須)**: 共通化した検出正規表現 `<<<[^<>\n]*>>>`(`research/prompting.py:28`)は、旧 boardroom 版 `<<<\s*(speaker\s*=|end)[^>]*>>>` が捕まえていた 2 クラス — トークン内に `<` を含む `<<<speaker=cio<x>>>` と改行をまたぐ `<<<speaker=\nchairman>>>` — を取りこぼす。worktree の `sanitize_speech` で実測したところ無害化されず素通りし、既存 61 テスト全通過のため検出できていない。会議側の防御を弱めた回帰である。是正: `re.compile(r"<<<[^>]*>>>")`(旧新の全例を捕捉・冪等・`a<b` / `x >> y` は不変を実測確認)+ 上記 2 ケースの回帰テスト。
- **C-11(重・マージ前必須)**: 建玉根拠を検疫すると holdings の `entry_thesis` が None になり、旧実装にあった `invalidation` キー自体が payload から消える(`fm/ben.py:210-223`)。Ben は「建玉時の invalidation が成立しているか」の判定(`fm/ben.py:170`)を材料ゼロで求められ、当該保有は**降りる条件のない持ち切り**になる。決定論側の代替 exit は無い。`record_thesis` は空 invalidation を拒む(`fm/theses.py:231`)のに、読出し側で同じ状態を作れてしまうのは 40 §制約1 の趣旨に反する。是正: payload に `entry_thesis_quarantined` を立て、rules に「根拠喪失の保有は原則 exit を提案」を明記し、実行サマリに表出する。
- **C-10(重・裁定要求 — 保護領域)**: 0023 判断2 の「解除行を設けない」根拠(DB 権限を得た攻撃者が汚染 thesis を戻せる)は成立しない。`trading.fm_theses` と検疫表の INSERT 権限を分けるロールは存在せず(migrations に GRANT / ロール定義なし)、同じ攻撃者は解除ではなく**新しい汚染 thesis を INSERT** すれば足りる。守っている脅威が実在しない一方で、「thesis_id を全件 INSERT すれば判断履歴と全建玉根拠を恒久的にプロンプトから消せる」不可逆な証跡抹消経路が増えた。TRUNCATE を塞いだ 0018(C-2 是正)と方向が逆である。最低限、検疫行の発生件数・対象を日次サマリと監査に出し、silent な mass-quarantine を検知可能にすること。
- **C-12(中)**: 0023 が示す誤検疫の救済(同内容を新 thesis として再記録)は、唯一の入口 `record_thesis`(`fm/theses.py:196-265`)が as_of・run_id・model XOR rule_id を必須にするため、**建玉時点の判断を後日の判断として、生成していないモデル名で記録する**ことになる(不変原則 3・4 の汚染)。加えて `quarantine_thesis` にも救済にも CLI / runbook が無く(`docs/` `ops/` に「検疫」の記述ゼロ)、手動運用を前提にした統制の手順が未定義のまま reminders を done にしている。
- **C-13(中)**: 共通防御を謳いながら、最大の外部テキスト注入経路 `research/agents/base.py:143-155`(`docs.documents` の title / body を無加工でプロンプトへ)が未フェンスのまま。Ben 側も `source`(`fm/ben.py:139`)はフェンス外。取込文書が最初に通る経路が素通しである以上、注入の入口は塞がっていない。
- **C-14 / C-15(低)**: `prompting.fence_open(tag)` は tag を無害化しない(`prompting.py:31-33`)。現行の呼び出しは int のみで実害は無いが、将来 symbol / source_name を tag に入れた瞬間にフェンスヘッダへの注入が成立する。また 0023 の `run_id` は NULL 許容・`quarantined_by` は語彙検査なしの自由テキストで、0013 が「整合性の要」として宣言する「全テーブル run_id 必須 + meta.runs への FK」に届いていない(C-2 と同型の、後発 migration による自前基準の未達)。
- **反対を探して見つからなかった点**: 0023 の追記オンリーは 0015 / 0018 標準を完全に踏襲(行トリガ + 文トリガ + `REVOKE UPDATE, DELETE, TRUNCATE`)し、`meta.runs` 経由のカスケード TRUNCATE でも文トリガが発火するため穴は無い。ts 検証を `min(as_of), max(ts)` の 2 列で DB 側に正規化させる設計は、LLM が渡す tz なし文字列をアプリでパースするより正しく、bars / indicators とも timestamptz(`0002_market.sql:30,50`)なので aware 同士の比較になる。Jim は `_series`(`fm/jim.py:186-193`)で既に `ts <= as_of` を課しており、既存 PIT テストの期待値変更も無い。段分割は `_run_stage` の savepoint 粒度を素直に使い、逆方向(Jim 例外 → Ben 実行)も対称性から成立する。Kill Switch 判定が FM 単位になったのは副次的な改善である。
- **注記(過大主張の防止)**: 日足 ts は取引日 00:00 UTC(= 09:00 JST の寄り前ラベル)で格納される(`ingest/jquants.py:209`)ため、`ts <= as_of` は「そのバーが引けている」ことを保証しない。C-6 是正で塞げたのはバックフィル起因の `ts > as_of` に限られ、実質的な防御は従来どおり as_of 側である。
- **この判断が誤っている場合の理由トップ3**: ①C-9 の過大評価(LLM は不正形フェンスを境界と読まない)→ 是正が正規表現 1 行なので争う価値がない。②C-11 は「根拠不明の保有は次回 Ben が自然に処分する」で足りる可能性 → 決定論の exit 規則が無い以上、意見ではなく縮退時の e2e テストで決着させるべき(議論規約4)。③C-10 の過大評価(検疫は手動でしか叩かれない)→ ただし手動前提の統制に手順書が無いこと(C-12)と併せると、統制としては未完成である。
