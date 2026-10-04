"""
keyword_generator.py
=====================
IR（ObjectiveCard）から検索・発散用のキーワード候補を生成する。

* 日本語キーワードと英語キーワードの両方を生成する
* LLM が使える場合は LLM、使えない場合はルールベースで生成する
* CiNii Research で検索しやすいように、IR内の自然な表現を
  学術論文で使われやすい語・一般概念語へ変換する

返り値の形:
    {"ja": [...], "en": [...]}
"""

from __future__ import annotations

import re
from typing import Dict, List

from config import AppConfig
from models import ObjectiveCard
from llm_client import chat_json, LLMError


# ルールベース抽出で使う簡易ストップワード（助詞・記号など）
_JA_STOP = set("をにはがのへとでもやか、。・（）「」『』！？…の")
_PUNCT_RE = re.compile(r"[、。，．・（）\(\)「」『』【】\[\]\"'!?！？:：;；/／\\]+")


# IR ドメイン（家事・育児・調理・時間管理）から、英語キーワードへの対応辞書。
# ルールベース時に「日本語の語が本文に出現したら、対応する英語も採用する」ために使う。
_JA_EN_HINTS = {
    "家事": "household chores",
    "育児": "childcare",
    "子育て": "parenting",
    "調理": "cooking",
    "料理": "meal preparation",
    "献立": "meal planning",
    "栄養": "nutrition",
    "時間": "time management",
    "段取り": "task scheduling",
    "片付け": "kitchen cleanup",
    "通知": "notification",
    "共有": "sharing",
    "協働": "collaboration",
    "可視化": "visualization",
    "習慣": "habit formation",
    "行動変容": "behavior change",
    "ストレス": "stress reduction",
    "負担": "workload reduction",
    "冷凍": "meal prep freezing",
    "レシピ": "recipe recommendation",
    "推薦": "recommendation system",
    "保育園": "daycare",
    "夕食": "dinner routine",
}


# IR中の自然な表現を、CiNii検索向けの学術語・一般概念語へ変換する辞書
# 例:「自炊負担」のような生活語のままでは論文がヒットしにくいため、
#     「家事負担」「調理負担」など論文で使われる語へ言い換える。
_CINII_TERM_MAP = {
    "自炊": ["食生活", "調理", "家庭料理"],
    "自炊負担": ["家事負担", "調理負担", "食生活"],
    "家事": ["家事労働", "家事負担", "家事分担"],
    "家事負担": ["家事労働", "家事分担", "生活時間"],
    "家事の効率化": ["家事労働", "家事時間", "生活時間"],
    "夕食": ["夕食 調理 時間", "食生活", "調理時間"],
    "夕食準備": ["夕食 調理 時間", "食生活 簡便化", "調理時間"],
    "平日夕方": ["生活時間", "家事時間", "時間使用"],
    "子ども": ["育児", "子育て", "親子関係"],
    "保育園児": ["育児", "子育て支援", "就労母親"],
    "親子": ["親子関係", "親子コミュニケーション", "育児"],
    "親子の情緒": ["親子関係", "親子コミュニケーション", "育児支援"],
    "情緒的関わり": ["親子関係", "親子コミュニケーション", "育児支援"],
    "罪悪感": ["育児不安", "母親ストレス", "心理的負担"],
    "疲労感": ["母親ストレス", "育児ストレス", "心理的負担"],
    "フルタイム": ["就労母親", "共働き", "ワークライフバランス"],
    "共働き": ["共働き 家事分担", "共働き 育児", "ワークライフバランス"],
    "経営コンサルタント": ["就労母親", "フルタイム勤務", "ワークライフバランス"],
    "栄養": ["栄養バランス", "食生活", "献立"],
    "栄養価": ["栄養バランス", "食生活", "献立"],
    "手作り": ["家庭料理", "食生活", "調理"],
    "片付け": ["家事労働", "食器洗い", "家事時間"],
    "時間管理": ["時間使用", "生活時間", "家事時間"],
    "可処分時間": ["生活時間", "時間使用", "ワークライフバランス"],
}


def _to_cinii_academic_terms(text: str) -> List[str]:
    """IR中の表現をCiNii検索向けの学術語・一般概念語へ変換する。

    アルゴリズム:
        1. _CINII_TERM_MAP のキーが IR 本文に含まれていれば、対応する学術語を採用する
        2. さらに、話題（育児/食事/家事）ごとに検索語を補助的に足す
        3. 重複を除いて返す（出現順を保つため set は使わない）
    ※ この辞書は本 IR のドメインに特化しているため、汎用性の面では弱点になる。
       将来は LLM による変換（_llm_keywords）を主機能とし、本関数は fallback に留める。
    """
    terms: List[str] = []

    # 1. 辞書に載っている表現が本文にあれば、その学術語を採用する
    for key, mapped_terms in _CINII_TERM_MAP.items():
        if key in text:
            terms.extend(mapped_terms)

    # 汎用的に強い検索語を補助的に入れる
    # 2. 辞書に直接載っていなくても、話題が分かる語があれば定番の検索語を足す
    if "母親" in text or "子ども" in text or "保育園" in text:
        terms.extend(["就労母親", "育児 家事負担", "子育て世帯 生活時間"])

    if "夕食" in text or "調理" in text or "食事" in text:
        terms.extend(["夕食 調理 時間", "食生活 簡便化", "献立 栄養バランス"])

    if "家事" in text or "片付け" in text:
        terms.extend(["共働き 家事分担", "家事時間", "家事労働"])

    # 重複削除
    # 3. 出現順を保ったまま重複を除く（先に出た語ほど検索で優先されるため）
    unique_terms: List[str] = []
    for t in terms:
        if t and t not in unique_terms:
            unique_terms.append(t)

    return unique_terms


def _rule_based_keywords(card: ObjectiveCard) -> Dict[str, List[str]]:
    """IR テキストからCiNii向けキーワードを抽出する。

    LLM が使えないときの代替。IR の全項目を 1 つの文字列に連結し、
    (1) 学術語への変換 → (2) 辞書ヒント語 → (3) 記号分割による断片、
    の順に候補を積み、上位 10 件を採用する（前段ほど検索に有効な語）。
    """
    text = " ".join(
        [
            card.target_user,
            card.problem,
            card.contradiction,
            card.objective,
            card.value,
            card.background,
            " ".join(card.extras),
        ]
    )

    ja: List[str] = []

    # 1. CiNii検索向けの学術語・一般概念語を優先
    ja.extend(_to_cinii_academic_terms(text))

    # 2. 辞書ヒント語の出現も補助的に拾う
    for word in _JA_EN_HINTS:
        if word in text:
            ja.append(word)

    # 3. ヒットが少なければ、記号で粗く分割した名詞っぽい断片も補う
    #    （辞書に無い領域の IR でも、最低限の検索語を確保するための保険）
    if len(ja) < 5:
        chunks = _PUNCT_RE.split(text)
        for ch in chunks:
            ch = ch.strip()
            if 2 <= len(ch) <= 10 and re.search(r"[一-龠ぁ-んァ-ン]", ch):
                if ch not in ja and ch not in _JA_STOP:
                    ja.append(ch)
            if len(ja) >= 10:
                break

    # 英語: 日本語ヒント語に対応する英語を採用
    en: List[str] = []
    for word, eng in _JA_EN_HINTS.items():
        if word in text and eng not in en:
            en.append(eng)

    # CiNii向けの日本語が主なので、英語はScopus/特許DB向け補助として使う
    # 英語が 1 つも取れなかった場合の最低限の既定語
    if not en:
        en = ["work-life balance", "household labor", "childcare burden"]

    # 重複除去・件数調整
    # dict.fromkeys は挿入順を保つため、優先順位を崩さずに重複だけ落とせる
    ja = list(dict.fromkeys([x for x in ja if x]))[:10]
    en = list(dict.fromkeys([x for x in en if x]))[:10]

    return {"ja": ja, "en": en}


def _llm_keywords(card: ObjectiveCard, client, config: AppConfig) -> Dict[str, List[str]]:
    """LLM でキーワードを生成する。

    IR の自然文をそのまま検索語にすると論文がヒットしないため、
    プロンプトで「学術語へ言い換える」ことを変換例つきで具体的に指示する。
    生成結果が自然文寄りだった場合に備え、最後にルールベースの学術語で補強する。
    """
    system = (
        "あなたは要求工学と文献調査に詳しいリサーチャーです。"
        "与えられたIR（利用者の問題意識）から、CiNii Research・Scopus・特許DBを検索する際に"
        "有効なキーワードを、日本語と英語の両方で抽出します。"
        "特に日本語キーワードはCiNii Researchで論文検索しやすい語に変換してください。"
        "出力はJSONのみとします。"
    )

    user = f"""# IR 要約
- 対象ユーザー: {card.target_user}
- 課題: {card.problem}
- 矛盾: {card.contradiction}
- 目的: {card.objective}
- 提供価値: {card.value}
- 背景: {card.background}

# 指示
上記IRの検索・発散に使えるキーワードを抽出してください。

重要：
- IRの表現をそのまま検索語にしないでください。
- CiNii Researchで論文検索しやすいように，学術論文で使われやすい一般概念語・研究分野語へ変換してください。
- 「自炊負担」「親子の情緒的関わり」「罪悪感の軽減」のような自然文寄りの語だけにしないでください。
- 日本語キーワードは，CiNiiでヒットしやすい2〜3語の組み合わせを優先してください。
- 英語キーワードは，Scopusや特許DBでも利用できるように一般的な研究語にしてください。
- 一般的すぎる語（例：アプリ，システム，支援，AI）だけにならないようにしてください。
- IRの課題・目的・矛盾に直結する語を優先してください。

# 変換例
- 自炊負担 → 家事負担，調理負担，食生活
- 親子の情緒的関わり → 親子関係，親子コミュニケーション，育児支援
- 平日夕方の過ごし方 → 家事時間，生活時間，時間使用
- 罪悪感の軽減 → 育児不安，母親ストレス，心理的負担
- フルタイム勤務 → 共働き，就労母親，ワークライフバランス
- 栄養価の高い食事 → 食生活，栄養バランス，献立
- 子どもとのコミュニケーション → 親子コミュニケーション，家庭教育，育児支援

# 日本語キーワードの望ましい例
- 共働き 家事分担
- 育児 家事負担
- 夕食 調理 時間
- ワークライフバランス 育児
- 子育て世帯 食生活
- 就労母親 家事時間
- 食生活 簡便化
- 親子関係
- 母親ストレス
- 栄養バランス

# 出力条件
- 日本語 8〜10 個
- 英語 5〜10 個
- 日本語はCiNii Research向け，英語はScopus・特許DB向けとする

# 出力スキーマ（このJSONのみを出力）
{{"ja": ["..."], "en": ["..."]}}"""

    data = chat_json(client, config, system, user)

    # 文字列以外や空文字が混ざっても落ちないよう、ここで型と空白を正規化する
    ja = [str(x).strip() for x in data.get("ja", []) if str(x).strip()]
    en = [str(x).strip() for x in data.get("en", []) if str(x).strip()]

    if not ja and not en:
        raise LLMError("キーワードが空でした。")

    # LLMがまだ自然文寄りの語を出した場合に，ルールベースのCiNii語で補強する
    text = " ".join(
        [
            card.target_user,
            card.problem,
            card.contradiction,
            card.objective,
            card.value,
            card.background,
            " ".join(card.extras),
        ]
    )
    cinii_terms = _to_cinii_academic_terms(text)

    # 先頭はLLM出力，足りない分を辞書変換で補う
    # （dict.fromkeys で順序を保ったまま重複を除き、上位 10 件に切る）
    ja = list(dict.fromkeys(ja + cinii_terms))[:10]
    en = list(dict.fromkeys(en))[:10]

    return {"ja": ja, "en": en}


def generate_keywords(
    card: ObjectiveCard,
    client=None,
    config: AppConfig = None
) -> Dict[str, List[str]]:
    """IR からキーワードを生成する。LLM 利用可なら LLM、失敗時はルールベース。"""
    if client is not None and config is not None and config.use_llm:
        try:
            return _llm_keywords(card, client, config)
        except LLMError:
            # LLM 失敗時はルールベースへ静かにフォールバック
            pass

    return _rule_based_keywords(card)
