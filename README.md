# PyJsonEditor

A lightweight JSON editor built with Python and Tkinter.

零第三方依赖的跨平台桌面 JSON 编辑器（Python + tkinter）。左树结构化编辑，右文本直接手写，双向同步；语义指纹脏检测、命令式撤销重做、原子保存 + `.bak` 轮转备份。

## 运行

需要 Python ≥ 3.10 且自带 tkinter（python.org 安装包自带；Linux 需 `sudo apt install python3-tk`；macOS Homebrew Python 需 `brew install python-tk`）。

```bash
python3 pyjsoneditor.py               # 空文档启动
python3 pyjsoneditor.py config.json   # 打开指定文件
python3 pyjsoneditor.py --selftest    # 数据层自测（40+ 项断言，含 300 轮随机属性测试，无 GUI）
python3 tests/gui_smoke.py           # GUI 冒烟测试（47 项，自动应答弹窗）
```

## 功能

- **文件**：打开（对话框 / 命令行 / 后期文件关联）、保存、另存为；UTF-8/UTF-16/BOM 自动探测；外部修改检测
- **格式化/压缩**：只改变文本显示（缩进 2/4/8/Tab 可选）；保存跟随当前显示格式；非法草稿不会被静默丢弃
- **树视图**：惰性加载；双击/回车就地编辑、右键新增六种类型/删除、上移下移（选中跟随）、全部/单节点展开收缩
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

采用惰性建树：打开文件只建根节点，展开时按批（500 条/批）加载子节点。**已展开的大型子树发生修改/撤销时会重建可见投影**，数十万节点的全展开状态下可能出现 UI 峰值——这是 V1 的已知边界，增量树更新留待 V2。3.8MB / 5 万节点实测：解析 268ms、语义指纹 153ms，日常编辑流畅。

## 后续计划（V2）

macOS `.app` / Windows `.exe` / Linux 打包与系统文件关联、鼠标拖拽排序、多文件 Tab、JSONPath 定位、JSON5/JSONC 一等支持、复制粘贴子树。打包注意事项见 `docs/build-notes.md`。
