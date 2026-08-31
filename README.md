# PyJsonEditor

A lightweight JSON editor built with Python and Tkinter.

零第三方依赖的跨平台桌面 JSON 编辑器（Python + tkinter）。左树结构化编辑，右文本直接手写，双向同步；语义指纹脏检测、命令式撤销重做、原子保存 + `.bak` 轮转备份。

## 运行

需要 Python ≥ 3.10 且自带 tkinter（python.org 安装包自带；Linux 需 `sudo apt install python3-tk`；macOS Homebrew Python 需 `brew install python-tk`）。

```bash
python3 pyjsoneditor.py               # 空文档启动
python3 pyjsoneditor.py config.json   # 打开指定文件
python3 pyjsoneditor.py --version     # 输出版本号
python3 pyjsoneditor.py --selftest    # 数据层自测（40+ 项断言，含 300 轮随机属性测试，无 GUI）
python3 tests/gui_smoke.py           # GUI 冒烟测试（47 项，自动应答弹窗）
python3 tests/bench_v1_1.py           # 基准线：7 类操作 × 1k/10k/50k 三档规模，验证单节点编辑不随规模线性变慢
python3 tests/bench_v1_1.py --gui     # GUI TTFI：打开到可交互耗时（Parse/FirstTree/TTFI，需显示环境）
```

## 功能

- **文件**：打开（对话框 / 命令行 / 后期文件关联）、保存、另存为；UTF-8/UTF-16/BOM 自动探测；外部修改检测
- **格式化/压缩**：只改变文本显示（缩进 2/4/8/Tab 可选）；保存跟随当前显示格式；非法草稿不会被静默丢弃
- **树视图**：惰性加载；双击/回车就地编辑、右键新增六种类型/删除、上移下移（选中跟随）、全部/单节点展开收缩、复制路径（JSONPath）/值/JSON 字面量
- **引号开关**：仅影响树视图的字符串显示，不影响数据
- **查找/过滤/替换**：N/M 计数、上一个/下一个、大小写/全词/正则三开关（正则替换支持 `\1` 捕获组）、范围限 Key/Value；过滤只显示命中节点+祖先链；全部替换一步撤销
- **撤销/重做**：命令模式（栈深 200）；文本连续编辑合并为一个事务；待提交文本在撤销/重做前先 flush；覆盖改值/改名/增删/移动/替换
- **安全保存**：tmp + fsync + os.replace 原子写；`.bak` 即时回滚 + 时间戳轮转（保留 10 份）；未修改时不重写文件

## 快捷键

| 动作 | Win/Linux | macOS |
|---|---|---|
| 打开 / 保存 / 另存为 | Ctrl+O / Ctrl+S / Ctrl+Shift+S | Cmd+O / Cmd+S / ⇧Cmd+S |
| 撤销 / 重做 | Ctrl+Z / Ctrl+Y | Cmd+Z / Cmd+Y（或 ⇧Cmd+Z） |
| 查找/过滤/替换面板 | Ctrl+F | Cmd+F |
| 面板内下一/上一个 | Enter / Shift+Enter | 同 |
| 格式化 / 压缩 | Ctrl+Shift+F / M | ⇧Cmd+F / M |
| 全部展开 / 全部收缩 | Ctrl+Shift+E / K | ⇧Cmd+E / K |
| 引号显示开关 | Ctrl+Shift+Q | ⇧Cmd+Q |
| 折叠 / 展开节点 | Ctrl+← / → | Cmd+← / → |
| 节点上移 / 下移 | Ctrl+↑ / ↓ | Cmd+↑ / ↓ |
| 编辑选中节点 / 删除 | F2(Enter) / Delete | 同 |

## 已知不保真项（语义保真 ≠ 文本保真）

保存后**语义等价但文本可能变化**，以下项不会保留：

1. **注释**：带 `//`、`/* */` 的文件可打开（状态机剥离，字符串内的 `//` 不受影响），保存即丢失，保存前有确认
2. **重复键**：按标准只保留最后一个值，打开时警告
3. **数字文本形态**：`1e+03` → `1000.0`、`-0` → `0`、`1.0` 保持 `1.0`（类型不丢）、超大整数不失真（但受 Python 3.11+ 十进制转换长度限制）
4. **编码**：保存统一 UTF-8 无 BOM（读取支持 UTF-8/BOM/UTF-16/GB18030 探测）
5. **NaN / Infinity**：非标准 JSON，打开即拒绝

键顺序、中文（不转义）、整数/浮点/布尔/null 类型**完整保留**。

## 设计要点

- 单一数据源 `JsonModel`，树与文本只是投影；所有修改必须经 Command → History（mutation guard 常开强制）
- dirty 由**语义指纹**（canonical 序列化 sha256）相对磁盘快照判断——改了再撤销，dirty 自动归 False
- 撤销是严格 LIFO + 精确逆操作 + 路径失配 fail-stop（绝不猜修复），并有 300 轮随机属性测试兜底
- 非法文本草稿绝不触碰模型；树/文本/模型三状态分离；任何会覆盖草稿的操作（树编辑/undo/格式化/关闭）都先询问
- 根节点可为任意 JSON 值（含 `null`、标量）

## 性能边界（如实说明）

采用**分页虚拟化建树**（V1.1.1）：TreeView 只承载用户实际浏览到的节点——每层最多物化 2000 个子项，超出部分以「还有 N 项 · 双击加载更多」占位行呈现，点击逐页加载；子节点 Path 流式生成（生成器），不预先构造全部。因此**首次建树/全部展开耗时与整个 JSON 节点数量脱钩**（6MB / 40 万节点实测：全展开 254ms，仅物化 2 万 item）。**单节点编辑（改值/改名/增删/移动）走局部刷新**——只更新对应行 values、交换相邻行或单点增删，不再全量重建可见投影，展开状态/滚动位置/选中项均保持；单节点修改耗时不随文档规模线性变慢（benchmark 实测 10k→50k 增幅 <2×）。过滤模式与文档级操作（打开文件/格式化/引号开关/全部展开收缩）仍走全量重建（保守兜底，同样受分页约束）。

大文件分档解析：<2MB 实时解析（300ms）；2~8MB 降频到 600ms；>8MB 暂停实时解析，仅在 Ctrl+S / Ctrl+Enter / 离开编辑区 / 保存打开关闭前解析。**Text 重建防抖**（V1.1.1）：>1MB 文档下连续树编辑不反复重建文本视图——序列化 + 落盘合并为一次（500ms 防抖），打开/保存后立即同步。6MB / 40 万节点实测：打开到可交互 687ms（修复前 5.4s），连击编辑 3 次 316ms，解析 332ms。

## 后续计划（V2）

鼠标拖拽排序、多文件 Tab、JSONPath 定位面板、JSON5/JSONC 一等支持、复制粘贴子树、Windows/Linux 打包正式产物（构建冒烟已入 CI，v1.2 扩展）。macOS 打包说明见 `docs/build-notes.md`。
