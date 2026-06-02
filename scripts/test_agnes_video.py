"""generate_video（Agnes Video V2.0）测试。

异步任务：POST 创建 → GET 轮询 → 下载 mp4。全程 monkeypatch requests + time.sleep，
不真联网、不烧额度。key 用 monkeypatch config 控制。
"""
import glob
import os

import requests

from src import config
from src.tools import generate_video


class FakeResp:
    def __init__(self, status=200, json_data=None, content=b"", text=""):
        self.status_code = status
        self._json = json_data
        self.content = content
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class TestGenerateVideo:
    def test_no_key_graceful(self, monkeypatch):
        monkeypatch.setattr(config, "AGNES_API_KEY", "")
        assert "未配置" in generate_video.func("a cat")

    def test_create_error(self, monkeypatch):
        monkeypatch.setattr(config, "AGNES_API_KEY", "k")
        monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(401, text="bad key"))
        assert "HTTP 401" in generate_video.func("a cat")

    def test_success_flow(self, project_dir, monkeypatch):
        monkeypatch.setattr(config, "AGNES_API_KEY", "k")
        monkeypatch.setattr("src.tools.time.sleep", lambda *_: None)
        monkeypatch.setattr(requests, "post",
                            lambda *a, **k: FakeResp(200, {"id": "task_1", "status": "queued"}))
        calls = {"n": 0}

        def fake_get(url, *a, **k):
            if url.endswith("/task_1"):          # 轮询
                calls["n"] += 1
                if calls["n"] == 1:
                    return FakeResp(200, {"status": "processing", "progress": 50})
                return FakeResp(200, {"status": "completed", "video_url": "http://vid/x.mp4",
                                      "size": "1152x768", "seconds": "5.0"})
            return FakeResp(200, content=b"MP4BYTES")   # 下载
        monkeypatch.setattr(requests, "get", fake_get)

        out = generate_video.func("a cat walking", max_wait=60)
        assert "已生成视频" in out
        files = glob.glob(os.path.join(str(project_dir), "outputs", "*.mp4"))
        assert files and open(files[0], "rb").read() == b"MP4BYTES"

    def test_failed_status(self, project_dir, monkeypatch):
        monkeypatch.setattr(config, "AGNES_API_KEY", "k")
        monkeypatch.setattr("src.tools.time.sleep", lambda *_: None)
        monkeypatch.setattr(requests, "post",
                            lambda *a, **k: FakeResp(200, {"id": "t", "status": "queued"}))
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, {"status": "failed"}))
        assert "失败" in generate_video.func("x", max_wait=60)

    def test_image_to_video_sets_image_field(self, project_dir, monkeypatch):
        monkeypatch.setattr(config, "AGNES_API_KEY", "k")
        monkeypatch.setattr("src.tools.time.sleep", lambda *_: None)
        captured = {}

        def fake_post(url, json=None, **k):
            captured.update(json or {})
            return FakeResp(200, {"id": "t", "status": "queued"})
        monkeypatch.setattr(requests, "post", fake_post)
        monkeypatch.setattr(
            requests, "get",
            lambda u, *a, **k: (FakeResp(200, {"status": "completed", "video_url": "http://v/x.mp4"})
                                if u.endswith("/t") else FakeResp(200, content=b"X")))
        generate_video.func("make it move", image="http://img/a.png", max_wait=60)
        assert captured.get("image") == "http://img/a.png"
        assert captured.get("model") == "agnes-video-v2.0"

    def test_total_cap(self, project_dir, monkeypatch):
        # max_wait=0 → 循环不进，直接命中总时长上限分支（不靠进度判卡死，只认 status + 总上限）
        monkeypatch.setattr(config, "AGNES_API_KEY", "k")
        monkeypatch.setattr("src.tools.time.sleep", lambda *_: None)
        monkeypatch.setattr(requests, "post",
                            lambda *a, **k: FakeResp(200, {"id": "t", "status": "queued"}))
        monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp(200, {"status": "processing"}))
        assert "未完成" in generate_video.func("x", max_wait=0)

    def test_video_url_from_remixed_field(self, project_dir, monkeypatch):
        # Agnes 实测把视频地址放在 remixed_from_video_id（不是文档写的 video_url）→ 也要能取到
        monkeypatch.setattr(config, "AGNES_API_KEY", "k")
        monkeypatch.setattr("src.tools.time.sleep", lambda *_: None)
        monkeypatch.setattr(requests, "post",
                            lambda *a, **k: FakeResp(200, {"id": "t", "status": "queued"}))

        def fake_get(url, *a, **k):
            if url.endswith("/t"):
                return FakeResp(200, {"status": "completed", "progress": 100,
                                      "remixed_from_video_id": "http://v/real.mp4",
                                      "size": "1152x768", "seconds": "5.0"})
            return FakeResp(200, content=b"VID")
        monkeypatch.setattr(requests, "get", fake_get)
        out = generate_video.func("x", max_wait=60)
        assert "已生成视频" in out
        assert glob.glob(os.path.join(str(project_dir), "outputs", "*.mp4"))

    def test_local_image_uploaded_to_litterbox(self, project_dir, monkeypatch):
        # 本地图片路径 → 先传 litterbox 换公网 URL → 该 URL 进 Agnes 请求体的 image 字段
        monkeypatch.setattr(config, "AGNES_API_KEY", "k")
        monkeypatch.setattr("src.tools.time.sleep", lambda *_: None)
        img = project_dir / "pic.png"
        img.write_bytes(b"\x89PNG_fake_image")
        captured = {}

        def fake_post(url, *a, **k):
            if "litterbox" in url:
                return FakeResp(200, text="https://litter.catbox.moe/abc.png")
            captured.update(k.get("json") or {})        # Agnes 创建任务
            return FakeResp(200, {"id": "t", "status": "queued"})
        monkeypatch.setattr(requests, "post", fake_post)
        monkeypatch.setattr(
            requests, "get",
            lambda u, *a, **k: (FakeResp(200, {"status": "completed",
                                               "remixed_from_video_id": "http://v/x.mp4"})
                                if u.endswith("/t") else FakeResp(200, content=b"V")))
        generate_video.func("animate it", image=str(img), max_wait=60)
        assert captured.get("image") == "https://litter.catbox.moe/abc.png"
