"""
problem_decomposer.py
=====================
IR（ObjectiveCard）を複数の部分問題に分割する。

本モジュールは，発散支援モジュールにおける最初の追加工程である
「問題分割AIエージェント」を担当する。

従来のように IR 全体から直接キーワードや企画案を生成するのではなく，
まず IR に含まれる問題意識を複数の部分問題へ分解する。

これにより，後続工程である
    - 部分問題ごとの検索語生成
    - 部分問題ごとの文献・特許検索
    - 部分問題ごとのアイデアの種抽出
    - 複数の種を組み合わせた企画案生成
へ接続しやすくする。

本研究では，問題分割を AI エージェントの役割として扱う。
そのため，LLM が利用できない場合の rule-based fallback は行わず，
設定不足または実行失敗として明示的にエラーを返す。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from config import AppConfig
from llm_client import LLMError, chat_json
from models import (
    DecompositionPattern,
    ObjectiveCard,
    ProblemDecompositionResult,
    SubProblem,
)


class ProblemDecompositionError(Exception):
    """問題分割AIエージェントに関する例外。"""


def _format_explanations(card: ObjectiveCard) -> str:
    """ObjectiveCard内の根拠情報をプロンプト用の文字列に整形する。

    問題分割では，problem だけでなく，contradiction，objective，value，
    さらに各 explanation を参照することで，IRの文脈からずれにくい分割を行う。
    """

    explanations = card.all_explanations()
    lines: List[str] = []

    # 「項目名: 根拠1, 根拠2, ...」という入れ子の箇条書きに整形する。
    # 根拠が空の項目は行を作らない（プロンプトに無駄な空項目を渡さないため）。
    for key, items in explanations.items():
        if not items:
            continue

        lines.append(f"- {key}:")
        for item in items:
            lines.append(f"  - {item}")

    if not lines:
        return "（根拠情報なし）"

    return "\n".join(lines)


def _format_ir(card: ObjectiveCard) -> str:
    """問題分割AIエージェントに渡すIR情報を文字列化する。

    IRの主要項目をまとめて渡すことで，AIエージェントが
    どの課題・矛盾・目的・価値をもとに分割すべきかを明確にする。
    """

    # 主要項目は「キー: 値」形式で列挙し，AIがIR項目名（problem など）を
    # linked_ir_items にそのまま使えるようにする。
    lines: List[str] = [
        f"- target_user: {card.target_user}",
        f"- problem: {card.problem}",
        f"- contradiction: {card.contradiction}",
        f"- objective: {card.objective}",
        f"- value: {card.value}",
        f"- background: {card.background}",
    ]

    # extras は存在するときだけ追加する
    if card.extras:
        lines.append(f"- extras: {', '.join(card.extras)}")

    # 根拠（explanations）は別セクションとして続ける
    lines.append("")
    lines.append("## explanations")
    lines.append(_format_explanations(card))

    return "\n".join(lines)


def _build_prompt(card: ObjectiveCard, num_patterns: int) -> Tuple[str, str]:
    """問題分割AIエージェント用の system / user プロンプトを作成する。

    このプロンプトでは，AIに企画案や解決策を出させず，
    あくまで「検索・調査すべき部分問題」へ分割するように制約する。
    """

    # system: エージェントの役割（＝解決策ではなく問題分割を担当する）を固定する
    system = (
        "あなたはソフトウェア企画支援における問題分割AIエージェントです。"
        "あなたの役割は，IRから直接アイデアや解決策を出すことではありません。"
        "IRに含まれる問題意識を複数の部分問題に分解し，"
        "後続の検索語生成・文献検索・アイデアの種抽出につながる形で整理することです。"
        "出力は指定されたJSON形式のみとし，説明文やMarkdownは出力しないでください。"
    )

    # user: 入力IR・制約条件・分割観点の例・出力スキーマを具体的に与える。
    # 「分割は1通りではない」ことを明示し，観点の異なる複数パターンを生成させる。
    user = f"""# 入力IR
{_format_ir(card)}

# 指示
IRに含まれる問題を，複数の観点から部分問題に分割してください。

# 重要条件
- 企画案やアプリ案を出してはいけません。
- 解決策ではなく，検索・調査すべき部分問題に分けてください。
- 分割パターンは {num_patterns} 件作ってください。
- 各パターンは異なる観点にしてください。
- 1つのパターンには，3〜6個程度の部分問題を含めてください。
- 問題分割は1通りではないことを前提にしてください。
- 各部分問題には，どのIR項目に対応するかを linked_ir_items に入れてください。
- search_intent には，その部分問題に対してどのような文献・特許を探したいかを書いてください。
- constraints には，IRから読み取れる制約条件を入れてください。
- 特定の解決策や機能名ではなく，調査・探索すべき問題として表現してください。

# 分割観点の例
以下は例です。IRの内容に応じて，必要なら別の観点を使っても構いません。

- 時間制約中心
- 関係者・役割中心
- 矛盾・トレードオフ中心
- 提供価値中心
- 利用場面中心
- 心理的負担中心
- 実現条件・制約中心

# 出力JSONスキーマ
{{
  "patterns": [
    {{
      "pattern_id": "DP-001",
      "pattern_name": "時間制約中心の分割",
      "viewpoint": "どの観点で分割したか",
      "reason": "なぜこの分割がIRに対して有効か",
      "subproblems": [
        {{
          "subproblem_id": "SP-001",
          "name": "部分問題名",
          "description": "部分問題の説明",
          "decomposition_axis": "time_constraint",
          "linked_ir_items": ["problem", "objective"],
          "search_intent": "この部分問題に対して探すべき文献・特許の方向性",
          "constraints": ["IRから読み取れる制約1", "IRから読み取れる制約2"]
        }}
      ]
    }}
  ]
}}

# 出力
上記スキーマに従うJSONオブジェクトのみを出力してください。
"""

    return system, user


def _as_str_list(value: Any) -> List[str]:
    """LLM出力の値を文字列リストに正規化する。

    linked_ir_items や constraints が文字列で返された場合でも，
    後続処理で扱いやすいように List[str] にそろえる。
    """

    if value is None:
        return []

    # 配列で返ってきた場合: 各要素を文字列化し，空要素は落とす
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]

    # 単一値で返ってきた場合: 要素1件のリストとして扱う
    text = str(value).strip()
    return [text] if text else []


def _parse_subproblem(
    raw_sp: Dict[str, Any],
    pattern_index: int,
    subproblem_index: int,
) -> Optional[SubProblem]:
    """LLM出力の1つの部分問題を SubProblem に変換する。

    name または description が空の場合は，有効な部分問題として扱わず None を返す。
    """

    # ID が返ってこない場合は，パターン番号と通し番号から機械的に採番する
    # （後段で企画案が「どの部分問題由来か」を必ず参照できるようにするため）。
    subproblem = SubProblem(
        subproblem_id=str(
            raw_sp.get("subproblem_id")
            or f"SP-{pattern_index:03d}-{subproblem_index:03d}"
        ).strip(),
        name=str(raw_sp.get("name", "")).strip(),
        description=str(raw_sp.get("description", "")).strip(),
        decomposition_axis=str(raw_sp.get("decomposition_axis", "")).strip(),
        linked_ir_items=_as_str_list(raw_sp.get("linked_ir_items")),
        search_intent=str(raw_sp.get("search_intent", "")).strip(),
        constraints=_as_str_list(raw_sp.get("constraints")),
    )

    # 名称も説明も無いものは中身が無いため，部分問題として採用しない
    if not subproblem.name and not subproblem.description:
        return None

    return subproblem


def _parse_pattern(
    raw_pattern: Dict[str, Any],
    pattern_index: int,
) -> Optional[DecompositionPattern]:
    """LLM出力の1つの分割パターンを DecompositionPattern に変換する。

    有効な部分問題を1つも含まない場合は None を返す。
    """

    # subproblems が配列でない（想定外の型）場合は空として扱い，落とさずに進む
    raw_subproblems = raw_pattern.get("subproblems", [])
    if not isinstance(raw_subproblems, list):
        raw_subproblems = []

    subproblems: List[SubProblem] = []

    # 部分問題を1件ずつ変換し，有効なものだけを残す
    for subproblem_index, raw_sp in enumerate(raw_subproblems, start=1):
        if not isinstance(raw_sp, dict):
            continue

        subproblem = _parse_subproblem(
            raw_sp,
            pattern_index=pattern_index,
            subproblem_index=subproblem_index,
        )

        if subproblem is not None:
            subproblems.append(subproblem)

    # 部分問題が1件も無いパターンは，分割として意味を持たないので捨てる
    if not subproblems:
        return None

    return DecompositionPattern(
        pattern_id=str(raw_pattern.get("pattern_id") or f"DP-{pattern_index:03d}").strip(),
        pattern_name=str(raw_pattern.get("pattern_name", "")).strip(),
        viewpoint=str(raw_pattern.get("viewpoint", "")).strip(),
        reason=str(raw_pattern.get("reason", "")).strip(),
        subproblems=subproblems,
    )


def _parse_decomposition(data: Any, generator_mode: str) -> ProblemDecompositionResult:
    """LLM出力を ProblemDecompositionResult に変換する。

    LLMから返されたJSONが期待スキーマから多少ずれていても，
    patterns 配下の有効な分割パターンと部分問題を可能な範囲で抽出する。
    """

    # 最上位が JSON オブジェクトでなければ，スキーマ違反として扱う
    if not isinstance(data, dict):
        raise ProblemDecompositionError(
            "問題分割AIエージェントの出力がJSONオブジェクトではありません。"
        )

    raw_patterns = data.get("patterns", [])
    if not isinstance(raw_patterns, list):
        raise ProblemDecompositionError(
            "問題分割AIエージェントの出力に patterns 配列がありません。"
        )

    patterns: List[DecompositionPattern] = []

    # 1パターンずつ変換する。壊れた要素は読み飛ばし，取れる分だけを活かす
    for pattern_index, raw_pattern in enumerate(raw_patterns, start=1):
        if not isinstance(raw_pattern, dict):
            continue

        pattern = _parse_pattern(raw_pattern, pattern_index=pattern_index)

        if pattern is not None:
            patterns.append(pattern)

    # 1件も残らなかった場合は，後続工程へ渡せないため明示的に失敗させる
    if not patterns:
        raise ProblemDecompositionError(
            "問題分割AIエージェントの出力から有効な分割パターンを取得できませんでした。"
        )

    return ProblemDecompositionResult(
        patterns=patterns,
        generator_mode=generator_mode,
    )


def decompose_problem(
    card: ObjectiveCard,
    client,
    config: AppConfig,
    num_patterns: int = 3,
) -> ProblemDecompositionResult:
    """IRを問題分割AIエージェントによって複数の部分問題に分解する。

    本研究では，問題分割をAIエージェントの役割として扱う。
    そのため，LLMが利用できない場合に rule-based fallback は行わず，
    設定不足として明示的にエラーを返す。

    処理の流れ:
        1. LLM が使える状態かを確認する（使えなければエラー）
        2. IR からプロンプトを組み立てる
        3. chat_json で問い合わせる（JSON パース失敗時は llm_client 側で再試行）
        4. 出力を ProblemDecompositionResult へ正規化する

    Args:
        card: 問題分割対象の ObjectiveCard
        client: OpenAI互換クライアント
        config: LLM設定
        num_patterns: 生成する問題分割パターン数

    Returns:
        ProblemDecompositionResult: 問題分割結果

    Raises:
        ProblemDecompositionError:
            LLM設定不足，LLM実行失敗，または出力スキーマ不正の場合
    """

    # 1. 問題分割はAIエージェントの役割であり，ルールベースでは代替しない。
    #    そのため，LLMが使えない場合はここで明示的に停止する。
    if client is None or config is None or not config.use_llm:
        raise ProblemDecompositionError(
            "問題分割AIエージェントを実行できません。"
            "OPENAI_API_KEY を設定し，LLMを利用できる状態にしてください。"
        )

    if num_patterns < 1:
        raise ProblemDecompositionError(
            "num_patterns は1以上を指定してください。"
        )

    # 2. IR とパターン数からプロンプトを構築する
    system, user = _build_prompt(card, num_patterns=num_patterns)

    # 3. LLM へ問い合わせる（JSON 抽出・再試行は chat_json が担当）
    try:
        data = chat_json(
            client,
            config,
            system,
            user,
        )
    except LLMError as exc:
        raise ProblemDecompositionError(
            f"問題分割AIエージェントの実行に失敗しました: {exc}"
        ) from exc

    # 4. 出力JSONを内部データ構造へ正規化して返す
    return _parse_decomposition(data, generator_mode="llm")


def load_decomposition_json(path: str) -> ProblemDecompositionResult:
    """保存済みの問題分割結果（write_decomposition_json の出力）を読み込む。

    問題分割は実行のたびに LLM が作り直すため，同じ分割で後続工程を
    繰り返し実行したい場合（評価実験・動作確認）に使う。
    読み込み時も LLM 出力と同じ正規化（_parse_decomposition）を通す。
    generator_mode はファイルに記録された値を引き継ぐ。

    Raises:
        ProblemDecompositionError: ファイルを読めない，または内容が不正な場合
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise ProblemDecompositionError(
            f"問題分割結果の JSON を読み込めません: {path}\n  → {exc}"
        ) from exc

    generator_mode = ""
    if isinstance(data, dict):
        generator_mode = str(data.get("generator_mode") or "").strip()

    return _parse_decomposition(data, generator_mode=generator_mode)
