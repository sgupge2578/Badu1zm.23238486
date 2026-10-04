# 発散モジュール（Divergence Module）

「生成AIを用いたソフトウェア企画支援ツール」の **3 番目の工程＝発散** を担うモジュールです。
先行研究の `1. 問題意識抽出` / `2. 根拠付与` で作られた **IR（中間表現）** を中心入力とし、
CiNii・Scopus・特許DB 等から得た外部情報源（文献・特許）を**補助的な発散材料**として用いて、
複数のソフトウェア企画案を生成します。

> 設計上の最重要方針: **生成案は IR の課題・目的・矛盾・根拠から逸脱しない。**
> 外部情報源は「IR から離れた一般アイデア」ではなく「IR に基づく企画案の多様化・具体化」に使います。
> そのため各企画案には **対応 IR 項目 / IR に基づく理由 / 参照外部情報源 / アイデアの種 / 新規性 / 実装可能性** を必ず付与します。
> 出力は後続の **評価・収束モジュール** に接続できる JSON 形式です。

---

## パイプライン（縦切り 1 本）

```
1. IR を読み込む                         (ir_loader)
2. stage=evidence の IR を選び objective_card を抽出 (ir_loader)
3. 外部情報源 CSV を読み込む               (source_loader)
4. IR から検索・発散キーワードを生成         (keyword_generator)
5. 外部情報源からアイデアの種を抽出          (idea_seed_extractor)
6. IR とアイデアの種から企画案を 3〜5 件生成   (idea_generator)
7. 各案に対応 IR 項目・参照情報源を付与       (idea_generator)
8. JSON と Markdown で保存                 (output_writer)
```

---

## セットアップ

```bash
# 1. 依存インストール
pip install -r requirements.txt

# 2. （任意）LLM を使う場合は .env を用意
cp .env.example .env
#   .env の OPENAI_API_KEY を設定。互換 API を使うなら OPENAI_BASE_URL も設定。
```

> **API キーが無くても動きます。** その場合は LLM を使わず、IR と外部情報源を
> 構造的に組み合わせる**ルールベース生成**にフォールバックして縦切りを通します。
> まず動作確認 → その後 `.env` を設定して LLM 生成へ、という順で進められます。

---

## 実行方法

```bash
# CiNii のみを使って 5 件生成（手動CSVモード）
python src/main.py --ir data/IR_1215.jsonl --sources data/sources.csv --source-type CiNii --num-ideas 5

# source_type を指定せず全情報源を使う
python src/main.py --num-ideas 4

# API キーがあっても LLM を使わずルールベースで動かす
python src/main.py --no-llm

# CiNii Research を自動検索して発散（.env に CINII_APP_ID が必要）
python src/main.py --fetch cinii --cinii-queries 4 --cinii-count 10 --max-sources 12 --num-ideas 5
```

### 主な引数

| 引数 | 既定値 | 説明 |
|------|--------|------|
| `--ir` | `data/IR_1215.jsonl` | IR ファイル（JSONL / 整形 JSON 両対応） |
| `--fetch` | `csv` | 情報源の取得方法。`csv`=手動CSV / `cinii`=CiNii Research自動検索 |
| `--sources` | `data/sources.csv` | 外部情報源 CSV（`--fetch csv` 時） |
| `--source-type` | なし（全件） | CSVモードでの絞り込み（`CiNii` / `Scopus` / `Patent`） |
| `--cinii-queries` | `4` | 検索に使うキーワード数（1語ずつ検索しマージ） |
| `--cinii-count` | `10` | 1キーワードあたりの取得件数（1〜200） |
| `--max-sources` | `12` | マージ後に採用する情報源の最大件数 |
| `--from-year` / `--until-year` | なし | 出版年の範囲（YYYY / YYYYMM） |
| `--no-cache` | off | CiNii取得のキャッシュを使わない |
| `--save-fetched` | `data/sources_fetched.csv` | CiNii取得結果の保存先CSV |
| `--num-ideas` | `5` | 生成数（3〜5 にクランプ） |
| `--out-json` | `outputs/generated_ideas.json` | JSON 出力先 |
| `--out-md` | `outputs/generated_ideas.md` | Markdown 出力先 |
| `--no-llm` | off | LLM を使わずルールベース生成 |

各モジュールは単体実行でも動作確認できます:

```bash
python src/ir_loader.py data/IR_1215.jsonl     # IR の読み込み結果を表示
python src/source_loader.py data/sources.csv CiNii  # 外部情報源の読み込み結果を表示
```

---

## ディレクトリ構成

```text
divergence_module/
  README.md
  requirements.txt
  .env.example
  data/
    IR_1215.jsonl        # 先行研究から提供された実 IR（サンプル入力）
    sources.csv          # 外部情報源サンプル（CiNii/Scopus/Patent）
  outputs/               # 生成結果（JSON / Markdown）
  src/
    main.py              # CLI エントリポイント（パイプライン統合）
    ir_loader.py         # IR 読み込み・選定・objective_card 抽出
    source_loader.py     # 外部情報源 CSV 読み込み
    source_fetcher.py    # CiNii Research API 自動検索 → Source 正規化（--fetch cinii）
    keyword_generator.py # 日英キーワード生成
    idea_seed_extractor.py # アイデアの種抽出
    idea_generator.py    # 企画案生成（プロンプト構築 + 生成）
    output_writer.py     # JSON / Markdown 出力
    models.py            # データ構造（dataclasses）
    config.py            # .env 読み込み・設定
    llm_client.py        # LLM 呼び出し + 堅牢な JSON 抽出（補助）
```

---

## 入力ファイル仕様

### IR ファイル（実ファイルに合わせた頑健な読み込み）

提供された `IR_1215.jsonl` は、実際には **複数行にわたる整形済み JSON 1 件**でした
（厳密な「1 行 1 JSON」ではない）。また説明配列のキーは `*_root_explanation`
（例: `problem_root_explanation`）でした。本モジュールは以下を吸収します:

- 「1 行 1 JSON」と「整形 JSON / 複数オブジェクト連結」の**両方**を読む
- `*_explanation` と `*_root_explanation` の**両命名**を吸収
- `stage=evidence` を優先採用。無ければ**情報量が最大**の IR を採用
- `objective_card` が無ければ**近い構造を再帰探索**
- 欠損キーは**空文字 / 空配列**で補完
- 解析に失敗したら**何行目で失敗したか**を表示

### 外部情報源 CSV（`data/sources.csv` サンプル）

```csv
source_id,source_type,title,abstract,keywords,url
S001,CiNii,共働き世帯における夕食準備の時間負担と家事分担に関する調査,共働き家庭の…,共働き;家事分担;時間使用;夕食準備;育児,https://example.org/cinii/S001
S005,Scopus,A Recommender System for Time-Constrained Meal Planning in Households,Proposes a recommendation approach…,meal planning;recommender system;…,https://example.org/scopus/S005
S007,Patent,家事タスクの分担と進捗を共有する情報処理装置,家庭内の家事タスクを…,家事分担;進捗共有;通知;…,https://example.org/patent/S007
```

- `source_type` は `CiNii` / `Scopus` / `Patent`。最初は CiNii だけでも動作します。
- abstract に `,` を含む場合があるため、ファイルは **CSV のクォート規則**に従って作成してください
  （本サンプルは正しくクォート済み）。

---

## CiNii 自動検索モード（`--fetch cinii`）

IR から生成したキーワードで **CiNii Research** を検索し、結果を `Source` 形式へ
正規化してそのまま発散に使います（`source_fetcher.py`）。`source_loader` 以降は
CSV でも API でも同一に動くため、発散・出力のコードは無変更です。

> **重要（サービスの実態）**: 旧「CiNii Articles」は **2022/04/18 に CiNii Research へ統合**
> 済みで、単独サービスとしては存在しません。本モジュールは現行の CiNii Research の
> OpenSearch API（論文検索エンドポイント `https://cir.nii.ac.jp/opensearch/articles`）を
> 使用します。

### 事前準備（必須）

CiNii Research の OpenSearch は **アプリケーションID（`appid`）が必須**です。
NII の API 利用登録で取得し、`.env` に設定してください。

```ini
CINII_APP_ID=（取得した appid）
```

- 登録: https://support.nii.ac.jp/ja/cinii/api/developer
- 仕様: https://support.nii.ac.jp/ja/cir/r_opensearch

### 動作仕様

- `format=json`（JSON-LD）で取得し、各 item を次のように `Source` へ対応付けます。
  `title`→title、`description`→abstract（無い論文も多い）、`dc:subject`→keywords、
  `link.@id`→url、crid→source_id（`CIR<crid>`）。
- CiNii の `q` は複数語が AND 結合のため、**上位キーワードを 1 語ずつ検索してマージ**し、
  重複（crid）を除いて `--max-sources` 件まで採用します（再現率重視）。
- 取得した生レスポンスは `data/cache/` にキャッシュし、採用結果は `--save-fetched`
  （既定 `data/sources_fetched.csv`）に保存します。中身を確認・再現できます。
- リクエスト間に待機を入れ、`User-Agent` を付与しています（礼儀・レート対策）。
- 抄録（`description`）には CiNii の利用条件・抄録ライセンス（`abstractLicenseFlag`）が
  関わります。論文として引用・再配布する際は各データソースの条件を確認してください。

> 注: 卒研環境などインターネットに出られる環境で実行してください。`appid` が無効・未設定の
> 場合や該当論文が無い場合は、分かりやすいエラーで停止します。

---

## 出力 JSON（抜粋例）

```json
{
  "session_id": "session-001",
  "generated_at": "2026-06-07 16:12:16",
  "generator_mode": "rule-based",
  "used_source_type": "CiNii",
  "input_ir_summary": {
    "target_user": "東京在住の35歳、フルタイムで働く…母親。",
    "problem": "平日夕方以降は家事に追われ…",
    "contradiction": "手作り・栄養への納得感を守りたい一方…",
    "objective": "平日17〜20時に、子どもと30〜60分は…",
    "value": "親子の情緒が安定し、罪悪感や疲労感が軽減…"
  },
  "ideas": [
    {
      "idea_id": "IDEA-002",
      "concept_name": "手作り実感メーター付き献立プランナー",
      "concept_summary": "『手作り実感を残す範囲で外部の手を借りる』…",
      "target_user": "…母親。",
      "main_features": ["手作り実感スコアの可視化", "役割ごとの自炊/外部の提案", "栄養バランスチェック", "罪悪感の出にくい配分プリセット"],
      "solved_problem": "家事を減らすと納得度が下がるジレンマを緩和する。",
      "handled_contradiction": "納得感と実行可能性のトレードオフを部分的外部化で調停。",
      "provided_value": "納得感を保ちつつ負担を下げる。",
      "source_type": "CiNii",
      "source_ids": ["S002"],
      "source_titles": ["作り置き・下味冷凍を活用した平日調理の負荷軽減効果"],
      "idea_seed": "…の知見を IR の課題解決に転用する。",
      "linked_ir_items": [
        {"ir_key": "contradiction", "ir_text": "手作り・栄養への納得感を…", "reflection": "手作り実感を残す配分提案として反映"},
        {"ir_key": "value", "ir_text": "親子の情緒が安定し…", "reflection": "納得感を保ちつつ負担軽減する価値に対応"}
      ],
      "trace_reason": "対象ユーザー・課題・目的・矛盾という IR の主要項目に直接対応し…",
      "novelty_reason": "参照した CiNii『…』の知見を、本 IR の課題解決に特化させた点が差分。",
      "feasibility_note": "ローカルの献立・タスクデータと簡易ロジックで実装でき、Streamlit で試作可能。",
      "risks": ["テンプレ提案が実態に合わない場合の調整が必要", "効果測定の評価設計が別途必要"],
      "perspective": "矛盾解消型"
    }
  ]
}
```

同じ内容を人間向けに整形した `outputs/generated_ideas.md` も同時に出力されます。

---

## 発散観点

企画案は以下の観点を切り替えて多様に発散します
（ルールベース時は観点ごとにテンプレを割り当て、LLM 時はプロンプトで観点を提示）。

- ユーザー価値型 / 矛盾解消型 / 技術活用型 / 行動支援型 / 共有・協働型 / 最小実装型 / 外部知識活用型

---

## LLM に渡すプロンプトに含まれる要素

`idea_generator.build_prompt()` が以下の必須 6 要素を必ず含めます。

1. IR の要約
2. IR の主要項目（target_user / problem / contradiction / objective / value / background / explanations）
3. 外部情報源の一覧（title / abstract / keywords / source_type）＋抽出済みアイデアの種＋キーワード
4. 発散観点
5. 出力 JSON スキーマ
6. 注意事項（IR 対応の明示・情報源 id の明示・写経の禁止・3〜5 機能・粒度・多様性 等）

JSON は `llm_client.chat_json()` がコードフェンス除去・最外括弧抽出・**再試行**で頑健にパースします。

---

## 今後の拡張方針（Scopus / 特許DB の API 連携）

CiNii は実装済み（`--fetch cinii`）です。Scopus・特許DB も**同じ取得層パターン**で追加できます。
鍵は「各 API の結果を共通の `Source`（source_id / source_type / title / abstract / keywords / url）へ
正規化する」ことで、これにより発散・出力のコードは無変更のまま情報源だけ差し替えられます。

1. **取得層を追加**（`source_fetcher.py` に `fetch_scopus_sources` / `fetch_patent_sources` を追加、
   または `scopus_fetcher.py` 等に分割）。
   - Scopus Search API（要 API キー・機関契約）、特許（J-PlatPat / 各国特許API / 商用DB）を叩く。
2. **共通スキーマへ正規化** … 取得結果を `Source` に変換（`normalize_item` 相当を各APIに用意）。
3. **取得モードの切替** … `--fetch` の選択肢に `scopus` / `patent` を追加し、`main.py` で分岐。
4. **設定** … `.env` に `SCOPUS_API_KEY` などを追加。
5. **件数・品質制御** … 取得件数上限、言語フィルタ、重複除去（DOI / タイトル正規化）を取得層に実装。

AWS 上で自動化する場合は、取得層を Lambda 化して結果を S3 に置き、本モジュールが S3 から
読む構成にすれば、検索→発散をクラウド上で自然につなげられます。
