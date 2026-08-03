#!/usr/bin/env bash
#
# deploy-guards.sh — デプロイ統制のゲート(ops/deploy-dashboard.sh が source する)。
#
# 本ファイルは保護領域 deploy_path の一部。ここを緩めることは統制を緩めることと同義。
#
# 切り出した理由は**テスト可能性**の一点にある。統制コードは壊れても静かに通る
# (独立役員 再審査「次回 PR 対応」第3項)。デプロイ本体に直書きされたままでは、
# gcloud も VM も無い CI で「実際に中断すること」を確かめる手段が無い。関数にして
# おけば tests/ops/test_deploy_guards.py が gcloud をスタブし、一時 git リポジトリを
# 相手に各ゲートの中断挙動を直接検証できる。
#
# 約束事:
#   - 失敗は `return 1`(`exit` しない)。中断するかは呼び出し側が決める。
#     呼び出し側は必ず `|| exit 1` を付けること — 付け忘れは統制の無効化と同じ。
#   - 診断は stderr。stdout に出すのは呼び出し側が受け取る値だけ(guard_git_state の SHA)。
#   - source 時に副作用を持たない(関数定義のみ)。

# ── 承認済みコード一致(定款第5条・独立役員審査 重大-1 / 再審査 条件4)──────────
# デプロイできるのは「レビュー・CI・マージを通った main の実物」だけ。ローカルの
# 未コミット変更や未 push のコミットが本番へ入る経路をここで塞ぐ。
# 抜け道(--force 等)は意図的に用意しない — 用意すると統制目的が消えるため。
# 成功時のみ stdout にコミット SHA(= code_version)を出す。
guard_git_state() {  # $1=リポジトリルート $2=期待する origin URL
  local root="$1" expected_origin="$2"
  local origin_url dirty head origin_main

  if ! git -C "${root}" rev-parse --git-dir >/dev/null 2>&1; then
    echo "ERROR: ${root} は git リポジトリではない。デプロイは承認済み main の checkout から行う。" >&2
    return 1
  fi

  # origin が本物のリポジトリを指すことを確認する。HEAD == origin/main だけでは、
  # origin を攻撃者のリモートに差し替えれば任意コードが「承認済み」を騙れる(再審査 条件4)。
  origin_url="$(git -C "${root}" remote get-url origin 2>/dev/null || true)"
  if [ "${origin_url%.git}" != "${expected_origin%.git}" ]; then
    echo "ERROR: origin が想定と違う(取得='${origin_url}' 期待='${expected_origin}')。" >&2
    echo "       origin を差し替えれば HEAD==origin/main は容易に満たせるため、ここで中断する。" >&2
    echo "       SSH リモート(git@github.com:...)を使っている場合は EXPECTED_ORIGIN で明示すること。" >&2
    return 1
  fi

  dirty="$(git -C "${root}" status --porcelain)"
  if [ -n "${dirty}" ]; then
    echo "ERROR: 作業ツリーに未コミット/未追跡の変更がある。デプロイ対象は承認済み main のみ(定款第5条)。" >&2
    printf '%s\n' "${dirty}" >&2
    return 1
  fi

  # fetch 失敗を握り潰さない。古い origin/main と比較すれば、既に main から巻き戻された
  # コミットでも「一致」と判定されうる(検査が沈黙する — 再審査 条件1 と同じ思想)。
  if ! git -C "${root}" fetch origin main --quiet; then
    echo "ERROR: origin/main を取得できなかった。最新の承認済みコードと照合できないため中断する。" >&2
    return 1
  fi
  head="$(git -C "${root}" rev-parse HEAD)"
  origin_main="$(git -C "${root}" rev-parse origin/main)"
  if [ "${head}" != "${origin_main}" ]; then
    echo "ERROR: HEAD(${head}) が origin/main(${origin_main})と一致しない。" >&2
    echo "       PR をマージし、main を checkout してから再実行すること(全変更 PR 経由)。" >&2
    return 1
  fi

  printf '%s\n' "${head}"
}

# ポリシーテキストから公開メンバー(allUsers / allAuthenticatedUsers)の行を抜く。
# 行末アンカーを付けるのは `user:allUsers@example.com` のような**部分一致**で
# 誤検出しないため。
_deploy_guard_public_members() {  # $1=ポリシーのテキスト
  printf '%s\n' "$1" | grep -E '[[:space:]](allUsers|allAuthenticatedUsers)$' || true
}

_deploy_guard_service_iam_policy() {  # $1=service $2=project $3=region
  gcloud run services get-iam-policy "$1" --project "$2" --region "$3" \
    --flatten="bindings[].members" --format="value(bindings.role,bindings.members)"
}

# ── サービスレベルの公開バインディング検査と除去(重大-3 / 再審査 条件1)──────────
# 2026-08-02 の無認証 Cloud Run 公開版と同名サービスの場合、allUsers の run.invoker が
# 残っていると IAP を有効化しても直接 URL で全世界に公開されたままになる。
#
# **失敗は「公開なし」ではない**(再審査 条件1)。get-iam-policy が権限不足・API 断で
# 落ちたときに「検出ゼロ」と同じ扱いにすると、検査自体が沈黙して公開を見逃す。
# 取得の終了コードを見て、失敗なら中断する。
guard_service_public_bindings() {  # $1=service $2=project $3=region
  local service="$1" project="$2" region="$3"
  local policy found still role member

  if ! policy="$(_deploy_guard_service_iam_policy "${service}" "${project}" "${region}")"; then
    echo "ERROR: ${service} の IAM ポリシーを取得できなかった。公開状態を確認できないため中断する。" >&2
    echo "       (取得失敗を『公開なし』とみなすと検査が沈黙する — 再審査 条件1)" >&2
    return 1
  fi
  found="$(_deploy_guard_public_members "${policy}")"
  if [ -z "${found}" ]; then
    echo "-- サービスレベルの公開バインディングは無し"
    return 0
  fi

  echo "WARNING: 公開バインディングを検出したため除去する:" >&2
  printf '%s\n' "${found}" >&2
  while IFS=$'\t ' read -r role member; do
    [ -n "${role:-}" ] && [ -n "${member:-}" ] || continue
    # </dev/null: gcloud に while の入力(残りの行)を食わせない
    if ! gcloud run services remove-iam-policy-binding "${service}" \
        --project "${project}" --region "${region}" \
        --member="${member}" --role="${role}" --quiet >/dev/null </dev/null; then
      echo "ERROR: 公開バインディングを除去できなかった: ${role} ${member}" >&2
      return 1
    fi
  done <<< "${found}"

  if ! policy="$(_deploy_guard_service_iam_policy "${service}" "${project}" "${region}")"; then
    echo "ERROR: 除去後の IAM ポリシーを再取得できなかった。公開のままの可能性がある。" >&2
    return 1
  fi
  still="$(_deploy_guard_public_members "${policy}")"
  if [ -n "${still}" ]; then
    echo "ERROR: 公開バインディングを除去できなかった。サービスは全世界公開のままの可能性がある。" >&2
    printf '%s\n' "${still}" >&2
    return 1
  fi
  echo "-- 公開バインディングを除去した(現在は無し)"
}

# ── プロジェクトレベル IAM の公開バインディング(再審査 条件1)────────────────────
# roles/run.invoker がプロジェクト全体で allUsers に付いていると、サービス側の
# ポリシーが清潔でも全 Cloud Run サービスが無認証で叩ける。ここは**自動で消さない** —
# 他サービスへの影響が読めないため、検出したら中断して人間に判断させる。
guard_project_public_bindings() {  # $1=project
  local project="$1" policy public

  if ! policy="$(gcloud projects get-iam-policy "${project}" \
      --flatten="bindings[].members" --format="value(bindings.role,bindings.members)")"; then
    echo "ERROR: プロジェクトの IAM ポリシーを取得できなかった。公開状態を確認できないため中断する。" >&2
    return 1
  fi
  public="$(printf '%s\n' "${policy}" \
    | grep -E '^roles/run\.[a-zA-Z]+[[:space:]]+(allUsers|allAuthenticatedUsers)$' || true)"
  if [ -n "${public}" ]; then
    echo "ERROR: プロジェクトレベルで Cloud Run の公開バインディングがある(全サービスが無認証で到達可能):" >&2
    printf '%s\n' "${public}" >&2
    echo "       他サービスへの影響があるため自動では消さない。手動で除去してから再実行すること。" >&2
    return 1
  fi
  echo "-- プロジェクトレベルの公開バインディングは無し"
}
