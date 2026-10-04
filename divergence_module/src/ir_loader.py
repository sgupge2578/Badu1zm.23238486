"""
ir_loader.py
============
IR ファイルを読み込み、ObjectiveCard を取り出す。

実ファイル（IR_1215.jsonl）の調査で判明した重要点に対応する:

1. このファイルは「1 行 1 JSON」の厳密な JSONL ではなく、
   複数行にわたる **整形済みの 1 個の JSON オブジェクト** だった。
   → 厳密 JSONL と整形 JSON（複数オブジェクト連結も含む）の双方を読む。

2. 説明配列のキーが ``*_root_explanation``
   （例: ``problem_root_explanation``）だった。
   依頼書の想定は ``*_explanation`` だったため、両命名を吸収する。

3. objective_card が無い場合は近い構造を探索する。

4. JSONL 読み込みに失敗した場合は「何行目で失敗したか」を表示する。

設計方針:
    * まず「1 行 1 JSON」での解析を試す（真の JSONL なら行番号エラーが明瞭）。
    * 失敗が混じる、または 1 件しか取れない場合は、
      json.JSONDecoder().raw_decode によるスキャンへフォールバックし、
      整形 JSON / 連結 JSON のいずれも拾えるようにする。
    * 欠損キーは空文字 / 空配列で扱う。
    * stage=evidence を優先。無ければ info_score 最大の IR を採用。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from models import IRRecord, ObjectiveCard


class IRLoadError(Exception):
    """IR 読み込みに関する例外。"""


# --------------------------------------------------------------------------- #
# 低レベル: テキストから JSON オブジェクト群を取り出す
# --------------------------------------------------------------------------- #
def _parse_as_jsonl(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """「1 行 1 JSON」として解析を試みる。

    返り値: (オブジェクトのリスト, エラーメッセージのリスト)
    1 つでも非空行が JSON として解析できなければエラーを記録する。
    """
    objects: List[Dict[str, Any]] = []
    errors: List[str] = []
    # 1 行ずつ独立した JSON とみなして解析する。
    # 失敗しても即座に中断せず、何行目で失敗したかを全て集めてから返す
    # （利用者に「どこが壊れているか」を具体的に示すため）。
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append(f"{lineno} 行目: {exc.msg} (col {exc.colno})")
    return objects, errors


def _scan_json_objects(text: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """raw_decode で連続した JSON 値をスキャン抽出する。

    整形済み JSON（複数行 1 オブジェクト）や、複数オブジェクトが
    空白区切りで連結されているケースに対応する。
    解析できない位置に達したら、その行番号を記録して次行から復帰を試みる。
    """
    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    errors: List[str] = []
    idx = 0
    n = len(text)
    # アルゴリズム: 先頭から「1 個の JSON 値」を raw_decode で切り出し、
    # 終了位置 end を次の走査開始位置にする、を末尾まで繰り返す。
    # これにより改行位置に依存せず、整形 JSON でも連結 JSON でも拾える。
    while idx < n:
        # 空白読み飛ばし
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
            objects.append(obj)
            idx = end
        except json.JSONDecodeError as exc:
            # 壊れた箇所は「何行目付近か」を記録し、次の行から復帰を試みる
            # （1 か所の破損で全体を捨てないため）。
            lineno = text.count("\n", 0, idx) + 1
            errors.append(f"{lineno} 行目付近: {exc.msg}")
            # 次の改行まで飛ばして復帰を試みる
            next_nl = text.find("\n", idx)
            if next_nl == -1:
                break
            idx = next_nl + 1
    return objects, errors


def _load_json_objects(path: str) -> List[Dict[str, Any]]:
    """ファイルから JSON オブジェクト群を読み込む（両形式対応）。

    アルゴリズム（2 段構え）:
        1. まず厳密な JSONL（1 行 1 JSON）として解析する。
           全行がエラー無く解析できたときだけ、真の JSONL とみなして採用する。
        2. 1 行でも失敗した場合（＝整形 JSON の可能性）は raw_decode による
           スキャンへフォールバックする。
        どちらでも 1 件も取れなければ、収集した失敗位置を添えてエラーにする。
    """
    if not os.path.exists(path):
        raise IRLoadError(
            f"IR ファイルが見つかりません: {path}\n"
            f"  → --ir でパスを指定しているか、data/ にファイルがあるか確認してください。"
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise IRLoadError(f"IR ファイルを開けませんでした: {path} ({exc})") from exc

    if not text.strip():
        raise IRLoadError(f"IR ファイルが空です: {path}")

    # まず「1 行 1 JSON」を試す
    jsonl_objs, jsonl_errs = _parse_as_jsonl(text)

    # 全行が問題なく解析でき、1 件以上取れたなら真の JSONL とみなす
    if jsonl_objs and not jsonl_errs:
        return _as_dict_list(jsonl_objs, path)

    # それ以外（行解析にエラーがある／整形 JSON 等）はスキャンへフォールバック
    scan_objs, scan_errs = _scan_json_objects(text)
    if scan_objs:
        return _as_dict_list(scan_objs, path)

    # どちらでも取れなかった → 失敗位置を提示
    detail_lines = jsonl_errs + scan_errs
    detail = "\n  ".join(detail_lines) if detail_lines else "（位置情報を特定できませんでした）"
    raise IRLoadError(
        f"IR ファイルの JSON 解析に失敗しました: {path}\n  {detail}"
    )


def _as_dict_list(objs: List[Any], path: str) -> List[Dict[str, Any]]:
    """解析結果の JSON 値の並びを、IR レコード（dict）のリストへ整える。

    JSON のトップレベルが配列で包まれている場合（[{...}, {...}]）もあるため、
    リストが来たら中身を展開して平坦化する。dict でもリストでもない値
    （数値・文字列など）が混ざっていた場合はファイル異常としてエラーにする。
    """
    result: List[Dict[str, Any]] = []
    for i, o in enumerate(objs):
        if isinstance(o, dict):
            result.append(o)
        else:
            # 配列でラップされている等の場合は中身を展開
            if isinstance(o, list):
                result.extend(x for x in o if isinstance(x, dict))
            else:
                raise IRLoadError(
                    f"IR ファイル内に dict でない JSON 値が含まれています "
                    f"(index {i}, type {type(o).__name__}): {path}"
                )
    if not result:
        raise IRLoadError(f"IR ファイルから有効なオブジェクトを取得できませんでした: {path}")
    return result


# --------------------------------------------------------------------------- #
# objective_card の探索と正規化
# --------------------------------------------------------------------------- #
# 「objective_card らしさ」を判定するための代表フィールド
_CARD_FIELDS = ("target_user", "problem", "contradiction", "objective", "value")


def _find_objective_card(record: Dict[str, Any]) -> Dict[str, Any]:
    """record から objective_card 相当の dict を探す。

    1. ``objective_card`` キーがあればそれを使う。
    2. 無ければ、_CARD_FIELDS のうち 2 個以上を含む dict を再帰探索する。
    3. それでも無ければ record 自身を候補とする（トップレベル直書きのケース）。
    """
    oc = record.get("objective_card")
    if isinstance(oc, dict):
        return oc

    # 再帰的に「それらしい dict」を探す
    found = _search_card_like(record)
    if found is not None:
        return found

    # 最後の手段: トップレベルが直接フィールドを持っているか
    if sum(1 for k in _CARD_FIELDS if k in record) >= 2:
        return record

    return {}


def _search_card_like(node: Any) -> Optional[Dict[str, Any]]:
    """JSON の入れ子を深さ優先で辿り、objective_card らしい dict を 1 件返す。

    判定基準は「_CARD_FIELDS（target_user / problem など）を 2 個以上持つ dict」。
    IR のキー名が想定と違っていても、構造から目的の箇所を拾えるようにするための
    ヒューリスティック。見つからなければ None を返す。
    """
    if isinstance(node, dict):
        # この dict 自身が条件を満たすなら採用（浅い階層を優先）
        if sum(1 for k in _CARD_FIELDS if k in node) >= 2:
            return node
        # 満たさなければ子要素を再帰的に探索する
        for v in node.values():
            hit = _search_card_like(v)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = _search_card_like(v)
            if hit is not None:
                return hit
    return None


def _as_str(value: Any) -> str:
    """値を文字列へ安全に正規化する。"""
    # IR の項目が配列で書かれている場合もあるため、その場合は空白区切りで結合する
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(_as_str(v) for v in value if v is not None).strip()
    return str(value)


def _as_str_list(value: Any) -> List[str]:
    """値を文字列リストへ安全に正規化する。"""
    # 単一文字列で書かれていても要素 1 件のリストとして扱い、後段の処理を統一する
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for v in value:
            s = _as_str(v)
            if s:
                out.append(s)
        return out
    return [str(value)]


def _get_explanation(card: Dict[str, Any], field: str) -> List[str]:
    """``field`` の説明配列を、複数の命名揺れを吸収して取得する。

    優先順位:
        1. ``{field}_explanation``
        2. ``{field}_root_explanation``
        3. ``{field}`` を接頭辞に持ち "explanation" を含む任意キー
    """
    # 1, 2: 想定される 2 つの命名を順に確認する
    for suffix in ("_explanation", "_root_explanation"):
        key = f"{field}{suffix}"
        if key in card:
            return _as_str_list(card[key])
    # 3: それでも無ければ、接頭辞一致 + "explanation" を含むキーを総当たりで探す
    for key, val in card.items():
        if key.startswith(field) and "explanation" in key:
            return _as_str_list(val)
    return []


def normalize_objective_card(card: Dict[str, Any]) -> ObjectiveCard:
    """生 dict を ObjectiveCard へ正規化する（欠損は空で補完）。"""
    # 各項目は _as_str / _as_str_list を通し、型揺れ（文字列 or 配列）を吸収する。
    # 説明配列は _get_explanation で命名揺れ（*_explanation / *_root_explanation）を吸収する。
    return ObjectiveCard(
        target_user=_as_str(card.get("target_user")),
        problem=_as_str(card.get("problem")),
        contradiction=_as_str(card.get("contradiction")),
        objective=_as_str(card.get("objective")),
        value=_as_str(card.get("value")),
        background=_as_str(card.get("background")),
        extras=_as_str_list(card.get("extras")),
        target_user_explanation=_get_explanation(card, "target_user"),
        problem_explanation=_get_explanation(card, "problem"),
        contradiction_explanation=_get_explanation(card, "contradiction"),
        objective_explanation=_get_explanation(card, "objective"),
        value_explanation=_get_explanation(card, "value"),
    )


def _to_record(obj: Dict[str, Any]) -> IRRecord:
    """IR ファイル内の生 dict 1 件を IRRecord へ変換する。

    objective_card の位置を探索してから正規化し、元の生 dict も raw に保持する
    （後続工程で今回使っていない項目が必要になったときに参照できるようにするため）。
    """
    card_dict = _find_objective_card(obj)
    return IRRecord(
        session_id=_as_str(obj.get("session_id")),
        stage=_as_str(obj.get("stage")),
        revision=obj.get("revision"),
        objective_card=normalize_objective_card(card_dict),
        raw=obj,
    )


# --------------------------------------------------------------------------- #
# 公開 API
# --------------------------------------------------------------------------- #
def load_ir_records(path: str) -> List[IRRecord]:
    """IR ファイルを読み込み、IRRecord のリストを返す。"""
    objs = _load_json_objects(path)
    return [_to_record(o) for o in objs]


def select_ir_record(records: List[IRRecord]) -> IRRecord:
    """採用する IR を 1 件選ぶ。

    1. stage == "evidence" があれば最優先（複数あれば revision / 情報量で選ぶ）。
    2. 無ければ info_score（情報量）最大の IR を採用する。
    """
    if not records:
        raise IRLoadError("読み込んだ IR レコードが 0 件でした。")

    # 根拠付与済み（stage=evidence）の IR は発散の入力として最も適するため優先する
    evidence = [r for r in records if r.stage.lower() == "evidence"]
    if evidence:
        # revision が数値なら大きい方、なければ情報量で
        def _key(r: IRRecord):
            """並べ替えキー: (revision, 情報量)。revision が数値でなければ -1 とする。"""
            rev = r.revision if isinstance(r.revision, (int, float)) else -1
            return (rev, r.info_score())
        return max(evidence, key=_key)

    # evidence が無い → 最も情報量の多い IR
    return max(records, key=lambda r: r.info_score())


def load_and_select(path: str) -> IRRecord:
    """読み込み + 選定をまとめて行う便宜関数。"""
    records = load_ir_records(path)
    selected = select_ir_record(records)
    return selected


if __name__ == "__main__":
    # 単体動作確認用（python src/ir_loader.py data/IR_1215.jsonl）
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "data/IR_1215.jsonl"
    rec = load_and_select(target)
    print(f"session_id : {rec.session_id}")
    print(f"stage      : {rec.stage}")
    print(f"revision   : {rec.revision}")
    print("--- objective_card summary ---")
    for k, v in rec.objective_card.summary_dict().items():
        print(f"[{k}] {v}")
    print("--- explanation counts ---")
    for k, v in rec.objective_card.all_explanations().items():
        print(f"{k}: {len(v)} 件")
