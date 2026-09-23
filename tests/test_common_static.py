"""两个 app 共用的前端文件由各自的 /common/ 路由提供。"""


def test_两个app都能加载共用前端文件():
    from apps.notes2insight import server as notes_server
    from apps.summit2md import server as summit_server
    for app in (summit_server.app, notes_server.app):
        r = app.test_client().get("/common/spark_common.js")
        assert r.status_code == 200
        assert b"window.SparkCommon" in r.data
