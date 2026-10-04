"""
models.py
=========
発散モジュールで扱うデータ構造を定義する。

依頼仕様では pydantic もしくは dataclasses のいずれでもよいとされている。
ここでは追加依存やバージョン差異の影響を避けるため、標準ライブラリの
dataclasses を採用する。各データクラスには JSON 化のための ``to_dict`` を
用意し、出力スキーマ（依頼書の JSON 形式）にそのまま対応させる。

中心となる設計思想:
    * IR を中心入力として扱う（ObjectiveCard / IRRecord）
    * 外部情報源は補助的な発散材料として扱う（Source / IdeaSeed）
    * 各企画案には対応 IR 項目（LinkedIRItem）と参照外部情報源を必ず保持する
    * 後続の評価・収束モジュールへ接続できる JSON へ落とせるようにする
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List


# --------------------------------------------------------------------------- #
# IR（中間表現）
# --------------------------------------------------------------------------- #
@dataclass
class ObjectiveCard:
    """IR の中核となる objective_card。

    実 IR ファイル（IR_1215.jsonl）では説明配列のキーが
    ``*_root_explanation``（例: ``problem_root_explanation``）になっている。
    一方で依頼書の想定スキーマでは ``*_explanation`` だった。
    どちらの命名でも読み込めるよう、ir_loader 側で吸収してこの構造に詰める。
    欠損キーは空文字 / 空配列で初期化する。
    """

    target_user: str = ""
    problem: str = ""
    contradiction: str = ""
    objective: str = ""
    value: str = ""
    background: str = ""
    extras: List[str] = field(default_factory=list)

    # 各要素の根拠・背景（*_explanation / *_root_explanation を吸収）
    target_user_explanation: List[str] = field(default_factory=list)
    problem_explanation: List[str] = field(default_factory=list)
    contradiction_explanation: List[str] = field(default_factory=list)
    objective_explanation: List[str] = field(default_factory=list)
    value_explanation: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """全フィールドを dict 化する（JSON 出力・デバッグ用）。"""
        return asdict(self)

    def summary_dict(self) -> Dict[str, str]:
        """出力 JSON の ``input_ir_summary`` に対応する要約辞書。"""
        # 根拠（explanation）は含めず、企画案の由来をたどるのに必要な主要 5 項目だけを返す
        return {
            "target_user": self.target_user,
            "problem": self.problem,
            "contradiction": self.contradiction,
            "objective": self.objective,
            "value": self.value,
        }

    def all_explanations(self) -> Dict[str, List[str]]:
        """プロンプトへ渡す根拠（explanations）をまとめて返す。"""
        return {
            "target_user": self.target_user_explanation,
            "problem": self.problem_explanation,
            "contradiction": self.contradiction_explanation,
            "objective": self.objective_explanation,
            "value": self.value_explanation,
        }


@dataclass
class IRRecord:
    """IR ファイル 1 件分のレコード。"""

    session_id: str = ""
    stage: str = ""
    revision: Any = None
    objective_card: ObjectiveCard = field(default_factory=ObjectiveCard)
    # 元の生 dict（後続工程で追加情報が必要になった場合のため保持）
    raw: Dict[str, Any] = field(default_factory=dict)

    def info_score(self) -> int:
        """情報量スコア。stage=evidence が無いとき最有力 IR の選定に使う。

        objective_card の主要テキスト長と説明配列の件数から大まかに算出する。
        """
        oc = self.objective_card
        # 主要テキストの合計文字数を基礎点にする
        text_len = sum(
            len(t or "")
            for t in (
                oc.target_user,
                oc.problem,
                oc.contradiction,
                oc.objective,
                oc.value,
                oc.background,
            )
        )
        # 根拠が付いている IR ほど後段で使いやすいので、件数に重み（20 点）を掛けて加算する
        expl_count = sum(len(v) for v in oc.all_explanations().values())
        return text_len + expl_count * 20 + len(oc.extras) * 10


# --------------------------------------------------------------------------- #
# 問題分割
# --------------------------------------------------------------------------- #
@dataclass
class SubProblem:
    """IRから分割された部分問題を表す。

    1つのIRには複数の問題が含まれるため，検索・種抽出・企画生成の単位として
    部分問題を明示的に保持する。
    """

    subproblem_id: str = ""
    name: str = ""
    description: str = ""
    decomposition_axis: str = ""
    # この部分問題がIRのどの項目（problem / objective など）に対応するか
    linked_ir_items: List[str] = field(default_factory=list)
    # この部分問題を解決するために，どんな文献・特許を探したいか
    search_intent: str = ""
    # IRから読み取れる制約条件（時間帯・人数・体力など）
    constraints: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """部分問題を dict 化する（JSON 出力用）。"""
        return asdict(self)


@dataclass
class DecompositionPattern:
    """IRの問題分割パターンを表す。

    問題分割は1通りではないため，時間制約中心・心理的負担中心・家族分担中心など，
    複数の分割案を保持する。
    """

    pattern_id: str = ""
    pattern_name: str = ""
    viewpoint: str = ""
    reason: str = ""
    subproblems: List[SubProblem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """分割パターンを dict 化する（部分問題も再帰的に dict 化する）。"""
        d = asdict(self)
        # asdict はネストした dataclass も dict 化するが，
        # 各 SubProblem.to_dict() を明示的に通して変換規則を1か所に揃える
        d["subproblems"] = [sp.to_dict() for sp in self.subproblems]
        return d


@dataclass
class ProblemDecompositionResult:
    """問題分割エージェントの出力全体を表す。"""

    patterns: List[DecompositionPattern] = field(default_factory=list)
    generator_mode: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """問題分割結果全体を dict 化する（JSON 出力用）。"""
        return {
            "generator_mode": self.generator_mode,
            "patterns": [p.to_dict() for p in self.patterns],
        }


# --------------------------------------------------------------------------- #
# 外部情報源
# --------------------------------------------------------------------------- #
@dataclass
class Source:
    """外部情報源（文献・特許）1 件。"""

    source_id: str = ""
    source_type: str = ""  # CiNii / Scopus / Patent
    title: str = ""
    abstract: str = ""
    keywords: str = ""
    url: str = ""

    @property
    def search_text(self) -> str:
        """title + abstract + keywords を結合した検索・発散用テキスト。"""
        # 抄録が無い文献も多いため、空の項目は除いて結合する
        parts = [self.title, self.abstract, self.keywords]
        return " / ".join(p for p in parts if p)

    def to_dict(self) -> Dict[str, Any]:
        """外部情報源を dict 化する（JSON 出力用）。"""
        return asdict(self)


# --------------------------------------------------------------------------- #
# アイデアの種
# --------------------------------------------------------------------------- #
@dataclass
class IdeaSeed:
    """外部情報源または部分問題から抽出した「アイデアの種」。

    現時点では既存の idea_seed_extractor.py との互換性を保つため，
    旧形式の idea_seed も保持する。
    今後，部分問題単位の種抽出に移行する際に，
    source_finding / design_hint / possible_software_function を主に使う。
    """

    seed_id: str = ""
    pattern_id: str = ""
    subproblem_id: str = ""

    source_id: str = ""
    source_title: str = ""
    source_type: str = ""

    # 既存の idea_seed_extractor.py / idea_generator.py との互換用
    idea_seed: str = ""

    # 新方針用：文献知見 → 設計ヒント → 機能候補
    source_finding: str = ""
    design_hint: str = ""
    possible_software_function: str = ""

    related_ir_item: str = ""
    confidence: str = ""
    failure_note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """アイデアの種を dict 化する（JSON 出力用）。"""
        return asdict(self)


# --------------------------------------------------------------------------- #
# 企画案
# --------------------------------------------------------------------------- #
@dataclass
class LinkedIRItem:
    """企画案が対応する IR 項目と、その反映先。"""

    ir_key: str = ""
    ir_text: str = ""
    reflection: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """対応 IR 項目を dict 化する（JSON 出力用）。"""
        return asdict(self)


@dataclass
class Idea:
    """生成されたソフトウェア企画案 1 件。依頼書の出力スキーマに対応。"""

    idea_id: str = ""
    concept_name: str = ""
    concept_summary: str = ""
    target_user: str = ""
    main_features: List[str] = field(default_factory=list)
    solved_problem: str = ""
    handled_contradiction: str = ""
    provided_value: str = ""
    source_type: str = ""
    source_ids: List[str] = field(default_factory=list)
    source_titles: List[str] = field(default_factory=list)
    idea_seed: str = ""
    # どの種・どの部分問題を組み合わせて作った案かをたどるための情報
    seed_ids: List[str] = field(default_factory=list)
    covered_subproblem_ids: List[str] = field(default_factory=list)
    combination_reason: str = ""
    linked_ir_items: List[LinkedIRItem] = field(default_factory=list)
    trace_reason: str = ""
    novelty_reason: str = ""
    feasibility_note: str = ""
    risks: List[str] = field(default_factory=list)

    # 発散観点（どの観点で出した案かを保持。後続の収束で多様性確認に使える）
    perspective: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """企画案を dict 化する（対応 IR 項目も再帰的に dict 化する）。"""
        d = asdict(self)
        # linked_ir_items は asdict で既に dict 化されるが、明示しておく
        # （LinkedIRItem 以外が混入していた場合もそのまま通す）
        d["linked_ir_items"] = [li.to_dict() if isinstance(li, LinkedIRItem) else li
                                 for li in self.linked_ir_items]
        return d


@dataclass
class IdeaEvaluation:
    """生成された企画案を評価エージェントが評価した結果。"""

    idea_id: str = ""
    traceability_score: int = 0
    subproblem_coverage_score: int = 0
    evidence_usage_score: int = 0
    feasibility_score: int = 0
    novelty_score: int = 0
    diversity_score: int = 0
    overall_score: float = 0.0
    strengths: List[str] = field(default_factory=list)
    weaknesses: List[str] = field(default_factory=list)
    improvement_suggestions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """評価結果を dict 化する（JSON 出力用）。"""
        return asdict(self)


@dataclass
class GeneratedIdeas:
    """発散モジュールの最終成果物。

    生成された企画案だけでなく，問題分割，アイデアの種，評価結果も保持する。
    これにより，IRから企画案が生成されるまでの過程を後から確認できる。
    """

    session_id: str = ""
    generated_at: str = ""
    input_ir_summary: Dict[str, str] = field(default_factory=dict)
    ideas: List[Idea] = field(default_factory=list)

    # 試行ログ用ID
    trial_id: str = ""

    # 問題分割結果
    decomposition: Dict[str, Any] = field(default_factory=dict)

    # アイデアの種
    seeds: List[IdeaSeed] = field(default_factory=list)

    # 評価結果
    evaluations: List[IdeaEvaluation] = field(default_factory=list)

    # 補助情報（後続工程やデバッグ用。出力 JSON には含めるが評価モジュールは無視可）
    keywords: Dict[str, List[str]] = field(default_factory=dict)
    used_source_type: str = ""
    generator_mode: str = ""  # "llm" / "rule-based"

    def to_dict(self) -> Dict[str, Any]:
        """成果物全体を出力 JSON のスキーマどおりの dict に変換する。

        キーの並びが出力 JSON の並びになるため，
        「入力(IR) → 過程(分割・種) → 結果(企画案・評価)」の順で並べ，
        生成過程を追いやすくしている。
        """
        return {
            "session_id": self.session_id,
            "generated_at": self.generated_at,
            "trial_id": self.trial_id,
            "generator_mode": self.generator_mode,
            "used_source_type": self.used_source_type,
            "keywords": self.keywords,
            "input_ir_summary": self.input_ir_summary,
            "decomposition": self.decomposition,
            "seeds": [seed.to_dict() for seed in self.seeds],
            "ideas": [idea.to_dict() for idea in self.ideas],
            "evaluations": [ev.to_dict() for ev in self.evaluations],
        }
