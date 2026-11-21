import unittest

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.db.isolated import isolated_engine
from backend.app.schemas.task import TaskCreate, TaskUpdate
from backend.app.services import action_service, task_service


class TaskUpdateScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = isolated_engine()
        self.engine = self.database.__enter__()

    def tearDown(self) -> None:
        self.database.__exit__(None, None, None)

    def create(self, title: str, start: str):
        with Session(self.engine) as session:
            return task_service.create_task(
                TaskCreate(title=title, start_at=start, duration_minutes=60), session
            )

    def propose(self, task_id: int, **changes):
        with Session(self.engine) as session:
            return action_service.propose_update(
                session, task_id, TaskUpdate(**changes)
            )

    def confirm(self, action_id: int):
        with Session(self.engine) as session:
            return action_service.confirm_action(session, action_id)

    def get(self, task_id: int):
        with Session(self.engine) as session:
            return task_service.get_task(task_id, session)

    def test_start_change_keeps_duration_until_confirmation(self) -> None:
        task = self.create("学习 Python", "2030-01-02T14:00:00+08:00")
        proposal = self.propose(task.id, start_at="2030-01-02T16:00:00+08:00")

        self.assertEqual(proposal.after.start_at.isoformat(), "2030-01-02T16:00:00+08:00")
        self.assertEqual(proposal.after.end_at.isoformat(), "2030-01-02T17:00:00+08:00")
        self.assertEqual(proposal.after.duration_minutes, 60)
        self.assertEqual(self.get(task.id).start_at, task.start_at)

        result = self.confirm(proposal.id)
        updated = self.get(task.id)
        self.assertEqual(result.status, "executed")
        self.assertEqual(updated.start_at, proposal.after.start_at)
        self.assertEqual(updated.end_at, proposal.after.end_at)
        self.assertEqual(updated.duration_minutes, 60)
        self.assertEqual(updated.version, 2)

    def test_end_change_derives_duration_and_rejects_inconsistent_input(self) -> None:
        task = self.create("学习 Python", "2030-01-02T14:00:00+08:00")
        with self.assertRaises(HTTPException) as raised:
            self.propose(
                task.id,
                end_at="2030-01-02T15:30:00+08:00",
                duration_minutes=60,
            )
        self.assertEqual(raised.exception.status_code, 422)

        proposal = self.propose(task.id, end_at="2030-01-02T15:30:00+08:00")
        self.assertEqual(proposal.after.duration_minutes, 90)
        self.confirm(proposal.id)
        self.assertEqual(self.get(task.id).duration_minutes, 90)

    def test_conflict_is_checked_at_proposal_and_again_at_confirmation(self) -> None:
        task = self.create("A", "2030-01-02T14:00:00+08:00")
        self.create("B", "2030-01-02T16:00:00+08:00")

        with self.assertRaises(HTTPException) as raised:
            self.propose(task.id, start_at="2030-01-02T15:30:00+08:00")
        self.assertEqual(raised.exception.status_code, 409)

        proposal = self.propose(task.id, start_at="2030-01-02T18:00:00+08:00")
        self.create("C", "2030-01-02T18:00:00+08:00")
        with self.assertRaises(HTTPException) as raised:
            self.confirm(proposal.id)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.get(task.id).start_at, task.start_at)
        with Session(self.engine) as session:
            self.assertEqual(action_service.get_action(session, proposal.id).status, "conflicted")

    def test_done_to_todo_checks_occupied_time(self) -> None:
        task = self.create("A", "2030-01-02T14:00:00+08:00")
        done = self.propose(task.id, status="done")
        self.confirm(done.id)
        self.create("B", "2030-01-02T14:00:00+08:00")

        with self.assertRaises(HTTPException) as raised:
            self.propose(task.id, status="todo")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.get(task.id).status, "done")

    def test_title_priority_due_date_and_status_are_editable(self) -> None:
        task = self.create("学习 Python", "2030-01-02T14:00:00+08:00")
        proposal = self.propose(
            task.id,
            title="复习 Python",
            priority="high",
            due_at="2030-01-03T18:00:00+08:00",
            status="done",
        )
        self.confirm(proposal.id)

        updated = self.get(task.id)
        self.assertEqual(updated.title, "复习 Python")
        self.assertEqual(updated.priority, "high")
        self.assertEqual(updated.due_at.isoformat(), "2030-01-03T18:00:00+08:00")
        self.assertEqual(updated.status, "done")

        clear_due_date = self.propose(task.id, due_at=None)
        self.confirm(clear_due_date.id)
        self.assertIsNone(self.get(task.id).due_at)

    def test_changing_due_date_does_not_change_schedule(self) -> None:
        task = self.create("学习 Python", "2030-01-02T14:00:00+08:00")
        proposal = self.propose(task.id, due_at="2030-01-01T12:00:00+08:00")
        self.assertEqual(proposal.after.start_at, task.start_at)
        self.assertEqual(proposal.after.end_at, task.end_at)
        self.assertEqual(proposal.after.duration_minutes, task.duration_minutes)
        self.confirm(proposal.id)
        updated = self.get(task.id)
        self.assertEqual(updated.due_at.isoformat(), "2030-01-01T12:00:00+08:00")
        self.assertEqual(updated.end_at, task.end_at)

    def test_start_only_task_can_be_rescheduled_and_given_duration(self) -> None:
        with Session(self.engine) as session:
            task = task_service.create_task(
                TaskCreate(title="只有开始", start_at="2030-01-02T14:00:00+08:00"), session
            )
        moved = self.propose(task.id, start_at="2030-01-02T16:00:00+08:00")
        self.assertIsNone(moved.after.duration_minutes)
        self.assertIsNone(moved.after.end_at)
        self.confirm(moved.id)

        scheduled = self.propose(task.id, duration_minutes=30)
        self.assertEqual(scheduled.after.end_at.isoformat(), "2030-01-02T16:30:00+08:00")
        self.confirm(scheduled.id)
        self.assertEqual(self.get(task.id).duration_minutes, 30)

    def test_due_only_task_keeps_at_least_one_time(self) -> None:
        with Session(self.engine) as session:
            task = task_service.create_task(
                TaskCreate(title="只有截止", due_at="2030-01-03T18:00:00+08:00"), session
            )
        with self.assertRaises(HTTPException) as raised:
            self.propose(task.id, due_at=None)
        self.assertEqual(raised.exception.status_code, 422)
        proposal = self.propose(task.id, start_at="2030-01-02T14:00:00+08:00", due_at=None)
        self.confirm(proposal.id)
        self.assertIsNone(self.get(task.id).due_at)
        self.assertIsNone(self.get(task.id).duration_minutes)


if __name__ == "__main__":
    unittest.main()
