"""
subproblem_keyword_generator.py
===============================
分割パターンの部分問題ごとに，検索・発散用のキーワードを生成する（評価実験の条件 C）。

IR 全体から検索語を作る keyword_generator.py（条件 B）とは別ファイルにして，
両条件の処理をファイル単位で比較・差し替えできるようにしている。

* 入力は SubProblem の name・description・search_intent・linked_ir_items
* constraints（例:「平日17時に退社」）は検索語に含めない。
  検索語にすると論文がヒットしにくくなるため，部分問題の側に保持したまま
  文献フィルタリングの段階で使う
* 部分問題ごとに LLM を 1 回呼ぶ。まとめて 1 回で生成すると，部分問題同士が
  影響し合って検索語が似やすくなるため
* 問題分割と同じく，ルールベースの代替は持たない。
  LLM 由来とルール由来の検索語が混ざると，条件 B・C の比較ができなくなるため，
  LLM が使えない・失敗した場合は SubProblemKeywordError を送出する

返り値:
    KeywordPlan（condition="C"）。各 SubProblemKeywords が pattern_id と
    subproblem_id を持ち，どの検索語がどの部分問題に由来するかをたどれる。
"""

from __future__ import annotations

from typing import Any, List, Optional

from config import AppConfig
from llm_client import LLMError, chat_json
from models import (
    DecompositionPattern,
    KeywordPlan,
    ObjectiveCard,
    SubProblem,
    SubProblemKeywords,
)


# 部分問題 1 件あたりの語数（日本語・英語それぞれ）
MIN_TERMS = 3
MAX_TERMS = 5


class SubProblemKeywordError(Exception):
    """部分問題ごとの検索語生成に関する例外。"""


def _as_terms(value: Any) -> List[str]:
    """LLM 出力の値を，空白を除いた重複なしの語リストに正規化する。"""
    if not isinstance(value, list):
        value = [value] if value else []
    terms = [str(x).strip() for x in value if str(x).strip()]
    # dict.fromkeys は挿入順を保つため，優先順位を崩さずに重複だけ落とせる
    return list(dict.fromkeys(terms))


def _build_prompt(card: ObjectiveCard, subproblem: SubProblem):
    """部分問題 1 件の検索語を生成するプロンプト（system, user）を組み立てる。

    IR の要約は「目的から外れないための文脈」として渡し，
    検索語を作る対象はあくまで部分問題に限るよう指示する。
    """
    system = (
        "あなたは要求工学と文献調査に詳しいリサーチャーです。"
        "IR（利用者の問題意識）から分割された部分問題 1 件について，"
        "CiNii Research・Scopus・特許DBを検索する際に有効なキーワードを，"
        "日本語と英語の両方で抽出します。"
        "出力はJSONのみとします。"
    )

    linked = ", ".join(subproblem.linked_ir_items) or "（なし）"
    constraints = "\n".join(f"  - {c}" for c in subproblem.constraints) or "  - （なし）"

    user = f"""# IR 要約（文脈。検索語の対象ではない）
- 対象ユーザー: {card.target_user}
- 課題: {card.problem}
- 矛盾: {card.contradiction}
- 目的: {card.objective}
- 提供価値: {card.value}

# 検索語を作る対象の部分問題
- 部分問題名: {subproblem.name}
- 説明: {subproblem.description}
- 対応IR項目: {linked}
- 検索意図: {subproblem.search_intent}
- 制約条件（検索語には含めないこと）:
{constraints}

# 指示
上記の部分問題に対応する文献・特許を探すための検索語を作ってください。

重要：
- IR 全体ではなく，この部分問題の説明と検索意図に対応する語にしてください。
- 部分問題の表現をそのまま検索語にしないでください。
- CiNii Researchで論文検索しやすいように，学術論文で使われやすい一般概念語・研究分野語・類義語・上位概念へ変換してください。
- 日本語キーワードは，CiNiiでヒットしやすい1〜3語の組み合わせにしてください。
- 英語キーワードは，Scopusや特許DBでも利用できる一般的な研究語にしてください。
- 時間帯・勤務時刻などの制約条件は検索語に含めないでください（後段の文献評価で使います）。
- 一般的すぎる語（例：アプリ，システム，支援，AI）だけにならないようにしてください。
- 解決策（アプリの機能など）を先に決めつける語にしないでください。

# 変換例
- 自炊負担 → 家事負担，調理負担，食生活
- 親子の情緒的関わり → 親子関係，親子コミュニケーション，育児支援
- 平日夕方の過ごし方 → 家事時間，生活時間，時間使用
- 罪悪感の軽減 → 育児不安，母親ストレス，心理的負担
- 栄養価の高い食事 → 食生活，栄養バランス，献立

# 出力条件
- 日本語 {MIN_TERMS}〜{MAX_TERMS} 個
- 英語 {MIN_TERMS}〜{MAX_TERMS} 個

# 出力スキーマ（このJSONのみを出力）
{{"ja": ["..."], "en": ["..."]}}"""

    return system, user


def _llm_subproblem_keywords(
    card: ObjectiveCard,
    pattern_id: str,
    subproblem: SubProblem,
    client,
    config: AppConfig,
) -> SubProblemKeywords:
    """LLM で部分問題 1 件の検索語を生成する。

    語数が上限を超えた場合は先頭から MAX_TERMS 語に切る。
    下限に満たない場合は，出力をそのまま採用せず失敗として扱う
    （補完のために辞書語などを足すと，LLM 由来の出力ではなくなるため）。
    """
    system, user = _build_prompt(card, subproblem)

    try:
        data = chat_json(client, config, system, user)
    except LLMError as exc:
        raise SubProblemKeywordError(
            f"{pattern_id}/{subproblem.subproblem_id} の検索語生成に失敗しました: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise SubProblemKeywordError(
            f"{pattern_id}/{subproblem.subproblem_id} の検索語生成の出力が"
            "JSONオブジェクトではありません。"
        )

    ja = _as_terms(data.get("ja"))[:MAX_TERMS]
    en = _as_terms(data.get("en"))[:MAX_TERMS]

    if len(ja) < MIN_TERMS or len(en) < MIN_TERMS:
        raise SubProblemKeywordError(
            f"{pattern_id}/{subproblem.subproblem_id} の検索語が不足しています"
            f"（ja={len(ja)}語, en={len(en)}語。各 {MIN_TERMS} 語以上が必要）。"
        )

    return SubProblemKeywords(
        pattern_id=pattern_id,
        subproblem_id=subproblem.subproblem_id,
        subproblem_name=subproblem.name,
        ja=ja,
        en=en,
        generator_mode="llm",
    )


def generate_subproblem_keywords(
    card: ObjectiveCard,
    pattern: DecompositionPattern,
    client=None,
    config: Optional[AppConfig] = None,
    decomposition_source: str = "generated",
) -> KeywordPlan:
    """選択した分割パターンの部分問題ごとに検索語を生成する（条件 C）。

    Args:
        card: 元の IR（検索語の文脈として使う）
        pattern: 対象とする分割パターン
        client: OpenAI 互換クライアント（None ならエラー）
        config: LLM 設定
        decomposition_source: 分割結果の出どころ（"generated" または JSON のパス）

    Raises:
        SubProblemKeywordError: LLM が使えない場合，または 1 件でも生成に失敗した場合
    """
    # 条件 C はルールベースの代替を持たないため，LLM が無ければここで止める
    if client is None or config is None or not config.use_llm:
        raise SubProblemKeywordError(
            "条件 C の検索語生成には LLM が必要です。"
            "OPENAI_API_KEY を設定し，LLMを利用できる状態にしてください。"
        )

    if not pattern.subproblems:
        raise SubProblemKeywordError(
            f"分割パターン {pattern.pattern_id} に部分問題がありません。"
        )

    subproblem_keywords = [
        _llm_subproblem_keywords(card, pattern.pattern_id, sp, client, config)
        for sp in pattern.subproblems
    ]

    return KeywordPlan(
        condition="C",
        pattern_id=pattern.pattern_id,
        decomposition_source=decomposition_source,
        subproblem_keywords=subproblem_keywords,
    )
