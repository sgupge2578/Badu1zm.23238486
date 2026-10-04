"""
source_fetcher.py
=================
（自動検索モード用の追加モジュール）

CiNii Research の OpenSearch API から論文を検索し、本モジュールの共通スキーマ
``Source`` へ正規化する取得層。これにより source_loader 以降（種抽出・企画生成・
出力）は CSV でも API でも一切変更せずに「検索 → 発散」がつながる。

重要な前提（2026 年時点で公式仕様を確認済み）:
    * 旧「CiNii Articles」は 2022/04/18 に CiNii Research へ統合済み。
      現行の論文検索エンドポイントは:
        https://cir.nii.ac.jp/opensearch/articles
    * クエリパラメータ ``appid``（アプリケーションID）は**必須**。
      事前に NII の API 利用登録で取得する:
        https://support.nii.ac.jp/ja/cinii/api/developer
    * ``format=json`` で JSON-LD が返る。各 item の主なフィールド:
        title                : タイトル
        description          : 抄録（無い場合も多い）
        dc:subject           : キーワード（文字列 or 配列）
        dc:creator           : 著者（文字列 or 配列）
        link.@id / @id       : 詳細ページのパーマリンク（crid を含む）
        dc:identifier        : DOI 等（{"@type","@value"} の配列）

ネットワーク注意:
    本モジュールは requests を遅延 import する（CSV モードでは不要）。
    実通信できない環境では parse_cinii_response でモック JSON を渡して
    パーサだけを検証できる。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from models import Source


class CiNiiFetchError(Exception):
    """CiNii 取得に関する例外。"""


# v1 OpenSearch。論文検索の検索種別は "articles"。
CINII_OPENSEARCH_URL = "https://cir.nii.ac.jp/opensearch/articles"
_USER_AGENT = "divergence-module/0.1 (graduation-research prototype)"


# --------------------------------------------------------------------------- #
# 正規化ヘルパ
# --------------------------------------------------------------------------- #
def _as_list(value: Any) -> List[Any]:
    """値を必ずリストにそろえる。

    JSON-LD は、要素が 1 件のときは配列ではなく単一値で返ることがある
    （例: dc:subject が "通知" か ["家事","通知"] のどちらもあり得る）。
    後続処理を 1 本化するため、ここで必ずリスト形式へ寄せる。
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _text(value: Any) -> str:
    """JSON-LD の値（文字列 / {"@value":...} / 配列）から代表文字列を作る。"""
    # JSON-LD では同じ項目が「文字列」「{"@value": ...} の辞書」「その配列」の
    # いずれの形でも来るため、再帰的に文字列へ畳み込む。
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("@value", "")).strip()
    if isinstance(value, list):
        return " ".join(_text(v) for v in value if v is not None).strip()
    return str(value)


def _extract_crid(url: str) -> str:
    """CiNii のパーマリンク URL から crid（論文の一意 ID）を取り出す。

    URL 形式は https://cir.nii.ac.jp/crid/1390000000000000001 のため、
    "/crid/" の直後の英数字列を正規表現で抜き出す。取れなければ空文字。
    """
    m = re.search(r"/crid/(\w+)", url or "")
    return m.group(1) if m else ""


def _pick_url(item: Dict[str, Any]) -> str:
    """1 件の item から詳細ページの URL を取り出す。

    link が辞書（{"@id": ...}）の場合と文字列の場合があり、どちらも無ければ
    item 自身の @id を URL とみなす、という優先順で拾う。
    """
    link = item.get("link")
    if isinstance(link, dict) and link.get("@id"):
        return str(link["@id"])
    if isinstance(link, str) and link:
        return link
    return str(item.get("@id", ""))


def _keywords_str(item: Dict[str, Any]) -> str:
    """dc:subject（キーワード）を ";" 区切りの 1 つの文字列にまとめる。

    CSV（sources.csv）の keywords 列と同じ形式にそろえることで、
    CSV 由来の情報源と API 由来の情報源を後段が区別せず扱えるようにする。
    """
    subjects = _as_list(item.get("dc:subject"))
    parts = [_text(s) for s in subjects]
    return ";".join(p for p in parts if p)


def normalize_item(item: Dict[str, Any]) -> Source:
    """CiNii JSON-LD の 1 item を Source へ正規化する。"""
    # source_id は crid から作る（CIR + crid）。crid が取れない場合は URL で代用し、
    # 後段の重複除去（同一論文の判定）が成立するようにする。
    url = _pick_url(item)
    crid = _extract_crid(url) or _extract_crid(str(item.get("@id", "")))
    source_id = f"CIR{crid}" if crid else (url or "CIR-UNKNOWN")
    return Source(
        source_id=source_id,
        source_type="CiNii",  # 既存パイプラインの source_type 絞り込みに合わせる
        title=_text(item.get("title")),
        abstract=_text(item.get("description")),
        keywords=_keywords_str(item),
        url=url,
    )


def parse_cinii_response(data: Dict[str, Any]) -> List[Source]:
    """CiNii の JSON-LD レスポンス全体を Source のリストへ変換する。

    ネットワーク不要。モック JSON でのパーサ検証にも使える。
    """
    if not isinstance(data, dict):
        raise CiNiiFetchError("CiNii レスポンスが dict ではありません。")
    items = data.get("items")
    if items is None:
        return []
    # ヒット 1 件のとき items が配列でないことがあるためリストへそろえる
    if not isinstance(items, list):
        items = [items]
    sources: List[Source] = []
    for it in items:
        if isinstance(it, dict):
            src = normalize_item(it)
            if src.title:  # タイトルの無いものは除外
                sources.append(src)
    return sources


# --------------------------------------------------------------------------- #
# 通信
# --------------------------------------------------------------------------- #
def _request_json(params: Dict[str, Any], appid: str, timeout: int = 20) -> Dict[str, Any]:
    """OpenSearch へ GET し JSON-LD を返す。requests は遅延 import。"""
    try:
        import requests
    except ImportError as exc:
        raise CiNiiFetchError(
            "requests が必要です。`pip install -r requirements.txt` を実行してください。"
        ) from exc

    # appid（必須）と format=json は毎回自動で付与する
    q = {**params, "appid": appid, "format": "json"}
    try:
        resp = requests.get(
            CINII_OPENSEARCH_URL, params=q, timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise CiNiiFetchError(f"CiNii への通信に失敗しました: {exc}") from exc
    try:
        return resp.json()
    except ValueError as exc:
        # appid 不正時などは HTML が返ることがあるため、原因が分かる形で通知する
        raise CiNiiFetchError(
            "CiNii レスポンスを JSON として解釈できませんでした。"
            "appid が有効か、format=json が効いているか確認してください。"
        ) from exc


def fetch_articles(query: str, appid: str, count: int = 10, start: int = 1,
                   sortorder: int = 4, lang: str = "ja",
                   from_year: Optional[str] = None,
                   until_year: Optional[str] = None) -> List[Source]:
    """1 クエリ分を検索して Source のリストを返す。

    sortorder: 0=新しい順, 1=古い順, 4=関連度順(既定), 10=被引用順(論文のみ)
    """
    # count は API 上限（200）を超えないようにクランプする
    params: Dict[str, Any] = {
        "q": query,
        "count": max(1, min(count, 200)),
        "start": max(1, start),
        "sortorder": sortorder,
        "lang": lang,
    }
    # 年範囲は指定されたときだけ付ける（未指定なら全期間）
    if from_year:
        params["from"] = from_year
    if until_year:
        params["until"] = until_year
    data = _request_json(params, appid)
    return parse_cinii_response(data)


# --------------------------------------------------------------------------- #
# 複数キーワードでの取得 + キャッシュ + 重複除去
# --------------------------------------------------------------------------- #
def _cache_key(query: str, opts: Dict[str, Any]) -> str:
    """検索語と検索条件から、キャッシュファイル名用の短いハッシュを作る。

    同じ検索語でも条件（年範囲・並び順・件数）が違えば結果が変わるため、
    両方を含めた文字列の MD5 先頭 12 桁をキーにして取り違えを防ぐ。
    """
    raw = query + "|" + json.dumps(opts, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def fetch_cinii_sources(keywords: Dict[str, List[str]], appid: str,
                        num_queries: int = 4, per_query_count: int = 10,
                        max_sources: int = 12, sleep_sec: float = 1.0,
                        cache_dir: str = "data/cache", use_cache: bool = True,
                        sortorder: int = 4, lang: str = "ja",
                        from_year: Optional[str] = None,
                        until_year: Optional[str] = None) -> List[Source]:
    """IR 由来のキーワードで CiNii を複数回検索し、重複を除いて返す。

    CiNii の q は複数語が AND 結合のため、語を ANDで詰め込むと過剰に絞られる。
    そこで「上位キーワードを 1 語ずつ」検索して結果をマージし、再現率を確保する。
    """
    if not appid:
        raise CiNiiFetchError(
            "CiNii の appid が未設定です。.env に CINII_APP_ID を設定してください。\n"
            "  → 取得は NII の API 利用登録から: "
            "https://support.nii.ac.jp/ja/cinii/api/developer"
        )

    # 検索語の選定（日本語を優先し、英語も補う。重複除去）
    # CiNii は日本語文献が中心のため ja を先に並べ、上位 num_queries 語だけ使う。
    terms: List[str] = []
    for t in list(keywords.get("ja", [])) + list(keywords.get("en", [])):
        t = (t or "").strip()
        if t and t not in terms:
            terms.append(t)
    if num_queries > 0:
        terms = terms[:num_queries]
    if not terms:
        raise CiNiiFetchError("検索キーワードが空です。IR からキーワードを生成できませんでした。")

    os.makedirs(cache_dir, exist_ok=True)
    # キャッシュキーに含める検索条件（語が同じでも条件が違えば別キャッシュになる）
    search_opts = dict(sortorder=sortorder, lang=lang,
                       from_year=from_year, until_year=until_year,
                       count=per_query_count)

    seen_ids = set()   # 既に採用した source_id（crid）。同じ論文の二重採用を防ぐ
    sources: List[Source] = []

    # 1 語ずつ検索 → 正規化 → 重複除去しながらマージする
    for term in terms:
        key = _cache_key(term, search_opts)
        cache_path = os.path.join(cache_dir, f"cinii_{key}.json")

        # (a) キャッシュがあれば通信しない（再現性の確保とレート制限対策）
        data: Optional[Dict[str, Any]] = None
        if use_cache and os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                print(f"[info] CiNii キャッシュ使用: '{term}'")
            except (OSError, ValueError):
                # キャッシュが壊れていた場合は無視して通常の通信へ進む
                data = None

        # (b) キャッシュが無ければ実際に検索し、生レスポンスを保存する
        if data is None:
            try:
                params = {
                    "q": term, "count": per_query_count, "start": 1,
                    "sortorder": sortorder, "lang": lang,
                }
                if from_year:
                    params["from"] = from_year
                if until_year:
                    params["until"] = until_year
                data = _request_json(params, appid)
                # 生レスポンスをキャッシュ（再現性・レート制限対策）
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"[info] CiNii 検索: '{term}' → "
                      f"total={data.get('opensearch:totalResults', '?')}")
                time.sleep(sleep_sec)  # 礼儀としての待機
            except CiNiiFetchError as exc:
                # 1 語の失敗で全体を止めない（他の語の結果は活かす）
                print(f"[警告] '{term}' の検索に失敗（スキップ）: {exc}")
                continue

        # (c) 正規化して、未出の論文だけを採用する。上限に達したら即座に打ち切る
        for src in parse_cinii_response(data):
            if src.source_id in seen_ids:
                continue
            seen_ids.add(src.source_id)
            sources.append(src)
            if len(sources) >= max_sources:
                return sources

    return sources


# --------------------------------------------------------------------------- #
# 取得結果の CSV 保存（検証・再現用。sources.csv と同じスキーマ）
# --------------------------------------------------------------------------- #
def save_sources_to_csv(sources: List[Source], path: str) -> None:
    """取得した Source を sources.csv と同じ列構成で書き出す。

    自動検索で何を取ってきたかを人間が確認でき、かつ同じ CSV を
    --fetch csv モードで再入力できる（検証・再現用）。
    """
    # 出力先ディレクトリが無ければ作る
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    # 抄録中のカンマで列がずれないよう、csv モジュールに引用符処理を任せる
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(["source_id", "source_type", "title", "abstract", "keywords", "url"])
        for s in sources:
            w.writerow([s.source_id, s.source_type, s.title, s.abstract,
                        s.keywords, s.url])


if __name__ == "__main__":
    # パーサ単体検証（ネットワーク不要）
    sample = {
        "items": [
            {
                "@id": "https://cir.nii.ac.jp/crid/1390000000000000001",
                "title": "共働き家庭における家事時間の研究",
                "link": {"@id": "https://cir.nii.ac.jp/crid/1390000000000000001"},
                "dc:creator": ["山田 太郎", "鈴木 花子"],
                "description": "本研究は共働き家庭の家事時間配分を分析した。",
                "dc:subject": ["家事", "共働き", "時間配分"],
            },
            {
                "@id": "https://cir.nii.ac.jp/crid/1390000000000000002",
                "title": "通知による行動支援の検討",
                "dc:creator": "単独 著者",
                "description": "",
                "dc:subject": "通知",
            },
        ]
    }
    for s in parse_cinii_response(sample):
        print(s.source_id, "|", s.title, "|", s.keywords, "|", s.url)
