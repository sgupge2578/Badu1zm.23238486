# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 研究コンテキスト

研究の背景・方針・作業計画は @docs/research_context.md を参照すること。
詳細な経緯が必要な場合のみ docs/reference/研究成果まとめ_0830.md を読むこと。

## 概要

コードはすべて `divergence_module/` にある。これは「生成AIを用いたソフトウェア企画支援ツール」の **3 番目の工程（発散）** を担うモジュールである。入力は前段の工程（1. 問題意識抽出、2. 根拠付与）が作る **IR（中間表現）** で、外部情報源（CiNii、今後は Scopus・特許DB）は**補助的な材料**として使う。出力はソフトウェア企画案 3〜5 件の JSON と Markdown で、後続の評価・収束モジュールが読み込む。

最重要の設計方針: **生成案は IR の課題・目的・矛盾・根拠から逸脱しない。** 各 `Idea` には `linked_ir_items`、`trace_reason`、`source_ids` / `source_titles`、`idea_seed`、`novelty_reason`、`feasibility_note` を必ず持たせる。生成ロジックを変更する際も、これらの項目が埋まるようにすること。

コードのコメント・docstring・CLI メッセージ・README は日本語で書かれている。この慣習に従うこと。

## コマンド

すべて `divergence_module/` から実行する。`data/IR_1215.jsonl` や `outputs/` などの既定パスがここからの相対パスになっているため。ローカルの venv は `divergence_module/.venv` にある。

```bash
cd divergence_module
pip install -r requirements.txt

# 手動 CSV の情報源を使う（既定）
python src/main.py --ir data/IR_1215.jsonl --sources data/sources.csv --source-type CiNii --num-ideas 5

# CiNii Research 自動検索（.env に CINII_APP_ID が必要。ネットワーク接続も必要）
python src/main.py --fetch cinii --cinii-queries 4 --cinii-count 10 --max-sources 12 --num-ideas 5

# モジュール単体の動作確認（__main__ ブロックあり）
python src/ir_loader.py data/IR_1215.jsonl
python src/source_loader.py data/sources.csv CiNii
```

テストスイート、リンター設定、ビルド手順はない。

## 設定

`src/config.py` が `.env` を読み込み、`AppConfig` を組み立てる。キーは `OPENAI_API_KEY`、`OPENAI_BASE_URL`（任意の OpenAI 互換エンドポイント）、`OPENAI_MODEL`（既定 `gpt-4o-mini`）、`LLM_TEMPERATURE`（既定 0.7）、`LLM_MAX_JSON_RETRIES`（JSON 生成失敗時の再試行回数、既定 2）、`CINII_APP_ID`。空文字は未設定として扱われ、既定値が使われる。**`.env` は読まないこと**（`.claude/settings.json` で拒否設定済み。このルールは `.env.example` にも当たる）。

## アーキテクチャ

`src/main.py` が直列のパイプラインを実行する。`main.py` が `src/` を `sys.path` に追加しているため、モジュール同士はトップレベル名で import している（`from models import ...`）。スクリプトは `-m` ではなく `python src/<file>.py` で実行すること。

1. `ir_loader`: IR を解析して 1 件を選び（`stage=evidence` を優先）、その `ObjectiveCard` を返す。パーサーは意図的に寛容にしてある。実際の IR ファイルは厳密な JSONL ではなく、整形済み JSON や JSON の連結になっている。`*_explanation` と `*_root_explanation` の両方のキー名を受け付け、`objective_card` に近い dict を再帰的に探索し、欠損キーは補完する。
2. `problem_decomposer`: IR を複数の分割パターン（部分問題の集合）に分ける LLM エージェント。結果は `outputs/problem_decomposition.{json,md}` に書き出す。**設計上、ルールベースの代替を持たない**ため、LLM が無いとこの工程でエラー終了する。そのため現状では、README に「API キーが無くても動く」とあるにもかかわらず、`--no-llm` や API キー未設定では実行全体が失敗する。分割結果は保存されるが、キーワード生成・種抽出・企画案生成には**まだ渡されていない**。部分問題ごとの検索・種抽出は docstring に今後の予定として書かれている。
3. `keyword_generator`: `{"ja": [...], "en": [...]}` 形式のキーワードを生成する。CiNii の検索クエリとプロンプトの両方に使われる。
4. 情報源の取得。どちらの経路も共通の `Source` dataclass（`source_id, source_type, title, abstract, keywords, url`）を返すため、以降の処理は情報源の種類に依存しない。
   - `source_loader` は CSV を読み込む（`--fetch csv`）。
   - `source_fetcher` は CiNii Research の OpenSearch API を呼ぶ（`--fetch cinii`）。CiNii は複数語のクエリを AND 結合するため、キーワードを 1 語ずつ検索し、結果をマージして CRID で重複を除く。生レスポンスは `data/cache/cinii_<hash>.json` にキャッシュされ、マージ結果は `--save-fetched` に書き出される。Scopus や特許DB を追加する場合も同じパターンに従う。`Source` へ正規化する取得関数を書き、`--fetch` に選択肢を追加する。
5. `idea_seed_extractor`: 各情報源の知見を、IR の解決にどう転用できるかを示す `IdeaSeed` に変換する。
6. `idea_generator`: `build_prompt()` は必須の 6 要素を必ず含めること。IR の要約、IR の各項目、情報源・種・キーワード、発散観点（ユーザー価値型 / 矛盾解消型 / 技術活用型 / …）、出力 JSON スキーマ、注意事項の 6 つ。`_coerce_idea` が LLM の出力を `Idea` に正規化する。`num_ideas` は 3〜5 にクランプされる。
7. `output_writer`: `GeneratedIdeas`（`models.py`）を `outputs/` 配下に JSON と Markdown で書き出す。`outputs/` は gitignore 対象。

**LLM とルールベースの切り替え:** `AppConfig.use_llm`（API キーが設定されているときだけ true）が唯一の分岐点。各エージェントには `client` が渡され、LLM を使わないときは `None` になる。`problem_decomposer` 以外の工程は、その場合ルールベース生成にフォールバックする。どちらの経路で実行されたかは、出力の `generator_mode` に記録される。JSON を必要とする LLM 呼び出しはすべて `llm_client.chat_json()` を経由する。この関数はコードフェンスを除去し、最も外側の括弧を抽出し、失敗時は再試行する（`max_json_retries`）。

## データ

- `data/IR_1215.jsonl`: 前段の工程から提供された実際のサンプル IR。
- `data/sources.csv`: 手作りのサンプル情報源（CiNii / Scopus / Patent）。URL は example.org。abstract にカンマを含むため、CSV のクォートを崩さないこと。
- `data/sources_fetched_cinii*.csv` と `data/cache/`: 実際の CiNii 取得結果をコミットしたスナップショット。`--fetch csv --sources ...` で指定すれば、オフラインでも同じ条件で再実行できる。

## 開発環境と運用

- 実行環境は **Python 3.8**（AWS Cloud9 と揃えている）。3.9 以降の構文は使わない。
  - 使わない: `match` 文、`list[str]` / `dict[str, int]` などの組み込み型ジェネリクス、`X | Y` の型ヒント、`str.removeprefix()`
  - 代わりに `typing` の `List`、`Dict`、`Optional`、`Union` を使う
- `requirements.txt` にパッケージを追加・更新する場合は、Python 3.8 に対応したバージョンを指定する
- 開発はローカル（Mac）で行い、`git push` する。本番実行は AWS Cloud9 で `git pull` してから行う。Cloud9 側では直接編集しない
- 絶対パス（`/Users/...` や `/home/ec2-user/...`）をコードに書かない
