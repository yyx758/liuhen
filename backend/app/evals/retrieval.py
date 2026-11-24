"""离线检索评测（spec 5.6）：不调用模型，只看关键词搜索能不能把证据找出来。

把 document_fixtures 的全部笔记导入隔离库，对每道题的关键词调用 search_chunks(limit=8)，
"文档标题相同且片段正文包含引文"的第一个结果为命中名次，输出 Recall@8 与 MRR。
证据用"标题 + 引文"而不是片段 ID，换切片策略后题目仍可复用。只输出，不设通过门槛。

    python -m backend.app.evals.retrieval
"""

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.isolated import create_user, isolated_engine
from backend.app.evals import document_fixtures as docs
from backend.app.models.document import DocumentChunkORM
from backend.app.schemas.document import DocumentCreate
from backend.app.services import document_service
from backend.app.services.document_search import normalize_queries, search_chunks

TOP_K = 8


@dataclass(frozen=True)
class Question:
    id: str
    note: str
    queries: tuple[str, ...]
    title: str
    quote: str


QUESTIONS = (
    Question("31", "直接命中", ("缓存击穿",), docs.REDIS, "热点数据在过期瞬间"),
    Question("32-原话", "用户原话（预期找不到，说明为什么要换说法）", ("热 key", "失效"), docs.REDIS, "热点数据在过期瞬间"),
    Question("32-换说法", "换成笔记里的说法", ("缓存击穿", "互斥锁"), docs.REDIS, "热点数据在过期瞬间"),
    Question("33-面试", "多篇综合之一", ("RDB", "面试"), docs.REDIS, "RDB 是某一时刻的内存快照"),
    Question("33-线上", "多篇综合之二", ("AOF", "线上配置"), docs.OPS, "appendfsync 设为 everysec"),
    Question("35", "结论在片段后部", ("看门狗",), docs.LOCK_REVIEW, "最终决定不用看门狗"),
    Question("36", "按标题找", ("周会",), docs.WEEKLY, "峰值 QPS 到 3200"),
    Question("37", "按小节找", ("复习计划", "第一周"), docs.STUDY_PLAN, "复习 Redis 持久化"),
    Question("40-第一跳", "多跳：先找线索", ("超时", "原因"), docs.RETRO, "热点 key 过期"),
    Question("40-第二跳", "多跳：用线索再搜", ("热点", "过期"), docs.REDIS, "热点数据在过期瞬间"),
    Question("41-第一跳", "多跳：找到带互链的复盘", ("0812", "处理"), docs.RETRO, "处理方法见"),
    Question("43-评审", "汇总之一", ("分布式锁",), docs.LOCK_REVIEW, "库存扣减需要分布式锁"),
    Question("43-Redis", "汇总之二", ("分布式锁",), docs.REDIS, "SET key value NX PX 30000"),
    Question("43-ZooKeeper", "汇总之三", ("分布式锁",), docs.ZOOKEEPER, "临时顺序节点实现分布式锁"),
    Question("43-长文", "汇总之四（长文每篇最多 3 段）", ("分布式锁",), docs.ARCHITECTURE, "涉及分布式锁的使用场景之一"),
    Question("44", "答案只在图片说明里", ("nginx",), docs.DEPLOY, "nginx 转发到两台 api"),
    Question("JVM", "长文里的主题", ("GC 日志",), docs.JVM_TITLES[2], "关于GC 日志的调优"),
    Question("运维", "配置项", ("淘汰策略",), docs.OPS, "allkeys-lru"),
    Question("穿透", "同篇其他小节", ("布隆过滤器",), docs.REDIS, "布隆过滤器"),
    Question("雪崩", "同篇其他小节", ("雪崩", "随机"), docs.REDIS, "过期时间加随机值"),
    Question("无关-跑步", "非技术笔记", ("配速",), docs.RUNNING, "配速六分半"),
)


def evaluate(*, large_library: bool = False) -> dict:
    """在隔离库里导入语料并逐题搜索；返回每题名次与汇总指标。
    large_library=True 时另加约 100 篇干扰笔记，看库变大后召回和排序如何变化。"""
    corpus = {**docs.DISTRACTORS, **docs.CORPUS} if large_library else docs.CORPUS
    with isolated_engine() as engine:
        user_id = create_user(engine, "retrieval_eval")
        for title, content in corpus.items():
            with Session(engine) as session:
                document_service.create_document(session, DocumentCreate(title=title, content=content), user_id=user_id)

        rows = []
        with Session(engine) as session:
            for question in QUESTIONS:
                hits = search_chunks(session, normalize_queries(question.queries), user_id=user_id, limit=TOP_K)
                contents = dict(session.execute(
                    select(DocumentChunkORM.id, DocumentChunkORM.content)
                    .where(DocumentChunkORM.id.in_([hit.chunk_id for hit in hits]))
                ).all()) if hits else {}
                rank = next((
                    position for position, hit in enumerate(hits, start=1)
                    if hit.title == question.title and question.quote in contents.get(hit.chunk_id, "")
                ), None)
                rows.append({
                    "id": question.id, "note": question.note, "queries": list(question.queries),
                    "evidence": f"《{question.title}》{question.quote}", "rank": rank,
                    "top": [f"{hit.title} > {' > '.join(hit.heading_path)}" for hit in hits[:3]],
                })

    found = [row["rank"] for row in rows if row["rank"] is not None]
    return {
        "documents": len(corpus),
        "questions": len(rows),
        "recall_at_8": round(len(found) / len(rows), 4),
        "mrr": round(sum(1 / rank for rank in found) / len(rows), 4),
        "missed": [row["id"] for row in rows if row["rank"] is None],
        "rows": rows,
    }


def main() -> None:
    for large_library in (False, True):
        report = evaluate(large_library=large_library)
        print(f"\n== {'大库' if large_library else '基础'}语料：{report['documents']} 篇 ==")
        for row in report["rows"]:
            rank = row["rank"] if row["rank"] is not None else "—"
            print(f"{row['id']:<14} 名次 {rank:<3} {row['evidence']}  关键词 {' '.join(row['queries'])}")
        summary = {key: report[key] for key in ("documents", "questions", "recall_at_8", "mrr", "missed")}
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
