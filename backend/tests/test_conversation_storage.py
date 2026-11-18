import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.services import conversation_service


class ConversationStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database_url = URL.create(
            "sqlite+pysqlite",
            database=str(Path(self.directory.name) / "conversations.db"),
        )
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.directory.cleanup()

    def test_history_survives_reopening_database(self) -> None:
        with Session(self.engine) as session:
            conversation_id = conversation_service.create_conversation(session)

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

        self.engine.dispose()
        self.engine = create_engine(self.database_url)
        with Session(self.engine) as session:
            conversation = conversation_service.load_history(session, conversation_id)
        self.assertEqual(conversation.conversation_id, conversation_id)
        self.assertEqual(conversation.message_count, 4)
        self.assertEqual(conversation.to_llm_history(), messages)

    def test_stale_append_and_system_message_are_rejected(self) -> None:
        with Session(self.engine) as session:
            conversation_id = conversation_service.create_conversation(session)

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


if __name__ == "__main__":
    unittest.main()
