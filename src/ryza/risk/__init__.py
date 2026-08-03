"""risk — リスクエンジン(T-015。保護領域 — 定款第5条)。

IPS §3.2 のハードリミットを測定して執行フラグ(``risk.limits_state``)に変換する
決定論エンジン。LLM 不関与。ゲート(T-014 G-10)が唯一の参照者。

- ``engine``: 純計算(DD・EWMA 実現ボラ・日次 ES95・ガードレール消費率)
- ``state``: ``risk.limits_state`` の更新(dd_hard ラッチ)と委員会解除・イベント台帳
- ``classify``: 銘柄マスタ由来の決定論分類(ゲート G-1/G-2 入力の配線)
- ``daily``: 日次サイクル(系列読出 → engine → limits_state 更新 → ops レポート)
"""
