#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 冒烟测试（无人工交互）：打开→树渲染→树编辑→undo/redo→文本→模型→
非法 Draft→搜索/过滤→保存/.bak。用 after 链驱动事件循环。"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import pyjsoneditor as je  # noqa: E402

# Windows 控制台默认非 UTF-8，断言名含中文会 UnicodeEncodeError
je._ensure_utf8_stdio()

# 弹窗可控自动应答，避免阻塞
ANS = {"yesno": True, "yesnocancel": True}
je.messagebox.askyesno = lambda *a, **k: ANS["yesno"]
je.messagebox.askyesnocancel = lambda *a, **k: ANS["yesnocancel"]
je.messagebox.showwarning = lambda *a, **k: None
je.messagebox.showerror = lambda *a, **k: print("[showerror]", a)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


app = je.App()
data = {"name": "PyJsonEditor", "version": 1, "items": ["a", "b", "c"],
        "nested": {"ok": True, "pi": 3.14, "none": None}}
tmpdir = tempfile.mkdtemp(prefix="je_smoke_")
tmp = os.path.join(tmpdir, "smoke.json")
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)


def step1():
    app.open_file(tmp)
    check("open: root item", app.tree_pane.tree.exists("[]"))
    check("open: dirty false", not app.model.model_dirty)
    check("open: node count", app._node_count == 11)
    app.open_paths = {(), ("items",), ("nested",)}
    app.rebuild_tree()
    check("tree children of root", len(app.tree_pane.tree.get_children("[]")) == 4)
    # 树编辑 → 文本同步
    ok = app.run_command(je.SetValueCommand(("name",), "PyJsonEditor", "新名称"))
    check("tree edit accepted", ok)
    check("model updated", app.model.root["name"] == "新名称")
    check("text synced from tree", "新名称" in app.text_pane.get_text())
    check("dirty after edit", app.model.model_dirty)
    # undo / redo
    app.do_undo()
    check("undo restores", app.model.root["name"] == "PyJsonEditor")
    check("undo clears dirty", not app.model.model_dirty)  # 语义指纹
    app.do_redo()
    check("redo reapplies", app.model.root["name"] == "新名称")
    app.do_undo()
    # 文本 → 模型（合法 draft）
    app.text_pane.text.delete("1.0", "end")
    app.text_pane.text.insert("1.0", '{"x": 42}')
    app.text_pane._on_modified()
    app.after(500, step2)


def step2():
    app.update()
    check("text→model commit", app.model.root == {"x": 42})
    check("draft valid", app.draft_valid)
    check("tree resynced", app.tree_pane.tree.exists('["x"]'))
    # 非法 draft：Model 不动
    before = app.model.root
    app.text_pane.text.delete("1.0", "end-1c")
    app.text_pane.text.insert("1.0", '{"x":')
    app.text_pane._on_modified()
    app.after(500, step3)
    _ = before


def step3():
    app.update()
    check("invalid draft detected", not app.draft_valid)
    check("model untouched on invalid draft", app.model.root == {"x": 42})
    # 非法 draft 下树编辑：弹窗被 patch 为"放弃修改并继续"
    ok = app.run_command(je.SetValueCommand(("x",), 42, 7))
    check("tree edit after draft discard", ok and app.model.root == {"x": 7})
    check("draft restored", app.draft_valid)
    # 搜索
    sp = app.search_panel
    app.show_search()
    sp.query.set("7")
    sp._update_matches()
    check("search count", len(sp.matches) == 1 and sp.matches[0][0] == ("x",))
    sp.query.set("")
    sp._update_matches()
    # 过滤（视图层，不改模型）
    sp.query.set("7")
    sp._update_matches()
    sp.filter_var.set(True)
    sp.apply_filter()
    check("filter active", app.filter_paths is not None)
    check("filter model untouched", app.model.root == {"x": 7})
    sp.filter_var.set(False)
    sp.apply_filter()
    check("filter cleared", app.filter_paths is None)
    # 保存：.bak 生成、文件内容正确、dirty 归零、undo 历史保留
    app.save_file()
    check("saved file", json.load(open(tmp, encoding="utf-8")) == {"x": 7})
    check("bak exists", os.path.exists(tmp + ".bak"))
    check("dirty cleared", not app.model.model_dirty)
    check("undo history kept", app.history.can_undo())  # 硬约束②
    # 注释兜底
    cmt = os.path.join(tmpdir, "commented.json")
    with open(cmt, "w", encoding="utf-8") as f:
        f.write('{\n  // hello\n  "a": 1\n}')
    app.after(50, step4)


def step4():
    """P0-2 回归：非法 Draft + undo → 询问放弃后执行；redo 正常。"""
    app.update()
    app.text_pane.text.delete("1.0", "end-1c")
    app.text_pane.text.insert("1.0", '{"x":')
    app.text_pane._on_modified()
    app.after(400, step5)


def step5():
    app.update()
    check("invalid draft again", not app.draft_valid)
    app.do_undo()   # 弹窗已 patch 为"放弃 Draft"→ 执行 undo
    app.update()
    check("undo after draft discard", app.draft_valid and app.model.root == {"x": 42})
    app.do_redo()
    check("redo back", app.model.root == {"x": 7})
    # P0-2b：合法 Draft 已产生但防抖未触发时 undo → 先 flush 再 undo
    app.text_pane.text.delete("1.0", "end-1c")
    app.text_pane.text.insert("1.0", '{"x": 42, "extra": true}')
    app.on_text_changed()          # 只调度，未 commit
    check("pending commit scheduled", app._text_job is not None)
    app.do_undo()                  # flush → commit → undo
    app.update()
    check("pending flushed then undone", app.model.root == {"x": 7})
    # P0-1：null 根
    null_file = os.path.join(tmpdir, "null.json")
    with open(null_file, "w", encoding="utf-8") as f:
        f.write("null")
    app.open_file(null_file)
    check("null root preserved", app.model.root is None
          and app.model.to_text() == "null")
    # P1-5：未闭合块注释 → 打开失败，当前文档不变
    bad = os.path.join(tmpdir, "unclosed.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write('{"a":1} /* 未闭合')
    app.open_file(bad)
    check("unclosed comment rejected", app.model.file_path == null_file)
    # P1-6：移动后选中跟随（重新写入原始数据，避免被先前保存覆盖）
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    app.open_file(tmp)
    app.open_paths = {(), ("items",)}
    app.rebuild_tree(select_path=("items", 0))
    app.tree_pane.tree.selection_set('["items", 0]')
    app.on_move(1)
    app.update()
    check("move selection follows",
          app.tree_pane.tree.selection() == ('["items", 1]',)
          and app.model.root["items"] == ["b", "a", "c"])
    # 注释剥离 + 保存返回值
    cmt = os.path.join(tmpdir, "commented.json")
    with open(cmt, "w", encoding="utf-8") as f:
        f.write('{\n  // hello\n  "a": 1\n}')
    print("[smoke] opening cmt ...")
    app.open_file(cmt)
    print("[smoke] cmt opened")
    check("comments stripped", app.model.root == {"a": 1} and app.had_comments)
    check("save_file returns True", app.save_file() is True)
    app.after(50, step6)


def step6():
    """P0 统一 Model Boundary 回归：pending 合法 Draft 在各边界先 commit。"""
    app.update()
    def set_pending_text(s):
        app.text_pane.text.delete("1.0", "end-1c")
        app.text_pane.text.insert("1.0", s)
        app.on_text_changed()          # 只调度，不 commit
    # 1) pending + 树命令
    app.open_file(tmp)
    set_pending_text(json.dumps(dict(data, name="Jack"), ensure_ascii=False))
    check("pending scheduled", app._text_job is not None
          and app.model.root["name"] == "PyJsonEditor")
    ok = app.run_command(je.SetValueCommand(("version",), 1, 2))
    check("boundary flush before tree cmd",
          ok and app.model.root["name"] == "Jack"
          and app.model.root["version"] == 2)
    # 2) pending + Ctrl+S
    set_pending_text(json.dumps(dict(data, name="Mary"), ensure_ascii=False))
    ret = app.save_file()
    check("boundary flush before save", ret is True
          and json.load(open(tmp, encoding="utf-8"))["name"] == "Mary")
    # 3) pending + Ctrl+O（放弃确认 patched 保存 → 新内容先落盘）
    other = os.path.join(tmpdir, "other.json")
    with open(other, "w", encoding="utf-8") as f:
        json.dump({"other": 1}, f)
    set_pending_text(json.dumps(dict(data, name="Nancy"), ensure_ascii=False))
    app.open_file(other)
    check("boundary flush before open", app.model.root == {"other": 1}
          and json.load(open(tmp, encoding="utf-8"))["name"] == "Nancy")
    # 4) pending + 格式化
    app.open_file(tmp)
    set_pending_text(json.dumps(dict(data, name="Olivia"), ensure_ascii=False))
    app.do_format(True)
    check("boundary flush before format",
          app.model.root["name"] == "Olivia"
          and "Olivia" in app.text_pane.get_text())
    # 5) 非法 Draft + 树命令：用户选择返回修复 → 命令中止、模型不动、Draft 保留
    ANS["yesno"] = False
    app.text_pane.text.delete("1.0", "end-1c")
    app.text_pane.text.insert("1.0", '{"name":')
    app.text_pane._on_modified()
    app.after(400, step7)


def step7():
    app.update()
    check("invalid draft again", not app.draft_valid)
    ok = app.run_command(je.SetValueCommand(("name",), "Olivia", "X"))
    check("tree cmd aborted on invalid draft",
          ok is False and app.draft_valid is False
          and app.model.root["name"] == "Olivia")
    # 6) 缩进切换取消后内部状态不漂移
    before_indent = app.indent
    app._indent_var.set("2")
    app._on_indent_changed()
    check("indent unchanged after cancel",
          app.indent == before_indent
          and app._indent_var.get() == str(before_indent))
    ANS["yesno"] = True
    app.after(50, step8)


def step8():
    """Cmd/Ctrl+Up/Down 必须移动选中行（widget 层绑定，class Keynav 不抢跑）。"""
    app.deiconify()  # 无头环境窗口可能未映射，合成按键事件需窗口可见才派发
    app.update()
    app.open_file(tmp)
    app.open_paths = {(), ("items",)}
    app.rebuild_tree()
    t = app.tree_pane.tree
    t.selection_set('["items", 1]')
    t.focus('["items", 1]')
    t.focus_set()
    t.event_generate("<Command-Up>" if je.IS_MAC else "<Control-Up>")
    app.update()
    check("move key targets selected row",
          app.model.root["items"] == ["b", "a", "c"]
          and t.selection() == ('["items", 0]',))
    t.event_generate("<Command-Down>" if je.IS_MAC else "<Control-Down>")
    app.update()
    check("move key down targets selected row",
          app.model.root["items"] == ["a", "b", "c"]
          and t.selection() == ('["items", 1]',))
    # Object 键序移动 + 选中保持（focus item 优先）
    # 惰性建树：nested 分支必须先加入 open_paths 并重建，否则节点尚未实例化
    app.open_paths = {(), ("items",), ("nested",)}
    app.rebuild_tree()
    key = '["nested", "ok"]'
    t.selection_set(key)
    t.focus(key)
    t.event_generate("<Command-Up>" if je.IS_MAC else "<Control-Up>")
    app.update()
    check("object move keeps selection",
          list(app.model.root["nested"].keys())[0] == "ok"
          and t.focus() == key)
    app.after(50, step9)


def step9():
    """V1.1 UI：版本 / Toolbar 分组 / 状态栏 Ln·Col / Copy 三件套。"""
    check("app version 1.1.0", je.APP_VERSION == "1.1.0")
    check("window title has version", je.APP_VERSION in app.title())
    # Toolbar 分组：格式化/展开 Menubutton 下拉
    mbs = [w.cget("text") for w in app.toolbar.winfo_children()
           if isinstance(w, je.ttk.Menubutton)]
    check("toolbar format menubutton", any("格式化" in t for t in mbs))
    check("toolbar expand menubutton", any("展开" in t for t in mbs))
    # 状态栏 Ln/Col
    app.text_pane.text.mark_set("insert", "2.3")
    app._update_cursor_pos()
    check("status Ln/Col", app.st_pos.cget("text") == "Ln 2, Col 4")
    # Copy 三件套：选中 ["items", 0]（值 "a"）
    # monkeypatch 剪贴板写入（真实 clipboard 在 macOS 无头/CI 环境可能阻塞）
    t = app.tree_pane.tree
    tp = app.tree_pane
    captured = []
    orig_clear, orig_append = tp.clipboard_clear, tp.clipboard_append
    tp.clipboard_clear = lambda: None
    tp.clipboard_append = lambda s: captured.append(s)
    app.open_paths = {(), ("items",), ("nested",)}
    app.rebuild_tree()
    t.selection_set('["items", 0]')
    tp._copy_path()
    check("copy path JSONPath", captured and captured[-1] == "$.items[0]")
    tp._copy_value()
    check("copy value raw", captured and captured[-1] == "a")
    tp._copy_json()
    check("copy json literal", captured and captured[-1] == '"a"')
    tp.clipboard_clear = orig_clear
    tp.clipboard_append = orig_append
    app.after(50, finish)


def finish():
    app.destroy()
    fails = [n for n, ok in results if not ok]
    print()
    if fails:
        print("GUI SMOKE FAILED: %d/%d" % (len(fails), len(results)), fails)
        sys.exit(1)
    print("GUI SMOKE PASSED: %d/%d" % (len(results), len(results)))
    sys.exit(0)


app.after(200, step1)


def _dump_stacks():
    import traceback
    import sys as _sys
    print("TIMEOUT —— 线程栈转储：", flush=True)
    for tid, frame in _sys._current_frames().items():
        print("--- thread", tid, flush=True)
        traceback.print_stack(frame)
    app.destroy()
    sys.exit(2)


app.after(30000, _dump_stacks)
app.mainloop()
