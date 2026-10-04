"""
idea_seed_extractor.py
=======================
外部情報源（文献・特許）から、企画に使えそうな「アイデアの種」を抽出する。

出力（IdeaSeed）には以下を含める:
    source_id, source_title, source_type, idea_seed, related_ir_item

related_ir_item は、その種が IR のどの項目（problem/contradiction/objective/value）
に最も関連しそうかを示す。後続の企画生成で「IR への対応付け」を助ける。

LLM が使える場合は LLM、使えない場合はルールベースで抽出する。
"""

from __future__ import annotations

from typing import List

from config import AppConfig
from models import IdeaSeed, ObjectiveCard, Source
from llm_client import chat_json, LLMError


# 種を紐づける先として許可する IR 項目（LLM が変な値を返した場合の検証にも使う）
_IR_ITEM_KEYS = ["problem", "contradiction", "objective", "value"]


def _guess_related_ir_item(source: Source, card: ObjectiveCard) -> str:
    """種が関連しそうな IR 項目を、語の重なりで素朴に推定する。

    アルゴリズム: IR の各項目テキストを 2 文字の n-gram に分解し、
    文献テキスト（title+abstract+keywords）に何個含まれるかを数える。
    最もスコアが高い項目を「関連する IR 項目」とみなす。
    形態素解析を使わない簡易版だが、日本語でも語の重なりを大まかに測れる。
    """
    text = source.search_text
    candidates = {
        "problem": card.problem,
        "contradiction": card.contradiction,
        "objective": card.objective,
        "value": card.value,
    }
    best_key = "problem"   # どれとも重ならない場合の既定値
    best_score = -1
    for key, ir_text in candidates.items():
        if not ir_text:
            continue
        # 2 文字以上の共通断片の数を素朴にスコア化
        score = sum(1 for frag in _ngrams(ir_text, 2) if frag in text)
        if score > best_score:
            best_score = score
            best_key = key
    return best_key


def _ngrams(text: str, n: int) -> List[str]:
    """文字列を n 文字ずつずらして切り出した n-gram のリストを返す。

    日本語は空白で単語分割できないため、形態素解析の代わりに
    文字 n-gram を使って語の重なりを測る（_guess_related_ir_item で使用）。
    """
    text = text.replace(" ", "")
    return [text[i : i + n] for i in range(max(0, len(text) - n + 1))]


def _rule_based_seed(source: Source, card: ObjectiveCard) -> IdeaSeed:
    """ルールベースでアイデアの種を作る（タイトル/キーワードを要約的に転記）。"""
    # LLM が使えないときの代替。文献のタイトル（無ければ抄録の冒頭）を核にして
    # 「IR の課題解決に転用する」という定型文へはめ込み、最低限の種を機械的に作る。
    base = source.title or (source.abstract[:40] if source.abstract else source.source_id)
    kw = source.keywords
    seed_text = (
        f"「{base}」の知見を、IR の課題解決に転用する。"
        + (f" 着目点: {kw}。" if kw else "")
    )
    return IdeaSeed(
        source_id=source.source_id,
        source_title=source.title,
        source_type=source.source_type,
        idea_seed=seed_text,
        related_ir_item=_guess_related_ir_item(source, card),
    )


def _llm_seeds(sources: List[Source], card: ObjectiveCard,
               keywords, client, config: AppConfig) -> List[IdeaSeed]:
    """LLM でアイデアの種をまとめて抽出する。

    アルゴリズム:
        1. IR・キーワード・全文献を 1 つのプロンプトに詰めて 1 回で問い合わせる
        2. 返ってきた種を source_id で元の文献に突き合わせる
           （存在しない source_id をでっち上げていたら捨てる）
        3. LLM が出し漏らした文献はルールベースの種で補完する
           → 文献の件数と種の件数が食い違わないようにする
    """
    system = (
        "あなたは要求工学とソフトウェア企画に詳しい研究者です。"
        "外部情報源（文献・特許）の知見を、IR（利用者の問題意識）に結びつく"
        "『アイデアの種』として再構成します。文献の内容をそのまま写すのではなく、"
        "IR の課題・目的・矛盾の解決に転用する観点で 1〜2 文に要約します。"
        "出力は JSON のみとします。"
    )

    # 文献一覧をプロンプト用のテキストブロックへ整形する
    src_lines = []
    for s in sources:
        src_lines.append(
            f"- source_id: {s.source_id}\n"
            f"  source_type: {s.source_type}\n"
            f"  title: {s.title}\n"
            f"  abstract: {s.abstract}\n"
            f"  keywords: {s.keywords}"
        )
    sources_block = "\n".join(src_lines)

    user = f"""# IR 要約
- 対象ユーザー: {card.target_user}
- 課題(problem): {card.problem}
- 矛盾(contradiction): {card.contradiction}
- 目的(objective): {card.objective}
- 提供価値(value): {card.value}

# 検索・発散キーワード
- 日本語: {", ".join(keywords.get("ja", []))}
- 英語: {", ".join(keywords.get("en", []))}

# 外部情報源一覧
{sources_block}

# 指示
各 source について「アイデアの種」を 1 件ずつ作ってください。
- idea_seed: 文献/特許の知見を IR 解決に転用する観点で 1〜2 文に要約（そのまま写さない）
- related_ir_item: problem / contradiction / objective / value のうち最も関連する 1 つ

# 出力スキーマ（この JSON のみ）
{{"seeds": [
  {{"source_id": "S001", "idea_seed": "...", "related_ir_item": "problem"}}
]}}"""

    data = chat_json(client, config, system, user)
    raw_seeds = data.get("seeds", []) if isinstance(data, dict) else []

    # source_id をキーに突き合わせ（LLM が落とした分はルールベースで補完）
    by_id = {s.source_id: s for s in sources}
    seeds: List[IdeaSeed] = []
    seen = set()
    for item in raw_seeds:
        sid = str(item.get("source_id", "")).strip()
        src = by_id.get(sid)
        # 実在しない source_id（LLM の捏造）はここで除外される
        if not src:
            continue
        # related_ir_item が想定外の値なら、語の重なりから推定し直す
        related = str(item.get("related_ir_item", "")).strip().lower()
        if related not in _IR_ITEM_KEYS:
            related = _guess_related_ir_item(src, card)
        seeds.append(
            IdeaSeed(
                source_id=src.source_id,
                source_title=src.title,
                source_type=src.source_type,
                # idea_seed が空で返ってきた場合もルールベースの文面で埋める
                idea_seed=str(item.get("idea_seed", "")).strip()
                or _rule_based_seed(src, card).idea_seed,
                related_ir_item=related,
            )
        )
        seen.add(sid)

    # LLM が触れなかった文献を取りこぼさないよう、残りをルールベースで補う
    for s in sources:
        if s.source_id not in seen:
            seeds.append(_rule_based_seed(s, card))

    if not seeds:
        raise LLMError("アイデアの種が空でした。")
    return seeds


def extract_idea_seeds(sources: List[Source], card: ObjectiveCard,
                       keywords=None, client=None,
                       config: AppConfig = None) -> List[IdeaSeed]:
    """外部情報源からアイデアの種を抽出する。

    LLM が使える場合は LLM で抽出し、API キーが無い・LLM が失敗した場合は
    ルールベース抽出へ静かにフォールバックする（縦切りを止めないため）。
    """
    keywords = keywords or {"ja": [], "en": []}
    if client is not None and config is not None and config.use_llm and sources:
        try:
            return _llm_seeds(sources, card, keywords, client, config)
        except LLMError:
            pass
    return [_rule_based_seed(s, card) for s in sources]
