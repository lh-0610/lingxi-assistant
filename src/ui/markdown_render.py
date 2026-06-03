"""Markdown 渲染 + 思考块管理（mixin for ChatUI）。

从 chat_window.py 抽出来的 AI 输出渲染相关方法：

- `render_final_markdown`：agent 线程入口（通过 Signal 走主线程）+ TTS hook
- `_render_markdown`：UI 线程槽，把流式纯文本替换成 Markdown HTML
- `_md_to_html`：核心转换，主题色全部 inline 进 HTML（QTextBrowser 不吃 <style>）
- `_remove_thinking` / `_update_thinking`：思考指示器原地替换
- `_show_thinking_dialog`：右侧弹窗看完整思考过程

依赖宿主提供：self.bridge / self.chat_area / self._t / self._scroll_guard /
self._inline_svg_img / self._svg_icon / self._ai_reply_start /
self._msg_buffers / self._code_blocks / self._thinking_* / self._tts*
"""
from PySide6.QtGui import QTextCursor, QTextCharFormat, QColor, QFont

from .helpers import _strip_markdown_for_tts


class MarkdownRenderMixin:
    """AI 回复 Markdown 渲染 + 思考块原地替换的全部逻辑。"""

    def render_final_markdown(self, md_text, speak=True):
        """通知 UI 渲染最终 Markdown（从 agent 线程调用）。

        speak=True 时朗读（仅最终回复用）；多轮工具调用的中间轮传 speak=False，
        只渲染不朗读——否则 TTS 会把每轮"我先看下文件"之类的过程话都念出来。
        """
        # 后台会话 / 本轮被切走过的会话：不实时渲染（标 needs_redraw，完成时整体重绘）
        from .. import session as _session
        _sess = _session.current_session()
        if _sess is not _session.get_active() or _sess.suppress_render:
            _sess.needs_redraw = True
            return
        self.bridge.render_md.emit(md_text)
        if not speak:
            return
        # 朗读 AI 回复（如果 TTS 开关打开）
        from ..paths import logger as _lg
        tts_enabled = getattr(self, "_tts_enabled", False)
        tts_obj = getattr(self, "_tts", None)
        _lg.info(f"[TTS hook] enabled={tts_enabled}, tts_obj={tts_obj is not None}, text_len={len(md_text)}")
        if tts_enabled and tts_obj is not None:
            plain = _strip_markdown_for_tts(md_text)
            _lg.info(f"[TTS hook] 触发 speak, plain_len={len(plain)}")
            if plain.strip():
                tts_obj.speak(plain)

    def _md_to_html(self, md_text):
        """Markdown 转带内联样式的 HTML（QTextBrowser 不支持 <style> 标签）"""
        import markdown
        import re as _re

        # 注意：不要做 `md_text.replace('\n\n', '\n&nbsp;\n')` 这种"保留空行"的 hack——
        # 它会删掉所有空行，而 markdown 靠空行分隔块（表格靠空行结束、标题/列表靠空行分隔）。
        # 空行没了，整篇会被当成一个块：表格后面的标题 / 列表会被表格扩展整行吞成单元格。
        # 段落间距交给下面的 <p style="margin:6px 0"> 处理即可。

        # 保留 *xxx* 字面量（动作描写不渲染成斜体），但保留 **xxx** 加粗：
        # 1) 先用占位符暂存 **...** 加粗（用控制字符避免与正常文本冲突）
        _BOLD_OPEN = '\x01B\x02'
        _BOLD_CLOSE = '\x01E\x02'
        md_text = _re.sub(r'\*\*([^*]+)\*\*', lambda m: f'{_BOLD_OPEN}{m.group(1)}{_BOLD_CLOSE}', md_text)
        # 2) 转义剩余单 * 为字面量
        md_text = md_text.replace('*', r'\*')
        # 3) 恢复加粗占位符
        md_text = md_text.replace(_BOLD_OPEN, '**').replace(_BOLD_CLOSE, '**')

        html = markdown.markdown(md_text, extensions=['tables', 'fenced_code', 'nl2br'])

        # 去掉 <ul>/<ol>/<li> 列表标签，保留纯文本换行
        html = _re.sub(r'</?ul[^>]*>', '', html)
        html = _re.sub(r'</?ol[^>]*>', '', html)
        html = _re.sub(r'<li[^>]*>', '<p style="margin:2px 0;">', html)
        html = html.replace('</li>', '</p>')
        # 去掉 <em>/<i> 斜体标签，避免动作描述*...* 与正常对话字体不一样
        html = _re.sub(r'</?em[^>]*>', '', html)
        html = _re.sub(r'</?i[^>]*>', '', html)
        html = html.replace('<p>', '<p style="margin:6px 0;">')
        html = html.replace(
            '<code>',
            f'<code style="background:{self._t("md_code_bg")};color:{self._t("md_code_text")};'
            f'padding:1px 6px;font-family:Consolas,\'Cascadia Code\';font-size:14px;border-radius:3px;">'
        )
        pre_border = self._t("md_pre_border_left")
        pre_border_css = (
            f'border-left:2px solid {pre_border};' if pre_border != "transparent" else ''
        )
        html = html.replace(
            '<pre>',
            f'<pre style="background:{self._t("md_pre_bg")};color:{self._t("md_pre_text")};'
            f'padding:14px 16px;font-family:Consolas,\'Cascadia Code\';font-size:14px;'
            f'white-space:pre-wrap;{pre_border_css}border-radius:6px;">'
        )

        # ---- #5 Code block copy buttons ----
        copy_bg = self._t("md_copy_btn_bg")
        copy_text = self._t("md_copy_btn_text")
        copy_border = self._t("md_copy_btn_border")
        copy_border_css = (
            f'border:1px solid {copy_border};' if copy_border != "transparent" else 'border:none;'
        )
        def _add_copy_btn(match):
            block = match.group(0)
            code_match = _re.search(r'<code[^>]*>(.*?)</code>', block, _re.DOTALL)
            raw_code = code_match.group(1) if code_match else block
            raw_code = (raw_code.replace('&amp;', '&').replace('&lt;', '<')
                        .replace('&gt;', '>').replace('&nbsp;', ' ').replace('&quot;', '"'))
            idx = len(self._code_blocks)
            self._code_blocks[str(idx)] = raw_code
            copy_icon = self._inline_svg_img("copy_lucide.svg", copy_text, 14, "Copy")
            return (
                f'<div style="position:relative;">'
                f'<a href="action:copy_code:{idx}" '
                f'style="position:absolute;top:4px;right:4px;z-index:1;'
                f'background:{copy_bg};color:{copy_text};font-size:13px;padding:3px 8px;'
                f'{copy_border_css}border-radius:6px;text-decoration:none;" title="复制代码">{copy_icon}</a>'
                f'{block}</div>'
            )
        html = _re.sub(r'<pre[^>]*>.*?</pre>', _add_copy_btn, html, flags=_re.DOTALL)

        html = html.replace(
            '<table>',
            f'<table style="border-collapse:collapse;margin:8px 0;border:1px solid {self._t("md_table_border")};"'
            f' cellpadding="6" cellspacing="0">'
        )
        html = html.replace(
            '<th>',
            f'<th style="background:{self._t("md_th_bg")};color:{self._t("md_th_text")};'
            f'padding:6px 12px;border:1px solid {self._t("md_table_border")};font-weight:600;">'
        )
        html = html.replace(
            '<td>',
            f'<td style="padding:6px 12px;border:1px solid {self._t("md_table_border")};'
            f'color:{self._t("md_td_text")};">'
        )
        bq_bg = self._t("md_blockquote_bg")
        bq_bg_css = f'background:{bq_bg};' if bq_bg != "transparent" else ''
        html = html.replace(
            '<blockquote>',
            f'<blockquote style="border-left:3px solid {self._t("md_blockquote_border")};margin:6px 0;'
            f'padding:4px 14px;color:{self._t("md_blockquote_text")};{bq_bg_css}">'
        )
        # 标题
        for i in range(1, 4):
            html = html.replace(
                f'<h{i}>', f'<h{i} style="margin:14px 0 6px 0;color:{self._t("md_h_color")};">'
            )

        return (
            f'<div style="color:{self._t("md_text")};font-size:15px;'
            f'font-family:\'Microsoft YaHei\',\'Microsoft YaHei UI\',\'Segoe UI\';line-height:1.7;">'
            f'{html}</div>'
        )

    def _render_markdown(self, md_text):
        """用 Markdown 渲染结果替换纯文本 AI 回复"""
        if self._ai_reply_start is None:
            return

        scroll = self._scroll_guard()
        styled_html = self._md_to_html(md_text)

        cursor = self.chat_area.textCursor()
        cursor.setPosition(self._ai_reply_start)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        cursor.insertHtml(styled_html)
        # QTextDocument 对 <div margin> / <p padding> 的支持非常有限，靠它给按钮
        # 留空白基本失效。最稳的办法是直接插一个表格 spacer——固定高度的空 <td>
        # 是 HTML 邮件里通用的留白手法，QTextDocument 完全认。
        spacer = '<table border="0" cellspacing="0" cellpadding="0"><tr><td style="height:18px;font-size:1px;line-height:1px;">&nbsp;</td></tr></table>'
        cursor.insertHtml(spacer)

        # ---- #6 Copy / Regenerate action links ----
        msg_idx = len(self._msg_buffers)
        self._msg_buffers[str(msg_idx)] = md_text
        copy_icon = self._inline_svg_img("copy_lucide.svg", self._t("copy_link"), 15, "Copy")
        regen_icon = self._inline_svg_img("refresh_cw_lucide.svg", self._t("copy_link"), 15, "Regenerate")
        cursor.insertHtml(
            f'<a href="action:copy_msg:{msg_idx}" style="color:{self._t("copy_link")};font-size:13px;'
            f'text-decoration:none;padding:3px 8px;background:{self._t("copy_link_bg")};border-radius:5px;" title="复制">'
            f'{copy_icon}</a>'
            f'&nbsp;<a href="action:regenerate" style="color:{self._t("copy_link")};font-size:13px;'
            f'text-decoration:none;padding:3px 8px;background:{self._t("copy_link_bg")};border-radius:5px;" title="重新生成">'
            f'{regen_icon}</a>'
        )
        cursor.insertText("\n\n")

        self._ai_reply_start = None

        scroll()

    def _remove_thinking(self):
        """精确移除思考指示器"""
        if not hasattr(self, '_thinking_start') or self._thinking_start is None:
            return
        if self._thinking_end is None:
            self._thinking_start = None
            return
        cursor = self.chat_area.textCursor()
        cursor.setPosition(self._thinking_start)
        cursor.setPosition(self._thinking_end, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        self._thinking_start = None
        self._thinking_end = None

    def _update_thinking(self, text):
        """更新等待指示器文本（原地替换）"""
        if not hasattr(self, '_thinking_start') or self._thinking_start is None:
            return
        cursor = self.chat_area.textCursor()
        cursor.setPosition(self._thinking_start)
        cursor.setPosition(self._thinking_end, QTextCursor.KeepAnchor)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self._t("thinking")))
        thinking_font = QFont("Microsoft YaHei")
        thinking_font.setPixelSize(14)
        thinking_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        thinking_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        fmt.setFont(thinking_font)
        fmt.setBackground(QColor(self._t("thinking_bg")))
        cursor.insertText(text, fmt)
        self._thinking_end = cursor.position()

    def _show_thinking_dialog(self, think_id):
        """在主窗口右侧弹出思考过程"""
        content = self._thinking_history.get(think_id, "")
        if not content:
            return

        from PySide6.QtWidgets import QDialog, QVBoxLayout, QTextBrowser as _QTB, QLabel

        if self._thinking_dialog is None:
            dlg = QDialog(self)
            dlg.setWindowTitle("思考过程")
            icon = self._svg_icon("brain_lucide.svg", self._t("think_dlg_label"))
            if not icon.isNull():
                dlg.setWindowIcon(icon)
            dlg.resize(450, 600)
            layout = QVBoxLayout(dlg)
            layout.setContentsMargins(12, 12, 12, 12)
            label = QLabel("思考过程")
            label.setStyleSheet(
                f"color:{self._t('think_dlg_label')};font-weight:bold;font-size:17px;"
                f"padding:4px 0;letter-spacing:{self._t('think_dlg_letter_sp')};"
            )
            layout.addWidget(label)
            browser = _QTB(dlg)
            browser.setStyleSheet(
                f"QTextBrowser {{ background:{self._t('think_dlg_bg')}; color:{self._t('think_dlg_text')}; "
                f"font-size:14px; padding:14px; border:1px solid {self._t('think_dlg_border')}; border-radius:10px; "
                f"selection-background-color:{self._t('chat_sel_bg')}; }}"
                f"QScrollBar:vertical {{ width: 6px; background: transparent; }}"
                f"QScrollBar::handle:vertical {{ background: {self._t('chat_scroll_handle')}; border-radius: 3px; }}"
                f"QScrollBar::handle:vertical:hover {{ background: {self._t('chat_scroll_handle_hover')}; }}"
                f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
            )
            layout.addWidget(browser)
            dlg._browser = browser
            self._thinking_dialog = dlg

        self._thinking_dialog._browser.setPlainText(content)
        # 定位到主窗口右侧
        main_geom = self.geometry()
        screen_geom = self.screen().availableGeometry()
        x = min(main_geom.right() + 10, screen_geom.right() - 460)
        y = main_geom.top()
        self._thinking_dialog.move(x, y)
        self._thinking_dialog.show()
        self._thinking_dialog.raise_()
        self._thinking_dialog.activateWindow()
