"""
output_writer.py
================
生成された企画案を JSON と Markdown で保存する。

* JSON: 後続の評価・収束モジュールへ接続する機械可読形式（依頼書スキーマ準拠）
* Markdown: 人間がレビューするための読みやすい形式
* outputs フォルダが無ければ作成する
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

from models import (
    DecompositionPattern,
    GeneratedIdeas,
    Idea,
    KeywordPlan,
    ProblemDecompositionResult,
)


def _ensure_dir(path: str) -> None:
    """出力先ファイルの親ディレクトリが無ければ作成する。

    outputs/ が未作成の状態で実行しても書き出しが失敗しないようにするための前処理。
    パスにディレクトリ部分が無い場合（カレントへの出力）は何もしない。
    """
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def write_json(generated: GeneratedIdeas, path: str) -> None:
    """企画案の成果物を JSON ファイルとして書き出す。

    後続の評価・収束モジュールが読み込む機械可読形式。日本語をそのまま読めるよう
    ensure_ascii=False とし、差分を追いやすいよう indent=2 で整形する。
    """
    _ensure_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(generated.to_dict(), f, ensure_ascii=False, indent=2)


def _idea_to_md(idea: Idea) -> str:
    """企画案 1 件を Markdown の 1 セクション（文字列）へ整形する。

    レビュー時に「この案がどのIR項目・どの文献に基づくか」を追えることを重視し、
    参照情報源・対応IR項目・IRに基づく理由・新規性・実装可能性・懸念点まで出力する。
    値が空の項目は行そのものを出さず、読みやすさを保つ。
    """
    lines: List[str] = []
    lines.append(f"### {idea.idea_id}: {idea.concept_name}")
    if idea.perspective:
        lines.append(f"*発散観点: {idea.perspective}*")
    lines.append("")
    lines.append(idea.concept_summary)
    lines.append("")
    lines.append(f"- 対象ユーザー: {idea.target_user}")

    if idea.main_features:
        lines.append("- 主要機能:")
        for f in idea.main_features:
            lines.append(f"    - {f}")

    lines.append(f"- 解決する課題: {idea.solved_problem}")
    lines.append(f"- 扱う矛盾・トレードオフ: {idea.handled_contradiction}")
    lines.append(f"- 提供価値: {idea.provided_value}")

    # 参照外部情報源
    # 企画案の根拠をたどれるよう、参照した文献の id とタイトルを併記する
    if idea.source_ids:
        srcs = ", ".join(idea.source_ids)
        titles = "; ".join(idea.source_titles) if idea.source_titles else ""
        type_str = f"（{idea.source_type}）" if idea.source_type else ""
        lines.append(f"- 参照外部情報源{type_str}: {srcs}"
                     + (f" — {titles}" if titles else ""))
    if idea.idea_seed:
        lines.append(f"- アイデアの種: {idea.idea_seed}")

    # 対応 IR 項目
    # 「IR のどの記述が、企画のどこに反映されたか」を 2 段の箇条書きで示す
    if idea.linked_ir_items:
        lines.append("- 対応 IR 項目:")
        for li in idea.linked_ir_items:
            lines.append(f"    - `{li.ir_key}`: {li.ir_text}")
            if li.reflection:
                lines.append(f"        - 反映先: {li.reflection}")

    if idea.trace_reason:
        lines.append(f"- IR に基づく理由: {idea.trace_reason}")
    if idea.novelty_reason:
        lines.append(f"- 新規性（外部情報源による差分）: {idea.novelty_reason}")
    if idea.feasibility_note:
        lines.append(f"- 実装可能性: {idea.feasibility_note}")
    if idea.risks:
        lines.append("- 懸念点:")
        for r in idea.risks:
            lines.append(f"    - {r}")

    lines.append("")
    return "\n".join(lines)


def write_markdown(generated: GeneratedIdeas, path: str) -> None:
    """企画案の成果物を、人間がレビューしやすい Markdown として書き出す。

    構成は「実行条件（session_id 等）→ 入力IR要約 → 検索キーワード → 企画案一覧」。
    入力から結果までを 1 ファイルで追えるようにし、進捗報告にそのまま使えるようにする。
    """
    _ensure_dir(path)
    s = generated.input_ir_summary
    lines: List[str] = []
    # 1. ヘッダ（どの条件で生成したかを最初に示す）
    lines.append("# 生成されたソフトウェア企画案")
    lines.append("")
    lines.append(f"- session_id: `{generated.session_id}`")
    lines.append(f"- generated_at: {generated.generated_at}")
    lines.append(f"- condition: {generated.condition or '（指定なし）'}")
    lines.append(f"- generator_mode: `{generated.generator_mode}`")
    lines.append(f"- used_source_type: {generated.used_source_type or '（指定なし）'}")
    lines.append("")

    # 2. 入力 IR の要約（生成結果が IR から逸脱していないか照合するため）
    lines.append("## 入力 IR 要約")
    lines.append(f"- 対象ユーザー: {s.get('target_user', '')}")
    lines.append(f"- 課題: {s.get('problem', '')}")
    lines.append(f"- 矛盾: {s.get('contradiction', '')}")
    lines.append(f"- 目的: {s.get('objective', '')}")
    lines.append(f"- 提供価値: {s.get('value', '')}")
    lines.append("")

    # 3. 検索・発散キーワード（どの語で文献を集めたかの記録）
    if generated.keywords:
        ja = ", ".join(generated.keywords.get("ja", []))
        en = ", ".join(generated.keywords.get("en", []))
        lines.append("## 検索・発散キーワード")
        lines.append(f"- 日本語: {ja or '（なし）'}")
        lines.append(f"- 英語: {en or '（なし）'}")
        lines.append("")

    # 4. 企画案一覧（1 件ずつ _idea_to_md で整形）
    lines.append(f"## 企画案一覧（{len(generated.ideas)} 件）")
    lines.append("")
    for idea in generated.ideas:
        lines.append(_idea_to_md(idea))

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_outputs(generated: GeneratedIdeas, json_path: str, md_path: str) -> None:
    """JSON と Markdown をまとめて書き出す便宜関数。"""
    write_json(generated, json_path)
    write_markdown(generated, md_path)


def write_decomposition_json(
    decomposition: ProblemDecompositionResult,
    path: str,
) -> None:
    """問題分割結果をJSON形式で保存する。

    問題分割エージェントが生成した分割パターンと部分問題を，
    後続工程や進捗報告で確認できるように保存する。
    """

    _ensure_dir(path)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(decomposition.to_dict(), f, ensure_ascii=False, indent=2)


def _decomposition_to_md(decomposition: ProblemDecompositionResult) -> str:
    """問題分割結果をMarkdown形式の文字列に変換する。"""

    # Markdownとして出力する各行を順番に格納する。
    lines: List[str] = []

    # ファイル全体の見出しと基本情報を出力する。
    lines.append("# 問題分割結果")
    lines.append("")
    lines.append(f"- generator_mode: `{decomposition.generator_mode}`")
    lines.append(f"- 分割パターン数: {len(decomposition.patterns)}")
    lines.append("")

    # 各分割パターンを順番にMarkdownへ変換する。
    # 「分割は1通りではない」ことが読み手に伝わるよう，パターンごとに
    # 観点（viewpoint）と，その分割を選んだ理由（reason）を必ず示す。
    for pattern in decomposition.patterns:
        lines.append(f"## {pattern.pattern_id}: {pattern.pattern_name}")
        lines.append("")
        lines.append(f"- 分割観点: {pattern.viewpoint}")
        lines.append(f"- この分割を行う理由: {pattern.reason}")
        lines.append(f"- 部分問題数: {len(pattern.subproblems)}")
        lines.append("")

        # 分割パターンに含まれる各部分問題を出力する。
        for subproblem in pattern.subproblems:
            lines.append(f"### {subproblem.subproblem_id}: {subproblem.name}")
            lines.append("")
            lines.append(f"- 説明: {subproblem.description}")
            lines.append(f"- 分割軸: {subproblem.decomposition_axis}")

            # 部分問題がIRのどの項目に対応しているかを出力する。
            # 空の場合も「（なし）」と明示し，対応漏れが分かるようにする。
            if subproblem.linked_ir_items:
                linked = ", ".join(subproblem.linked_ir_items)
                lines.append(f"- 対応IR項目: {linked}")
            else:
                lines.append("- 対応IR項目: （なし）")

            # 検索意図（この部分問題を解決するために何を探すか）を出力する。
            lines.append(f"- 検索意図: {subproblem.search_intent}")

            # IRから読み取れる制約条件があれば箇条書きで出力する。
            if subproblem.constraints:
                lines.append("- 制約条件:")
                for constraint in subproblem.constraints:
                    lines.append(f"    - {constraint}")
            else:
                lines.append("- 制約条件: （なし）")

            lines.append("")

    return "\n".join(lines)


def write_decomposition_markdown(
    decomposition: ProblemDecompositionResult,
    path: str,
) -> None:
    """問題分割結果をMarkdown形式で保存する。

    JSONは機械処理用，Markdownは人間がレビューしやすい確認用として使う。
    """

    _ensure_dir(path)

    with open(path, "w", encoding="utf-8") as f:
        f.write(_decomposition_to_md(decomposition))


def write_decomposition(
    decomposition: ProblemDecompositionResult,
    json_path: str,
    md_path: str,
) -> None:
    """問題分割結果をJSONとMarkdownの両方で保存する。"""

    write_decomposition_json(decomposition, json_path)
    write_decomposition_markdown(decomposition, md_path)


def _unique_ratio(terms: List[str], others: List[str]) -> str:
    """terms のうち others に出てこない語の割合を「k/n」の形で返す。"""
    if not terms:
        return "0/0"
    unique = [t for t in terms if t not in others]
    return f"{len(unique)}/{len(terms)}"


def _keyword_plan_to_md(
    plan: KeywordPlan,
    pattern: Optional[DecompositionPattern] = None,
) -> str:
    """検索語生成の結果を Markdown 形式の文字列に変換する。

    条件 C では，部分問題ごとに検索意図・制約条件・検索語を並べ，
    他の部分問題と重複しない語の割合も示す。
    「部分問題ごとに異なる検索語が生成されているか」を確認するため。
    """
    lines: List[str] = []
    lines.append("# 検索語生成結果")
    lines.append("")
    lines.append(f"- condition: {plan.condition}")

    if plan.condition != "C":
        # 条件 B: IR 全体から生成した検索語だけを示す
        lines.append("- 入力: IR 全体")
        lines.append("")
        lines.append("## 検索語")
        lines.append(f"- 日本語: {', '.join(plan.ir_keywords.get('ja', [])) or '（なし）'}")
        lines.append(f"- 英語: {', '.join(plan.ir_keywords.get('en', [])) or '（なし）'}")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"- 分割パターン: {plan.pattern_id}"
                 + (f"（{pattern.pattern_name}）" if pattern is not None else ""))
    lines.append(f"- 分割結果の出どころ: {plan.decomposition_source}")
    lines.append(f"- 部分問題数: {len(plan.subproblem_keywords)}")
    lines.append("")

    # 部分問題 ID から SubProblem を引けるようにする（検索意図・制約条件の表示用）
    subproblems = {}
    if pattern is not None:
        subproblems = {sp.subproblem_id: sp for sp in pattern.subproblems}

    # 1. 部分問題ごとの一覧表（重複の少なさを一目で確認するため）
    lines.append("## 部分問題ごとの検索語")
    lines.append("")
    lines.append("| 部分問題 | 日本語 | 英語 | 固有語（ja） | 固有語（en） |")
    lines.append("|---|---|---|---|---|")
    for k in plan.subproblem_keywords:
        other_ja = [t for o in plan.subproblem_keywords if o is not k for t in o.ja]
        other_en = [t for o in plan.subproblem_keywords if o is not k for t in o.en]
        lines.append(
            f"| {k.subproblem_id}: {k.subproblem_name} "
            f"| {', '.join(k.ja)} | {', '.join(k.en)} "
            f"| {_unique_ratio(k.ja, other_ja)} | {_unique_ratio(k.en, other_en)} |"
        )
    lines.append("")
    lines.append("固有語: 他の部分問題の検索語に出てこない語の数 / その部分問題の語数")
    lines.append("")

    # 2. 部分問題ごとの詳細（検索語と，検索語に含めなかった制約条件の対応）
    for k in plan.subproblem_keywords:
        lines.append(f"### {k.subproblem_id}: {k.subproblem_name}")
        lines.append("")
        sp = subproblems.get(k.subproblem_id)
        if sp is not None:
            lines.append(f"- 検索意図: {sp.search_intent}")
            constraints = ", ".join(sp.constraints) or "（なし）"
            lines.append(f"- 制約条件（検索語には含めず保持）: {constraints}")
        lines.append(f"- 生成モード: `{k.generator_mode}`")
        lines.append(f"- 日本語: {', '.join(k.ja)}")
        lines.append(f"- 英語: {', '.join(k.en)}")
        lines.append("")

    # 3. 後続工程へ実際に渡した順序（部分問題をまたいで交互に並べたもの）
    merged = plan.merged()
    lines.append("## 後続工程へ渡す検索語（部分問題をまたいで交互に並べた順）")
    lines.append("")
    lines.append(f"- 日本語: {', '.join(merged['ja']) or '（なし）'}")
    lines.append(f"- 英語: {', '.join(merged['en']) or '（なし）'}")
    lines.append("")
    return "\n".join(lines)


def write_keyword_plan(
    plan: KeywordPlan,
    json_path: str,
    md_path: str,
    pattern: Optional[DecompositionPattern] = None,
) -> None:
    """検索語生成の結果を JSON と Markdown の両方で保存する。

    pattern を渡すと，Markdown に部分問題の検索意図と制約条件も併記する。
    """
    _ensure_dir(json_path)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(plan.to_dict(), f, ensure_ascii=False, indent=2)

    _ensure_dir(md_path)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(_keyword_plan_to_md(plan, pattern))
