"""
idea_generator.py
=================
IR とアイデアの種を使って、複数のソフトウェア企画案を生成する。

* LLM へ渡すプロンプトには依頼仕様の必須 6 要素を必ず含める:
    1. IR の要約
    2. IR の主要項目（target_user, problem, contradiction, objective, value,
       background, explanations）
    3. 外部情報源の一覧（title, abstract, keywords, source_type）
    4. 発散観点
    5. 出力 JSON スキーマ
    6. 注意事項
* 出力は依頼書の Idea スキーマに合わせる
* JSON パースエラーが起きにくいよう、llm_client.chat_json（再試行つき）を使う
* API キーが無い／LLM 失敗時も縦切りが通るよう、ルールベース生成へフォールバック
  （生成された案がどの観点・どの IR 項目・どの情報源に基づくかは常に明示する）
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from config import AppConfig
from models import Idea, IdeaSeed, LinkedIRItem, ObjectiveCard, Source
from llm_client import chat_json, LLMError


# 発散観点（依頼仕様）。key は内部識別、label は日本語名。
PERSPECTIVES: List[Tuple[str, str]] = [
    ("user_value", "ユーザー価値型"),
    ("contradiction_resolution", "矛盾解消型"),
    ("tech_leverage", "技術活用型"),
    ("behavior_support", "行動支援型"),
    ("sharing_collaboration", "共有・協働型"),
    ("minimal_implementation", "最小実装型"),
    ("external_knowledge", "外部知識活用型"),
]

_PERSPECTIVE_GUIDE = "\n".join(
    f"- {label}: " + desc
    for (_, label), desc in zip(
        PERSPECTIVES,
        [
            "利用者の不満や目的を直接解決する案",
            "IR 内の矛盾・トレードオフを緩和する案",
            "AI・推薦・可視化・通知・スケジューリング等を活用する案",
            "利用者の行動変容や習慣化を支援する案",
            "家族・友人・同僚など第三者との共有・協働を含む案",
            "卒業研究の範囲でプロトタイプ化しやすい案",
            "文献・特許の知見を明確に取り込んだ案",
        ],
    )
)


# --------------------------------------------------------------------------- #
# プロンプト構築
# --------------------------------------------------------------------------- #
def _format_explanations(card: ObjectiveCard) -> str:
    """IR の根拠情報（explanations）をプロンプト用の箇条書き文字列へ整形する。

    根拠が 1 件も無い項目は行を作らず、全項目が空なら「（根拠情報なし）」を返す。
    企画案が IR の根拠から逸脱しないよう、根拠ごと LLM へ渡すために使う。
    """
    lines = []
    for key, items in card.all_explanations().items():
        if items:
            joined = "\n    ".join(f"- {x}" for x in items)
            lines.append(f"  {key}:\n    {joined}")
    return "\n".join(lines) if lines else "  （根拠情報なし）"


def _format_sources(sources: List[Source]) -> str:
    """外部情報源の一覧をプロンプト用のテキストブロックへ整形する。

    各行に source_id を必ず併記する。これにより LLM は生成した企画案に
    「どの文献を根拠にしたか」を source_ids として書き戻せる。
    """
    if not sources:
        return "（外部情報源なし）"
    lines = []
    for s in sources:
        lines.append(
            f"- source_id: {s.source_id} / source_type: {s.source_type}\n"
            f"  title: {s.title}\n"
            f"  abstract: {s.abstract}\n"
            f"  keywords: {s.keywords}"
        )
    return "\n".join(lines)


def _format_seeds(seeds: List[IdeaSeed]) -> str:
    """抽出済みのアイデアの種をプロンプト用の 1 行形式へ整形する。

    「- [出典id] (対応IR項目) 種の内容」という形にそろえ、種・出典・IR 項目の
    対応関係を保ったまま企画案へ展開できるようにする。
    """
    if not seeds:
        return "（アイデアの種なし）"
    return "\n".join(
        f"- [{s.source_id}] ({s.related_ir_item}) {s.idea_seed}" for s in seeds
    )


_OUTPUT_SCHEMA = """{
  "ideas": [
    {
      "concept_name": "企画案名",
      "concept_summary": "企画案の概要",
      "target_user": "対象ユーザー",
      "main_features": ["主要機能1", "主要機能2", "主要機能3"],
      "solved_problem": "この案が解決する課題",
      "handled_contradiction": "この案が扱う矛盾・トレードオフ",
      "provided_value": "提供価値",
      "source_ids": ["S001"],
      "idea_seed": "外部情報源から得たアイデアの種",
      "linked_ir_items": [
        {"ir_key": "problem", "ir_text": "IR内の該当内容", "reflection": "企画案のどこに反映したか"}
      ],
      "trace_reason": "この企画案がIRに基づいている理由",
      "novelty_reason": "外部情報源を使うことで得られた新規性・差分",
      "feasibility_note": "卒研でプロトタイプ化する場合の実装可能性",
      "risks": ["懸念点1", "懸念点2"],
      "perspective": "発散観点（上記のいずれか）"
    }
  ]
}"""


def build_prompt(card: ObjectiveCard, keywords: Dict[str, List[str]],
                 sources: List[Source], seeds: List[IdeaSeed],
                 num_ideas: int) -> Tuple[str, str]:
    """LLM 用の (system, user) プロンプトを構築する。

    user 側は依頼仕様の必須 6 要素（1.IR要約 / 2.IR主要項目 / 3.外部情報源 /
    4.発散観点 / 5.出力スキーマ / 6.注意事項）をこの順に連結して作る。
    ブロックごとに文字列を分けて組み立てるため、要素の差し替え・追加を
    局所的に行える。
    """
    system = (
        "あなたは情報ネットワーク工学・ソフトウェア工学・要求工学・生成AI応用に詳しい"
        "システム開発者です。利用者の問題意識を整理した中間表現(IR)を中心入力とし、"
        "外部情報源（文献・特許）を補助的な発散材料として用いて、複数のソフトウェア"
        "企画案を生成します。生成案は IR の課題・目的・矛盾・根拠から逸脱しないこと、"
        "各案がどの IR 項目とどの外部情報源に基づくかを必ず明示することが最重要です。"
        "出力は指定スキーマに厳密一致する JSON のみとします。"
    )

    # 1. IR 要約 / 2. IR 主要項目 + explanations
    ir_block = f"""## 1. IR の要約
対象ユーザー「{card.target_user}」が抱える課題「{card.problem}」に対し、
矛盾「{card.contradiction}」を踏まえつつ、目的「{card.objective}」を達成し、
価値「{card.value}」を実現することを狙う。

## 2. IR の主要項目
- target_user: {card.target_user}
- problem: {card.problem}
- contradiction: {card.contradiction}
- objective: {card.objective}
- value: {card.value}
- background: {card.background}
- extras: {", ".join(card.extras) if card.extras else "（なし）"}
- explanations（根拠）:
{_format_explanations(card)}"""

    # 3. 外部情報源 + アイデアの種 + キーワード
    src_block = f"""## 3. 外部情報源の一覧
{_format_sources(sources)}

## 3b. 抽出済みアイデアの種（参考）
{_format_seeds(seeds)}

## 3c. 検索・発散キーワード
- 日本語: {", ".join(keywords.get("ja", [])) or "（なし）"}
- 英語: {", ".join(keywords.get("en", [])) or "（なし）"}"""

    # 4. 発散観点
    persp_block = f"""## 4. 発散観点（観点を変えて多様に発散すること）
{_PERSPECTIVE_GUIDE}"""

    # 5. 出力スキーマ
    schema_block = f"""## 5. 出力 JSON スキーマ（この形式の JSON のみ出力）
{_OUTPUT_SCHEMA}"""

    # 6. 注意事項
    notes_block = f"""## 6. 注意事項（必ず守る）
- {num_ideas} 件の企画案を生成する。
- 単なる一般的なアプリ案にしない。必ず IR の課題・目的・矛盾・根拠に対応させる。
## 7. 企画案の具体性に関する追加制約
生成する企画案は，一般的なアプリ案にしないこと。

以下のような抽象的な名称・機能は禁止する。
- 家事効率化支援アプリ
- AIアシスタント
- タスク管理アプリ
- コミュニケーション支援アプリ
- 献立推薦アプリ
- タスク可視化機能
- 通知機能
- 進捗管理機能

これらを使う場合は，必ずIRの具体的条件に合わせて言い換えること。

例：
- タスク管理アプリ
  → 平日17〜20時の家事・育児タイムライン自動調整アプリ
- 通知機能
  → 18:30食事開始・19:45片付け完了から逆算する段取り通知
- 献立推薦アプリ
  → 手作り実感を保ちながら20分以内で作れる下味冷凍献立推薦
- 家族間コミュニケーション支援
  → 夫婦の帰宅時刻に応じた夕方タスク分担・引き継ぎボード

各企画案では，必ず以下を明示すること。
1. CiNii文献のどの知見を使ったか
2. その知見をどの機能に変換したか
3. IRのどの課題・矛盾・目的に対応するか
4. 既存の一般的なアプリと何が違うか
5. 卒研プロトタイプとして実装するなら，最小機能は何か

特に，IRに含まれる以下の具体条件を必ず反映すること。
- 平日17〜20時
- 子どもと30〜60分穏やかに関わる時間
- 栄養と納得感のある食事
- 必要最小限の片付け
- 手作り感と時短のトレードオフ
- 自炊負担による心身の余裕の低下

出力する企画案名は，抽象名ではなく，対象場面と解決方法が分かる具体名にすること。

- 各案で linked_ir_items に、基づいた IR 項目（problem/contradiction/objective/value 等）を
  最低 2 件示し、ir_text には IR の該当内容を、reflection には企画のどこに反映したかを書く。
- source_ids には参照した外部情報源の id を必ず入れる（複数可）。
- 文献・特許の内容はそのまま写さず、ソフトウェア企画案として再構成する。
- IR に根拠のない飛躍した前提を入れすぎない。
- 卒業研究でプロトタイプ化可能な粒度にする。
- main_features は 1 案につき 3〜5 個。
- 似た案ばかりにならないよう、できるだけ異なる発散観点を割り当てる。
- perspective には採用した発散観点名を入れる。"""

    # 6 つのブロックを空行区切りで連結し、1 つの user プロンプトにまとめる
    user = "\n\n".join([ir_block, src_block, persp_block, schema_block, notes_block])
    return system, user


# --------------------------------------------------------------------------- #
# LLM 生成
# --------------------------------------------------------------------------- #
def _coerce_idea(item: dict, idx: int, card: ObjectiveCard,
                 sources: List[Source]) -> Idea:
    """LLM が返した企画案 1 件分の dict を、Idea データ構造へ正規化する。

    LLM 出力は型・項目が揺れるため、ここで次を機械的にそろえる:
        * idea_id を通し番号で採番し直す（LLM の採番に依存しない）
        * source_ids を実在する文献に突き合わせ、タイトルと種別を補完する
        * linked_ir_items / main_features / risks の型と余分な空白を正規化する
        * target_user が空なら IR の対象ユーザーで補う
    """
    # 参照文献 id から Source を引くための索引
    by_id = {s.source_id: s for s in sources}
    source_ids = [str(x).strip() for x in item.get("source_ids", []) if str(x).strip()]
    # 実在する id だけタイトルを引く（LLM が存在しない id を書いた場合は無視される）
    source_titles = [by_id[sid].title for sid in source_ids if sid in by_id]
    # source_type は、参照文献のうち最初に種別が判明したものを代表として採用する
    src_type = ""
    for sid in source_ids:
        if sid in by_id and by_id[sid].source_type:
            src_type = by_id[sid].source_type
            break

    # 対応 IR 項目（dict 以外の要素が混ざっていた場合は捨てる）
    linked = []
    for li in item.get("linked_ir_items", []):
        if isinstance(li, dict):
            linked.append(
                LinkedIRItem(
                    ir_key=str(li.get("ir_key", "")).strip(),
                    ir_text=str(li.get("ir_text", "")).strip(),
                    reflection=str(li.get("reflection", "")).strip(),
                )
            )

    # 主要機能は空要素を除いたうえで、下の main_features=features[:5] で 5 件までに切る
    features = [str(f).strip() for f in item.get("main_features", []) if str(f).strip()]

    return Idea(
        idea_id=f"IDEA-{idx:03d}",
        concept_name=str(item.get("concept_name", "")).strip(),
        concept_summary=str(item.get("concept_summary", "")).strip(),
        target_user=str(item.get("target_user", "")).strip() or card.target_user,
        main_features=features[:5],
        solved_problem=str(item.get("solved_problem", "")).strip(),
        handled_contradiction=str(item.get("handled_contradiction", "")).strip(),
        provided_value=str(item.get("provided_value", "")).strip(),
        source_type=src_type,
        source_ids=source_ids,
        source_titles=source_titles,
        idea_seed=str(item.get("idea_seed", "")).strip(),
        linked_ir_items=linked,
        trace_reason=str(item.get("trace_reason", "")).strip(),
        novelty_reason=str(item.get("novelty_reason", "")).strip(),
        feasibility_note=str(item.get("feasibility_note", "")).strip(),
        risks=[str(r).strip() for r in item.get("risks", []) if str(r).strip()],
        perspective=str(item.get("perspective", "")).strip(),
    )


def _llm_generate(card: ObjectiveCard, keywords, sources: List[Source],
                  seeds: List[IdeaSeed], num_ideas: int,
                  client, config: AppConfig) -> List[Idea]:
    """LLM で企画案を生成する（プロンプト構築 → 問い合わせ → 正規化）。

    出力が {"ideas": [...]} でも、いきなり [...] でも受け取れるようにしておく。
    1 件も取れなかった場合だけ LLMError を送出し、呼び出し側の
    ルールベース生成へフォールバックさせる。
    """
    system, user = build_prompt(card, keywords, sources, seeds, num_ideas)
    data = chat_json(client, config, system, user)
    # スキーマ揺れの吸収: dict なら "ideas" キー、配列ならそれ自体を企画案リストとみなす
    raw = data.get("ideas", []) if isinstance(data, dict) else (
        data if isinstance(data, list) else []
    )
    # 1 件ずつ Idea へ正規化する（idx は 1 始まりの通し番号）
    ideas = [_coerce_idea(item, i + 1, card, sources)
             for i, item in enumerate(raw) if isinstance(item, dict)]
    if not ideas:
        raise LLMError("企画案が空でした。")
    return ideas


# --------------------------------------------------------------------------- #
# ルールベース生成（API キーなしでも縦切りを通すためのフォールバック）
# --------------------------------------------------------------------------- #
def _pick_seed_for(perspective_key: str, seeds: List[IdeaSeed],
                   used: set) -> IdeaSeed:
    """観点に対し未使用のアイデアの種を 1 つ選ぶ（無ければ使い回す）。

    used（既に使った source_id の集合）を見て先頭から未使用の種を探すことで、
    企画案ごとに異なる文献が参照されるようにし、根拠の偏りを避ける。
    種を使い切った場合は先頭の種を再利用し、種が 0 件なら空の IdeaSeed を返す。
    """
    # 先頭から未使用の種を探し、見つけたら used に登録して返す
    for s in seeds:
        if s.source_id not in used:
            used.add(s.source_id)
            return s
    # 全て使用済み（または種が 0 件）の場合のフォールバック
    return seeds[0] if seeds else IdeaSeed()


def _rule_based_generate(card: ObjectiveCard, sources: List[Source],
                         seeds: List[IdeaSeed], num_ideas: int) -> List[Idea]:
    """テンプレートで構造的に完全な企画案を作る（IR 対応・情報源対応を保持）。

    API キーが無くても縦切りを通すためのフォールバック。
    アルゴリズム:
        1. 発散観点（PERSPECTIVES）の定義順に、観点ごとのテンプレートを取り出す
        2. 観点ごとに未使用のアイデアの種を 1 つ割り当て、参照文献を紐づける
        3. 観点ごとに定めた linked_ir_items（IR 対応）を付けて Idea を組み立てる
    ※ テンプレート本文は本 IR（家事・育児ドメイン）に特化した固定文であり、
       他ドメインの IR では成立しない。本命は LLM 生成で、これは動作確認用。
    """
    # 観点ごとの企画テンプレート定義
    def linked(*pairs: Tuple[str, str]) -> List[LinkedIRItem]:
        """(IR項目キー, 反映先の説明) の並びから LinkedIRItem のリストを作る補助関数。

        IR 本文（ir_text）は card から自動で引くため、呼び出し側は
        「どの IR 項目を、企画のどこに反映したか」だけを書けばよい。
        """
        text_map = {
            "problem": card.problem,
            "contradiction": card.contradiction,
            "objective": card.objective,
            "value": card.value,
            "target_user": card.target_user,
        }
        return [LinkedIRItem(ir_key=k, ir_text=text_map.get(k, ""), reflection=r)
                for k, r in pairs]

    templates = {
        "user_value": dict(
            concept_name="夕方タイムライン・アシスタント",
            concept_summary="帰宅から就寝までの時間帯を、調理・配膳・片付け・子どもとの時間に"
                            "自動で割り付け、目的の『穏やかに関わる時間』を先に確保する支援アプリ。",
            main_features=[
                "帰宅時刻と就寝時刻から夕方の時間ブロックを自動生成",
                "『子どもと向き合う30〜60分』を先取り予約し残りに家事を配置",
                "当日の残タスクを 1 画面で可視化",
                "完了チェックで進捗と所要時間を記録",
            ],
            solved_problem="夕方に家事が集中し子どもと向き合う時間が確保しづらい課題を、"
                          "時間の先取り配分で緩和する。",
            handled_contradiction="家事を減らさずとも、関わる時間を先に確保することで"
                                  "『納得度』と『情緒的関わり』の両立を図る。",
            provided_value="罪悪感と焦りの軽減、可処分時間の回復。",
            persp_label="ユーザー価値型",
        ),
        "contradiction_resolution": dict(
            concept_name="手作り実感メーター付き献立プランナー",
            concept_summary="『手作り実感を残す範囲で外部の手を借りる』という IR の許容条件を"
                            "ルール化し、主菜のみ外部調達・主食と汁は自宅、などの配分を提案する。",
            main_features=[
                "手作り実感スコア（自炊比率）を可視化",
                "主食・主菜・副菜・汁物の役割ごとに『自炊/外部』を提案",
                "栄養バランス（主食主菜副菜＋汁物）チェック",
                "外部活用しても罪悪感が出にくい配分のプリセット",
            ],
            solved_problem="家事を減らすと納得度が下がるジレンマを、"
                          "実感を残す配分提案で緩和する。",
            handled_contradiction="手作り・栄養への納得感と、限られた時間での実行可能性の"
                                  "トレードオフを、部分的外部化で調停する。",
            provided_value="納得感を保ちつつ負担を下げ、心身の余裕を残す。",
            persp_label="矛盾解消型",
        ),
        "tech_leverage": dict(
            concept_name="下味冷凍ストック推薦エンジン",
            concept_summary="『下味冷凍＋オーブン調理の日は余裕が残った』という IR の成功体験を"
                            "起点に、在庫と所要時間から仕込みメニューを推薦する。",
            main_features=[
                "週末の仕込み候補をオーブン/ワンパン調理前提で推薦",
                "冷凍ストック在庫の管理と消費期限通知",
                "平日の所要時間（目標20分台）から逆算した献立提示",
                "調理時間と洗い物点数の予測表示",
            ],
            solved_problem="仕込みゼロ週に一からの調理で負荷が増す課題を、"
                          "推薦による仕込み習慣で予防する。",
            handled_contradiction="栄養と納得感を保ちながら調理時間を圧縮する両立を、"
                                  "推薦アルゴリズムで支援する。",
            provided_value="調理負荷の平準化と、余裕のある夕方の再現。",
            persp_label="技術活用型",
        ),
        "behavior_support": dict(
            concept_name="夕方ルーティン習慣化トラッカー",
            concept_summary="タイマー運用や声かけで上手くいった日を再現できるよう、"
                            "夕方の段取りを習慣として定着させる行動支援アプリ。",
            main_features=[
                "理想進行（18:30食べ始め/19:45シンク片付け目処）のテンプレ化",
                "タイマー連動の段取りガイドと声かけ通知",
                "うまくいった日のパターンを記録・再提案",
                "週次で『子どもと関われた時間』の推移を振り返り",
            ],
            solved_problem="日によって段取りが崩れ余裕が出ない課題を、"
                          "成功パターンの習慣化で安定させる。",
            handled_contradiction="毎日の判断負荷を増やさずに納得度の高い進行を再現する。",
            provided_value="焦りの減少と、安定した親子の時間の確保。",
            persp_label="行動支援型",
        ),
        "sharing_collaboration": dict(
            concept_name="夫婦タスク分担シェアボード",
            concept_summary="『夫婦連携がハマった日は余裕が生まれた』という IR を踏まえ、"
                            "夕方タスクの分担と進捗を二人で共有するボード。",
            main_features=[
                "夕方タスク（迎え/調理/配膳/片付け/風呂）の担当割り当て",
                "進捗のリアルタイム共有と残タスク通知",
                "声かけ・引き継ぎメモの共有",
                "週次でうまくいった分担の振り返り",
            ],
            solved_problem="夕方タスクが一方に集中し余白が生まれにくい課題を、"
                          "明確な分担共有で緩和する。",
            handled_contradiction="役割分担の調整コストを増やさずに連携の質を上げる。",
            provided_value="夫婦の連携による余裕創出と、食後の関わり時間の確保。",
            persp_label="共有・協働型",
        ),
        "minimal_implementation": dict(
            concept_name="ワンパン20分・段取りカード（最小版）",
            concept_summary="『一鍋に切り替えた日は20分で食卓』という成功体験だけに絞り、"
                            "カード形式で当日の段取りを提示する最小構成プロトタイプ。",
            main_features=[
                "20分で完成するワンパン献立カードの提示",
                "手順を3〜4ステップに圧縮して表示",
                "完了時に所要時間と気分を1タップ記録",
            ],
            solved_problem="忙しい日に調理と子ども対応が衝突する課題を、"
                          "最小手順の提示で緩和する。",
            handled_contradiction="品数削減でも『ちゃんと作れた』感を損ねにくい献立に限定する。",
            provided_value="即日使える負担軽減と、家族同時に温かい食事。",
            persp_label="最小実装型",
        ),
        "external_knowledge": dict(
            concept_name="文献知見ベースの段取り最適化ガイド",
            concept_summary="参照した文献・特許の知見（時間管理・段取り・行動変容など）を"
                            "明示的に取り込み、根拠付きで夕方の段取りを提案するガイド。",
            main_features=[
                "文献知見に基づく段取り最適化の提案（根拠リンク付き）",
                "提案ごとに参照元の文献/特許を表示",
                "利用者の実績ログと文献知見の突き合わせ",
                "効果が出た施策の継続提案",
            ],
            solved_problem="自己流の段取りでは余裕が安定しない課題を、"
                          "外部知見の裏付けで底上げする。",
            handled_contradiction="納得感と時間制約の両立を、根拠ベースの提案で支援する。",
            provided_value="根拠に支えられた安心感と、再現性のある時間確保。",
            persp_label="外部知識活用型",
        ),
    }

    # linked_ir_items を観点ごとに割り当て
    linked_map = {
        "user_value": linked(("objective", "夕方の関わり時間を先取り確保する設計に反映"),
                             ("problem", "家事集中による時間不足の緩和に対応")),
        "contradiction_resolution": linked(
            ("contradiction", "手作り実感を残す配分提案として反映"),
            ("value", "納得感を保ちつつ負担軽減する価値に対応")),
        "tech_leverage": linked(("objective", "下味冷凍×推薦で調理時間圧縮に反映"),
                                ("problem", "仕込みゼロ週の負荷増を予防")),
        "behavior_support": linked(("objective", "理想進行の習慣化として反映"),
                                   ("value", "焦り軽減と安定した時間確保に対応")),
        "sharing_collaboration": linked(
            ("value", "夫婦連携による余裕創出に対応"),
            ("objective", "分担共有で片付け目処を立てる進行に反映")),
        "minimal_implementation": linked(
            ("problem", "調理と子ども対応の衝突緩和に反映"),
            ("objective", "20分台で食卓に着く進行を最小機能で実現")),
        "external_knowledge": linked(
            ("contradiction", "根拠ベース提案で納得感と時間の両立に反映"),
            ("objective", "文献知見で段取り最適化を裏付け")),
    }

    # 観点の定義順に、要求件数ぶん（ただし観点数が上限）企画案を作る
    order = [k for k, _ in PERSPECTIVES]
    n = max(1, min(num_ideas, len(order)))
    used_sources: set = set()   # 企画案どうしで参照文献が重複しないよう記録する
    ideas: List[Idea] = []

    for i in range(n):
        pkey = order[i]
        t = templates[pkey]
        seed = _pick_seed_for(pkey, seeds, used_sources)
        src_ids = [seed.source_id] if seed.source_id else []
        by_id = {s.source_id: s for s in sources}
        src_titles = [by_id[sid].title for sid in src_ids if sid in by_id]
        src_type = by_id[src_ids[0]].source_type if src_ids and src_ids[0] in by_id else ""

        # 新規性の説明は、割り当てた種（＝参照文献）に応じて動的に組み立てる
        novelty = (
            f"参照した {seed.source_type or '外部情報源'}『{seed.source_title or '（無題）'}』の知見"
            f"（{seed.idea_seed}）を、一般的なアプリではなく本 IR の"
            f"『{card.problem[:24]}…』の解決に特化させた点が差分。"
        ) if seed.source_id else "外部情報源の知見を IR 固有の課題解決に特化させた点が差分。"

        ideas.append(
            Idea(
                idea_id=f"IDEA-{i + 1:03d}",
                concept_name=t["concept_name"],
                concept_summary=t["concept_summary"],
                target_user=card.target_user,
                main_features=t["main_features"],
                solved_problem=t["solved_problem"],
                handled_contradiction=t["handled_contradiction"],
                provided_value=t["provided_value"],
                source_type=src_type,
                source_ids=src_ids,
                source_titles=src_titles,
                idea_seed=seed.idea_seed,
                linked_ir_items=linked_map[pkey],
                trace_reason=(
                    f"対象ユーザー・課題・目的・矛盾という IR の主要項目に直接対応し、"
                    f"特に『{t['persp_label']}』の観点から IR を具体化したため。"
                ),
                novelty_reason=novelty,
                feasibility_note=(
                    "ローカルの献立・タスクデータと簡易ロジックで実装でき、"
                    "Streamlit などで卒研プロトタイプ化が可能な粒度。"
                ),
                risks=[
                    "テンプレ提案が利用者の実態に合わない場合の調整が必要",
                    "効果測定（関わり時間の増加）の評価設計が別途必要",
                ],
                perspective=t["persp_label"],
            )
        )
    return ideas


# --------------------------------------------------------------------------- #
# 公開 API
# --------------------------------------------------------------------------- #
def generate_ideas(card: ObjectiveCard, keywords: Dict[str, List[str]],
                   sources: List[Source], seeds: List[IdeaSeed],
                   num_ideas: int = 5, client=None,
                   config: AppConfig = None) -> Tuple[List[Idea], str]:
    """企画案を生成する。

    LLM が使えるときは LLM 生成を試し、失敗したらルールベース生成へ切り替える。
    どちらで生成したかを mode として返すため、出力 JSON から生成経路を追える。

    Returns:
        (ideas, mode)  mode は "llm" または "rule-based"
    """
    num_ideas = max(3, min(num_ideas, 5))  # 仕様: 3〜5 件
    if client is not None and config is not None and config.use_llm:
        try:
            ideas = _llm_generate(card, keywords, sources, seeds, num_ideas,
                                  client, config)
            return ideas, "llm"
        except LLMError as exc:
            print(f"[警告] LLM 生成に失敗したためルールベースに切替: {exc}")
    ideas = _rule_based_generate(card, sources, seeds, num_ideas)
    return ideas, "rule-based"
