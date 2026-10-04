"""
main.py
=======
発散モジュールのエントリポイント（CLI）。

処理の流れ（問題分割AIエージェントを追加した現行版）:
    1. IR を読み込む（ir_loader）
    2. stage=evidence の IR を選び objective_card を抽出する
    3. 【条件 C のみ】IR を複数の部分問題へ分割する（problem_decomposer）
       → 分割は 1 通りではないため、観点の異なる複数パターンを生成する
       → --decomposition-json 指定時は分割を実行せず、保存済みの結果を読み込む
    3b. 【条件 C のみ】問題分割の結果を JSON / Markdown で保存し（output_writer）、
        後続へ渡す分割パターンを 1 つ選ぶ（pattern_selector）
    4. 検索・発散キーワードを生成し、JSON / Markdown で保存する
        - 条件 B: IR 全体から生成する（keyword_generator）
        - 条件 C: 選んだパターンの部分問題ごとに生成する（subproblem_keyword_generator）
    5. 外部情報源を用意する
        - --fetch csv  : 手動 CSV を読み込む（source_loader）
        - --fetch cinii: CiNii Research API で自動検索する（source_fetcher）
    6. 外部情報源からアイデアの種を抽出する（idea_seed_extractor）
    7. IR とアイデアの種から企画案を 3〜5 件生成する（idea_generator）
       各案には対応 IR 項目と参照外部情報源を付与する（idea_generator 内で実施）
    8. 企画案を JSON と Markdown で保存する（output_writer）

注意:
    問題分割と部分問題ごとの検索語生成は、ルールベースの代替を持たない。
    条件 C で LLM が使えない場合は明示的にエラー終了する（--no-llm では通らない）。
    条件 B は従来どおり、LLM が無ければルールベースで最後まで実行する。

コマンド例:
    # 手動 CSV モード（従来）
    python src/main.py --ir data/IR_1215.jsonl --sources data/sources.csv \
        --source-type CiNii --num-ideas 5

    # 保存済みの分割結果を使い、2 番目のパターンで条件 C を実行する
    python src/main.py --decomposition-json outputs/problem_decomposition.json \
        --pattern-id DP-002

    # 条件 B（IR 全体から検索語を生成。問題分割は行わない）
    python src/main.py --condition B

    # CiNii 自動検索モード（.env に CINII_APP_ID が必要）
    python src/main.py --ir data/IR_1215.jsonl --fetch cinii \
        --cinii-queries 4 --cinii-count 10 --max-sources 12 --num-ideas 5
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys

# src/ をスクリプトとして直接実行する場合に、同階層モジュールを import できるよう調整
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config                       # noqa: E402
from ir_loader import load_ir_records, select_ir_record, IRLoadError  # noqa: E402
from source_loader import load_sources, SourceLoadError              # noqa: E402
from keyword_generator import generate_keywords      # noqa: E402
from subproblem_keyword_generator import (            # noqa: E402
    generate_subproblem_keywords, SubProblemKeywordError,
)
from pattern_selector import select_pattern, PatternSelectionError  # noqa: E402
from idea_seed_extractor import extract_idea_seeds    # noqa: E402
from idea_generator import generate_ideas             # noqa: E402
from output_writer import (                           # noqa: E402
    write_outputs, write_decomposition, write_keyword_plan,
)
from models import GeneratedIdeas, KeywordPlan         # noqa: E402
from llm_client import build_client, LLMError          # noqa: E402
from problem_decomposer import (                       # noqa: E402
    decompose_problem, load_decomposition_json, ProblemDecompositionError,
)


def parse_args(argv=None) -> argparse.Namespace:
    """コマンドライン引数を解析して Namespace として返す。

    引数は用途ごとに 4 群に分かれる:
        * 入出力・LLM 有無（--ir / --out-* / --no-llm）
        * 評価実験の条件と分割パターン（--condition / --pattern-id / --decomposition-json）
        * 外部情報源の取得方法（--fetch と、CSV モード / CiNii モードの各設定）
        * 生成条件（--num-ideas）
    既定値だけで data/IR_1215.jsonl と data/sources.csv を使って動くようにしている。
    """
    p = argparse.ArgumentParser(
        description="IR と外部情報源からソフトウェア企画案を発散生成するモジュール"
    )
    p.add_argument("--ir", default="data/IR_1215.jsonl", help="IR ファイル(JSONL/JSON)パス")
    p.add_argument("--num-ideas", type=int, default=5, help="生成する企画案数（3〜5）")
    p.add_argument("--out-json", default="outputs/generated_ideas.json")
    p.add_argument("--out-md", default="outputs/generated_ideas.md")
    # 問題分割の結果は、企画案とは別ファイルに出す（分割の過程を単独で確認できるように）
    p.add_argument(
        "--out-decomposition-json",
        default="outputs/problem_decomposition.json",
        help="問題分割結果のJSON出力先",
    )
    p.add_argument(
        "--out-decomposition-md",
        default="outputs/problem_decomposition.md",
        help="問題分割結果のMarkdown出力先",
    )
    p.add_argument(
        "--out-keywords-json",
        default="outputs/keyword_plan.json",
        help="検索語生成結果のJSON出力先",
    )
    p.add_argument(
        "--out-keywords-md",
        default="outputs/keyword_plan.md",
        help="検索語生成結果のMarkdown出力先",
    )
    p.add_argument("--no-llm", action="store_true",
                   help="API キーがあっても LLM を使わずルールベースで生成する（条件 B のみ）")

    # 評価実験の条件と、条件 C で使う分割パターン
    p.add_argument("--condition", choices=["B", "C"], default="C",
                   help="B=IR全体から検索語を生成 / C=問題分割し部分問題ごとに検索語を生成")
    p.add_argument("--pattern-id", default=None,
                   help="条件Cで使う分割パターンのID（例: DP-002）。省略時は先頭のパターン")
    p.add_argument("--decomposition-json", default=None,
                   help="条件Cで、問題分割を実行せずに読み込む保存済みの分割結果JSON")

    # 外部情報源の取得方法
    p.add_argument("--fetch", choices=["csv", "cinii"], default="csv",
                   help="情報源の取得方法。csv=手動CSV / cinii=CiNii Research自動検索")

    # CSV モード
    p.add_argument("--sources", default="data/sources.csv", help="外部情報源 CSV パス")
    p.add_argument("--source-type", default=None,
                   help="CSVモードでの絞り込み（CiNii / Scopus / Patent）。省略時は全件")

    # CiNii 自動検索モード
    p.add_argument("--cinii-queries", type=int, default=4,
                   help="検索に使うキーワード数（1語ずつ検索してマージ）")
    p.add_argument("--cinii-count", type=int, default=10,
                   help="1キーワードあたりの取得件数（1〜200）")
    p.add_argument("--max-sources", type=int, default=12,
                   help="マージ後に採用する情報源の最大件数")
    p.add_argument("--from-year", default=None, help="出版年の下限（YYYY / YYYYMM）")
    p.add_argument("--until-year", default=None, help="出版年の上限（YYYY / YYYYMM）")
    p.add_argument("--no-cache", action="store_true", help="CiNii 取得のキャッシュを使わない")
    p.add_argument("--save-fetched", default="data/sources_fetched.csv",
                   help="CiNii 取得結果を保存する CSV パス")
    args = p.parse_args(argv)

    # 条件 B は問題分割を使わないため、分割に関する指定は黙って無視せずエラーにする
    if args.condition == "B" and (args.pattern_id or args.decomposition_json):
        p.error("--pattern-id / --decomposition-json は --condition C のときだけ指定できます。")
    return args


def main(argv=None) -> int:
    """発散モジュールを 1 回実行する（CLI 本体）。

    IR の読み込みから、問題分割・キーワード生成・文献取得・種抽出・企画案生成・
    出力までを順に呼び出す。各工程は失敗しても原因が分かるようにエラーを個別に
    捕捉し、正常終了なら 0、失敗なら 1 を返す（シェルから成否を判定できる）。
    """
    args = parse_args(argv)

    # 設定読み込み
    config = load_config()
    if args.no_llm:
        config.api_key = None  # use_llm を False にする

    # LLM クライアント（API キーが無ければ None）
    # ここで作った client を各エージェントへ渡す。None の場合、LLM を使う工程は
    # ルールベースへフォールバックする（ただし問題分割だけは代替を持たない）。
    client = None
    if config.use_llm:
        try:
            client = build_client(config)
            print(f"[info] LLM 使用: model={config.model}"
                  + (f", base_url={config.base_url}" if config.base_url else ""))
        except LLMError as exc:
            print(f"[警告] LLM クライアント生成に失敗: {exc}\n        → ルールベースで継続します。")
            config.api_key = None
    else:
        print("[info] API キー未設定（または --no-llm）。ルールベースで生成します。")

    # 1〜2. IR 読み込み・選定
    # 複数の IR が入っていても、stage=evidence（根拠付与済み）を優先して 1 件に絞る。
    try:
        records = load_ir_records(args.ir)
        ir = select_ir_record(records)
    except IRLoadError as exc:
        print(f"[エラー] IR 読み込み失敗:\n{exc}", file=sys.stderr)
        return 1
    print(f"[info] IR 選定: session_id={ir.session_id}, stage={ir.stage} "
          f"(全 {len(records)} 件中)")
    card = ir.objective_card

    print(f"[info] 実験条件: {args.condition}")

    # 3〜4. 検索語生成
    # 条件 B は IR 全体から、条件 C は問題分割の部分問題ごとに検索語を作る。
    # どちらも KeywordPlan にまとめ、検索語と部分問題の対応を保存する。
    decomposition = None
    pattern = None
    if args.condition == "C":
        # 3. 問題分割
        # 検索語をいきなり作らず、まず IR を複数パターンの部分問題へ分割する。
        # 問題分割は AI エージェントの役割のため、LLM が無い場合はここで停止する。
        # 保存済みの分割結果が指定された場合は分割を実行せず、それを使う
        # （同じ分割で後続工程を繰り返し実行できるようにするため）。
        try:
            if args.decomposition_json:
                decomposition = load_decomposition_json(args.decomposition_json)
                decomposition_source = args.decomposition_json
            else:
                decomposition = decompose_problem(
                    card,
                    client=client,
                    config=config,
                    num_patterns=3,
                )
                decomposition_source = "generated"
        except ProblemDecompositionError as exc:
            print(f"[エラー] 問題分割失敗:\n{exc}", file=sys.stderr)
            return 1

        print(
            f"[info] 問題分割: {len(decomposition.patterns)} パターン "
            f"(mode={decomposition.generator_mode}, 出どころ={decomposition_source})"
        )

        # 分割パターンの一覧を表示し、試行錯誤の過程を追えるようにする
        for dp in decomposition.patterns:
            print(f"  - {dp.pattern_id}: {dp.pattern_name} ({len(dp.subproblems)} subproblems)")

        # 3b. 問題分割結果の保存
        # 企画案とは別ファイルに残し、分割の妥当性を単独でレビューできるようにする。
        # 読み込んだ場合も、この実行で使った分割として outputs/ に残す。
        write_decomposition(
            decomposition,
            json_path=args.out_decomposition_json,
            md_path=args.out_decomposition_md,
        )
        print(
            f"[info] 問題分割結果を保存: "
            f"{args.out_decomposition_json}, {args.out_decomposition_md}"
        )

        # 後続へ渡す分割パターンを 1 つ選ぶ（作業 2 で選択機能に置き換える予定）
        try:
            pattern = select_pattern(decomposition, args.pattern_id)
        except PatternSelectionError as exc:
            print(f"[エラー] 分割パターンの選択に失敗:\n{exc}", file=sys.stderr)
            return 1
        print(f"[info] 分割パターン選択: {pattern.pattern_id} ({pattern.pattern_name})")

        # CiNii 検索は先頭から --cinii-queries 語しか使わないため、
        # 部分問題の数より少ないと、検索されない部分問題が出る
        if args.fetch == "cinii" and args.cinii_queries < len(pattern.subproblems):
            print(
                f"[警告] --cinii-queries ({args.cinii_queries}) が部分問題の数 "
                f"({len(pattern.subproblems)}) より少ないため、"
                "検索語が使われない部分問題が出ます。"
            )

        # 4. 部分問題ごとのキーワード生成（ルールベースの代替は持たない）
        try:
            plan = generate_subproblem_keywords(
                card, pattern, client=client, config=config,
                decomposition_source=decomposition_source,
            )
        except SubProblemKeywordError as exc:
            print(f"[エラー] 部分問題ごとの検索語生成に失敗:\n{exc}", file=sys.stderr)
            return 1
        for k in plan.subproblem_keywords:
            print(f"[info] 検索語 {k.subproblem_id}: ja={k.ja} / en={k.en}")
    else:
        # 4. IR 全体からのキーワード生成（LLM が無ければルールベース）
        plan = KeywordPlan(
            condition="B",
            ir_keywords=generate_keywords(card, client=client, config=config),
        )

    # 検索語と部分問題の対応を保存する（後から検索経路をたどれるように）
    write_keyword_plan(plan, args.out_keywords_json, args.out_keywords_md, pattern=pattern)
    print(f"[info] 検索語生成結果を保存: {args.out_keywords_json}, {args.out_keywords_md}")

    # 後続工程へは従来どおりの {"ja": [...], "en": [...]} 形式で渡す
    keywords = plan.merged()
    print(f"[info] キーワード: ja={len(keywords['ja'])}件, en={len(keywords['en'])}件 "
          f"/ ja={keywords['ja']}")

    # 5. 外部情報源の用意
    # --fetch の値で「手動 CSV」と「CiNii 自動検索」を切り替える。
    # どちらの経路でも共通スキーマ Source のリストになるため、以降の処理は同一。
    if args.fetch == "cinii":
        try:
            from source_fetcher import fetch_cinii_sources, save_sources_to_csv, CiNiiFetchError
            sources = fetch_cinii_sources(
                keywords, appid=config.cinii_app_id,
                num_queries=args.cinii_queries, per_query_count=args.cinii_count,
                max_sources=args.max_sources, use_cache=not args.no_cache,
                from_year=args.from_year, until_year=args.until_year,
            )
        except CiNiiFetchError as exc:
            print(f"[エラー] CiNii 自動検索に失敗:\n{exc}", file=sys.stderr)
            return 1
        # 1 件も取れない場合、後続の種抽出・企画生成が成立しないので停止する
        if not sources:
            print("[エラー] CiNii から該当論文を取得できませんでした。"
                  "キーワードや年範囲を見直してください。", file=sys.stderr)
            return 1
        # 取得結果を CSV 保存（中身を確認・再現できるように）
        try:
            save_sources_to_csv(sources, args.save_fetched)
            print(f"[info] CiNii 取得結果を保存: {args.save_fetched}")
        except OSError as exc:
            # 保存に失敗しても、取得済みの情報源で発散は続行できるため警告に留める
            print(f"[警告] 取得結果 CSV の保存に失敗: {exc}")
        used_source_type = "CiNii"
        print(f"[info] 外部情報源: {len(sources)} 件取得 (CiNii Research 自動検索)")
    else:
        try:
            sources = load_sources(args.sources, args.source_type)
        except SourceLoadError as exc:
            print(f"[エラー] 外部情報源 CSV 読み込み失敗:\n{exc}", file=sys.stderr)
            return 1
        used_source_type = args.source_type or ""
        print(f"[info] 外部情報源: {len(sources)} 件読み込み "
              f"(CSV, source_type={args.source_type or '全件'})")

    # 6. アイデアの種抽出
    # 文献の知見を「IR の解決に転用するヒント」へ変換する（企画案はまだ作らない）。
    seeds = extract_idea_seeds(sources, card, keywords=keywords,
                               client=client, config=config)
    print(f"[info] アイデアの種: {len(seeds)} 件抽出")

    # 7〜8. 企画案生成（対応 IR 項目・参照情報源を付与）
    # 種を材料に企画案を作る。mode には "llm" / "rule-based" のどちらで作ったかが入る。
    ideas, mode = generate_ideas(card, keywords, sources, seeds,
                                 num_ideas=args.num_ideas,
                                 client=client, config=config)
    print(f"[info] 企画案: {len(ideas)} 件生成 (mode={mode})")

    # 成果物の組み立て
    # 入力（IR 要約）・条件（キーワード / 情報源種別 / 生成モード）・結果（企画案）を
    # 1 つのオブジェクトにまとめ、後続工程が生成過程ごと参照できるようにする。
    generated = GeneratedIdeas(
        session_id=ir.session_id,
        generated_at=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        input_ir_summary=card.summary_dict(),
        ideas=ideas,
        condition=args.condition,
        keyword_plan=plan.to_dict(),
        decomposition=decomposition.to_dict() if decomposition is not None else {},
        keywords=keywords,
        used_source_type=used_source_type,
        generator_mode=mode,
    )

    # 9. 出力
    try:
        write_outputs(generated, args.out_json, args.out_md)
    except OSError as exc:
        print(f"[エラー] 出力の書き出しに失敗: {exc}", file=sys.stderr)
        return 1

    print(f"[完了] JSON: {args.out_json}")
    print(f"[完了] Markdown: {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
