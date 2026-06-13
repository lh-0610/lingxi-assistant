# 灵犀 Web / PWA 前端

手机浏览器 / 任意 HTTP 客户端访问灵犀 AI 对话。与桌面版**共用同一套核心**(`src/`),不依赖 Qt。

## 依赖

```bash
pip install fastapi uvicorn
```

(只装这两个;不影响桌面端,也不进桌面打包。)

## 三种部署(同一套代码)

### ① 本机自用(默认,最安全)

```bash
python web/serve.py
```

只绑 `127.0.0.1:8787`,仅本机浏览器可访问。启动时会打印带 token 的访问链接,直接点开即用。

### ② 局域网 → 手机版灵犀(零服务器成本)

```bash
python web/serve.py --host 0.0.0.0
```

启动会打印 `http://<本机内网IP>:8787/?token=xxx`。**手机连同一个 WiFi**,浏览器打开这个链接即可;
在手机浏览器"添加到主屏幕"后就是一个 App。确保防火墙放行 8787。

### ③ 云服务器常驻

```bash
# systemd unit 示例 /etc/systemd/system/lingxi-web.service
[Service]
WorkingDirectory=/path/to/lingxi
ExecStart=/usr/bin/python3 web/serve.py --host 0.0.0.0 --port 8787
Restart=always
```

```bash
systemctl enable --now lingxi-web
```

**公网部署务必**:① 上 HTTPS(套 nginx / caddy 反代 + 证书);② 防火墙只放 SSH 和该端口;
③ 见下方"工具权限"。

## 鉴权 token

- **默认必有 token,绝不裸奔**。优先级:`--token` > 环境变量 `LINGXI_WEB_TOKEN` > `config.json` 的 `web.token`
  > **自动生成**(持久化到 `chat_memory/web_token.json`,下次启动复用、链接不变)。
- 携带方式:URL `?token=xxx`(首次点链接/扫码,前端会自动存下并从地址栏抹掉)、
  请求头 `X-Auth-Token` 或 `Authorization: Bearer xxx`。
- token 校验用 `secrets.compare_digest`(防时序攻击)。

## 工具权限(重要)

Web 会话默认打 `remote_session=True`,复用桌面端的"遥控安全分级"(`config.json` 的 `remote_control.mode`):

| mode | 行为 |
|---|---|
| `chat_only`(默认/未配) | 禁所有工具,纯对话——**最安全,公网首选** |
| `safe_readonly` | 只放行 read_file / search_in_file / list_directory + 敏感文件黑名单 |
| `unrestricted` | 不拦读类工具 |

另外**写类工具(改文件 / 跑命令)在 Web 端一律被确认环节拒绝**(`confirm_* → False`),即使 mode=unrestricted
也不会远程改你的文件/执行命令(M1 没有网页确认卡)。**公网暴露建议把 `remote_control.mode` 设为 `chat_only` 或 `safe_readonly`。**

**联网查询独立开关**:`remote_control.allow_web_search: true`(默认 false)单独放行 `fetch_url` / `web_search`
(只读网络工具,fetch_url 有 SSRF 防护拒内网/本机/云元数据),**不论 mode 是什么都生效、且不碰文件读写**。
想让手机版灵犀"能上网查"但不开放文件读时用它(保持 mode=chat_only + allow_web_search=true 即可)。
`web_search` 还需配 `web_search_api_key`(Tavily);没配则 `fetch_url` 仍可用、`web_search` 优雅降级。

## PWA 安装

手机浏览器打开后添加到主屏幕:iOS Safari「分享 → 添加到主屏幕」;Android Chrome「菜单 → 添加到主屏幕」。

## 常见问题

- **手机连不上**:确认手机和电脑同一 WiFi、防火墙放行端口、启动用了 `--host 0.0.0.0`。
- **Telegram 与 Web 并存**:互不影响,Telegram 遥控继续用,Web 是不依赖第三方的通用通道。
- **服务器内存紧(2C2G)**:`config.json` 别配 `mcp_servers`(npx/node 吃内存)。

## 架构

```
手机/浏览器 ──HTTP, NDJSON 流──▶ FastAPI(web/app.py)
                                  │  HeadlessWebUI(无 Qt,confirm_* 一律拒绝)
                                  │  ChatService(一个常驻 session.Session,remote_session=True)
                                  ▼
                            src/agent.py  agent_loop(ui)  ← 与桌面完全同一套核心
```

- `/api/chat`(POST):NDJSON 流式(`application/x-ndjson`,逐行 `{"type":...}`);事件 `msg`/`md`/`retry`/`done`/`error`/`ping`。
- `/api/status` `/api/stop` `/api/history`:状态 / 停止 / 历史。
- 单进程(uvicorn 默认 workers=1):多 worker 会各持一个会话,破坏单会话语义。

> M1 范围:流畅的手机聊天。**不含**:网页内联确认卡(放开写工具)、会话列表切换、语音/图片、原生 App——后续里程碑。
