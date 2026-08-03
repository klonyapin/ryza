-- 0032: press.outbox の内部キー(author.member_id)を embed_json の外へ出す
--       (独立役員審査 0020 C-10 の恒久是正 — ops/reminders.yaml outbox-internal-key-separation)
--
-- ────────────────────────────────────────────────────────────────────────────
-- 何が問題だったか
-- ────────────────────────────────────────────────────────────────────────────
-- 0020 は「アイコン上書きを**配送時**に解決する」方式を採った(投入時に焼き付けると、
-- 滞留中の投稿が古いアイコンのまま出る)。配送時に「この embed は誰の発言か」を知る
-- ために、生成側が embed_json の author へ内部キー ``member_id`` を混ぜ、送信直前に
-- ``org.resolve_author`` が取り除いていた。
--
-- これは Discord API のフィールドではない値を press.outbox の全行に永続化し、その除去を
-- **送信直前の1関数だけ**に依存させる構造である(独立役員審査 0020 C-10)。将来 embed_json を
-- 別経路(新しい配送先・再送ツール・エクスポート)へ流す実装が resolve_author を通し忘れると、
-- 未知フィールドがそのまま Discord へ送られる。現に実害が出ていないのは Discord が未知
-- フィールドを黙って無視するからであり、それは仕様保証ではない。
--
-- ────────────────────────────────────────────────────────────────────────────
-- 是正: 列として構造分離し、スキーマ自身に「混入しない」を守らせる
-- ────────────────────────────────────────────────────────────────────────────
-- 1. ``author_member_id`` 列を足す。投入側(``ryza.bot.outbox.enqueue``)が embed から
--    内部キーを**外して**この列へ移し、配送側(``bot/main.py``)は列を読む。
-- 2. embed_json 側に内部キーが再混入しないことを CHECK 制約で強制する。関数1つの規律で
--    はなく表の性質にすることで、「新しい書込経路が strip を忘れる」を書込時に落とす。
--
-- **NOT VALID にする理由**: 既存行(0020 以降・本マイグレーション適用前に投入された行)は
-- embed_json の author に member_id を持ったままである。VALIDATE すると適用が失敗するか、
-- 過去行を書き換える羽目になる。press.outbox は配送済みの投稿記録=証跡であり、遡って
-- 書き換えない。NOT VALID の CHECK は**以後の INSERT/UPDATE にだけ効く**ので、
-- 「既存行は従来経路のまま(列は NULL)、新規行は構造分離」という移行が過去を改変せずに済む。
-- 配送側は当面どちらも扱える(列が NULL の行は従来どおり embed 内のキーを見る)。

ALTER TABLE press.outbox ADD COLUMN author_member_id text;

-- 台帳(config/org.yaml)の members[].id と同じ字種に限る。DB にメンバー表は無く FK は
-- 張れない(0020 と同じ理由)ため、ここで縛れるのは形だけである。台帳との突合は
-- ローダ側(org.effective_members)が行い、台帳に無い id は表示側で無視される。
ALTER TABLE press.outbox ADD CONSTRAINT outbox_author_member_id_format_check
    CHECK (author_member_id IS NULL OR author_member_id ~ '^[a-z0-9][a-z0-9_-]{0,63}$');

-- embed_json に内部キーを混ぜない(C-10 の本丸)。author が object でない/無い embed は
-- jsonb_exists が NULL または false を返し、NOT の結果は NULL/true — CHECK は FALSE の
-- ときだけ失敗するので、author を持たない起動通知などはそのまま通る。
ALTER TABLE press.outbox ADD CONSTRAINT outbox_embed_has_no_internal_keys_check
    CHECK (NOT jsonb_exists(embed_json -> 'author', 'member_id')) NOT VALID;

COMMENT ON COLUMN press.outbox.author_member_id IS
    'embed の発信者キャラクター(config/org.yaml の members[].id)。配送時のアイコン上書き'
    '解決に使う内部キーで、Discord へは送らない。NULL=0032 以前の行(embed_json 内の'
    'author.member_id を見る従来経路)。';
