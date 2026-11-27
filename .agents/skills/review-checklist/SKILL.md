---
name: review-checklist
description: PR のレビューで確認する観点の一覧。すべての PR で確認する観点（一般：設定値の連動、デッドコード、DBカラムの棚卸し）と、画面・CSS・JS に触れる PR で確認する観点（モダンWeb：ネイティブ dialog、popover と anchor positioning、View Transitions、prefers-reduced-motion、カスケードレイヤー、color-mix と Forced Colors、:user-invalid、:focus-visible）。PR のレビューを行うときに使う。
---

# レビュー観点

PR レビューの確認観点（旧 AGENTS.md から移設）。レビューを行うときは
このスキルを読むこと。

## 観点（一般）

すべての PR のレビューで確認する観点。画面・CSS・JS に触れない
PR でも、該当する項目は確認すること。

- **設定値の連動：** 設定値を追加・変更する PR では、その値を
  表示・参照する箇所がすべて設定に連動しているかを確認する。
  固定文言など、値の出所が二重になる残りを残さない。
- **デッドコード：** PR ごとの確認は差分のみを対象とし、その
  PR の差分によって、どこからも呼ばれていない関数・メソッド、
  到達しない分岐、使われなくなった変数・設定キー・テンプレート
  片などが生じていないかを確認する。コードベース全体の
  洗い出しは裏取りのコストが大きいため毎回のレビューでは
  行わず、リリース・デプロイ前の区切りや大きなリファクタリング
  の後にまとめて実施する（プロパティテストの heavy プロファイル
  を通すタイミングと揃える）。
- **DBカラムの棚卸し：** PR ごとの確認は行わず、デッドコードの
  全体洗い出しと同じタイミング（リリース・デプロイ前の区切りや
  大きなリファクタリングの後）でまとめて実施する。全テーブルの
  全カラムについて、コードでの読み書きと本番データの実態
  （NULL のみ・単一値のみ等）を突き合わせる。読み出しが一切ない
  カラムは削除候補としてユーザーに提示し、削除の判断は
  ユーザーが行う。

## 観点（モダンWeb）

画面・CSS・JS に触れる PR のレビューで確認する観点。
[GoogleChrome/modern-web-guidance](https://github.com/GoogleChrome/modern-web-guidance)
のガイドに基づく。ガイド本文は関係する PR のときだけ該当ガイドを
引く運用とし、ここには判定に使う要点だけを置く。DESIGN.md の
方針をレビューで判定可能にするためのもの。該当しない項目は
読み飛ばすこと。

- **モーダルはネイティブ `<dialog>`：** `showModal()` を使う。
  inert 化・フォーカス閉じ込め・Esc・閉じた後のフォーカス復帰は
  ブラウザが提供するので、`role="dialog"`／`aria-modal`／
  自作フォーカストラップ／Esc ハンドラを足さない。背面クリックで
  閉じる場合は `closedby="any"`、名前付けは `aria-labelledby`。
  背景スクロールの抑止は `overscroll-behavior` で行い、`body` の
  `overflow: hidden` は使わない。
- **トップレイヤーのアニメーション：** dialog／popover の
  表示・非表示を動かす場合は `@starting-style`、
  `transition-behavior: allow-discrete`、`transition` への
  `overlay` の3点セットを使う。`::backdrop` も同様に遷移させる。
  空間的な動きは `prefers-reduced-motion: no-preference` の
  内側にだけ置く。
- **ツールチップは popover と anchor positioning：** `<dialog>`
  ではなく `popover` を使い、位置決めは CSS Anchor Positioning
  でトリガーに吸着させ、画面端では反転させる。WCAG 1.4.13
  （Esc で消せる・上にポインタを乗せられる・勝手に消えない）を
  満たすこと。対応が揃っていない機能は `@supports` 等で
  段階的強化にする。
- **View Transitions：** ページ遷移の動きは cross-document の
  `@view-transition { navigation: auto; }` を
  `prefers-reduced-motion: no-preference` で囲って入れる。
  動かすプロパティは `opacity`／`transform` などコンポジタで
  処理できるものに限る。
- **`prefers-reduced-motion` の扱い：** 動きを一律に潰さない。
  無くす動きは「既定は静止、`no-preference` で有効化」、緩める
  動きは `reduce` 側でパラメータだけ変える。色の遷移など
  「動きでない遷移」は止めない。
- **カスケードレイヤーの順序：** `@layer` の順序を CSS の先頭で
  宣言し、詳細度の競合を予測可能にする。既定値には `:where()`
  を使い、`*` へのグローバルリセットはしない。
- **色の導出と Forced Colors：** 濃淡を動的に作る場合は
  `color-mix(in oklab, …)` を使い、無彩色との混合に `in oklch`
  は使わない。定義済みトークンで済むなら動的生成より優先する。
  Forced Colors Mode では影やグラデーションが消えるので、
  境界や状態を影だけで伝えない。
- **フォーム検証は `:user-invalid`：** エラー表示は `:invalid`
  ではなく `:user-invalid` で、入力中・表示直後にエラーを
  出さない。エラーは色だけでなく文とアイコンで示し、直し方を
  書く。`aria-invalid` は視覚状態と同期させ、エラーメッセージは
  不正な間だけ `aria-describedby` に含める。検証は `required`／
  `pattern` 等の標準属性に寄せる。
- **フォーカス表示は `:focus-visible`：** キーボード操作時の
  フォーカスが視覚的に分かるようにし、フォーカスリングを
  消さない（`outline: none` を単独で使わない）。
