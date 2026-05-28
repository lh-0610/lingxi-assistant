"""UI 杂项工具：图标生成、图片协议块构造、Markdown→TTS 文本剥离、HTML 转义。

这些都是 UI 模块内部的无状态 helper，与 ChatUI 解耦，可以被任何 ui/ 子模块复用。
"""
import re

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import (
    QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygon,
)

from .. import agent


def _build_image_content_block(ext, b64):
    """根据当前模型类型，构造正确格式的图片内容块
    - Anthropic 协议 (mimo, anthropic): {"type": "image", "source": {...}}
    - OpenAI 协议 (cloud, ollama, etc): {"type": "image_url", "image_url": {...}}
    """
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
            "gif": "gif", "bmp": "bmp", "webp": "webp"}.get(ext, "png")
    mtype = agent.MODEL_LIST[agent.current_model_index][1]
    if mtype in ("anthropic", "mimo"):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": f"image/{mime}",
                "data": b64,
            }
        }
    else:
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/{mime};base64,{b64}"}
        }


def _make_button_icon(arrow=True):
    """程序化绘制发送上箭头/停止(暂停)图标，白色，透明背景"""
    size = 30
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#ffffff"))
    if arrow:
        # 上箭头：三角 + 矩形
        tri = QPolygon([QPoint(15, 4), QPoint(6, 15), QPoint(24, 15)])
        p.drawPolygon(tri)
        p.drawRect(12, 14, 6, 12)
    else:
        # 暂停：两个竖
        p.drawRoundedRect(6, 5, 6, 20, 2, 2)
        p.drawRoundedRect(18, 5, 6, 20, 2, 2)
    p.end()
    return QIcon(px)


def _make_upload_icon(color="#888888"):
    """绘制上传文件图标（文档轮廓+上传箭头），单色，可适配主题"""
    size = 30
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)

    # 文档轮廓（带右上折角）
    p.setPen(QPen(c, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    doc = QPainterPath()
    doc.moveTo(8, 4)
    doc.lineTo(18, 4)
    doc.lineTo(22, 8)
    doc.lineTo(22, 26)
    doc.lineTo(8, 26)
    doc.lineTo(8, 4)
    p.drawPath(doc)
    # 折角小三角
    fold = QPainterPath()
    fold.moveTo(18, 4)
    fold.lineTo(18, 8)
    fold.lineTo(22, 8)
    p.drawPath(fold)

    # 上传箭头（竖线 + V形）
    p.setPen(QPen(c, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.drawLine(QPoint(15, 24), QPoint(15, 15))
    p.drawLine(QPoint(11, 18), QPoint(15, 14))
    p.drawLine(QPoint(19, 18), QPoint(15, 14))

    p.end()
    return QIcon(px)


def _strip_markdown_for_tts(md_text: str) -> str:
    """把 Markdown 简化成适合 TTS 朗读的纯文本：去掉 *、#、链接、代码块等。
    注意：`*xxx*` 在角色卡场景里用于动作描写（如 *轻抚发梢*），不该朗读，整段删掉。
    """
    s = md_text or ""
    # 代码块整段移除（朗读代码没意义）
    s = re.sub(r"```[\s\S]*?```", "（代码块略）", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    # 图片
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)
    # 链接：保留文字，去 URL
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    # 标题、引用、列表前缀
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s{0,3}>\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.MULTILINE)
    # 强调：**加粗** 保留内容；*动作描写* 整段删掉（角色卡常用 *xxx* 表示动作）
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*[^*\n]+\*", "", s)
    # 同理处理常被用作动作描写的下划线对：_xxx_ 也整段删（保守）
    # 但避免误伤变量名（用 \b 边界 + 配对）
    s = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", "", s)
    s = re.sub(r"~~([^~]+)~~", r"\1", s)
    # 表格分隔
    s = re.sub(r"^\s*\|[\s\-:|]+\|\s*$", "", s, flags=re.MULTILINE)
    # 删掉因为去掉动作而出现的多余空行
    s = re.sub(r"\n{3,}", "\n\n", s)
    # 行首行尾的空白每行清一下
    s = "\n".join(line.rstrip() for line in s.splitlines())
    # 过滤 GBK 编不了的字符（emoji / 特殊符号）
    # GPT-SoVITS API 内部用 GBK 处理文本，遇到 ✨🎀💫 之类的会 400 报错
    try:
        s = s.encode("gbk", errors="ignore").decode("gbk")
    except Exception:
        pass
    return s.strip()


def _escape(text):
    """HTML 转义"""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
            .replace(" ", "&nbsp;"))
