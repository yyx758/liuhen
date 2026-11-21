import unittest
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.isolated import isolated_engine
from backend.app.models.conversation import ConversationORM
from backend.app.services import chat_turn_service, conversation_service


def add_conversation(session_factory) -> str:
    conversation_id = str(uuid4())
    with session_factory() as session, session.begin():
        session.add(ConversationORM(id=conversation_id, created_at=0, updated_at=0, message_count=0))
    return conversation_id


class ConversationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()
        self.database_url = self.engine.url

    def tearDown(self) -> None:
        self.database.__exit__(None, None, None)

    def test_history_survives_reopening_database(self) -> None:
        conversation_id = add_conversation(sessionmaker(bind=self.engine))

        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "list_tasks", "arguments": "{}"},
        }
        messages = [
            {"role": "user", "content": "我有哪些任务？"},
            {"role": "assistant", "content": None, "tool_calls": [tool_call]},
            {"role": "tool", "tool_call_id": "call_1", "content": "[]"},
            {"role": "assistant", "content": "目前没有任务。"},
        ]
        with Session(self.engine) as session:
            count = conversation_service.append_messages(
                session, conversation_id, messages, expected_count=0
            )
        self.assertEqual(count, 4)

        reopened_engine = create_engine(self.database_url)
        try:
            with Session(reopened_engine) as session:
                conversation = conversation_service.load_history(session, conversation_id)
        finally:
            reopened_engine.dispose()
        self.assertEqual(conversation.conversation_id, conversation_id)
        self.assertEqual(conversation.message_count, 4)
        self.assertEqual(conversation.to_llm_history(), messages)

    def test_stale_append_and_system_message_are_rejected(self) -> None:
        conversation_id = add_conversation(sessionmaker(bind=self.engine))

        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            conversation_service.append_messages(
                session,
                conversation_id,
                [{"role": "system", "content": "过时的当前时间"}],
                expected_count=0,
            )
        self.assertEqual(raised.exception.status_code, 422)

        with Session(self.engine) as session:
            conversation_service.append_messages(
                session,
                conversation_id,
                [{"role": "user", "content": "第一轮"}],
                expected_count=0,
            )
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            conversation_service.append_messages(
                session,
                conversation_id,
                [{"role": "user", "content": "过期的写入"}],
                expected_count=0,
            )
        self.assertEqual(raised.exception.status_code, 409)

        with Session(self.engine) as session:
            conversation = conversation_service.load_history(session, conversation_id)
        self.assertEqual(conversation.message_count, 1)
        self.assertEqual(
            conversation.to_llm_history(),
            [{"role": "user", "content": "第一轮"}],
        )

    def test_processing_conversation_cannot_be_deleted(self) -> None:
        conversation_id = add_conversation(sessionmaker(bind=self.engine))
        request_id = str(uuid4())
        chat_turn_service.acquire_turn(
            request_id, "正在处理", conversation_id, sessionmaker(bind=self.engine)
        )
        with Session(self.engine) as session, self.assertRaises(HTTPException) as raised:
            conversation_service.delete_conversation(session, conversation_id)
        self.assertEqual(raised.exception.status_code, 409)
        with Session(self.engine) as session:
            self.assertIsNotNone(session.get(ConversationORM, conversation_id))


if __name__ == "__main__":
    unittest.main()
