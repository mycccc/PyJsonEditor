# PyJSONEditor — 需求与架构决策备忘录（design-log）

> 本文档是用户原始需求、三轮 AI 交叉评审与全部架构决策的完整记录。
> 实施与后续维护均以本文档为准；改动架构前先更新此文档。
> 最后更新：开工前（架构讨论收官，双方确认 Plan 可执行）。

---

## 1. 用户原始需求（逐字）

用 python 的 tkinter 做一个 GUI 版的 json 编辑器。
json 解析编辑可以利用网络上现有的开源库，避免重复造轮子。
功能实现主要有：

- 格式化\美化 方便查看
- 压缩 方便保存
- 可以 全部展开 全部收缩
- 也可以单独展开收缩某节点（ctrl+左箭头 \ ctrl+右箭头 快捷键）
- 查看和编辑 可以有个选项 设置是否显示""
- 条目和节点可以上移和下移，快捷键如 ctrl+上箭头、ctrl+下箭头（Ctrl+Up and Ctrl+Down）
- 加一个 undo 和 redo 的快捷键 ctrl+z 和 ctrl+y
- ctrl+S 保存时 自动存一个 .bak 的备份文件

补充需求（后续追加）：**过滤、查找（显示统计数量）、替换功能**。

输出版本先给一个 .py 版本；最终后期考虑加出 macOS 的 .app、Windows 的 .exe 以及 Linux 版本，
因为这样可以在系统中关联 json 文件。

## 2. 澄清结论（用户两轮确认，均为最终决定）

### 第一轮

| 项 | 决定 |
|---|---|
| 代码位置 | 独立工程 `/Users/c/Desktop/webcode/JsonEditor/`，不进 www123 / 0pub / 1docs |
| 界面形态 | 左树（结构化编辑）+ 右文本（JSON 文本视图）双栏，双向同步 |
| 快捷键 | 双套并存：macOS 用 Cmd 且同时保留 Ctrl 映射；Win/Linux 用 Ctrl |
| 依赖 | 零第三方依赖，仅标准库 tkinter + json，高亮自写正则 |
| 交付 | 先单文件 .py；打包（.app/.exe/Linux）为后期阶段，本期仅预留 build/ 说明 |

### 第二轮（针对另一位 AI 的补充建议 + 用户新增需求）

- 增量功能：**只做「新增/删除节点」**（右键菜单，六种类型）。
  明确不做：复制/粘贴子树、复制节点 JSON/路径、递归展开折叠（Ctrl+Shift+←/→）、编辑值类型下拉。
- 推迟到 V2（全部确认）：鼠标拖拽排序、多文件 Tab、JSONPath 定位、JSON5/JSONC 一等支持。
  本期对 JSONC 仅做「带注释文件能打开（状态机剥离）并明确提示注释不保留」。
- 文件规模目标：**≤20MB**（惰性加载 + 防抖高亮足够）。不做 ijson 流式解析。
- 搜索行为（全部确认）：
  - 三开关：大小写敏感 / 全词 / 正则（默认关闭）
  - 范围限定：仅键名 / 仅值 / 两者
  - 过滤 = 只显示命中节点+祖先链（视图层，不改 Model），区别于查找（只高亮跳转）
  - 替换 = 替换当前 / 全部替换；全部替换整体一个 undo 步骤；查找显示 N/M 计数

## 3. 现成开源库调研结论（勿重复调研）

- `PyJSONViewer`：纯只读，不可编辑 → 不可用
- `zargit/tkinter-json-editor`：19★/4 commits，无 undo/移动/快捷键 → 不可依赖
- `munich-ml/tkinter_json_editor`：0★，仅单元格编辑+展开折叠 → 仅借鉴 Entry overlay（bbox 定位）写法
- 结论：解析用标准库 `json`（即"不重复造轮子"），UI 层自研

## 4. 第一位评审 AI（第一轮）的坑清单与处置

| 坑 | 处置 |
|---|---|
| macOS Ctrl+方向键被 Mission Control 占用 | 双套绑定 Cmd+Ctrl |
| Treeview 全量建节点卡死 | 惰性加载（`<<TreeviewOpen>>`）+ 占位节点 + 每批 500 条分批插入 |
| JSON 保真（注释/重复键/1.0与1/大整数/NaN） | 严格 JSON（parse_constant 拒绝 + allow_nan=False）+ 重复键 object_pairs_hook 检测弹窗 |
| Undo 全量快照内存爆炸 | 命令模式，只存受影响数据与必要子树快照 |
| 节点移动循环引用/边界 | 只做同级移动；首末节点禁用 |
| .bak 被二次覆盖 | .bak（即时回滚）+ 时间戳轮转保留 10 份 |
| 编码/换行 | BOM/UTF-16 启发式探测（含无 BOM UTF-16 的 NUL 字节判定），保存 UTF-8 无 BOM，newline 显式 |
| 打包坑 | 本期仅 build/README.md 记录（Info.plist CFBundleDocumentTypes、注册表、DPI、签名公证） |
| 双栏同步回环 | 守卫标志位 + 三状态机（见 §7） |

## 5. 关键架构分歧：Stable Node ID（不采纳，双方最终一致）

- 对方最初主张每节点唯一 node_id；我方坚持 **路径元组（Path）寻址**。
- 我方论证：undo 是严格 LIFO 逆序回退，回退第 N 条命令时模型恰好处于该命令刚执行完的状态，
  更早命令记录的路径必然有效；redo 顺序重放同理。对方的批评针对"undo 时重新寻址"的误用。
- 对方最终认可：「在严格 LIFO + 命令可逆且原子的前提下成立」，附加三个前提（见 §6）。
- 兜底：undo/redo 路径失配（KeyError/IndexError）→ 回滚栈状态 → `HistoryError` → fail-stop，绝不猜修复。
- Treeview iid = `json.dumps(list(path))`，任意 key/下标唯一可逆。

## 6. 锁定的架构硬规则（全部经双方确认）

### 6.1 六条硬规则（GUI 开发前）

1. **Path 只表示 JSON 结构位置**，不携带类型标记；数组段用 int 下标。示例：`("users", 3, "address", "city")`。
2. **所有 Model 修改必须经 Command/History**；禁止 UI 直接修改 Model。实施为
   **mutation guard（常开，无开关）**：`JsonModel._mutation_depth`，修改原语入口检查，
   `Command.do/undo` 通过 `model.mutation_scope()` 上下文进入；UI 裸调 → AssertionError。
   （对方要求开发期开启；我方决定常开——一次 int 比较的代价，换"没有后门"。）
3. **三状态分离**：`Model`（最后有效 JSON）→ serialize → `Text Widget`；用户输入 → `Text Draft`（可非法）；
   parse 成功才 commit 回 Model → Tree。非法 Draft 绝不触碰 Model。
4. **Text 事务 = merge 方案**：防抖 300ms + parse 成功 → `ReplaceDocumentCommand`；
   merge 条件 = 栈顶是文本命令 **且 `text_transaction` 仍开启**。事务在以下事件强制关闭：
   焦点离开文本区 / 执行树命令 / undo / redo / 保存·打开·另存为。
   Draft 解析失败期间零命令产生。
5. **Undo/Redo 不变量**：do→undo 精确恢复操作前状态；undo→redo 精确恢复操作后状态；
   由属性测试兜底（随机嵌套 JSON × 六类命令 × do/undo/redo 断言）。
6. **先数据层 + 测试，后 GUI**。数据层 33 项自测已通过。

### 6.2 最后三条硬约束（App 接线前，对方提出）

1. **dirty 以磁盘快照为基准（语义指纹），而非"执行过命令"**：
   `model_dirty = sha256(serialize(root, compact)) != saved_fingerprint`。
   修改→Undo 回到原状 → dirty 自动 False。revision 计数不能用于 dirty 判断（undo 也会 +1）。
   fingerprint 按需计算并按 revision 缓存。
2. **Save 后更新 saved baseline，但不清空 Undo/Redo**：保存后仍可 Ctrl+Z 回到保存前状态（dirty 变 True）。
   仅打开新文件时 history.clear()。
3. **View 操作绝不进 Model History**：搜索/过滤/展开/收缩/选中/滚动/引号开关/列宽/分栏/主题/格式化/压缩
   均不 push 命令。Ctrl+Z 只表示撤销数据编辑。

### 6.3 行为定义细则

- **非法 Draft 时的树编辑**：弹窗「文本区存在无法解析的 JSON，未应用的修改将丢失」
  [放弃文本修改，继续树编辑] / [返回修复]。只拦编辑类操作，查询类放行。
  **不加"本次会话不再提示"**（Q9 确认）。
- **非法 Draft 时 Ctrl+S**：弹窗确认，文案明确「保存将使用最后一次有效数据，非法修改不会写入文件」
  [仍然保存] / [返回编辑]。**保存成功后不覆盖 Draft**——文本区保留用户非法输入，
  状态栏持续提示，直到修复（parse 成功→正常 commit）或放弃。（Q3 + 对方补充）
- **保存格式**：工具栏格式化/压缩只改 Text View；保存跟随当前视图格式（pretty/compact + 缩进档位）。
  `model_dirty=False, view_dirty=True`（只点过格式化）时 Ctrl+S **正常写盘**，状态栏提示
  「数据未修改，已按当前显示格式写盘」（Q10 确认）。
- **注释文件的保存确认**：原文件含注释时，即使 model_dirty=False，Ctrl+S 仍弹
  「保存并移除注释 / 取消」确认；打开后状态栏常驻提示。
- **重复键**：object_pairs_hook 在 dict 构造前检测（取消→整个解析作废，内存干净）；
  继续→按标准只保留最后值。V1 不做"保留全部重复键供选择"。（Q5）
- **编辑值不改类型**：字符串输入原样是字符串（区分 "18" 与 18）；数字节点校验 int/float；
  bool 双击直接切换；null 不可编辑（提示可删除后新增）。类型转换 = 删除后新增。
- **搜索结果失效**：点击结果时重新 resolve，失败→不猜测相邻节点（fail-stop），
  刷新搜索结果并提示「搜索结果已因数据变化失效，已重新搜索」。
- **过滤只改显示**：`filter_paths` 是 App 层视图参数，Model 只读；过滤期间树编辑照常走 Command，
  完成后重算过滤集合。
- **Undo/Redo 后文本渲染**：遵循当前 view_format（格式化→改值→Ctrl+Z，文本保持格式化样式）。
- **大文件分级**：≤4MB 正常高亮+行号；4~8MB 保留行号跳过高亮；>8MB 提示建议用树编辑但不强制。（Q7）
- **全部展开**：节点数 >20000 弹三选「继续 / 仅展开前 3 层 / 取消」；只是确认门槛非性能预言。（Q6/Q8）
- **count_nodes**：打开时统计一次并缓存，不做增量维护。（Q6）
- **根节点可为任意 JSON 值**（含标量）；标量根时树操作快捷键全部 no-op，搜索可命中 root。（Q8）
- **Key 永远是字符串**，不做智能类型转换。
- **编码**：读 BOM/UTF-16 启发式探测；保存统一 UTF-8 无 BOM（README 声明非"编码保持"）。
- **产品保真定位**：**语义保真，非文本保真**。键序/中文/整数/浮点类型保持；
  1e+03/-0 文本形态、注释、重复键不保留。README 明示。
- **原子写顺序**：serialize → 写 .tmp → flush+fsync → 旧文件→时间戳 .bak（轮转留 10）→ 旧文件→.bak →
  os.replace(tmp, path) → 更新 saved baseline（fingerprint + mtime/size）。
- **外部修改检测**：保存前比对 mtime/size，不一致弹「覆盖 / 取消」。
- **dirty 未变时 Ctrl+S**：跳过写盘，提示「未检测到修改」（避免"打开→保存"无意义改变文件）。

### 6.4 状态机终图（对方确认闭合）

```
Disk ──Load──▶ Model(权威数据) ──┬──▶ Tree Projection
                                 └──serialize(view_format)──▶ Text/Draft
Draft ──parse success──▶ Model ──▶ Tree
非法 Draft ──▶ Model 不动；树编辑/保存需确认
Save: Model ─serialize→ tmp →fsync→ bak →replace→ saved baseline 更新（不清 Undo）
Undo/Redo: Command → Model → Tree + Text(按当前 view_format 重渲染)
```

## 7. 双方 Q&A 结论速查

| # | 问题 | 结论 |
|---|---|---|
| Q1 | mutation guard 强度 | B：`_mutation_depth` 检查，**常开无开关** |
| Q2 | Text 事务 | merge 方案 + text_transaction 开关；Draft 非法零命令 |
| Q3 | 非法 Draft + Ctrl+S | 明确确认弹窗；保存后不覆盖 Draft |
| Q4 | 保存格式 | 跟随当前视图格式；拆 model_dirty / view_dirty |
| Q5 | 重复键 | 检测→警告→继续/取消；不保留重复键 |
| Q6 | count_nodes | 打开时缓存；全展开三选弹窗 |
| Q7 | 大文件分级 | 4MB 跳高亮 / 8MB 提示，不强制 |
| Q8 | 根标量 | 支持；树操作 no-op；搜索可命中 |
| Q9 | "不再提示"复选框 | 不加 |
| Q10 | view_dirty + Ctrl+S | 正常落盘；注释文件除外（仍弹注释确认） |

## 8. 环境事实

- Python 3.14.7（`/Library/Frameworks/Python.framework/Versions/3.14/bin/python3`，自带 tkinter）
- Tk 9.0；macOS (darwin)
- 兼容性下限：Python 3.10+；不依赖 Tk 9.0 特有 API
- Windows 启动前 `SetProcessDpiAwareness(1)`；字体按平台选择（Menlo/Consolas/DejaVu + 中文字体）

## 9. 实施进度（截至本备忘更新）

- ✅ 数据层完成并通过自测（40+ 项断言含 300 轮属性测试）
- ✅ GUI 全部接线完成；GUI 冒烟 36 项全过
- ✅ Code Review 修复轮（外部评审实测发现，全部修复并回归）：
  - P0-1 `JsonModel(None)` 把合法 null 根变成 {} → sentinel 修复 + 回归测试
  - P0-2 undo/redo 未过 Draft 防护（含 pending 防抖事务）→ `_flush_or_guard_draft()`：
    pending 合法草稿先立即 commit，非法草稿询问放弃/修复，取消则中止
  - P0-3 格式化/压缩/缩进静默吃掉非法 Draft → `do_format` 前置 `_ensure_draft_ok`
  - P0-4 `_confirm_discard`+`save_file` 误放行 → `save_file` 返回 bool，
    取消/失败阻止关闭与打开；"未重写"跳过条件追加 `draft_valid`
  - P1-5 未闭合 `/*` 被静默吞到 EOF → `strip_comments` 返回三元组，未闭合判定解析失败
  - P1-6 移动后选中不跟随 → 数组元素按 `new_index` 计算新路径传入 `select_path`
  - P1-7 替换当前首击只跳转不替换 → 首击定位第一个匹配并直接替换
  - P1-8 dup_sink 注释兜底重试重复收集 → 每次 `_load` 独立 sink，结果合并
  - P2-9 正则替换 `\1` 反向引用被转义破坏 → 正则模式原样传 repl（普通模式仍转义）
  - P2-10 README 性能承诺改为如实描述（已展开大子树修改时重建可见投影）
  - P1-14 交付报告测试数字与实际不符 → README 不写死总数
- 冒烟脚本自身问题（非产品 bug）：测试序列依赖被保存覆盖的样例文件、`cmt` 变量作用域
  （NameError 被 Tk 回调吞掉导致超时假象）——已重排序列并加栈转储诊断
- ✅ 最终轮修复（外部评审第二轮，P0 统一 Model Boundary）：
  - `_flush_or_guard_draft()` 升级为唯一 Model Boundary（cancel 防抖 timer → 同步
    `_commit_text()` → 非法 Draft 询问），接入全部边界：
    `run_command` / `save_file` / `open_file` / `do_format` / `show_search`
    （undo/redo 此前已接入）。pending 合法草稿在树命令/保存/打开/格式化前先 commit，
    不再被旧 Model 覆盖；Open 顺序 = flush → dirty 重判 → _confirm_discard
  - `_on_indent_changed` 顺序修正：先 Boundary，取消后回滚缩进单选显示，内部状态不漂移
  - `.bak` 时间戳升级微秒级（`%H%M%S-%f`），同秒多次保存不互覆盖
  - `_ensure_draft_ok` 文案通用化（"继续当前操作"）
  - 冒烟新增 8 项边界回归：pending+树命令 / pending+保存 / pending+打开 /
    pending+格式化 / 非法 Draft+树命令中止 / 缩进取消回滚；弹窗改为可控应答
- ✅ 用户实测反馈修复：Cmd/Ctrl+↑↓ 移动错行 —— 根因是 aqua 上 Treeview class binding
  `<Up>/<Down>` → `ttk::treeview::Keynav` 先于 bind_all 执行、抢跑移动了选中项，
  我们的 handler 随后移动的是"新的选中行"。修复：树导航键（方向键/F2/Delete）全部
  改绑 Treeview widget 层并 `return "break"`（widget bindtag 先于 class），删除已无
  用的 guard_tree；冒烟新增 2 项真实事件回归（Up/Down 均以选中行为目标）
- 最终回归：数据层 selftest 45 项全 PASS；GUI 冒烟 46/46 全 PASS

## 10. App 接线顺序（13 步，每步冒烟后进下一步）

> 交付节奏（对方最终要求）：每完成一个阶段，向用户提交一份简短变更记录 + 测试结果，
> 供其转交评审 AI 做 code review；对方最终结论为「可以正式进入代码执行阶段」。
> 开工前最后确认（对方终轮回复，已全部纳入 §6/§7）：
> Q9 不加"不再提示"、Q10 view_dirty 落盘（注释文件除外）、
> 三条硬约束（指纹 dirty / Save 不清 Undo / View 操作不进 History）、
> Undo 后按 view_format 重渲染、搜索失效提示但不猜相邻节点。

1. App + Model 生命周期 → 2. 打开文件 → 3. Model→Tree 单向 → 4. Tree 编辑→Model →
5. Undo/Redo → 6. Text Draft → 7. Text parse→Model → 8. 双向同步 → 9. Search →
10. Save/Backup → 11. 外部修改检测 → 12. 快捷键 → 13. 最终冒烟

**第 8 步验收链路**：打开 → 树显示 → 改树 → 文本同步 → 改文本(合法) → 模型更新 → 树同步 → Undo → Redo。

**数据层还需补的测试**：属性测试（随机 do/undo/redo 等价）、mutation guard 断言、
fingerprint dirty（改→undo→dirty=False）、转义往返（\n \r \t \\ \" \b \f \uXXXX）、
数字往返（0/-1/1.0/1e10/-1.5e-3/30位整数）、「打开→不改→保存不重写」、
「重复键取消打开后内存干净」。

## 11. V1 明确不做（V2 候选）

鼠标拖拽排序 · 多文件 Tab · JSONPath 定位 · JSON5/JSONC 一等支持 · 复制/粘贴子树 ·
复制节点 JSON/路径 · 递归展开折叠快捷键 · 编辑值类型下拉菜单 · ijson 流式大文件 ·
三平台打包与文件关联（build/ 仅留说明）

## 12. 快捷键表（V1 最终）

| 动作 | Win/Linux | macOS |
|---|---|---|
| 打开 / 保存 / 另存为 | Ctrl+O / S / Shift+S | Cmd+O / S / Shift+S |
| 撤销 / 重做 | Ctrl+Z / Y | Cmd+Z / Cmd+Y（或 Shift+Cmd+Z） |
| 查找/过滤/替换面板 | Ctrl+F | Cmd+F（Esc 关闭） |
| 格式化 / 压缩 | Ctrl+Shift+F / M | Shift+Cmd+F / M |
| 全部展开 / 全部收缩 | Ctrl+Shift+E / K | Shift+Cmd+E / K |
| 引号显示开关 | Ctrl+Shift+Q | Shift+Cmd+Q |
| 折叠 / 展开节点 | Ctrl+← / → | Cmd+← / → |
| 节点上移 / 下移 | Ctrl+↑ / ↓ | Cmd+↑ / ↓ |
| 编辑选中节点 | F2 / Enter | 同 |
| 删除节点 | Delete | 同 |
