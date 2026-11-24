"""图片上传、读取与清理（spec 2.6）。使用隔离数据库。"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.app.config.clock import fixed_now
from backend.app.db.isolated import create_user, isolated_engine
from backend.app.schemas.document import DocumentCreate
from backend.app.services import document_service, image_service

PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body"
GIF87 = b"GIF87a" + b"gif-body"
GIF89 = b"GIF89a" + b"gif-body"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"webp-body"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
T0 = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)


class ImageServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = isolated_engine()
        cls.engine = cls.database.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.database.__exit__(None, None, None)

    def setUp(self) -> None:
        self.user = create_user(self.engine, f"u{uuid4().hex[:8]}")
        self.other = create_user(self.engine, f"o{uuid4().hex[:8]}")

    def upload(self, data: bytes, name: str | None = "a.png", *, user=None, at=T0):
        with fixed_now(at), Session(self.engine) as session:
            return image_service.upload_image(session, data, name, user_id=user or self.user)

    def get(self, public_id: str, *, user=None):
        with Session(self.engine) as session:
            return image_service.get_image(session, public_id, user_id=user or self.user)

    def assert_http(self, status: int, func, *args, **kwargs) -> None:
        with self.assertRaises(HTTPException) as raised:
            func(*args, **kwargs)
        self.assertEqual(raised.exception.status_code, status, raised.exception.detail)

    def test_formats_are_detected_by_file_header(self) -> None:
        for data, expected in ((PNG, "image/png"), (JPEG, "image/jpeg"), (GIF87, "image/gif"),
                               (GIF89, "image/gif"), (WEBP, "image/webp")):
            with self.subTest(expected=expected):
                # 文件名故意写错扩展名：格式只看文件头。
                image = self.upload(data + expected.encode(), "wrong.txt")
                self.assertEqual(image.content_type, expected)
                self.assertEqual(image.url, f"/documents/images/{image.id}")
                self.assertRegex(image.id, r"^[0-9a-f]{32}$")
                self.assertEqual(self.get(image.id), (data + expected.encode(), expected))

    def test_rejected_content(self) -> None:
        self.assert_http(415, self.upload, SVG, "evil.png")
        self.assert_http(415, self.upload, b"<html><body>hi</body></html>", "page.png")
        self.assert_http(415, self.upload, b"RIFF\x10\x00\x00\x00WAVEfmt ", "sound.webp")
        self.assert_http(422, self.upload, b"", "empty.png")

    def test_size_limit(self) -> None:
        with patch.object(image_service, "IMAGE_MAX_BYTES", len(PNG) + 3):
            self.upload(PNG + b"123")
            self.assert_http(413, self.upload, PNG + b"1234")

    def test_same_content_is_reused_without_counting_twice(self) -> None:
        first = self.upload(PNG, "a.png")
        again = self.upload(PNG, "b.png")
        self.assertEqual((again.id, again.reused, first.reused), (first.id, True, False))
        with Session(self.engine) as session:
            from backend.app.repositories import document_repository
            self.assertEqual(document_repository.total_image_bytes(session, user_id=self.user), len(PNG))
        # 不同用户之间不复用：避免探测别人有没有某张图。
        foreign = self.upload(PNG, user=self.other)
        self.assertFalse(foreign.reused)
        self.assertNotEqual(foreign.id, first.id)

    def test_total_quota(self) -> None:
        with patch.object(image_service, "IMAGE_MAX_TOTAL_BYTES_PER_USER", (len(PNG) + 1) * 2):  # 刚好两张
            self.upload(PNG + b"1")
            self.upload(PNG + b"2")
            self.assert_http(409, self.upload, PNG + b"3")
            # 复用已有图片不占用新空间。
            self.assertTrue(self.upload(PNG + b"1").reused)
            self.upload(PNG + b"3", user=self.other)

    def test_original_name_keeps_only_file_name(self) -> None:
        self.assertEqual(self.upload(PNG + b"1", "C:\\Users\\me\\图片\\截图.png").original_name, "截图.png")
        self.assertEqual(self.upload(PNG + b"2", "../../etc/passwd.png").original_name, "passwd.png")
        self.assertIsNone(self.upload(PNG + b"3", "   ").original_name)
        self.assertIsNone(self.upload(PNG + b"4", None).original_name)
        self.assertEqual(len(self.upload(PNG + b"5", "a" * 300 + ".png").original_name), 255)

    def test_other_users_and_invalid_ids_are_404(self) -> None:
        image = self.upload(PNG)
        self.assert_http(404, self.get, image.id, user=self.other)
        for public_id in ("0" * 32, "not-an-id", image.id.upper(), image.id + "0", "../" + image.id[3:]):
            with self.subTest(public_id=public_id):
                self.assert_http(404, self.get, public_id)

    def test_cleanup_removes_only_old_unreferenced_images(self) -> None:
        old = T0 - timedelta(hours=25)
        referenced = self.upload(PNG + b"ref", at=old)
        orphan = self.upload(PNG + b"orphan", at=old)
        fresh = self.upload(PNG + b"fresh", at=T0 - timedelta(hours=1))
        with Session(self.engine) as session:
            document_service.create_document(
                session, DocumentCreate(title="引用图片", content=f"![图]({referenced.url})"), user_id=self.user
            )
        with Session(self.engine) as session:
            deleted = image_service.cleanup_unreferenced_images(session, now=T0)
        self.assertGreaterEqual(deleted, 1)
        self.assertEqual(self.get(referenced.id)[1], "image/png")
        self.assertEqual(self.get(fresh.id)[1], "image/png")
        self.assert_http(404, self.get, orphan.id)


if __name__ == "__main__":
    unittest.main()
