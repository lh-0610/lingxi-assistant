from src import state
from src.tools import update_plan


def test_parse_and_render():
    state.current_plan = []
    out = update_plan.func("[x] 第一步\n[~] 第二步\n[ ] 第三步")
    assert len(state.current_plan) == 3
    assert state.current_plan[0]["status"] == "done"
    assert state.current_plan[1]["status"] == "in_progress"
    assert state.current_plan[2]["status"] == "pending"
    assert "1/3 完成" in out


def test_empty_clears():
    update_plan.func("[ ] 临时")
    out = update_plan.func("")
    assert state.current_plan == []
    assert "清空" in out


def test_full_overwrite():
    update_plan.func("[ ] A\n[ ] B")
    update_plan.func("[x] C")
    assert len(state.current_plan) == 1
    assert state.current_plan[0]["text"] == "C"


def test_tolerant_formats():
    """容错：markdown 列表前缀 / 大写 / checkbox 空格变体 / 完成字符变体。"""
    state.current_plan = []
    update_plan.func(
        "- [ ] 列表前缀\n"      # markdown "- " 前缀
        "* [X] 大写完成\n"       # "* " 前缀 + 大写 X
        "1. [~] 数字前缀\n"      # "1. " 前缀
        "[ x ] 内部空格\n"       # checkbox 内多空格
        "[✓] 对勾完成\n"         # ✓ 当完成
        "没有方框的一行"          # 无 checkbox → pending，整行作文本
    )
    p = state.current_plan
    assert len(p) == 6
    assert p[0] == {"text": "列表前缀", "status": "pending"}
    assert p[1] == {"text": "大写完成", "status": "done"}
    assert p[2] == {"text": "数字前缀", "status": "in_progress"}
    assert p[3] == {"text": "内部空格", "status": "done"}
    assert p[4] == {"text": "对勾完成", "status": "done"}
    assert p[5] == {"text": "没有方框的一行", "status": "pending"}
    state.current_plan = []
