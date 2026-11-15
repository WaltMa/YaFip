from __future__ import annotations

from fastapi.testclient import TestClient

from yafip.server import create_app


def test_cross_site_mutations_rejected(tmp_path):
    """回归：管理接口无认证，必须拦截浏览器跨站写请求（Origin vs Host 校验）。"""
    app = create_app(tmp_path)
    with TestClient(app) as client:
        # 第三方页面 Origin 与 Host 不一致 → 403，请求根本到不了业务层
        evil = {"Origin": "http://evil.example"}
        assert client.put("/api/config", json={"values": {}}, headers=evil).status_code == 403
        assert client.post("/api/system/stop", json={}, headers=evil).status_code == 403
        assert client.post("/api/system/vacuum", json={}, headers=evil).status_code == 403
        assert client.delete("/api/tasks/whatever", headers=evil).status_code == 403

        # 同源请求 / 无 Origin 的本地脚本放行（继续走到业务校验层）
        assert client.put("/api/config", json={"values": {}}).status_code == 400
        same_origin = {"Origin": "http://testserver", "Host": "testserver"}
        assert (
            client.put("/api/config", json={"values": {}}, headers=same_origin).status_code == 400
        )
