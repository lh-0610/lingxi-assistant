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


def test_full_overwrite_rejects_silent_removal():
    update_plan.func("[ ] A\n[ ] B")
    out = update_plan.func("[x] C")
    assert "计划未更新" in out
    assert len(state.current_plan) == 2
    assert state.current_plan[0]["text"] == "A"


def test_tolerant_formats():
    """容错：markdown 列表前缀 / 大写 / checkbox 空格变体 / 完成字符变体。"""
    state.current_plan = []
    update_plan.func(
        "- [ ] 列表前缀\n"      # markdown "- " 前缀
        "* [X] 大写完成\n"       # "* " 前缀 + 大写 X
        "1. [~] 数字前缀\n"      # "1. " 前缀
        "[ x ] 内部空格\n"       # checkbox 内多空格
        "[✓] 对勾完成\n"         # ✓ 当完成
        "没有方框的一行"          # 无 checkbox → 忽略，避免摘要/分析污染
    )
    p = state.current_plan
    assert len(p) == 5
    assert p[0] == {"text": "列表前缀", "status": "pending"}
    assert p[1] == {"text": "大写完成", "status": "done"}
    assert p[2] == {"text": "数字前缀", "status": "in_progress"}
    assert p[3] == {"text": "内部空格", "status": "done"}
    assert p[4] == {"text": "对勾完成", "status": "done"}
    state.current_plan = []


def test_history_summary_is_not_added_to_plan():
    """压缩摘要意外续接到参数时，只保留摘要前的 checklist。"""
    out = update_plan.func(
        "[x] 第一步\n"
        "[~] 第二步\n"
        "[ ] 第三步 [历史摘要]:\n"
        "**用户目标**\n"
        "1. 这不是计划项"
    )
    assert len(state.current_plan) == 3
    assert state.current_plan[-1]["text"] == "第三步"
    assert "1/3 完成" in out


def test_invalid_text_does_not_overwrite_existing_plan():
    update_plan.func("[~] 保留中的任务")
    out = update_plan.func('{"plan": "普通分析文本"}')
    assert state.current_plan == [{"text": "保留中的任务", "status": "in_progress"}]
    assert "未更新" in out


def test_update_plan_rejects_silent_removal_of_unfinished_steps():
    update_plan.func("[x] A\n[~] B\n[ ] C")

    out = update_plan.func("[x] A\n[~] B")

    assert "计划未更新" in out
    assert [it["text"] for it in state.current_plan] == ["A", "B", "C"]


def test_update_plan_allows_status_only_update():
    update_plan.func("[~] A\n[ ] B")

    out = update_plan.func("[x] A\n[~] B")

    assert "计划已更新" in out
    assert state.current_plan == [
        {"text": "A", "status": "done"},
        {"text": "B", "status": "in_progress"},
    ]


def test_update_plan_allows_structural_change_with_reason_for_non_validation_step():
    update_plan.func("[~] 调研实现\n[ ] 修改代码")

    out = update_plan.func("[~] 修改代码\n[ ] 跑测试\n调整说明：调研已并入修改代码步骤")

    assert "计划已更新" in out
    assert [it["text"] for it in state.current_plan] == ["修改代码", "跑测试"]


def test_update_plan_rejects_silent_removal_of_validation_steps_even_with_reason():
    update_plan.func("[x] 修改代码\n[ ] 运行全量测试\n[ ] git diff 检查改动")

    out = update_plan.func("[x] 修改代码\n调整说明：省略验收步骤")

    assert "不能删除尚未完成的验收步骤" in out
    assert [it["text"] for it in state.current_plan] == [
        "修改代码",
        "运行全量测试",
        "git diff 检查改动",
    ]
