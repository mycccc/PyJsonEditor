#!/usr/bin/env python3
"""V1.1 性能基准线（无 GUI 依赖，三平台 CI 可直接运行）。

验证核心目标：单节点修改（改值/改名/移动/增删）的数据层耗时
不随文档规模线性变慢——这是 TreeView 局部刷新（V1.1）成立的前置条件。

三档规模：1k / 10k / 50k 节点。
七类操作：改叶子、改深层、Move 数组元素、Rename key、Delete、Insert、大数组展开。
其中"大数组展开"为线性参照（投影行生成本来就是 O(N)）。

断言规则：set/move/rename/insert 在 10k→50k 增幅 < 5×（O(1) 操作，允许测量噪声）；
中间 delete 涉及 list 内存搬移，放宽 < 10×。

退出码：0 = 全部通过；1 = 性能回归。
"""
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import pyjsoneditor as M  # noqa: E402

SIZES = (1_000, 10_000, 50_000)
ROUNDS = 5
FAIL_THRESHOLD = 5.0      # 10k → 50k 允许增幅
FAIL_THRESHOLD_DELETE = 10.0


def build_doc(size: int):
    """构造 size 个叶节点的 JSON：meta 对象 + 大数组。"""
    return {
        "meta": {"name": "bench", "version": 1, "ok": True},
        "items": [
            {"id": i, "name": "item-%d" % i, "tags": ["a", "b"],
             "value": i * 1.5, "ok": i % 2 == 0}
            for i in range(size)
        ],
    }


def fresh_model(size: int) -> M.JsonModel:
    return M.JsonModel(build_doc(size))


def timeit(fn, rounds: int = ROUNDS) -> float:
    """运行 rounds 次取中位数（ms）。"""
    samples = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


def bench_op(name: str, size: int, mk_op, thresh: float) -> float:
    """在固定模型上以 do/undo 复用测量单次命令耗时（构造文档不计时）。"""
    m = fresh_model(size)
    op = mk_op(m)
    samples = []
    if hasattr(op, "do"):  # Command 实例：do 计时，undo 恢复状态
        for _ in range(ROUNDS):
            t0 = time.perf_counter()
            op.do(m)
            samples.append((time.perf_counter() - t0) * 1000.0)
            op.undo(m)
    else:                  # 纯函数（expand 参照）：不改变模型，直接重复
        for _ in range(ROUNDS):
            t0 = time.perf_counter()
            op()
            samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


def _set_leaf(m):
    return M.SetValueCommand(("items", 0, "name"), "item-0", "renamed-0")


def _set_deep(m):
    return M.SetValueCommand(("items", len(m.root["items"]) - 1, "tags", 1),
                             "b", "c")


def _move_tail(m):
    return M.MoveCommand(("items", len(m.root["items"]) - 2), 1)


def _rename(m):
    return M.RenameKeyCommand(("meta",), "name", "title")


def _delete_mid(m):
    return M.DeleteCommand(("items", len(m.root["items"]) // 2))


def _insert_tail(m):
    return M.InsertCommand(("items",), None,
                           {"id": -1, "name": "x", "tags": [], "value": 0,
                            "ok": False})


def _expand_all(m):
    """线性参照：生成全部可见投影行（局部刷新优化前的全量成本）。"""
    def run():
        for v in m.root["items"]:
            M.type_name(v)  # 仅做代表投影行类型的纯计算
    return run


def bench_gui() -> int:
    """GUI TTFI 基准（--gui，需要显示环境，macOS/Linux 桌面可直接运行）：
    输出用户可感知的「打开到可交互」耗时表——Parse / FirstTree / TTFI 三阶段。
    Tree 采用分页虚拟化（V1.1.1）：超大数组只实例化一页，因此 TTFI
    与「整个 JSON 节点数量」脱钩，这是本基准要守住的验收线。"""
    import tempfile

    try:
        import tkinter  # noqa: F401
        app = M.App()
    except Exception as e:  # 无显示环境（CI headless）
        print("GUI 基准需要显示环境，跳过：%s" % e)
        return 0
    app.withdraw()

    def pump(n: int = 3) -> None:
        for _ in range(n):
            app.update()

    pump()
    print("\nGUI TTFI 基准（打开到可交互；Tree 分页虚拟化下不应随节点数线性变慢）")
    print("%-22s %10s %10s %10s" % ("规模/文件", "Parse", "FirstTree", "TTFI"))
    print("-" * 58)
    for size in (1_000, 10_000, 50_000, 100_000):
        rows = [{"id": i, "name": "user-%d" % i, "age": i % 100,
                 "active": i % 2 == 0} for i in range(size)]
        text = json.dumps(rows, ensure_ascii=False)
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        try:
            # Parse：读盘 + 严格解析 + 建模型 + 节点计数（数据层）
            t0 = time.perf_counter()
            s, _enc = M.read_text_file(path)
            r = M.parse_json_text(s)
            model = M.JsonModel(r.data)
            model.count_nodes()
            t_parse = (time.perf_counter() - t0) * 1000.0
            # TTFI：open_file 全程（parse + 首次建树 + Text 落盘）
            t0 = time.perf_counter()
            app.open_file(path)
            t_ttfi = (time.perf_counter() - t0) * 1000.0
            pump(2)
            # FirstTree：重建树的首次绘制（分页后只物化 2000/层）
            t0 = time.perf_counter()
            app.rebuild_tree()
            t_first = (time.perf_counter() - t0) * 1000.0
            pump(2)
            mb = len(text) / 1048576.0
            print("%-22s %8.0fms %8.0fms %8.0fms" % (
                "%.1fMB / %d 节点" % (mb, size * 4 + 1), t_parse, t_first, t_ttfi))
        finally:
            app.open_paths = {()}
            os.unlink(path)
    app.destroy()
    return 0


def main() -> int:
    if "--gui" in sys.argv:
        return bench_gui()
    print("PyJsonEditor V1.1 性能基准线")
    print("节点规模: %s  轮次/操作: %d" % (SIZES, ROUNDS))
    print("-" * 78)
    header = "%-18s %10s %10s %10s %10s" % (
        "操作", "1k(ms)", "10k(ms)", "50k(ms)", "增幅(10k→50k)")
    print(header)
    print("-" * 78)

    results = {}
    ops = [
        ("set_leaf", _set_leaf, FAIL_THRESHOLD),
        ("set_deep", _set_deep, FAIL_THRESHOLD),
        ("move_tail", _move_tail, FAIL_THRESHOLD),
        ("rename", _rename, FAIL_THRESHOLD),
        ("delete_mid", _delete_mid, FAIL_THRESHOLD_DELETE),
        ("insert_tail", _insert_tail, FAIL_THRESHOLD),
        ("expand_all", _expand_all, FAIL_THRESHOLD * 10),  # 线性参照，仅报告
    ]

    all_pass = True
    for name, mk, thresh in ops:
        row = []
        for size in SIZES:
            row.append(bench_op(name, size, mk, thresh))
        ratio = row[2] / row[1] if row[1] else float("inf")
        results[name] = (row, ratio)
        print("%-18s %10.3f %10.3f %10.3f %10.2f×" % (
            name, row[0], row[1], row[2], ratio))
        if name != "expand_all" and ratio > thresh:
            all_pass = False

    # 单独报告语义指纹（已知事实：3.8MB/50k ≈ 150ms，不做断言）
    t0 = time.perf_counter()
    m = fresh_model(50_000)
    M.semantic_fingerprint(m.root)
    fp_ms = (time.perf_counter() - t0) * 1000.0
    print("-" * 78)
    print("fingerprint(50k) = %.1f ms（仅报告，不做断言）" % fp_ms)
    print("expand_all 为线性参照：10k→50k 预期 ≈ 5×，上方数据应接近该值。")

    print("-" * 78)
    if all_pass:
        print("PASS: 所有单节点修改增幅低于阈值（局部刷新前置成立）")
        return 0
    print("FAIL: 存在增幅超阈值的操作（疑似性能回归）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
