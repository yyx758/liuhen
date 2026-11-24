"""资料库路由（spec 2.8）：状态码、请求头、路由顺序、原始请求体上传、Origin 校验。"""

import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.api import documents as documents_api
from backend.app.api import security
from backend.app.api.deps import current_user_id
from backend.app.db.isolated import create_user, isolated_engine
from backend.app.db.session import get_db
from backend.app.main import app
from backend.app.services.auth_service import SESSION_COOKIE

PNG = b"\x89PNG\r\n\x1a\n" + b"api-test-image"


class DocumentsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database = isolated_engine()
        cls.engine = cls.database.__enter__()

        def test_db():
            with Session(cls.engine) as session:
                yield session

        app.dependency_overrides[get_db] = test_db
        app.dependency_overrides[current_user_id] = lambda: cls.user
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        app.dependency_overrides.clear()
        cls.database.__exit__(None, None, None)

    def setUp(self) -> None:
        type(self).user = create_user(self.engine, f"u{uuid4().hex[:8]}")

    def create(self, title="笔记", content="", **extra):
        response = self.client.post(
            "/documents", headers={"Idempotency-Key": str(uuid4())},
            json={"title": title, "content": content, **extra},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_crud_flow(self) -> None:
        created = self.create("Redis", "# 锁\n正文", category="学习")
        self.assertEqual((created["version"], created["category"]), (1, "学习"))
        self.assertNotIn("content", created)

        detail = self.client.get(f"/documents/{created['id']}").json()
        self.assertEqual((detail["content"], detail["outline"][0]["heading_path"]), ("# 锁\n正文", ["锁"]))

        patched = self.client.patch(f"/documents/{created['id']}", headers={"If-Match": "1"}, json={"category": None})
        self.assertEqual((patched.status_code, patched.json()["category"], patched.json()["version"]), (200, None, 2))
        stale = self.client.patch(f"/documents/{created['id']}", headers={"If-Match": "1"}, json={"title": "x"})
        self.assertEqual(stale.status_code, 409)

        self.assertEqual(self.client.delete(f"/documents/{created['id']}", headers={"If-Match": "2"}).status_code, 204)
        self.assertEqual(self.client.get(f"/documents/{created['id']}").status_code, 404)

    def test_list_and_categories(self) -> None:
        first = self.create("一", category="学习")
        second = self.create("二")
        page = self.client.get("/documents", params={"limit": 1}).json()
        self.assertEqual(([d["id"] for d in page["items"]], page["next_before_id"]), ([second["id"]], second["id"]))
        self.assertEqual(page["total"], 2)
        self.assertEqual(self.client.get("/documents", params={"category": "学习"}).json()["total"], 1)
        rest = self.client.get("/documents", params={"before_id": second["id"]}).json()
        self.assertEqual([d["id"] for d in rest["items"]], [first["id"]])
        self.assertEqual(self.client.get("/documents/categories").json(), [{"name": "学习", "count": 1}])

    def test_static_paths_are_not_captured_by_document_id(self) -> None:
        self.create("分布式锁笔记", "看门狗续期")
        response = self.client.get("/documents/search", params={"q": "看门狗"})
        self.assertEqual(response.status_code, 200, response.text)
        [group] = response.json()
        self.assertEqual(group["document"]["title"], "分布式锁笔记")
        self.assertEqual(group["hits"][0]["matched_queries"], ["看门狗"])
        self.assertEqual(self.client.get("/documents/categories").status_code, 200)
        self.assertEqual(self.client.get("/documents/images/" + "0" * 32).status_code, 404)
        self.assertEqual(self.client.get("/documents/not-a-number").status_code, 422)

    def test_search_groups_hits_by_document_and_validates_queries(self) -> None:
        self.create("长文", "\n".join(f"# 第{i}节\n提到看门狗" for i in range(5)))
        groups = self.client.get("/documents/search", params={"q": "看门狗  续期"}).json()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["hits"]), 3)
        too_many = " ".join(str(i) for i in range(6))
        self.assertEqual(self.client.get("/documents/search", params={"q": too_many}).status_code, 422)
        self.assertEqual(self.client.get("/documents/search", params={"q": "   "}).status_code, 422)

    def test_image_upload_reads_raw_body_and_serves_safe_headers(self) -> None:
        uploaded = self.client.post(
            "/documents/images", params={"name": "截图.png"}, content=PNG,
            headers={"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        image = uploaded.json()
        self.assertEqual((image["content_type"], image["original_name"], image["reused"]), ("image/png", "截图.png", False))

        served = self.client.get(image["url"])
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.content, PNG)
        self.assertEqual(served.headers["content-type"], "image/png")
        self.assertEqual(served.headers["x-content-type-options"], "nosniff")
        self.assertEqual(served.headers["content-disposition"], "inline")
        self.assertIn("private", served.headers["cache-control"])

        again = self.client.post("/documents/images", content=PNG)
        self.assertEqual((again.status_code, again.json()["reused"], again.json()["id"]), (201, True, image["id"]))

    def test_image_upload_rejections(self) -> None:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        self.assertEqual(self.client.post("/documents/images", content=svg, headers={"Content-Type": "image/png"}).status_code, 415)
        self.assertEqual(self.client.post("/documents/images", content=b"").status_code, 422)
        with patch.object(documents_api, "IMAGE_MAX_BYTES", 10):
            self.assertEqual(self.client.post("/documents/images", content=PNG).status_code, 413)

    def test_declared_length_over_limit_is_rejected_before_reading(self) -> None:
        response = self.client.post(
            "/documents/images", content=PNG, headers={"Content-Length": str(50 * 1024 * 1024)}
        )
        self.assertEqual(response.status_code, 413)

    def test_required_headers(self) -> None:
        self.assertEqual(self.client.post("/documents", json={"title": "x"}).status_code, 422)
        created = self.create("x")
        self.assertEqual(self.client.patch(f"/documents/{created['id']}", json={"title": "y"}).status_code, 422)
        self.assertEqual(self.client.delete(f"/documents/{created['id']}").status_code, 422)

    def test_write_requests_with_session_cookie_require_allowed_origin(self) -> None:
        self.client.cookies.set(SESSION_COOKIE, "any")
        try:
            blocked = self.client.post("/documents/images", content=PNG, headers={"Origin": "https://evil.example"})
            self.assertEqual(blocked.status_code, 403)
            if security.ALLOWED_ORIGINS:
                allowed = self.client.post(
                    "/documents/images", content=PNG, headers={"Origin": next(iter(security.ALLOWED_ORIGINS))}
                )
                self.assertEqual(allowed.status_code, 201)
            # 读取是安全方法，不校验 Origin（<img> 标签加载图片不会带 Origin）。
            self.assertEqual(self.client.get("/documents/categories").status_code, 200)
        finally:
            self.client.cookies.clear()


if __name__ == "__main__":
    unittest.main()
