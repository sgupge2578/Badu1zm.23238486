"""
pattern_selector.py
===================
問題分割の結果から，後続工程へ渡す分割パターンを 1 つ選ぶ。

現状は CLI の --pattern-id で指定されたパターン（未指定なら先頭）を返すだけの
最小限の実装である。作業計画 2「分割パターンの確認・選択機能」で，
利用者が分割を確認して選ぶ機能に置き換える想定。
"""

from __future__ import annotations

from typing import Optional

from models import DecompositionPattern, ProblemDecompositionResult


class PatternSelectionError(Exception):
    """分割パターンの選択に関する例外。"""


def select_pattern(
    decomposition: ProblemDecompositionResult,
    pattern_id: Optional[str] = None,
) -> DecompositionPattern:
    """pattern_id に一致する分割パターンを返す。未指定なら先頭のパターンを返す。

    同じ pattern_id が複数ある場合（LLM 出力の揺れ）は先頭の 1 件を返す。

    Raises:
        PatternSelectionError: パターンが 1 件も無い，または指定 ID が見つからない場合
    """
    if not decomposition.patterns:
        raise PatternSelectionError("問題分割の結果に分割パターンがありません。")

    if not pattern_id:
        return decomposition.patterns[0]

    for pattern in decomposition.patterns:
        if pattern.pattern_id == pattern_id:
            return pattern

    available = ", ".join(p.pattern_id for p in decomposition.patterns)
    raise PatternSelectionError(
        f"分割パターン {pattern_id} が見つかりません（候補: {available}）。"
    )
