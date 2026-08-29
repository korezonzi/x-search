# x-search

X（Twitter）から Claude Code の運用知見を収集して、採用する価値のあるものだけを Markdown レポートに落とすパイプライン。SocialData API を使う Python 製。

自分の `~/.claude/` 設定を「読んだ記事の勢い」ではなく、スコアと過去の決定との整合性で更新するために作った。

## 何が問題だったか

Claude Code の設定は、X で流れてくる Tips を見つけるたびに継ぎ足すと壊れる。理由は3つある。

1. 見つけたときのテンションで採用してしまい、半年後に矛盾する
2. すでに決めたこと（conventional commits を使う、など）を上書きする提案が紛れ込む
3. どこに書けばいいのか（`CLAUDE.md` か `rules/` か `skills/` か）が毎回ぶれる

だから収集と判断を分離して、判断のほうを設定ファイルに固定した。

## 3フェーズ

```
search.py    収集    queries.yaml のクエリで SocialData API を叩き、cache/ に生JSONを溜める
evaluate.py  評価    スコアリングと分類。どのファイルに向けた提案かをここで決める
apply.py     出力    採用・保留・棄却に仕分けた Markdown レポートを output/ に書く
```

適用そのものは自動化していない。レポートを読んで人間が反映する。設定ファイルへの自動書き込みは、間違えたときの巻き戻しコストが収集の手間より高い。

### 評価の中身

スコアはエンゲージメント・鮮度・分類の3要素から出す。閾値は `config.py` に置いた。

| 定数 | 値 | 意味 |
|---|---|---|
| `SCORE_APPLY` | 0.70 | これ以上は採用候補 |
| `SCORE_HOLD` | 0.40 | 0.40〜0.70 は保留 |
| `FRESHNESS_MAX_DAYS` | 365 | 1年より古い投稿は落とす |
| `CLAUDE_MD_SCORE_PENALTY` | 0.7 | `CLAUDE.md` 宛の提案はスコアを割り引く |

最後の1つが効く。`CLAUDE.md` は常時ロードされるので、1行増やすたびに全セッションのトークンを食う。だから他のファイル宛より高い基準を通さないと採用しない。

`FROZEN_DECISIONS` に決着済みの論点を並べてある。ここに当たる提案は、スコアがいくつでも捨てる。

分類は `models.py` の `ContentType` で型として定義していて、`behavioral_rule` なら `CLAUDE.md`、`domain_hooks` なら `rules/common/hooks.md` というように書き込み先が決まる。タグからの単純マッピングはフォールバックとしてだけ残した。

## watch — 常時監視のほう

単発の調査ではなく、保存クエリとアカウントを毎日追いかける側。

```
xq.py                収集。X の検索演算子をそのまま書ける CLI
x-watch-filter.py    LLM による関連度フィルタ
x-watch-slack.py     Slack 通知と Obsidian vault への保存
x-watch-cron.sh      launchd から叩かれるエントリポイント
```

`x-watch-filter.py` は `claude -p --model haiku` にバッチで判定させるが、その前に決定的なフィルタを2層通す。釣りタイトルの除外と重複排除で、LLM に見せる件数を先に削る。判定を全部 LLM に投げると、精度は上がらないのにコストだけ増える。

キュレーション済みのアカウントセクションはフィルタの対象外にした。自分で選んだアカウントの投稿を機械に落とさせる理由がない。

## コスト

SocialData は従量課金で、1件あたり $0.0002。サブスクリプションはない。

暴走を防ぐために `MAX_CALLS_PER_RUN = 200` を入れてある。1回の実行は最大でも $0.04。`apply.py` が生成するレポートの末尾に、その回の実行コストを円換算で出す。

## セットアップ

```bash
pip install -r requirements.txt
cp .env.example .env     # SOCIALDATA_API_KEY を書く
```

```bash
python3 search.py        # 収集
python3 evaluate.py      # 評価
python3 apply.py         # レポート生成 → output/
```

`xq.py` の使い方は [README-xq.md](README-xq.md) にまとめてある。

定期実行は `launchd/com.fuma.x-watch.plist.template` をコピーしてパスを書き換え、`~/Library/LaunchAgents/` に置く。

## 構成

```
search.py evaluate.py apply.py    3フェーズ本体
config.py models.py               閾値・型定義
queries.yaml                      収集クエリ
xq.py                             汎用 X 検索 CLI
scripts/                          watch パイプライン
launchd/                          定期実行テンプレート
```

`cache/` `output/` `logs/` は実行時に作られる。gitignore 済み。

## 注意

`SOCIALDATA_API_KEY` は `.env` に置く。`.env` は追跡していない。
