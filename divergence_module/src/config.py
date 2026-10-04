"""
config.py
=========
.env から API キーや LLM 設定を読み込む。

OpenAI 互換 API を想定しているため、以下を環境変数で受け取る:
    OPENAI_API_KEY   : API キー（必須。無い場合はルールベースにフォールバック）
    OPENAI_BASE_URL  : 互換 API のエンドポイント（省略可。OpenAI 本家なら不要）
    OPENAI_MODEL     : 使用モデル名（省略時 gpt-4o-mini）
    LLM_TEMPERATURE  : 生成温度（省略時 0.7）

API キーが無くてもパイプライン自体は通せるよう、use_llm フラグで分岐する。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# python-dotenv は必須依存にしない。未インストール環境でも import エラーで
# 落ちないように、同名のダミー関数で置き換えてから読み込みを続行する。
try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv 未インストール時もクラッシュさせない
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        """python-dotenv が無い環境用の代替関数。

        .env を読み込まずに False を返すだけの何もしない関数。
        環境変数が直接設定されていれば、これでも設定読み込みは成立する。
        """
        return False


@dataclass
class AppConfig:
    """LLM 呼び出しと外部 API 利用に必要な設定値をまとめて保持する。

    このオブジェクト 1 つを各エージェント（キーワード生成・問題分割・種抽出・
    企画生成）へ渡すことで、設定の読み込み箇所を 1 か所に集約する。
    """

    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    # JSON 生成失敗時の再試行回数
    max_json_retries: int = 2
    # CiNii Research OpenSearch API のアプリケーションID（自動検索モードで必須）
    cinii_app_id: Optional[str] = None

    @property
    def use_llm(self) -> bool:
        """LLM を使える設定になっているかを返す。

        API キーの有無だけを判定条件とし、これが LLM パスとルールベースパスの
        唯一の分岐点になる（--no-llm 指定時は呼び出し側が api_key を None にする）。
        """
        return bool(self.api_key)


def load_config() -> AppConfig:
    """.env と環境変数を読み込み、AppConfig を組み立てて返す。

    アルゴリズム:
        1. .env を探索して環境変数へ流し込む（無くても続行）
        2. 文字列項目は空文字を None に正規化し「未設定」と区別できるようにする
        3. 数値項目は不正な文字列が入っていても落ちないよう、
           変換失敗時に既定値へフォールバックする
    """
    load_dotenv()  # カレントや上位の .env を探索

    # 文字列項目: 空文字は None に落として「未設定」と同じ扱いにする
    api_key = os.getenv("OPENAI_API_KEY") or None
    base_url = os.getenv("OPENAI_BASE_URL") or None
    model = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

    # 数値項目: .env の記述ミス（数値でない値）でも例外を出さず既定値に戻す
    try:
        temperature = float(os.getenv("LLM_TEMPERATURE", "0.7"))
    except ValueError:
        temperature = 0.7

    try:
        max_json_retries = int(os.getenv("LLM_MAX_JSON_RETRIES", "2"))
    except ValueError:
        max_json_retries = 2

    return AppConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        max_json_retries=max_json_retries,
        cinii_app_id=os.getenv("CINII_APP_ID") or None,
    )
