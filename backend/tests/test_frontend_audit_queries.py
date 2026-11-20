import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import URL, create_engine
from sqlalchemy.orm import Session

from backend.app.api import actions as actions_api, tasks as tasks_api
from backend.app.db.base import Base
from backend.app.models.conversation import ConversationORM
from backend.app.models.request import ChatTurnORM
from backend.app.schemas.task import TaskCreate, TaskUpdate
from backend.app.services import action_service, audit_service, task_service


class FrontendAuditQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine(URL.create(
            "sqlite+pysqlite", database=str(Path(self.directory.name) / "queries.db")
        ))
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.directory.cleanup()

    def test_cursor_summary_and_task_origin(self) -> None:
        with Session(self.engine) as session:
            manual = task_service.create_task(
                TaskCreate(title="手动任务", start_at="2030-01-02T10:00:00+08:00"), session
            )
        with Session(self.engine) as session:
            session.info["audit_conversation_id"] = str(uuid4())
            agent = task_service.create_task(
                TaskCreate(title="Agent 任务", start_at="2030-01-03T10:00:00+08:00"), session
            )
        with Session(self.engine) as session:
            first = audit_service.list_events(session, limit=1)
            second = audit_service.list_events(session, before_id=first[-1].id, limit=1)
            self.assertEqual([row.task_id for row in first + second], [agent.id, manual.id])
            self.assertEqual(audit_service.summary(session).total, 2)
            future = int(datetime.now(timezone.utc).timestamp()) + 60
            self.assertEqual(audit_service.summary(session, since=future).total, 0)
            self.assertEqual(audit_service.list_events(session, since=future), [])
            listed = tasks_api.list_tasks(session)
            self.assertEqual(
                {task.id: task.created_by_agent for task in listed},
                {manual.id: False, agent.id: True},
            )

    def test_page_number_queries_return_total_and_empty_out_of_range_page(self) -> None:
        for number in range(5):
            with Session(self.engine) as session:
                task_service.create_task(
                    TaskCreate(
                        title=f"任务 {number}",
                        start_at=f"2030-01-{number + 2:02d}T10:00:00+08:00",
                    ), session,
                )
        with Session(self.engine) as session:
            first = audit_service.page_events(session, page=1, page_size=2)
        with Session(self.engine) as session:
            task_service.create_task(
                TaskCreate(title="翻页期间新增", start_at="2030-01-10T10:00:00+08:00"), session
            )
        with Session(self.engine) as session:
            second = audit_service.page_events(session, page=2, page_size=2, anchor_id=first.anchor_id)
            third = audit_service.page_events(session, page=3, page_size=2, anchor_id=first.anchor_id)
            beyond = audit_service.page_events(session, page=4, page_size=2, anchor_id=first.anchor_id)
            fresh = audit_service.page_events(session, page=1, page_size=2)
            self.assertEqual((first.total, first.total_pages, first.page_size, first.anchor_id), (5, 3, 2, 5))
            self.assertEqual(
                [event.id for result in (first, second, third) for event in result.items],
                [5, 4, 3, 2, 1],
            )
            self.assertEqual((beyond.page, beyond.total_pages, beyond.items), (4, 3, []))
            self.assertEqual((fresh.total, fresh.items[0].id), (6, 6))

    def test_pending_and_conversation_queries_keep_request_link(self) -> None:
        conversation_id, request_id = str(uuid4()), str(uuid4())
        with Session(self.engine) as session:
            task = task_service.create_task(
                TaskCreate(title="原任务", start_at="2030-01-02T10:00:00+08:00"), session
            )
        with Session(self.engine) as session, session.begin():
            session.add(ConversationORM(
                id=conversation_id, created_at=1, updated_at=1, message_count=0,
            ))
            session.add(ChatTurnORM(
                request_id=request_id, conversation_id=conversation_id, user_text="修改任务",
                status="completed", active_conversation_id=None, owner_token=None,
                lease_until=None, base_message_count=0, messages=[], answer="已生成提案",
            ))
        with Session(self.engine) as session:
            session.info.update(audit_request_id=request_id, audit_conversation_id=conversation_id)
            action = action_service.propose_update(session, task.id, TaskUpdate(title="新任务"))
        with Session(self.engine) as session:
            pending = actions_api.list_pending_actions(session)
            linked = actions_api.list_conversation_actions(conversation_id, session)
            self.assertEqual([item.id for item in pending], [action.id])
            self.assertEqual([(item.id, item.request_id) for item in linked], [(action.id, request_id)])
            self.assertEqual(audit_service.list_events(session, category="cancelled"), [])
            proposal = audit_service.list_events(session, action_id=action.id)[0]
            self.assertEqual(proposal.subject_title, "原任务")

        with Session(self.engine) as session:
            action_service.cancel_action(session, action.id)
        with Session(self.engine) as session:
            self.assertEqual(actions_api.list_pending_actions(session), [])
            self.assertEqual(actions_api.list_conversation_actions(conversation_id, session)[0].status, "cancelled")
            rows = audit_service.list_events(session, category="cancelled")
            self.assertEqual([row.event_type for row in rows], ["proposal_cancelled"])
            self.assertEqual(rows[0].subject_title, "原任务")
            page = audit_service.page_events(session, page=1, page_size=20, category="cancelled")
            self.assertEqual((page.total, page.total_pages, page.items[0].subject_title), (1, 1, "原任务"))
            summary = audit_service.summary(session)
            self.assertEqual((summary.proposals, summary.cancelled, summary.requests), (1, 1, 1))

if __name__ == "__main__":
    unittest.main()
