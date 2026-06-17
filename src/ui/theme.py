"""主题系统：light（白天 · 原版 ChatGPT 风）+ dark（墨翠 · 夜间）。

THEMES 是单一颜色字典，build_stylesheet 把它编译成全局 QSS。
load/save_theme_choice 负责把选中的主题持久化到 chat_memory/theme_config.json。
"""
import json
import os

from ._base import THEME_CONFIG_PATH


THEMES = {
    "light": {
        # 主表面
        "win_bg":            "#f8f9fb",
        "sidebar_bg":        "#f1f3f7",
        "sidebar_border":    "#e2e6ee",
        "header_bg":         "#f8f9fb",
        "header_border":     "#e6e9f0",
        "chat_bg":           "#f8f9fb",
        "input_bg":          "#ffffff",
        "input_border":      "#dfe4ee",
        "input_text":        "#1f2430",
        "input_sel_bg":      "#dfe6ff",
        "input_sel_text":    "#1f2430",
        # 文本
        "text":              "#1f2430",
        "text_dim":          "#6f7785",
        "text_subtle":       "#9aa3b2",
        "text_muted":        "#c5ccd8",
        # 历史项
        "history_label":           "#8a94a5",
        "history_label_spacing":   "0px",
        "history_item":            "#2c3442",
        "history_hover_bg":        "#e8ecf4",
        "history_hover_text":      "#1f2430",
        "history_active_bg":       "#e5e9ff",
        "history_active_text":     "#232b7a",
        "history_active_border":   "#5b66d6",
        "history_active_pad_left": "10px",
        # 滚动条
        "scrollbar_track":   "transparent",
        "scrollbar_handle":  "#c8cfda",
        "scrollbar_handle_hover": "#a8b2c1",
        "scrollbar_thin":    "#c8cfda",
        # 删除按钮
        "del_btn":           "#aab2bf",
        "del_btn_hover":     "#d45d4c",
        "del_btn_hover_bg":  "#fff0ed",
        # New chat 按钮
        "new_chat_bg":           "#ffffff",
        "new_chat_text":         "#1f2430",
        "new_chat_border":       "#dfe4ee",
        "new_chat_hover_bg":     "#e8ecff",
        "new_chat_hover_border": "#c7cdfc",
        "new_chat_hover_text":   "#3842b8",
        # Toggle btn
        "toggle":            "#7a8392",
        "toggle_hover":      "#343b4c",
        "toggle_hover_bg":   "#edf0f6",
        # 品牌字符
        "brand_visible":     "true",
        "brand_color":       "#4c57c8",
        "brand_dot":         "#d87755",
        "brand_letter_sp":   "2px",
        "header_title_font": "Microsoft YaHei UI",
        "header_title_letter_sp": "0px",
        # Footer
        "footer":            "#b4bdca",
        "footer_letter_sp":  "0px",
        # 发送按钮
        "send_disabled_bg":  "#d2d8e2",
        "send_active_bg":    "#5b66d6",
        "send_active_hover": "#4b55c4",
        "send_stop_bg":      "#d87755",
        "send_stop_hover":   "#c26345",
        "send_text":         "#ffffff",
        # Combobox
        "combo_bg":            "#ffffff",
        "combo_border":        "#dfe4ee",
        "combo_text":          "#28303d",
        "combo_hover_border":  "#b8bff5",
        "combo_hover_text":    "#3842b8",
        "combo_arrow":         "#737d8c",
        "combo_view_bg":       "#ffffff",
        "combo_view_border":   "#dfe4ee",
        "combo_view_text":     "#28303d",
        "combo_view_sel_bg":   "#e8ecff",
        "combo_view_sel_text": "#232b7a",
        # Think btn
        "think_on_bg":     "#e8ecff",
        "think_on_border": "#b8bff5",
        "think_on_text":   "#3842b8",
        "think_on_hover":  "#dde3ff",
        "think_on_hover_border": "#9ea7ed",
        "think_off_bg":    "#ffffff",
        "think_off_border":"#dfe4ee",
        "think_off_text":  "#8b94a3",
        "think_off_hover_border":"#c8d0de",
        "think_off_hover_text":  "#697386",
        # Role btn idle
        "role_bg":            "#ffffff",
        "role_border":        "#dfe4ee",
        "role_text":          "#697386",
        "role_hover_bg":      "#f7f8fb",
        "role_hover_border":  "#c8d0de",
        "role_hover_text":    "#343b4c",
        # Role btn active（已加载角色）
        "role_active_bg":            "#fff1e9",
        "role_active_border":        "#e7ae92",
        "role_active_text":          "#b25634",
        "role_active_hover_bg":      "#ffe7dc",
        "role_active_hover_border":  "#d58d6b",
        "role_active_hover_text":    "#984323",
        "role_active_weight":        "600",
        # 图片附件
        "img_btn":           "#7f8795",
        "img_btn_hover":     "#5b66d6",
        "img_thumb_border":  "#dfe4ee",
        "img_thumb_bg":      "#ffffff",
        "img_del_bg":        "#e74c3c",
        "img_del_text":      "#ffffff",
        "img_del_hover_bg":  "#c0392b",
        # 聊天区滚动条/选区
        "chat_text":         "#1f2430",
        "chat_sel_bg":       "#dfe6ff",
        "chat_sel_text":     "#1f2430",
        "chat_scroll_handle":       "#c8cfda",
        "chat_scroll_handle_hover": "#a8b2c1",
        # 浮动回到底部按钮
        "scroll_btn_bg":       "#ffffff",
        "scroll_btn_border":   "#dfe4ee",
        "scroll_btn_icon":     "#5b66d6",
        "scroll_btn_hover_bg": "#f4f6fb",
        # 全局菜单
        "menu_bg":           "#ffffff",
        "menu_border":       "#dfe4ee",
        "menu_text":         "#1f2430",
        "menu_sel_bg":       "#e8ecff",
        "menu_sel_text":     "#232b7a",
        "menu_disabled":     "#9aa3b2",
        "menu_separator":    "#e6e9f0",
        # Tooltip
        "tooltip_bg":        "#ffffff",
        "tooltip_text":      "#1f2430",
        "tooltip_border":    "#dfe4ee",
        # 聊天文本（QTextCharFormat）
        "user_label":        "#5b66d6",
        "ai_label":          "#d87755",
        "user_msg":          "#1f2430",
        "ai_msg":            "#1f2430",
        "thinking":          "#5b66d6",
        "thinking_bg":       "#eef1ff",
        "thinking_msg":      "#6f7785",
        "thinking_msg_bg":   "#f5f7fb",
        "tool":              "#5b66d6",
        "tool_bg":           "#eef1ff",
        "tool_result":       "#596273",
        "tool_result_bg":    "#f5f7fb",
        "warn":              "#e74c3c",
        "retry_link":        "#5b66d6",
        "retry_link_bg":     "#eef1ff",
        "retry_link_border": "#dfe4ee",
        "copy_link":         "#8b94a3",
        "copy_link_bg":      "transparent",
        "copy_link_border":  "transparent",
        # Markdown
        "md_text":           "#1f2430",
        "md_code_bg":        "#eef1f6",
        "md_code_text":      "#232b7a",
        "md_pre_bg":         "#f3f5f8",
        "md_pre_text":       "#1f2430",
        "md_pre_border_left":"#c8cdf7",
        "md_table_border":   "#d3d9e4",
        "md_th_bg":          "#eef1f6",
        "md_th_text":        "#232b7a",
        "md_td_text":        "#1f2430",
        "md_blockquote_border":"#c8cdf7",
        "md_blockquote_text":  "#596273",
        "md_blockquote_bg":    "transparent",
        "md_h_color":          "#1f2430",
        "md_copy_btn_bg":      "#e8ecff",
        "md_copy_btn_text":    "#3842b8",
        "md_copy_btn_border":  "#d7dcfb",
        # Drag overlay
        "drag_bg":           "rgba(255, 255, 255, 0.85)",
        "drag_border":       "#5b66d6",
        "drag_border_style": "3px dashed",
        "drag_text":         "#5b66d6",
        "drag_subtext":      "#8b94a3",
        # Toast
        "toast_bg":          "#333333",
        "toast_text":        "#ffffff",
        "toast_border":      "transparent",
        # Search bar
        "search_bg":         "#f5f7fb",
        "search_border":     "#dfe4ee",
        "search_input_bg":   "#ffffff",
        "search_input_border":"#dfe4ee",
        "search_input_text": "#1f2430",
        "search_input_focus":"#aeb6ef",
        "search_btn_text":   "#343b4c",
        "search_btn_bg":     "transparent",
        "search_btn_hover_bg":   "#e8ecff",
        "search_btn_hover_color":"#3842b8",
        "search_close":          "#8b94a3",
        "search_close_hover":    "#343b4c",
        "search_close_hover_bg": "transparent",
        # Think dialog
        "think_dlg_label":   "#5b66d6",
        "think_dlg_bg":      "#f8f9fb",
        "think_dlg_text":    "#343b4c",
        "think_dlg_border":  "#dfe4ee",
        "think_dlg_letter_sp":"0px",
    },
    "dark": {
        # 主表面
        "win_bg":            "#0d1117",
        "sidebar_bg":        "#0a0d12",
        "sidebar_border":    "#1a2028",
        "header_bg":         "#0d1117",
        "header_border":     "#1a2028",
        "chat_bg":           "#0d1117",
        "input_bg":          "#161c24",
        "input_border":      "#1f2933",
        "input_text":        "#e8e2d4",
        "input_sel_bg":      "#2a4a3c",
        "input_sel_text":    "#e8e2d4",
        # 文本
        "text":              "#e8e2d4",
        "text_dim":          "#b8b1a3",
        "text_subtle":       "#5a6470",
        "text_muted":        "#2f3a47",
        # 历史项
        "history_label":           "#4a5560",
        "history_label_spacing":   "2px",
        "history_item":            "#b8b1a3",
        "history_hover_bg":        "#161c24",
        "history_hover_text":      "#e8e2d4",
        "history_active_bg":       "#18221c",
        "history_active_text":     "#e8e2d4",
        "history_active_border":   "#6fa090",
        "history_active_pad_left": "10px",
        # 滚动条
        "scrollbar_track":   "transparent",
        "scrollbar_handle":  "#2a3440",
        "scrollbar_handle_hover":"#6fa090",
        "scrollbar_thin":    "#2a3440",
        # 删除按钮
        "del_btn":           "#4a5560",
        "del_btn_hover":     "#e07a5f",
        "del_btn_hover_bg":  "#2a1812",
        # New chat 按钮
        "new_chat_bg":           "#111820",
        "new_chat_text":         "#e8e2d4",
        "new_chat_border":       "#2a3440",
        "new_chat_hover_bg":     "#161c24",
        "new_chat_hover_border": "#6fa090",
        "new_chat_hover_text":   "#b9d4c5",
        # Toggle btn
        "toggle":            "#5a6470",
        "toggle_hover":      "#e8e2d4",
        "toggle_hover_bg":   "#161c24",
        # 品牌字符
        "brand_visible":     "true",
        "brand_color":       "#6fa090",
        "brand_dot":         "#b87a52",
        "brand_letter_sp":   "6px",
        "header_title_font": "KaiTi",
        "header_title_letter_sp": "6px",
        # Footer
        "footer":            "#2f3a47",
        "footer_letter_sp":  "2px",
        # 发送按钮
        "send_disabled_bg":  "#1f2933",
        "send_active_bg":    "#6fa090",
        "send_active_hover": "#84b8a4",
        "send_stop_bg":      "#b87a52",
        "send_stop_hover":   "#c89060",
        "send_text":         "#ffffff",
        # Combobox
        "combo_bg":            "#161c24",
        "combo_border":        "#2a3440",
        "combo_text":          "#e8e2d4",
        "combo_hover_border":  "#6fa090",
        "combo_hover_text":    "#b9d4c5",
        "combo_arrow":         "#6fa090",
        "combo_view_bg":       "#161c24",
        "combo_view_border":   "#2a3440",
        "combo_view_text":     "#e8e2d4",
        "combo_view_sel_bg":   "#18221c",
        "combo_view_sel_text": "#b9d4c5",
        # Think btn
        "think_on_bg":     "#18221c",
        "think_on_border": "#6fa090",
        "think_on_text":   "#b9d4c5",
        "think_on_hover":  "#1f2e26",
        "think_on_hover_border": "#84b8a4",
        "think_off_bg":    "transparent",
        "think_off_border":"#2a3440",
        "think_off_text":  "#5a6470",
        "think_off_hover_border":"#4a5560",
        "think_off_hover_text":  "#b8b1a3",
        # Role btn idle
        "role_bg":            "transparent",
        "role_border":        "#2a3440",
        "role_text":          "#b8b1a3",
        "role_hover_bg":      "#161c24",
        "role_hover_border":  "#6fa090",
        "role_hover_text":    "#b9d4c5",
        # Role btn active
        "role_active_bg":            "#2a1f10",
        "role_active_border":        "#8a6a3a",
        "role_active_text":          "#d4a574",
        "role_active_hover_bg":      "#3a2a18",
        "role_active_hover_border":  "#d4a574",
        "role_active_hover_text":    "#e6b988",
        "role_active_weight":        "600",
        # 图片附件
        "img_btn":           "#5a6470",
        "img_btn_hover":     "#6fa090",
        "img_thumb_border":  "#2a3440",
        "img_thumb_bg":      "#161c24",
        "img_del_bg":        "#c87060",
        "img_del_text":      "#0d1117",
        "img_del_hover_bg":  "#e07a5f",
        # 聊天区滚动条/选区
        "chat_text":         "#e8e2d4",
        "chat_sel_bg":       "#2a4a3c",
        "chat_sel_text":     "#e8e2d4",
        "chat_scroll_handle":       "#2a3440",
        "chat_scroll_handle_hover": "#4a7060",
        # 全局菜单
        "menu_bg":           "#161c24",
        "menu_border":       "#2a3440",
        "menu_text":         "#e8e2d4",
        # 浮动回到底部按钮
        "scroll_btn_bg":       "#1f2933",
        "scroll_btn_border":   "#2a3440",
        "scroll_btn_icon":     "#b8b1a3",
        "scroll_btn_hover_bg": "#2a3440",
        "menu_sel_bg":       "#18221c",
        "menu_sel_text":     "#b9d4c5",
        "menu_disabled":     "#5a6470",
        "menu_separator":    "#2a3440",
        # Tooltip
        "tooltip_bg":        "#1a2129",
        "tooltip_text":      "#b8b1a3",
        "tooltip_border":    "#2a3440",
        # 聊天文本
        "user_label":        "#d4a574",
        "ai_label":          "#6fa090",
        "user_msg":          "#e8e2d4",
        "ai_msg":            "#e8e2d4",
        "thinking":          "#b794d6",
        "thinking_bg":       "#1a1525",
        "thinking_msg":      "#9ca3a0",
        "thinking_msg_bg":   "#15191f",
        "tool":              "#8ba3c3",
        "tool_bg":           "#141a22",
        "tool_result":       "#9ca3a0",
        "tool_result_bg":    "#141a22",
        "warn":              "#e07a5f",
        "retry_link":        "#6fa090",
        "retry_link_bg":     "#18221c",
        "retry_link_border": "#2a4a3c",
        "copy_link":         "#6fa090",
        "copy_link_bg":      "#141a22",
        "copy_link_border":  "transparent",
        # Markdown
        "md_text":           "#e8e2d4",
        "md_code_bg":        "#1a2129",
        "md_code_text":      "#b9d4c5",
        "md_pre_bg":         "#141a22",
        "md_pre_text":       "#e8e2d4",
        "md_pre_border_left":"#6fa090",
        "md_table_border":   "#2a3440",
        "md_th_bg":          "#161c24",
        "md_th_text":        "#6fa090",
        "md_td_text":        "#e8e2d4",
        "md_blockquote_border":"#6fa090",
        "md_blockquote_text":  "#9ca3a0",
        "md_blockquote_bg":    "#141a22",
        "md_h_color":          "#e8e2d4",
        "md_copy_btn_bg":      "#18221c",
        "md_copy_btn_text":    "#6fa090",
        "md_copy_btn_border":  "#2a4a3c",
        # Drag overlay
        "drag_bg":           "rgba(13, 17, 23, 0.92)",
        "drag_border":       "#6fa090",
        "drag_border_style": "2px dashed",
        "drag_text":         "#6fa090",
        "drag_subtext":      "#5a6470",
        # Toast
        "toast_bg":          "#1a2129",
        "toast_text":        "#b9d4c5",
        "toast_border":      "#2a4a3c",
        # Search bar
        "search_bg":         "#161c24",
        "search_border":     "#2a3440",
        "search_input_bg":   "#0d1117",
        "search_input_border":"#2a3440",
        "search_input_text": "#e8e2d4",
        "search_input_focus":"#6fa090",
        "search_btn_text":   "#b8b1a3",
        "search_btn_bg":     "transparent",
        "search_btn_hover_bg":   "#2a3440",
        "search_btn_hover_color":"#6fa090",
        "search_close":          "#5a6470",
        "search_close_hover":    "#e07a5f",
        "search_close_hover_bg": "#2a3440",
        # Think dialog
        "think_dlg_label":   "#b794d6",
        "think_dlg_bg":      "#0d1117",
        "think_dlg_text":    "#b8b1a3",
        "think_dlg_border":  "#2a3440",
        "think_dlg_letter_sp":"2px",
    },
}

DEFAULT_THEME = "light"


def load_saved_theme():
    """读取持久化的主题选择，失败回到默认"""
    try:
        with open(THEME_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        name = data.get("theme")
        if name in THEMES:
            return name
    except Exception:
        pass
    return DEFAULT_THEME


def save_theme_choice(name):
    try:
        os.makedirs(os.path.dirname(THEME_CONFIG_PATH), exist_ok=True)
        with open(THEME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"theme": name}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def build_tooltip_qss(theme):
    """只含 QToolTip 的 QSS。

    QToolTip 是独立顶层弹窗，**不继承主窗口的 setStyleSheet**——必须设到
    QApplication 级别才生效。否则 tooltip 会用 Qt/系统默认（Windows 上是黑底），
    跟主题对不上。所以单独抽出来，由 ChatUI 设到 app 上。
    """
    p = THEMES[theme]
    return f"""
QToolTip {{
    background-color: {p['tooltip_bg']};
    color: {p['tooltip_text']};
    border: 1px solid {p['tooltip_border']};
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 11px;
}}
"""


def build_stylesheet(theme):
    p = THEMES[theme]
    return f"""
QMainWindow, QDialog {{
    background-color: {p['win_bg']};
    font-family: "Microsoft YaHei", "Microsoft YaHei UI", "Segoe UI";
}}

/* 侧边栏 */
#sidebar {{
    background-color: {p['sidebar_bg']};
    border-right: 1px solid {p['sidebar_border']};
}}
#sidebarBrand {{
    background: transparent;
}}
#sidebarBrandTitle {{
    color: {p['text']};
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 1px;
}}
#sidebarBrandSub {{
    color: {p['text_subtle']};
    font-size: 10px;
}}
#sidebarFooter {{
    background: transparent;
    border: none;
}}
#sidebarSettingsBtn {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    color: {p['text_dim']};
    font-size: 18px;
    padding: 0;
}}
#sidebarSettingsBtn:hover {{
    background: {p['history_hover_bg']};
    color: {p['text']};
    border-color: {p['sidebar_border']};
}}
#sidebarSettingsBtn:pressed {{
    background: {p['history_active_bg']};
}}
#sidebar QPushButton#newChatBtn {{
    background: {p['new_chat_bg']};
    border: 1px solid {p['new_chat_border']};
    color: {p['new_chat_text']};
    font-size: 14px;
    font-weight: 600;
    text-align: left;
    padding: 10px 16px;
    border-radius: 8px;
}}
#sidebar QPushButton#newChatBtn:hover {{
    background-color: {p['new_chat_hover_bg']};
    border-color: {p['new_chat_hover_border']};
    color: {p['new_chat_hover_text']};
}}
#sidebar QPushButton#projectHeader {{
    background: transparent;
    border: none;
    color: {p['history_label']};
    font-size: 12px;
    font-weight: 700;
    text-align: left;
    padding: 8px 10px 4px 10px;
    letter-spacing: 0.5px;
}}
#sidebar QPushButton#projectHeader:hover {{
    color: {p['text']};
}}
#sidebar QPushButton#projectHeaderActive {{
    background: transparent;
    border: none;
    color: {p['new_chat_hover_text']};
    font-size: 12px;
    font-weight: 700;
    text-align: left;
    padding: 8px 10px 4px 10px;
    letter-spacing: 0.5px;
}}
#sidebar #historyRow {{
    background: transparent;
}}
#sidebar #historyEmptyHint {{
    color: {p['history_label']};
    font-size: 11px;
    padding: 2px 0 6px 22px;
}}
#historyLabel {{
    color: {p['history_label']};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: {p['history_label_spacing']};
    padding: 18px 14px 8px 14px;
}}

/* 历史项 */
/* padding-right=0 让右侧的删除按钮(×)能贴近文字、不被 padding 推开。
 * 之前在 sidebar.py 里用 btn.setStyleSheet 注入这一句，会让 btn 自带 stylesheet
 * 从而切断 app 级 QToolTip 规则继承——tooltip 颜色异常。移到这里走类选择器就没这个坑。
 */
QPushButton[class="historyItem"] {{
    background: transparent;
    border: none;
    color: {p['history_item']};
    font-size: 13px;
    text-align: left;
    padding: 9px 0 9px 12px;
    border-radius: 8px;
}}
QPushButton[class="historyItem"]:hover {{
    background-color: {p['history_hover_bg']};
    color: {p['history_hover_text']};
}}
QPushButton[class="historyItemActive"] {{
    background-color: {p['history_active_bg']};
    border: none;
    border-left: 2px solid {p['history_active_border']};
    color: {p['history_active_text']};
    font-size: 13px;
    font-weight: 600;
    text-align: left;
    padding: 9px 0 9px {p['history_active_pad_left']};
    border-radius: 8px;
}}
/* 后台会话完成、尚未查看：绿点 + 绿字（切回该会话查看后恢复普通样式）。
 * 绿色两主题通用，故硬编码不走 palette。*/
QPushButton[class="historyItemDone"] {{
    background: transparent;
    border: none;
    color: #22c55e;
    font-size: 13px;
    font-weight: 600;
    text-align: left;
    padding: 9px 0 9px 12px;
    border-radius: 8px;
}}
QPushButton[class="historyItemDone"]:hover {{
    background-color: {p['history_hover_bg']};
    color: #16a34a;
}}

/* 聊天区 */
#chatArea {{
    background-color: {p['chat_bg']};
    border: none;
    padding: 20px;
    font-family: "Microsoft YaHei", "Microsoft YaHei UI", "Segoe UI";
    font-size: 14px;
    color: {p['chat_text']};
}}
#chatArea[empty="true"] {{
    color: {p['text']};
}}
#emptyState {{
    background: transparent;
}}
#emptyLogo {{
    color: {p['brand_color']};
    font-family: "KaiTi", "STKaiti", "Microsoft YaHei UI";
    font-size: 36px;
    letter-spacing: 4px;
}}
#emptyTitle {{
    color: {p['text']};
    font-size: 22px;
    font-weight: 700;
}}
#emptySubtitle {{
    color: {p['text_subtle']};
    font-size: 14px;
}}
#emptySuggestion {{
    background: {p['input_bg']};
    border: 1px solid {p['input_border']};
    border-radius: 10px;
    padding: 12px 20px;
    color: {p['text']};
    font-size: 13px;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI";
    text-align: left;
}}
#emptySuggestion:hover {{
    background: {p['history_hover_bg']};
    border-color: {p['brand_color']};
    color: {p['brand_color']};
}}

/* 输入区容器 */
#inputContainer {{
    background-color: {p['input_bg']};
    border: 1px solid {p['input_border']};
    border-radius: 16px;
    padding: 6px;
}}
#inputEdit {{
    background-color: transparent;
    border: none;
    font-family: "Microsoft YaHei", "Microsoft YaHei UI", "Segoe UI";
    font-size: 14px;
    padding: 8px 12px 50px 12px;
    color: {p['input_text']};
    selection-background-color: {p['input_sel_bg']};
    selection-color: {p['input_sel_text']};
}}

/* 发送按钮 */
#sendBtn {{
    border: none;
    border-radius: 15px;
    min-width: 30px;
    max-width: 30px;
    min-height: 30px;
    max-height: 30px;
    font-size: 16px;
    padding: 0px;
}}
#sendBtn[state="disabled"] {{
    background-color: {p['send_disabled_bg']};
    color: {p['send_text']};
}}
#sendBtn[state="enabled"] {{
    background-color: {p['send_active_bg']};
    color: {p['send_text']};
}}
#sendBtn[state="enabled"]:hover {{
    background-color: {p['send_active_hover']};
}}
#sendBtn[state="stop"] {{
    background-color: {p['send_stop_bg']};
    color: {p['send_text']};
    font-size: 16px;
}}
#sendBtn[state="stop"]:hover {{
    background-color: {p['send_stop_hover']};
}}

/* 顶栏 */
#header {{
    background-color: {p['header_bg']};
    border-bottom: 1px solid {p['header_border']};
}}
#toggleBtn {{
    background: transparent;
    border: none;
    font-size: 18px;
    color: {p['toggle']};
    padding: 4px 8px;
    border-radius: 6px;
}}
#toggleBtn:hover {{
    color: {p['toggle_hover']};
    background: {p['toggle_hover_bg']};
}}
#themeBtn {{
    background: transparent;
    border: none;
    font-size: 16px;
    color: {p['toggle']};
    padding: 4px 8px;
    border-radius: 6px;
}}
#themeBtn:hover {{
    color: {p['toggle_hover']};
    background: {p['toggle_hover_bg']};
}}
#headerTitle {{
    font-family: "{p['header_title_font']}", "Microsoft YaHei", "Microsoft YaHei UI";
    font-size: 17px;
    font-weight: bold;
    color: {p['text']};
    letter-spacing: {p['header_title_letter_sp']};
    padding: 0 8px;
}}
#headerBrand {{
    font-family: "KaiTi", "STKaiti", "Cambria", "Microsoft YaHei", "Microsoft YaHei UI";
    font-size: 17px;
    color: {p['brand_color']};
    letter-spacing: {p['brand_letter_sp']};
    padding: 0 4px 0 12px;
}}
#headerBrandDot {{
    color: {p['brand_dot']};
    font-size: 10px;
    padding: 0 8px 0 0;
}}
#footerLabel {{
    color: {p['footer']};
    font-size: 10px;
    letter-spacing: {p['footer_letter_sp']};
}}
#tokenUsageLabel {{
    color: {p['footer']};
    font-size: 10px;
    letter-spacing: {p['footer_letter_sp']};
}}

/* 全局菜单 */
QMenu {{
    background-color: {p['menu_bg']};
    border: 1px solid {p['menu_border']};
    border-radius: 10px;
    padding: 6px;
    color: {p['menu_text']};
}}
QMenu::item {{
    padding: 7px 28px 7px 14px;
    border-radius: 6px;
    font-size: 12px;
}}
QMenu::item:selected {{
    background-color: {p['menu_sel_bg']};
    color: {p['menu_sel_text']};
}}
QMenu::item:disabled {{
    color: {p['menu_disabled']};
}}
QMenu::separator {{
    height: 1px;
    background: {p['menu_separator']};
    margin: 6px 10px;
}}

/* 工具提示 */
QToolTip {{
    background-color: {p['tooltip_bg']};
    color: {p['tooltip_text']};
    border: 1px solid {p['tooltip_border']};
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 11px;
}}
"""
