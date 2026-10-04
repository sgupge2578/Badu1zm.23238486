"""
source_loader.py
================
外部情報源 CSV（文献・特許）を読み込む。

* pandas で CSV を読み込む
* source_type（CiNii / Scopus / Patent）で絞り込みできる
* title + abstract + keywords を結合した検索・発散用テキストを作る
  （Source.search_text プロパティ側で結合）

最初は CiNii だけでも動くが、source_type 指定が無ければ全件を返す。
"""

from __future__ import annotations

import os
from typing import List, Optional

import pandas as pd

from models import Source


class SourceLoadError(Exception):
    """外部情報源 CSV 読み込みに関する例外。"""


# CSV に存在しうる列名の揺れを吸収するためのエイリアス
# 論理名（左） 1 つに対し、実ファイルで使われうる列名（右）を候補として並べる。
_COLUMN_ALIASES = {
    "source_id": ["source_id", "id", "sid"],
    "source_type": ["source_type", "type", "db"],
    "title": ["title", "name", "タイトル"],
    "abstract": ["abstract", "summary", "概要", "abst"],
    "keywords": ["keywords", "keyword", "kw", "キーワード"],
    "url": ["url", "link", "uri"],
}


def _resolve_columns(df: pd.DataFrame) -> dict:
    """実際の DataFrame 列から、論理名→実列名の対応を作る。

    アルゴリズム: CSV の列名を小文字化した索引を作り、_COLUMN_ALIASES の候補を
    先頭から順に照合する。最初に一致した候補を採用するため、エイリアスの並び順が
    優先順位になる。見つからない論理名は辞書に入れない（後段で空文字扱い）。
    """
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    resolved = {}
    for logical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lower_map:
                resolved[logical] = lower_map[alias.lower()]
                break
    return resolved


def _cell(row: pd.Series, col: Optional[str]) -> str:
    """1 行分のデータから指定列の値を、欠損に強い形で文字列として取り出す。

    列が存在しない場合や欠損値（NaN）の場合は空文字を返し、後段の処理が
    None チェックをしなくて済むようにする。
    """
    if not col or col not in row:
        return ""
    val = row[col]
    if pd.isna(val):
        return ""
    return str(val).strip()


def load_sources(path: str, source_type: Optional[str] = None) -> List[Source]:
    """外部情報源 CSV を読み込み、Source のリストを返す。

    アルゴリズム:
        1. ファイル存在を確認し、UTF-8 → cp932 の順に読み込みを試す
        2. 列名の揺れを _resolve_columns で吸収する
        3. 1 行を 1 つの Source へ正規化する（source_id が空なら連番を採番）
        4. source_type が指定されていれば、その種別だけに絞り込む

    Args:
        path: CSV ファイルパス
        source_type: "CiNii" / "Scopus" / "Patent"。None なら絞り込みなし。
    """
    if not os.path.exists(path):
        raise SourceLoadError(
            f"外部情報源 CSV が見つかりません: {path}\n"
            f"  → --sources でパスを指定しているか、data/sources.csv があるか確認してください。"
        )

    try:
        # 文字コードは utf-8 を基本に、失敗時は cp932 も試す
        # dtype=str / keep_default_na=False で、数値化や NaN 化による値の変質を防ぐ
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        except UnicodeDecodeError:
            df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="cp932")
    except pd.errors.EmptyDataError as exc:
        raise SourceLoadError(f"外部情報源 CSV が空です: {path}") from exc
    except Exception as exc:  # noqa: BLE001  読み込み失敗を分かりやすく通知
        raise SourceLoadError(f"外部情報源 CSV の読み込みに失敗しました: {path} ({exc})") from exc

    if df.empty:
        raise SourceLoadError(f"外部情報源 CSV にデータ行がありません: {path}")

    # 列名の揺れを吸収。title も abstract も無い CSV は発散材料にならないので弾く
    cols = _resolve_columns(df)
    if "title" not in cols and "abstract" not in cols:
        raise SourceLoadError(
            f"CSV に title / abstract 列が見つかりません。列名を確認してください: {list(df.columns)}"
        )

    # 1 行 = 1 情報源として Source へ正規化する（source_id が無ければ S001… を採番）
    sources: List[Source] = []
    for i, (_, row) in enumerate(df.iterrows(), start=1):
        src = Source(
            source_id=_cell(row, cols.get("source_id")) or f"S{i:03d}",
            source_type=_cell(row, cols.get("source_type")),
            title=_cell(row, cols.get("title")),
            abstract=_cell(row, cols.get("abstract")),
            keywords=_cell(row, cols.get("keywords")),
            url=_cell(row, cols.get("url")),
        )
        sources.append(src)

    # source_type 指定があれば絞り込む（大文字小文字・前後空白の違いは無視する）
    if source_type:
        wanted = source_type.strip().lower()
        sources = [s for s in sources if s.source_type.strip().lower() == wanted]
        if not sources:
            raise SourceLoadError(
                f"source_type='{source_type}' に該当する行が CSV にありません。"
                f"  → --source-type の指定、または CSV の source_type 列を確認してください。"
            )

    return sources


if __name__ == "__main__":
    # 単体動作確認用（python src/source_loader.py data/sources.csv CiNii）
    import sys

    p = sys.argv[1] if len(sys.argv) > 1 else "data/sources.csv"
    st = sys.argv[2] if len(sys.argv) > 2 else None
    srcs = load_sources(p, st)
    print(f"{len(srcs)} 件読み込み (source_type={st})")
    for s in srcs:
        print(f"- [{s.source_id}/{s.source_type}] {s.title}")
        print(f"    search_text: {s.search_text[:80]}...")
