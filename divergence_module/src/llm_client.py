"""
llm_client.py
=============
（指定構成への小さな追加モジュール）

OpenAI 互換 API の呼び出しと、LLM 出力からの堅牢な JSON 抽出を 1 か所に集約する。
keyword_generator / idea_seed_extractor / idea_generator から再利用される。

* APIキーが無い場合は LLM を呼ばない（呼び出し側でルールベースへフォールバック）
* JSON 出力はコードフェンス除去・最外括弧抽出・再試行で落ちにくくする
"""

from __future__ import annotations

import json
import re
from typing import Any, List, Optional

from config import AppConfig


class LLMError(Exception):
    """LLM 呼び出し / JSON パースに関する例外。"""


def build_client(config: AppConfig):
    """OpenAI 互換クライアントを生成する。API キーが無ければ None。

    base_url を設定に含めることで、OpenAI 本家だけでなく Amazon Bedrock などの
    OpenAI 互換エンドポイントへも、コードを変えずに接続先を切り替えられる。
    """
    # API キーが無い場合は LLM を使わない合図として None を返す
    if not config.use_llm:
        return None
    # openai パッケージは遅延 import（ルールベース運用時は不要なため）
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMError(
            "openai パッケージが見つかりません。`pip install -r requirements.txt` を実行してください。"
        ) from exc
    kwargs = {"api_key": config.api_key}
    # base_url を渡したときだけ互換エンドポイントへ接続する
    if config.base_url:
        kwargs["base_url"] = config.base_url
    return OpenAI(**kwargs)


def chat(client, config: AppConfig, system: str, user: str,
         force_json: bool = False) -> str:
    """1 往復のチャット補完を行い、本文テキストを返す。

    force_json=True のときは response_format で JSON 出力を強制するが、
    互換 API がこのパラメータに未対応の場合は例外になる。その場合は
    response_format を外して自動的に再試行する（互換性の確保）。
    """
    if client is None:
        raise LLMError("LLM クライアントが None です（API キー未設定）。")

    create_kwargs = {
        "model": config.model,
        "temperature": config.temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if force_json:
        # 対応モデルのみ有効。互換 API が未対応なら例外になるため握りつぶして再試行。
        create_kwargs["response_format"] = {"type": "json_object"}

    try:
        resp = client.chat.completions.create(**create_kwargs)
    except Exception as exc:  # noqa: BLE001
        if force_json:
            # response_format 非対応の可能性 → 外して再試行
            create_kwargs.pop("response_format", None)
            resp = client.chat.completions.create(**create_kwargs)
        else:
            raise LLMError(f"LLM 呼び出しに失敗しました: {exc}") from exc

    # content が None で返るモデルもあるため空文字に正規化する
    return resp.choices[0].message.content or ""


# --------------------------------------------------------------------------- #
# JSON 抽出
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _strip_fences(text: str) -> str:
    """LLM 出力から Markdown のコードフェンス（```json ... ```）を取り除く。

    「JSON のみ出力せよ」と指示しても ``` で囲んで返すモデルがあるため、
    フェンス内の中身だけを取り出す。フェンスが無ければ元のテキストを返す。
    """
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _extract_outermost(text: str, open_ch: str, close_ch: str) -> Optional[str]:
    """最初の open_ch から対応する close_ch までを抽出する。

    アルゴリズム: 括弧の深さを数えながら 1 文字ずつ走査し、深さが 0 に戻った
    位置を「最初の括弧に対応する閉じ括弧」とみなす。これにより、前後に説明文が
    付いていても JSON 本体だけを切り出せる（ネストした括弧にも対応）。
    """
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            # 深さが 0 に戻った時点が、最初の括弧に対応する閉じ括弧
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_json(text: str) -> Any:
    """LLM 出力テキストから JSON をできる限り頑健に取り出してパースする。

    アルゴリズム（3 段階で緩やかに救済する）:
        1. フェンス除去後の全文をそのまま json.loads する
        2. 失敗したら最外のオブジェクト {...} を切り出して再試行する
        3. それも失敗したら最外の配列 [...] を切り出して再試行する
    いずれも失敗した場合のみ LLMError を送出する（呼び出し側が再試行できる）。
    """
    cleaned = _strip_fences(text)
    # 1) そのまま
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # 2) 最外のオブジェクト {...}
    obj = _extract_outermost(cleaned, "{", "}")
    if obj:
        try:
            return json.loads(obj)
        except json.JSONDecodeError:
            pass
    # 3) 最外の配列 [...]
    arr = _extract_outermost(cleaned, "[", "]")
    if arr:
        try:
            return json.loads(arr)
        except json.JSONDecodeError:
            pass
    raise LLMError("LLM 出力から有効な JSON を抽出できませんでした。")


def chat_json(client, config: AppConfig, system: str, user: str) -> Any:
    """JSON を期待するチャット。失敗時は再試行する。

    アルゴリズム: chat() → parse_json() を最大 (max_json_retries + 1) 回試す。
    失敗するたびに user プロンプトへ「JSON のみを返せ」という追記を足して
    指示を強め、同じ失敗を繰り返しにくくする。全て失敗したら LLMError。
    """
    last_err: Optional[Exception] = None
    attempts = max(1, config.max_json_retries + 1)
    cur_user = user
    for attempt in range(attempts):
        try:
            text = chat(client, config, system, cur_user, force_json=True)
            return parse_json(text)
        except LLMError as exc:
            last_err = exc
            # 再試行時は「JSON のみで返す」よう強く促す
            cur_user = (
                user
                + "\n\n# 重要\n直前の出力は有効な JSON として解析できませんでした。"
                + "前置き・後置き・コードフェンス・説明文を一切付けず、"
                + "指定スキーマに厳密一致する JSON オブジェクトのみを出力してください。"
            )
    raise LLMError(f"JSON 生成に {attempts} 回失敗しました: {last_err}")
