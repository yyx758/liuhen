"""Blind human review sampling and judge-versus-human calibration."""

import argparse
import hashlib
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path

from backend.app.evals.judge import judge_material


REVIEWS_DIR = Path(__file__).resolve().parents[2] / "evals" / "reviews"
BUSINESS_SCORES = {0, 100}
REPLY_SCORES = {0, 33, 67, 100}


def review_id(result: dict) -> str:
    return f"{result['case_id']}:{result['trial']}:{result.get('variant') or 'default'}"


def report_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_report(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    results = report.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError("报告中没有可供抽样的 results")
    ids = [review_id(result) for result in results]
    if len(ids) != len(set(ids)):
        raise ValueError("报告中存在重复的 case_id/trial/variant，无法对应人工评分")
    if any("business_accuracy" not in r or "agent_reply_quality" not in r for r in results):
        raise ValueError("请先使用已完成双维度评分的测评报告")
    return report


def build_review_item(result: dict) -> dict:
    """Give the human reviewer evidence and rules, without either automatic verdict."""
    material = judge_material(result)
    material["ground_truth"]["audit_events"] = result.get("audit_events", [])
    return {
        "review_id": review_id(result),
        "case_id": result["case_id"],
        "trial": result["trial"],
        "variant": result.get("variant"),
        **material,
        "business_expectations": [
            check["item"] for check in result["business_accuracy"]["checks"]
        ],
        "human_business_score": None,
        "human_reply_score": None,
        "human_factual_error": None,
        "human_forbidden_hit": None,
        "human_reason": "",
    }


def select_indices(results: list[dict], rate: float, seed: int) -> list[int]:
    """Cover observed score bands and risky cases, then fill randomly."""
    if not 0 < rate <= 1:
        raise ValueError("抽样比例必须大于 0 且不超过 1，例如 0.2")
    count = min(len(results), math.ceil(len(results) * rate))
    rng = random.Random(seed)
    candidates = list(range(len(results)))
    rng.shuffle(candidates)
    selected: set[int] = set()

    def cover(predicate) -> None:
        if len(selected) >= count or any(predicate(results[i]) for i in selected):
            return
        for index in candidates:
            if predicate(results[index]):
                selected.add(index)
                return

    for score in (0, 33, 67, 100, None):
        cover(lambda r, score=score: r["agent_reply_quality"].get("score") == score)
    for score in (0, 100):
        cover(lambda r, score=score: r["business_accuracy"]["score"] == score)
    for variant in ("confirm_first", "cancel_first", "simultaneous"):
        cover(lambda r, variant=variant: r["case_id"] == "14" and r.get("variant") == variant)
    for case_id in ("11", "12", "13", "15"):
        cover(lambda r, case_id=case_id: r["case_id"] == case_id)

    for index in candidates:
        if len(selected) >= count:
            break
        selected.add(index)
    return [index for index in candidates if index in selected]


def sample_report(report_path: Path, rate: float = 0.2, seed: int = 42) -> dict:
    report_path = report_path.resolve()
    report = read_report(report_path)
    indices = select_indices(report["results"], rate, seed)
    return {
        "source_report": str(report_path),
        "source_sha256": report_hash(report_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sampling": {
            "rate": rate,
            "seed": seed,
            "population": len(report["results"]),
            "sample_size": len(indices),
        },
        "reviews": [build_review_item(report["results"][index]) for index in indices],
    }


def compare_reviews(review_path: Path, report_path: Path | None = None) -> dict:
    review_path = review_path.resolve()
    review = json.loads(review_path.read_text(encoding="utf-8"))
    source = (report_path or Path(review["source_report"])).resolve()
    if report_hash(source) != review["source_sha256"]:
        raise ValueError("原测评报告已变化；请使用抽样时的报告")
    report = read_report(source)
    original = {review_id(result): result for result in report["results"]}
    items = review.get("reviews", [])
    if not items:
        raise ValueError("人工评审文件没有 reviews")
    ids = [item["review_id"] for item in items]
    if len(ids) != len(set(ids)) or any(item_id not in original for item_id in ids):
        raise ValueError("人工评审记录的 review_id 重复或不属于原报告")

    comparisons = []
    for item in items:
        item_id = item["review_id"]
        business = item.get("human_business_score")
        reply = item.get("human_reply_score")
        if type(business) is not int or business not in BUSINESS_SCORES:
            raise ValueError(f"{item_id} 的 human_business_score 必须填写 0 或 100")
        if type(reply) is not int or reply not in REPLY_SCORES:
            raise ValueError(f"{item_id} 的 human_reply_score 必须填写 0/33/67/100")
        for field in ("human_factual_error", "human_forbidden_hit"):
            if item.get(field) is not None and type(item[field]) is not bool:
                raise ValueError(f"{item_id} 的 {field} 必须是 true/false/null")
        if (item.get("human_factual_error") or item.get("human_forbidden_hit")) and reply != 0:
            raise ValueError(f"{item_id} 标记了事实错误或禁止内容，回复分必须为 0")
        result = original[item_id]
        judged = result["agent_reply_quality"]
        comparisons.append({
            "review_id": item_id,
            "human_business_score": business,
            "auto_business_score": result["business_accuracy"]["score"],
            "human_reply_score": reply,
            "judge_reply_score": judged.get("score"),
            "reply_score_difference": (
                judged["score"] - reply if judged.get("score") is not None else None
            ),
            "human_factual_error": item.get("human_factual_error"),
            "judge_factual_error": judged.get("factual_error"),
            "human_forbidden_hit": item.get("human_forbidden_hit"),
            "judge_forbidden_hit": judged.get("forbidden_hit"),
            "human_reason": item.get("human_reason", ""),
            "judge_reason": judged.get("reason"),
            "judge_error": judged.get("judge_error"),
        })

    judged = [item for item in comparisons if item["judge_reply_score"] is not None]
    factual_checked = [
        item for item in judged
        if item["human_factual_error"] is not None and item["judge_factual_error"] is not None
    ]
    forbidden_checked = [
        item for item in judged
        if item["human_forbidden_hit"] is not None and item["judge_forbidden_hit"] is not None
    ]
    differences = [item for item in comparisons if (
        item["human_business_score"] != item["auto_business_score"]
        or item["reply_score_difference"] not in (None, 0)
    )]
    critical = [item for item in judged if item["human_reply_score"] == 0 and item["judge_reply_score"] > 0]
    return {
        "source_report": str(source),
        "review_file": str(review_path),
        "compared_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "reviewed": len(comparisons),
            "judge_scored": len(judged),
            "judge_unavailable": len(comparisons) - len(judged),
            "business_agreement": sum(
                item["human_business_score"] == item["auto_business_score"]
                for item in comparisons
            ) / len(comparisons),
            "reply_exact_agreement": (
                sum(item["reply_score_difference"] == 0 for item in judged) / len(judged)
                if judged else None
            ),
            "reply_mean_absolute_error": (
                round(sum(abs(item["reply_score_difference"]) for item in judged) / len(judged), 2)
                if judged else None
            ),
            "critical_miss_count": len(critical),
            "factual_error_agreement": (
                sum(item["human_factual_error"] == item["judge_factual_error"] for item in factual_checked)
                / len(factual_checked) if factual_checked else None
            ),
            "forbidden_hit_agreement": (
                sum(item["human_forbidden_hit"] == item["judge_forbidden_hit"] for item in forbidden_checked)
                / len(forbidden_checked) if forbidden_checked else None
            ),
        },
        "disagreements": differences,
        "critical_misses": critical,
        "comparisons": comparisons,
    }


def next_artifact_path(label: str) -> Path:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEWS_DIR / f"{label}.json"
    sequence = 2
    while path.exists():
        path = REVIEWS_DIR / f"{label}-{sequence}.json"
        sequence += 1
    return path


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="抽样人工复核并校准 LLM 裁判")
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample", help="生成不含自动分数的盲审文件")
    sample.add_argument("report", type=Path)
    sample.add_argument("--rate", type=float, default=0.2)
    sample.add_argument("--seed", type=int, default=42)
    sample.add_argument("--output", type=Path)
    compare = commands.add_parser("compare", help="对比人工评分与程序、裁判评分")
    compare.add_argument("review", type=Path)
    compare.add_argument("--report", type=Path, help="原报告移动后指定其新路径")
    compare.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.command == "sample":
        data = sample_report(args.report, args.rate, args.seed)
        cases = {item["case_id"] for item in data["reviews"]}
        label = next(iter(cases)) if len(cases) == 1 else "all"
        output = args.output or next_artifact_path(f"{label}-review")
        write_json(output, data)
        print(f"已抽样 {len(data['reviews'])}/{data['sampling']['population']} 条：{output}")
    else:
        data = compare_reviews(args.review, args.report)
        cases = {item["review_id"].split(":", 1)[0] for item in data["comparisons"]}
        label = next(iter(cases)) if len(cases) == 1 else "all"
        output = args.output or next_artifact_path(f"{label}-compare")
        write_json(output, data)
        print(f"校准对比：{output}\n{json.dumps(data['summary'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
