"""灵犀 Web 前端启动入口。

    python web/serve.py                 # 本机:仅 127.0.0.1 可访问
    python web/serve.py --host 0.0.0.0  # 局域网:手机连本机内网 IP(手机版灵犀)

默认只绑 127.0.0.1(安全);要给手机/外部访问必须显式 --host 0.0.0.0。
鉴权 token 默认自动生成并持久化(chat_memory/web_token.json),启动时打印带 token 的访问 URL。
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys

logger = logging.getLogger("lingxi-web")


def _lan_ip() -> str | None:
    """探测本机内网 IP(不真发包,失败返回 None)。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lingxi-web",
        description="灵犀 Web/PWA 前端 —— 手机浏览器 / 任意 HTTP 客户端访问 AI 对话",
    )
    # 默认 127.0.0.1:不显式开 0.0.0.0 就不暴露到网络(安全默认)
    parser.add_argument("--host", default="127.0.0.1", help="监听地址(默认 127.0.0.1;给手机/外部访问用 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8787, help="监听端口(默认 8787)")
    parser.add_argument("--project", default=None, help="固定对话的目标项目路径(留空用当前活动项目)")
    parser.add_argument("--token", default=None, help="鉴权 token(留空则自动生成并持久化)")
    args = parser.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    try:
        from web.app import create_app
    except ImportError as e:
        logger.error("无法导入 web 应用: %s", e)
        sys.exit(1)

    app = create_app(project=args.project, auth_token=args.token)
    token = app.state.auth_token

    port = args.port
    print("\n灵犀 Web 前端已启动")
    print(f"  本机访问   http://127.0.0.1:{port}/?token={token}")
    if args.host == "0.0.0.0":
        ip = _lan_ip()
        if ip:
            print(f"  局域网     http://{ip}:{port}/?token={token}   (手机连同一 WiFi 打开)")
    print(f"  token      {token}" + ("  (自动生成,保存于 chat_memory/web_token.json)" if app.state.token_generated else ""))
    if args.host == "0.0.0.0":
        print("  注意:已绑 0.0.0.0 对外暴露。公网部署务必上 HTTPS + 防火墙只放该端口。")
    print()

    try:
        import uvicorn
    except ImportError:
        logger.error("uvicorn 未安装:pip install uvicorn")
        sys.exit(1)

    # 单进程(默认 workers=1):多 worker 会复制状态、各持一个会话,破坏单会话语义
    uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
