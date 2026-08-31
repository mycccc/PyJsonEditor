#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyJSONEditor — 一个基于 Python + Tkinter 的轻量级跨平台 JSON 编辑器（零依赖，仅标准库 tkinter + json）

架构（单文件内分层，自上而下）:
  [数据层] Path 工具 / 严格 JSON 解析 / 注释剥离状态机 / IO(编码探测+原子写+备份轮转)
           JsonModel 单一数据源 / Command 命令族 / History 撤销重做栈
  [视图层] TextPane(JSON 文本视图) / TreePane(结构树) / SearchPanel(查找/过滤/替换) / App

核心状态模型（三状态）:
  1) Model      —— 最后一次合法 JSON（唯一数据源）
  2) Text Draft —— 用户正在编辑的文本，允许暂时非法
  3) Tree       —— 只渲染 Model
  文本解析成功 → Model 更新 → Tree 刷新；解析失败 → Model 保持不变，状态栏报错。

保真声明: 本编辑器是「语义保真」而非「文本保真」——
  键顺序/中文/整数/浮点类型保持，但 1e+03/-0 等文本形态、注释、重复键不保留。

运行: python3 pyjsoneditor.py [文件.json]
自测: python3 pyjsoneditor.py --selftest
版本: python3 pyjsoneditor.py --version
"""

from __future__ import annotations

import glob
import hashlib
import itertools
import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------------------
# 常量与类型
# ---------------------------------------------------------------------------

APP_NAME = "PyJSONEditor"
APP_VERSION = "1.1.0"

UNDO_LIMIT = 200            # 撤销栈上限（超出丢弃最早的命令）
TREE_PAGE_SIZE = 2000       # V1.1.1：树分页虚拟化——每层最多实例化子项数，超出显示「双击加载更多」
EXPAND_CONFIRM_LIMIT = 20000  # 「全部展开」节点数保护阈值
HIGHLIGHT_LIMIT = 4 * 1024 * 1024  # 文本高亮上限：超过则跳过高亮（只保留行号）
MAX_HL_LINE = 32000          # 单行超过此长度视为超长行：跳过该行高亮/括号匹配/行号 bbox
TEXT_WARN_LIMIT = 8 * 1024 * 1024  # 文本区性能警告阈值（>此大小暂停实时解析）
TEXT_REBUILD_LIMIT = 1 * 1024 * 1024  # V1.1.1：树编辑 → Text 重建防抖阈值（>此大小合并重建）
TEXT_REBUILD_DELAY = 500            # 树编辑 → Text 大文档重建防抖（ms），连击编辑只重建一次
PARSE_MID_LIMIT = 2 * 1024 * 1024  # 中档 debounce 起点（2MB）
PARSE_DEBOUNCE_SMALL = 300         # <2MB：实时解析延迟（ms）
PARSE_DEBOUNCE_MID = 600           # 2~8MB：中档延迟（ms）
BACKUP_KEEP = 10            # 时间戳备份保留份数

Path = Tuple[Any, ...]      # 根为 ()，dict 用 str key、list 用 int 下标逐段追加


class HistoryError(Exception):
    """撤销/重做时历史与当前状态不一致（防御性保护，绝不错改数据）。"""


# ---------------------------------------------------------------------------
# 路径工具：路径 <-> Treeview iid <-> 显示文本
# ---------------------------------------------------------------------------

def path_to_iid(path: Path) -> str:
    """路径编码为 Treeview iid（JSON 编码，对任意 key/下标都唯一且可逆）。"""
    return json.dumps(list(path), ensure_ascii=False)


def iid_to_path(iid: str) -> Path:
    return tuple(json.loads(iid))


def format_path(path: Path) -> str:
    """路径显示为 $.a.b[0] 形式（仅用于状态栏/搜索结果展示）。"""
    s = "$"
    for seg in path:
        if isinstance(seg, int):
            s += "[%d]" % seg
        else:
            s += "." + str(seg) if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(seg)) \
                else "[%s]" % json.dumps(seg, ensure_ascii=False)
    return s


def type_name(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def scalar_label(value: Any, show_quotes: bool, max_len: int = 120) -> str:
    """标量值的树视图显示文本（引号开关只影响视图，不影响数据）。"""
    if isinstance(value, dict):
        return "{%d}" % len(value)
    if isinstance(value, list):
        return "[%d]" % len(value)
    if isinstance(value, str):
        s = '"%s"' % value if show_quotes else value
    elif value is None:
        s = "null"
    elif isinstance(value, bool):
        s = "true" if value else "false"
    else:
        s = json.dumps(value)
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


# ---------------------------------------------------------------------------
# 严格 JSON 解析：NaN/Infinity 拒绝、重复键检测、注释剥离兜底（状态机）
# ---------------------------------------------------------------------------

def _reject_constant(name: str) -> Any:
    raise ValueError("非标准 JSON 常量: %s（NaN/Infinity 不被标准 JSON 允许）" % name)


def _make_pairs_hook(dup_sink: List[Tuple[Path, str]]) -> Callable:
    """object_pairs_hook：检测重复 key（保留最后值，与标准行为一致），重复项写入 dup_sink。"""

    def hook(pairs):
        d = {}
        seen = set()
        for k, v in pairs:
            if k in seen:
                dup_sink.append(k)
            seen.add(k)
            d[k] = v
        return d

    return hook


def strip_comments(text: str) -> Tuple[str, bool, bool]:
    """状态机剥离 // 与 /* */ 注释。字符串字面量（含转义）内的 // 与斜杠不处理。
    注意：这不是裸正则，"https://a//b"、"a /* b */ c" 等 JSON 字符串值不受影响。
    返回 (剥离后文本, 是否含注释, 块注释是否未闭合)。
    未闭合的 /* 属于语法错误，调用方必须判定解析失败，不得静默吞到 EOF。"""
    out: List[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:          # 转义序列原样保留
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        else:
            if c == '"':
                in_str = True
                out.append(c)
                i += 1
            elif c == "/" and i + 1 < n and text[i + 1] == "/":   # 行注释
                while i < n and text[i] != "\n":
                    i += 1
            elif c == "/" and i + 1 < n and text[i + 1] == "*":  # 块注释
                j = text.find("*/", i + 2)
                if j == -1:
                    return "".join(out), True, True   # 未闭合：语法错误
                i = j + 2
            else:
                out.append(c)
                i += 1
    return "".join(out), False, False


class ParseResult:
    __slots__ = ("ok", "data", "error", "lineno", "colno",
                 "had_comments", "dup_keys")

    def __init__(self, ok: bool, data: Any = None, error: str = "",
                 lineno: int = 0, colno: int = 0,
                 had_comments: bool = False, dup_keys: Optional[List[str]] = None):
        self.ok = ok
        self.data = data
        self.error = error
        self.lineno = lineno
        self.colno = colno
        self.had_comments = had_comments
        self.dup_keys = dup_keys or []


def parse_json_text(text: str) -> ParseResult:
    """严格解析 JSON 文本。严格失败后尝试剥离注释兜底（成功则标记 had_comments）。
    未闭合的 /* 块注释直接判定解析失败。每次 _load 使用独立的 dup_sink，
    避免注释兜底重试时重复收集。dup_keys 非空表示源文本存在重复键。"""

    def _load(src: str) -> Tuple[bool, Any, Optional[json.JSONDecodeError], List[str]]:
        sink: List[str] = []
        hook = _make_pairs_hook(sink)
        try:
            obj = json.loads(src, parse_constant=_reject_constant,
                             object_pairs_hook=hook)
            return True, obj, None, sink
        except json.JSONDecodeError as e:
            return False, None, e, sink
        except ValueError as e:  # NaN/Infinity 等非标准常量
            return False, None, json.JSONDecodeError(str(e), src, 1), sink

    ok, obj, err, sink = _load(text)
    all_dups = list(sink)
    had_comments = False
    if not ok:
        stripped, had_comments, unterminated = strip_comments(text)
        if unterminated:
            return ParseResult(False,
                               error="存在未闭合的 /* 块注释（缺少 */）")
        ok, obj, err, sink2 = _load(stripped)
        all_dups.extend(sink2)
        if ok:
            had_comments = True

    if ok:
        return ParseResult(True, data=obj, had_comments=had_comments,
                           dup_keys=all_dups)
    return ParseResult(False, error=err.msg if err else "未知错误",
                       lineno=err.lineno if err else 0,
                       colno=err.colno if err else 0)


# ---------------------------------------------------------------------------
# IO：编码探测 / 原子写 / 备份轮转
# ---------------------------------------------------------------------------

def detect_encoding(raw: bytes) -> str:
    """按 BOM 优先探测编码；无 BOM 时验证 UTF-8，失败回退 UTF-16/UTF-8 宽松。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32-le"
    if raw.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32-be"
    if raw.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16-be"
    # 无 BOM 的 UTF-16 启发式：NUL 字节密集（合法 UTF-8 中 NUL 极罕见）
    head = raw[:256]
    if len(head) >= 4 and head.count(0) > len(head) // 4:
        zeros_odd = head[1::2].count(0)   # LE: ASCII 高位 0 在奇数索引
        zeros_even = head[0::2].count(0)  # BE: 相反
        return "utf-16-le" if zeros_odd >= zeros_even else "utf-16-be"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    for enc in ("utf-16", "gb18030"):
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"  # 最终兜底，读取时以 errors="replace" 提示


def read_text_file(path: str) -> Tuple[str, str]:
    """读文件，返回 (文本, 实际使用的编码)。"""
    with open(path, "rb") as f:
        raw = f.read()
    enc = detect_encoding(raw)
    if enc in ("utf-8", "utf-8-sig"):
        return raw.decode(enc, errors="replace"), enc
    return raw.decode(enc, errors="replace"), enc


def serialize_json(data: Any, pretty: bool, indent: Any) -> str:
    """模型 → JSON 文本。allow_nan=False 保证输出严格 JSON。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=False,
                      allow_nan=False, indent=(indent if pretty else None),
                      separators=(",", ": ") if pretty else (",", ":"))


def write_atomic(path: str, text: str) -> None:
    """原子写：写 .tmp → flush+fsync → os.replace，防止写一半崩溃损坏原文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def backup_before_save(path: str, keep: int = BACKUP_KEEP) -> None:
    """保存前的备份链（顺序经过设计，保证任一环节崩溃都有可用副本）：
    1) 旧文件 → <name>.<时间戳>.bak（历史版本，轮转保留 keep 份）
    2) 旧文件 → <name>.bak（即时回滚副本，覆盖上一份）
    之后调用方再执行原子写。"""
    if not os.path.exists(path):
        return
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")  # 微秒级，同秒多次保存不互覆盖
    stamp_bak = "%s.%s.bak" % (path, ts)
    try:
        shutil.copy2(path, stamp_bak)
        # 轮转：删除最旧的时间戳备份
        pattern = os.path.basename(path) + ".*.bak"
        stamps = sorted(glob.glob(os.path.join(os.path.dirname(path) or ".", pattern)))
        for old in stamps[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass
        shutil.copy2(path, path + ".bak")
    except OSError:
        pass  # 备份失败不阻塞保存（写操作仍是原子的）


def file_stat(path: str) -> Optional[Tuple[float, int]]:
    """返回 (mtime, size) 用于外部修改检测。"""
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def semantic_fingerprint(root: Any) -> str:
    """模型语义指纹：sha256(canonical compact 序列化)。
    dirty 判断依据（硬约束①：以磁盘快照为基准，而非"执行过命令"）。
    修改→Undo 回到原状时指纹相同 → dirty 自动恢复 False。"""
    return hashlib.sha256(
        serialize_json(root, False, None).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# JsonModel —— 单一数据源（root 可为任意 JSON 值）
# ---------------------------------------------------------------------------

class JsonModel:
    """内存中的权威数据。所有变更经由 Command 执行，模型只提供原子操作。"""

    _UNSET = object()  # 区分"未传参"与合法的 JSON null 根节点

    def __init__(self, root: Any = _UNSET):
        # 根可为任意 JSON 值（含 null）；仅未传参时默认空对象
        self.root: Any = {} if root is JsonModel._UNSET else root
        self.file_path: Optional[str] = None
        self.file_encoding: str = "utf-8"
        self.revision: int = 0            # 每次变更 +1（仅作指纹缓存失效，不用于 dirty 判断）
        self._mutation_depth: int = 0     # mutation guard（常开，无开关）
        self._saved_fingerprint: str = semantic_fingerprint(self.root)
        self._fp_cache: Tuple[int, str] = (0, self._saved_fingerprint)

    # -- mutation guard：所有修改必须发生在 Command.do/undo 内 -----------------

    @contextmanager
    def mutation_scope(self):
        """Command.do/undo 进入模型修改的合法上下文。"""
        self._mutation_depth += 1
        try:
            yield
        finally:
            self._mutation_depth -= 1

    def _assert_mutable(self) -> None:
        if self._mutation_depth <= 0:
            raise AssertionError(
                "JsonModel 修改必须发生在 Command.do/undo 内（mutation guard）")

    # -- 语义指纹 dirty（以磁盘快照为基准） --------------------------------------

    @property
    def model_dirty(self) -> bool:
        if self._fp_cache[0] != self.revision:
            self._fp_cache = (self.revision, semantic_fingerprint(self.root))
        return self._fp_cache[1] != self._saved_fingerprint

    @property
    def dirty(self) -> bool:  # 兼容别名
        return self.model_dirty

    def set_saved_baseline(self) -> None:
        """保存成功后更新磁盘基准（硬约束②：不清空 Undo 历史，由调用方保证）。"""
        self._saved_fingerprint = semantic_fingerprint(self.root)
        self._fp_cache = (self.revision, self._saved_fingerprint)

    def touch(self) -> None:
        self.revision += 1

    # -- 基础寻址（只读，无需 guard） ----------------------------------------

    def get_by_path(self, path: Path) -> Any:
        obj = self.root
        for seg in path:
            obj = obj[seg]  # KeyError/IndexError 向上抛，由调用方防御
        return obj

    def _locate(self, path: Path) -> Tuple[Any, Any]:
        """返回 (父容器, 该节点在父容器中的 key/index)。path 为 () 时无父。"""
        if not path:
            raise HistoryError("根节点没有父容器")
        parent = self.get_by_path(path[:-1])
        return parent, path[-1]

    # -- 变更原语（仅限 Command 在 mutation_scope 内调用） ------------------

    def set_value(self, path: Path, value: Any) -> None:
        self._assert_mutable()
        if not path:
            self.root = value
        else:
            parent, key = self._locate(path)
            parent[key] = value
        self.touch()

    def rename_key(self, parent_path: Path, old_key: str, new_key: str) -> None:
        """对象内改键名（保持原位置）。new_key 重复时抛 ValueError。"""
        self._assert_mutable()
        parent, key = self._locate(parent_path + (old_key,))
        if not isinstance(parent, dict):
            raise HistoryError("父容器不是对象")
        if new_key == old_key:
            return
        if new_key in parent:
            raise ValueError("键 %r 已存在" % new_key)
        items = list(parent.items())
        parent.clear()
        for k, v in items:
            parent[new_key if k == old_key else k] = v
        self.touch()

    def insert_child(self, parent_path: Path, key: Optional[str],
                     value: Any, index: Optional[int] = None) -> Path:
        """在容器中新增子节点（对象追加键 / 数组按下标插入），返回新子路径。"""
        self._assert_mutable()
        parent = self.get_by_path(parent_path)
        if isinstance(parent, dict):
            k = key if key is not None else self._unique_key(parent, "newKey")
            if k in parent:
                raise ValueError("键 %r 已存在" % k)
            parent[k] = value
            self.touch()
            return parent_path + (k,)
        if isinstance(parent, list):
            i = len(parent) if index is None else max(0, min(index, len(parent)))
            parent.insert(i, value)
            self.touch()
            return parent_path + (i,)
        raise HistoryError("容器类型错误")

    @staticmethod
    def _unique_key(d: dict, base: str) -> str:
        if base not in d:
            return base
        i = 1
        while "%s%d" % (base, i) in d:
            i += 1
        return "%s%d" % (base, i)

    def delete(self, path: Path) -> Tuple[Any, Any, Any]:
        """删除节点，返回 (父路径, key/index, 被删子树) 供命令精确恢复。"""
        self._assert_mutable()
        if not path:
            old_root = self.root
            self.root = {}
            self.touch()
            return (), None, old_root
        parent, key = self._locate(path)
        value = parent[key]
        del parent[key]
        self.touch()
        return path[:-1], key, value

    def move(self, path: Path, delta: int) -> Tuple[int, int]:
        """同级上移(delta=-1)/下移(delta=+1)。越界抛 HistoryError。
        对象通过重建有序 dict 实现键序调整（Python 3.7+ 保证插入序）。"""
        self._assert_mutable()
        if not path:
            raise HistoryError("根节点无法移动")
        parent, key = self._locate(path)
        if isinstance(parent, dict):
            keys = list(parent.keys())
            i = keys.index(key)
            j = i + delta
            if j < 0 or j >= len(keys):
                raise HistoryError("已到边界")
            value = parent.pop(key)
            keys.remove(key)
            keys.insert(j, key)   # 移除后按目标位插回（两个方向通用）
            rebuilt = {k: (value if k == key else parent[k]) for k in keys}
            parent.clear()
            parent.update(rebuilt)
            self.touch()
            return i, j
        if isinstance(parent, list):
            i = key
            j = i + delta
            if j < 0 or j >= len(parent):
                raise HistoryError("已到边界")
            parent.insert(j, parent.pop(i))
            self.touch()
            return i, j
        raise HistoryError("容器类型错误")

    def move_to(self, parent_path: Path, old_index: int, new_index: int) -> None:
        """undo/redo 用的精确位置恢复（dict 按键序表操作）。"""
        self._assert_mutable()
        parent = self.get_by_path(parent_path)
        if isinstance(parent, dict):
            keys = list(parent.keys())
            key = keys[old_index]
            value = parent.pop(key)
            keys = [k for k in keys if k != key]
            keys.insert(max(0, min(new_index, len(keys))), key)
            rebuilt = {k: (value if k == key else parent[k]) for k in keys}
            parent.clear()
            parent.update(rebuilt)
        else:
            parent.insert(new_index, parent.pop(old_index))
        self.touch()

    def replace_document(self, obj: Any) -> None:
        """整体替换根对象（文本视图 commit / 打开文件）。"""
        self._assert_mutable()
        self.root = obj
        self.touch()

    # -- 序列化与遍历 ------------------------------------------------------

    def to_text(self, pretty: bool = True, indent: Any = 4) -> str:
        return serialize_json(self.root, pretty, indent)

    def count_nodes(self, obj: Any = _UNSET) -> int:
        if obj is JsonModel._UNSET:
            obj = self.root
        n = 1
        if isinstance(obj, dict):
            for v in obj.values():
                n += self.count_nodes(v)
        elif isinstance(obj, list):
            for v in obj:
                n += self.count_nodes(v)
        return n

    def iter_nodes(self, obj: Any = None, path: Path = ()
                   ) -> Iterator[Tuple[Path, Any, str, Any]]:
        """深度优先遍历全部节点，产出 (path, key_label, type, value)。
        key_label: 对象键名字符串 / 数组下标整数字符串 / 根为 ""。
        搜索/过滤必须遍历模型而非 Treeview（惰性加载下 Tree 只有部分节点）。"""
        if obj is None and not path:
            obj = self.root
        label = "" if not path else (
            str(path[-1]) if isinstance(path[-1], int) else str(path[-1]))
        yield path, label, type_name(obj), obj
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from self.iter_nodes(v, path + (k,))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from self.iter_nodes(v, path + (i,))


# ---------------------------------------------------------------------------
# Command 命令族 —— 只存受影响数据与必要子树快照，不做全量文档快照
# ---------------------------------------------------------------------------

class Command:
    """所有可撤销操作的基类。子类实现 _do/_undo；
    基类负责进入 mutation_scope（mutation guard：修改只能经由命令发生）。"""

    def do(self, model: JsonModel) -> None:
        with model.mutation_scope():
            self._do(model)

    def undo(self, model: JsonModel) -> None:
        with model.mutation_scope():
            self._undo(model)

    def _do(self, model: JsonModel) -> None:
        raise NotImplementedError

    def _undo(self, model: JsonModel) -> None:
        raise NotImplementedError

    def affected_paths(self, direction: str = "do"):
        """受影响的投影变更描述，供视图局部刷新（纯增量，不改数据语义）。

        direction: "do" | "undo"。返回 (kind, ...)：
          ("full", None)                 → 全量 rebuild（保守兜底）
          ("update", path)               → 单行值更新（路径不变）
          ("update_multi", [path, ...])  → 多行值更新
          ("rename", parent_path)        → 父下直接子行重建（key 段变化使子 iid 全变）
          ("insert", parent_path)        → 父下插入（list 级联由视图层降级）
          ("delete", (parent, child))    → 父下删除
          ("move", cur_path, delta)      → 相邻移动：cur_path 处元素移到 cur+delta
        默认 ("full", None)：未知命令/文档级替换保守全量重建。
        """
        return ("full", None)


class SetValueCommand(Command):
    """改值（含类型切换产生的值变化）。"""

    def __init__(self, path: Path, old: Any, new: Any):
        self.path, self.old, self.new = path, old, new

    def affected_paths(self, direction: str = "do"):
        return ("update", self.path)

    def _do(self, model: JsonModel) -> None:
        model.set_value(self.path, self.new)

    def _undo(self, model: JsonModel) -> None:
        model.set_value(self.path, self.old)


class RenameKeyCommand(Command):
    def __init__(self, parent_path: Path, old_key: str, new_key: str):
        self.parent_path, self.old_key, self.new_key = parent_path, old_key, new_key

    def _do(self, model: JsonModel) -> None:
        model.rename_key(self.parent_path, self.old_key, self.new_key)

    def _undo(self, model: JsonModel) -> None:
        model.rename_key(self.parent_path, self.new_key, self.old_key)

    def affected_paths(self, direction: str = "do"):
        return ("rename", self.parent_path)


class InsertCommand(Command):
    """新增节点。do 时插入；undo 时按记录的位置删除。"""

    def __init__(self, parent_path: Path, key: Optional[str], value: Any,
                 index: Optional[int] = None):
        self.parent_path, self.key, self.value, self.index = \
            parent_path, key, value, index
        self.child_path: Optional[Path] = None

    def _do(self, model: JsonModel) -> None:
        self.child_path = model.insert_child(self.parent_path, self.key,
                                             self.value, self.index)

    def _undo(self, model: JsonModel) -> None:
        if self.child_path:
            model.delete(self.child_path)

    def path(self) -> Optional[Path]:
        return self.child_path

    def affected_paths(self, direction: str = "do"):
        if direction == "do":
            return ("insert", self.parent_path)
        # undo：恢复删除 → 视作删除该行（child_path 已记录）
        return ("delete", (self.parent_path, self.child_path))


class DeleteCommand(Command):
    """删除节点（保存被删子树以便精确恢复到原位置）。"""

    def __init__(self, path: Path):
        self.path = path
        self.parent_path: Optional[Path] = None
        self.key: Any = None
        self.value: Any = None
        self.index: Optional[int] = None

    def _do(self, model: JsonModel) -> None:
        self.parent_path, self.key, self.value = model.delete(self.path)
        self.index = self.key if isinstance(self.key, int) else None

    def _undo(self, model: JsonModel) -> None:
        if not self.path:
            # 删除的是根节点本身：恢复整棵文档
            model.replace_document(self.value)
        elif isinstance(self.key, int):
            model.insert_child(self.parent_path, None, self.value, self.index)
        else:
            model.insert_child(self.parent_path, self.key, self.value)

    def affected_paths(self, direction: str = "do"):
        if not self.path:
            return ("full", None)  # 根节点删除/恢复 → 文档级，全量重建
        if direction == "do":
            return ("delete", (self.parent_path, self.path))
        # undo：恢复删除 → 视作插入（list 级联由视图层降级）
        return ("insert", self.parent_path)


class MoveCommand(Command):
    def __init__(self, path: Path, delta: int):
        self.path, self.delta = path, delta
        self.parent_path: Optional[Path] = None
        self.old_index: Optional[int] = None
        self.new_index: Optional[int] = None

    def _do(self, model: JsonModel) -> None:
        self.old_index, self.new_index = model.move(self.path, self.delta)
        self.parent_path = self.path[:-1]

    def _undo(self, model: JsonModel) -> None:
        model.move_to(self.parent_path, self.new_index, self.old_index)

    def affected_paths(self, direction: str = "do"):
        if direction == "do":
            return ("move", self.path, self.delta)
        # undo：元素现位于 path+delta，移回原位置 path
        cur = self.path[:-1] + (self.path[-1] + self.delta,)
        return ("move", cur, -self.delta)


class ReplaceAllCommand(Command):
    """批量替换（搜索替换的「全部替换」）—— 整体一个 undo 步骤。"""

    def __init__(self, pairs: List[Tuple[Path, Any, Any]]):
        self.pairs = pairs  # [(path, old, new), ...]

    def _do(self, model: JsonModel) -> None:
        for path, _old, new in self.pairs:
            model.set_value(path, new)

    def _undo(self, model: JsonModel) -> None:
        for path, old, _new in reversed(self.pairs):
            model.set_value(path, old)

    def affected_paths(self, direction: str = "do"):
        return ("update_multi", [p for p, _o, _n in self.pairs])


class ReplaceDocumentCommand(Command):
    """文本视图 commit / 打开新文档：整体替换根对象。"""

    def __init__(self, old_obj: Any, new_obj: Any):
        self.old_obj, self.new_obj = old_obj, new_obj

    def _do(self, model: JsonModel) -> None:
        model.replace_document(self.new_obj)

    def _undo(self, model: JsonModel) -> None:
        model.replace_document(self.old_obj)

    def merge(self, other: "ReplaceDocumentCommand") -> bool:
        """连续的文本编辑合并为一条历史（Ctrl+Z 一次回到编辑会话之前）。"""
        self.new_obj = other.new_obj
        return True


# ---------------------------------------------------------------------------
# History —— LIFO 撤销/重做双栈
# ---------------------------------------------------------------------------

class History:
    def __init__(self, limit: int = UNDO_LIMIT):
        self.limit = limit
        self.undo_stack: List[Command] = []
        self.redo_stack: List[Command] = []

    def push(self, cmd: Command) -> None:
        self.undo_stack.append(cmd)
        self.redo_stack.clear()
        if len(self.undo_stack) > self.limit:
            self.undo_stack.pop(0)

    def push_merge(self, cmd: Command) -> None:
        """文本编辑专用：若栈顶仍是同一条未中断的文本命令则合并，否则新入栈。"""
        top = self.undo_stack[-1] if self.undo_stack else None
        if isinstance(top, ReplaceDocumentCommand) and \
                isinstance(cmd, ReplaceDocumentCommand) and top.merge(cmd):
            return
        self.push(cmd)

    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    def undo(self, model: JsonModel) -> Command:
        if not self.undo_stack:
            raise HistoryError("没有可撤销的操作")
        cmd = self.undo_stack.pop()
        try:
            cmd.undo(model)
        except (KeyError, IndexError, HistoryError) as e:
            self.undo_stack.append(cmd)  # 回滚栈状态
            raise HistoryError("历史与当前状态不一致，已停止撤销: %s" % e)
        self.redo_stack.append(cmd)
        return cmd

    def redo(self, model: JsonModel) -> Command:
        if not self.redo_stack:
            raise HistoryError("没有可重做的操作")
        cmd = self.redo_stack.pop()
        try:
            cmd.do(model)  # redo 即重放 do（MoveCommand 同样适用）
        except (KeyError, IndexError, HistoryError) as e:
            self.redo_stack.append(cmd)
            raise HistoryError("历史与当前状态不一致，已停止重做: %s" % e)
        self.undo_stack.append(cmd)
        return cmd

    def clear(self) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()


# ---------------------------------------------------------------------------
# 自测（无 GUI）：python3 pyjsoneditor.py --selftest
# ---------------------------------------------------------------------------

def _selftest() -> int:
    failures: List[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)
        print(("PASS" if cond else "FAIL"), name)

    # -- 解析器 ------------------------------------------------------------
    r = parse_json_text('{"a": 1, "b": [true, null, 1.5], "中文": "值"}')
    check("parse basic", r.ok and r.data["a"] == 1 and r.data["中文"] == "值")
    r = parse_json_text('{"url": "https://a//b", "t": "x /* y */ z"}')
    check("parse urls in strings", r.ok and r.data["url"] == "https://a//b")
    r = parse_json_text('{\n  // 注释\n  "a": 1 /* 块注释 */\n}')
    check("parse comments fallback", r.ok and r.had_comments and r.data["a"] == 1)
    r = parse_json_text('{"a": NaN}')
    check("reject NaN", not r.ok)
    r = parse_json_text('{"a": Infinity}')
    check("reject Infinity", not r.ok)
    r = parse_json_text('{"k": 1, "k": 2}')
    check("dup key detected", r.ok and r.dup_keys == ["k"] and r.data["k"] == 2)
    r = parse_json_text('{"a": 1,}')
    check("trailing comma invalid", not r.ok)
    r = parse_json_text('123')
    check("scalar root", r.ok and r.data == 123)
    r = parse_json_text('"hello"')
    check("string root", r.ok and r.data == "hello")

    # -- 编码探测 ------------------------------------------------------------
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="jsoneditor_test_")
    p_bom = os.path.join(tmpdir, "bom.json")
    with open(p_bom, "wb") as f:
        f.write(b"\xef\xbb\xbf" + '{"a":1}'.encode("utf-8"))
    text, enc = read_text_file(p_bom)
    check("bom detect", enc == "utf-8-sig" and parse_json_text(text).ok)
    p_u16 = os.path.join(tmpdir, "u16.json")
    with open(p_u16, "wb") as f:
        f.write('{"a":1}'.encode("utf-16-le"))
    _t, enc16 = read_text_file(p_u16)
    check("utf16 detect", "utf-16" in enc16)

    # -- 模型与命令 ----------------------------------------------------------
    m = JsonModel({"users": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
                   "cfg": {"x": 1, "y": 2}})
    h = History()

    # 改值
    p = ("users", 2, "name")
    old = m.get_by_path(p)
    cmd = SetValueCommand(p, old, "D")
    cmd.do(m)
    h.push(cmd)
    check("set value", m.get_by_path(p) == "D")

    # 移动 users[0] 下移（注意 users[2] 已被上一步改为 "D"）
    mv = MoveCommand(("users", 0), 1)
    mv.do(m)
    h.push(mv)
    check("move down", [u["name"] for u in m.root["users"]] == ["B", "A", "D"])

    # 删除 users[1]
    dl = DeleteCommand(("users", 1))
    dl.do(m)
    h.push(dl)
    check("delete", [u["name"] for u in m.root["users"]] == ["B", "D"])

    # 改名
    rn = RenameKeyCommand(("cfg",), "x", "xx")
    rn.do(m)
    h.push(rn)
    check("rename", "xx" in m.root["cfg"] and "x" not in m.root["cfg"])

    # 新增
    ins = InsertCommand(("cfg",), "z", 42)
    ins.do(m)
    h.push(ins)
    check("insert", m.root["cfg"]["z"] == 42)

    # 连续 undo 5 次（含 SetValue）→ 恢复到最初状态
    for _ in range(5):
        h.undo(m)
    check("undo chain", [u["name"] for u in m.root["users"]] == ["A", "B", "C"]
          and m.get_by_path(p) == "C" and list(m.root["cfg"].keys()) == ["x", "y"])

    # redo 按入栈逆序重放：SetValue -> Move -> Delete -> Rename -> Insert
    h.redo(m)  # SetValue
    h.redo(m)  # Move
    h.redo(m)  # Delete
    check("redo chain 1", [u["name"] for u in m.root["users"]] == ["B", "D"]
          and m.get_by_path(("users", 1, "name")) == "D")
    h.redo(m)  # Rename
    h.redo(m)  # Insert
    check("redo chain 2", m.root["cfg"]["z"] == 42 and "xx" in m.root["cfg"])

    # move_to 精确恢复（new_index = 移除后列表中的插入位置）
    m2 = JsonModel({"k": {"a": 1, "b": 2, "c": 3}})
    with m2.mutation_scope():
        m2.move(("k", "a"), 1)
    check("dict move", list(m2.root["k"].keys()) == ["b", "a", "c"])
    with m2.mutation_scope():
        m2.move_to(("k",), 1, 0)   # 把 index1(a) 移回 index0
    check("dict move_to", list(m2.root["k"].keys()) == ["a", "b", "c"])

    # 删除子树恢复
    big = {"user": {"name": "Tom", "age": 18, "addr": {"city": "Tokyo"}}}
    m3 = JsonModel(dict(big))
    d3 = DeleteCommand(("user",))
    d3.do(m3)
    h3 = History()
    h3.push(d3)
    check("delete subtree", m3.root == {})
    h3.undo(m3)
    check("restore subtree", m3.root == big)

    # 标量根文档
    m4 = JsonModel(42)
    with m4.mutation_scope():
        m4.set_value((), "hello")
    check("scalar root set", m4.root == "hello")

    # 批量替换（一步撤销）
    m5 = JsonModel({"a": "Tokyo", "b": {"c": "Tokyo"}, "d": ["Tokyo", "Osaka"]})
    pairs = [(("a",), "Tokyo", "Kyoto"),
             (("b", "c"), "Tokyo", "Kyoto"),
             (("d", 0), "Tokyo", "Kyoto")]
    ra = ReplaceAllCommand(pairs)
    ra.do(m5)
    h5 = History()
    h5.push(ra)
    check("replace all", m5.root["a"] == "Kyoto"
          and m5.root["b"]["c"] == "Kyoto" and m5.root["d"][0] == "Kyoto")
    h5.undo(m5)
    check("replace all undo", m5.root["a"] == "Tokyo"
          and m5.root["b"]["c"] == "Tokyo" and m5.root["d"][0] == "Tokyo")

    # 文本命令合并
    h6 = History()
    c1 = ReplaceDocumentCommand({}, {"a": 1})
    c1.do(m := JsonModel({}))
    h6.push_merge(c1)
    c2 = ReplaceDocumentCommand({"a": 1}, {"a": 1, "b": 2})
    c2.do(m)
    h6.push_merge(c2)
    check("text cmd merged", len(h6.undo_stack) == 1)
    h6.undo(m)
    check("text cmd undo to before session", m.root == {})

    # 移动边界
    m7 = JsonModel([1, 2, 3])
    try:
        with m7.mutation_scope():
            m7.move((0,), -1)
        check("move bound", False)
    except HistoryError:
        check("move bound", True)

    # 序列化保真
    m8 = JsonModel({"中文": "值", "n": 1.0, "i": 1, "b": False, "z": None})
    txt = m8.to_text(True, 2)
    r8 = parse_json_text(txt)
    check("serialize fidelity", r8.ok and r8.data == m8.root
          and "中文" in txt and '"n": 1.0' in txt)
    try:
        serialize_json(float("nan"), True, 2)
        check("allow_nan false", False)
    except ValueError:
        check("allow_nan false", True)

    # 原子写 + 备份
    p_save = os.path.join(tmpdir, "save.json")
    with open(p_save, "w", encoding="utf-8") as f:
        f.write('{"old": 1}')
    backup_before_save(p_save, keep=3)
    write_atomic(p_save, '{"new": 2}')
    check("atomic write", os.path.exists(p_save + ".bak")
          and parse_json_text(open(p_save, encoding="utf-8").read()).data == {"new": 2})

    # 注释剥离不破坏字符串
    tricky = '{"a": "he said \\"// ok\\"", "url": "http://x/?a=1", "s": "*/"}'
    st, _had, _unterm = strip_comments(tricky)
    check("strip keeps strings", parse_json_text(st).ok and st == tricky)

    # null 根节点（P0-1 回归：JsonModel(None) 不得把合法 null 根变成 {}）
    m_null = JsonModel(parse_json_text("null").data)
    check("null root preserved", m_null.root is None
          and m_null.to_text() == "null")

    # 未闭合块注释必须判定为解析错误（P1-5 回归）
    r_un = parse_json_text('{"a":1} /* unclosed')
    check("unterminated comment rejected", not r_un.ok)

    # dup_sink 每次解析独立收集（P1-8 回归：注释兜底重试不得重复计数）
    r_dup = parse_json_text('{"a": 1, "a": 2, /* c */ "b": 1, "b": 2}')
    check("dup sink isolated",
          r_dup.ok and sorted(r_dup.dup_keys) == ["a", "b"])

    # -- mutation guard（硬约束：修改必须经 Command） --------------------------
    m9 = JsonModel({"a": 1})
    try:
        m9.set_value(("a",), 2)   # 绕过 Command 裸调 → 必须被 guard 拦截
        check("mutation guard", False)
    except AssertionError:
        check("mutation guard", True)

    # -- 语义指纹 dirty（硬约束①：以磁盘快照为基准） ---------------------------
    m10 = JsonModel({"a": 1, "b": 2})
    check("dirty initial", not m10.model_dirty)
    sc = SetValueCommand(("a",), 1, 3)
    sc.do(m10)
    check("dirty after edit", m10.model_dirty)
    sc.undo(m10)
    check("dirty after undo", not m10.model_dirty)   # 修改→Undo 回原状 → dirty 恢复 False
    sc.do(m10)
    m10.set_saved_baseline()
    check("dirty after save", not m10.model_dirty)
    sc.undo(m10)
    check("dirty after undo post-save", m10.model_dirty)  # 保存后 Undo → dirty True（不清历史）

    # -- 属性测试：随机命令 do→undo→do(do) 不变量 -------------------------------
    import copy
    import random
    rng = random.Random(20260828)

    def rand_json(depth: int) -> Any:
        kinds = (["int", "str", "bool", "null", "float"] if depth <= 0
                 else ["dict", "list", "int", "str", "bool", "null"])
        t = rng.choice(kinds)
        if t == "dict":
            return {("k%d" % i): rand_json(depth - 1) for i in range(rng.randint(1, 4))}
        if t == "list":
            return [rand_json(depth - 1) for _ in range(rng.randint(1, 4))]
        if t == "int":
            return rng.randint(-1000, 1000)
        if t == "float":
            return round(rng.random() * 100, 3)
        if t == "str":
            return rng.choice(["a", "b", "中文", "x y", ""])
        if t == "bool":
            return rng.choice([True, False])
        return None

    def all_paths(obj: Any, path: Path = ()) -> Iterator[Path]:
        yield path
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from all_paths(v, path + (k,))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from all_paths(v, path + (i,))

    def random_cmd(model: JsonModel) -> Optional[Command]:
        paths = list(all_paths(model.root))
        p = rng.choice(paths)
        obj = model.get_by_path(p)
        kind = rng.choice(["set", "set", "move", "move", "insert",
                           "delete", "rename", "rename"])
        try:
            if kind == "set":
                if isinstance(obj, (dict, list)):
                    return SetValueCommand(p, obj, rng.randint(1, 99))
                return SetValueCommand(p, obj, rand_json(0))
            if kind == "move":
                return MoveCommand(p, rng.choice([-1, 1])) if p else None
            if kind == "insert":
                if isinstance(obj, dict):
                    return InsertCommand(p, None, rng.randint(0, 9))
                if isinstance(obj, list):
                    return InsertCommand(p, None, rng.randint(0, 9),
                                         rng.randint(0, len(obj)))
                return None
            if kind == "delete":
                return DeleteCommand(p) if p else None
            if kind == "rename":
                if p and isinstance(p[-1], str):
                    return RenameKeyCommand(p[:-1], p[-1],
                                            "renamed_%d" % rng.randint(0, 99))
                return None
        except (ValueError, HistoryError):
            return None
        return None

    prop_ok = True
    prop_detail = ""
    for _trial in range(300):
        mm = JsonModel(rand_json(3))
        before = copy.deepcopy(mm.root)
        cmd = random_cmd(mm)
        if cmd is None:
            continue
        try:
            cmd.do(mm)
        except (ValueError, HistoryError):
            continue  # 不适用的组合（如 rename 重名、move 越界）
        try:
            after = copy.deepcopy(mm.root)
            cmd.undo(mm)
            assert mm.root == before, "undo 未恢复操作前状态"
            cmd.do(mm)  # redo == 重放 do
            assert mm.root == after, "redo 未恢复操作后状态"
        except (AssertionError, ValueError, HistoryError) as e:
            prop_ok = False
            prop_detail = "%s: %s" % (type(cmd).__name__, e)
            break
    check("property invariant do/undo/redo", prop_ok)
    if not prop_ok:
        print("   属性测试失败详情:", prop_detail)

    # -- 转义往返（硬规则：保存必须正确重新转义） --------------------------------
    esc = {"nl": "a\nb\rc\td", "quote": "\"hello\"", "slash": "back\\slash",
           "bell": "x" + chr(8) + "y", "ff": chr(12), "uni": "中文\u4e2d文"}
    esc_txt = serialize_json(esc, True, 2)
    r_esc = parse_json_text(esc_txt)
    check("escape roundtrip", r_esc.ok and r_esc.data == esc)

    # -- 数字往返（语义保真） ----------------------------------------------------
    nums = {"z": 0, "neg": -1, "f": 1.0, "e10": 1e10, "m": -1.5e-3,
            "big": 999999999999999999999999999}
    nums_txt = serialize_json(nums, True, 2)
    r_num = parse_json_text(nums_txt)
    check("numbers roundtrip", r_num.ok and r_num.data == nums
          and isinstance(r_num.data["f"], float)
          and isinstance(r_num.data["big"], int))

    shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if failures:
        print("SELFTEST FAILED: %d 项未通过: %s" % (len(failures), ", ".join(failures)))
        return 1
    print("SELFTEST PASSED: 全部通过")
    return 0


# ---------------------------------------------------------------------------
# GUI 部分：主题 / 字体 / TextPane
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

MONO_FONT = "Menlo" if IS_MAC else ("Consolas" if IS_WIN else "DejaVu Sans Mono")
UI_FONT = "PingFang SC" if IS_MAC else ("Microsoft YaHei" if IS_WIN else "Noto Sans CJK SC")

THEMES = {
    "dark": {
        "bg": "#1E222A", "panel": "#252A33", "fg": "#E6EAF2", "muted": "#9AA4B5",
        "accent": "#3B82F6", "select_bg": "#2C313B", "field": "#1A1E25",
        "key": "#9CDCFE", "string": "#CE9178", "number": "#B5CEA8",
        "boolean": "#C586C0", "null": "#569CD6", "punct": "#6B7688",
        "error": "#F14C4C", "ok": "#4EC9B0", "warn": "#E2C08D",
        "linenum": "#5A6475", "curline": "#2A303B", "border": "#333A46",
    },
    "light": {
        "bg": "#F5F6F8", "panel": "#FFFFFF", "fg": "#24292F", "muted": "#6B7688",
        "accent": "#2563EB", "select_bg": "#DBE5F5", "field": "#FFFFFF",
        "key": "#0550AE", "string": "#953800", "number": "#116329",
        "boolean": "#8250DF", "null": "#0969DA", "punct": "#8B949E",
        "error": "#CF222E", "ok": "#1A7F37", "warn": "#9A6700",
        "linenum": "#9AA4B5", "curline": "#EFF2F6", "border": "#D8DEE4",
    },
}

_TOKEN_RE = re.compile(
    r'"(?:[^"\\]|\\.)*"'
    r'|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?'
    r'|true|false|null'
    r'|[(){}[\],:]'
)


def parse_scalar_input(text: str, current: Any) -> Any:
    """按节点当前类型解析输入（编辑不改变类型；类型转换通过删除后新增完成）。
    字符串节点输入什么都是字符串（可区分 "18" 与 18）；数字节点校验数值。"""
    if isinstance(current, bool):
        raise ValueError("布尔值请直接双击切换 true/false")
    if current is None:
        raise ValueError("null 值不可编辑（可删除后新增其他类型）")
    if isinstance(current, int):
        try:
            return int(text, 10)
        except ValueError:
            raise ValueError("需要整数，收到: %r" % text)
    if isinstance(current, float):
        try:
            v = float(text)
        except ValueError:
            raise ValueError("需要数字，收到: %r" % text)
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("非有限数字")
        return v
    return text  # 字符串：原样


class TextPane(ttk.Frame):
    """JSON 文本视图：正则语法高亮 / 行号 / 错误行定位。"""

    def __init__(self, master: "App"):
        super().__init__(master)
        self.app = master
        self._hl_job: Optional[str] = None
        self.text = tk.Text(self, wrap="none", undo=False, bd=0,
                            highlightthickness=0, font=(MONO_FONT, 13),
                            insertwidth=2)
        self.linenums = tk.Canvas(self, width=52, bd=0, highlightthickness=0)
        self._vs = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        hs = ttk.Scrollbar(self, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=self._on_vscroll, xscrollcommand=hs.set)
        self.linenums.grid(row=0, column=0, sticky="ns")
        self.text.grid(row=0, column=1, sticky="nsew")
        self._vs.grid(row=0, column=2, sticky="ns")
        hs.grid(row=1, column=1, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        for tag in ("key", "string", "number", "boolean", "null", "punct",
                    "errline", "curline", "bmatch", "bunmatch"):
            self.text.tag_configure(tag)
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<Configure>", lambda e: self._draw_linenums())
        # V1.1：离开编辑区立即解析（>8MB 暂停实时解析时的显式时机）
        self.text.bind("<FocusOut>",
                       lambda e: self.app._flush_text_parse())
        # V1.1.1：进入文本区前先落盘 Tree 编辑产生的待重建 Text（大文件防抖合并）
        self.text.bind("<FocusIn>",
                       lambda e: self.app._flush_text_rebuild())
        # V1.1：光标移动 → 当前行高亮 / 括号匹配 / 状态栏 Ln·Col
        self.text.bind("<KeyRelease>", self._on_cursor_moved)
        self.text.bind("<ButtonRelease-1>", self._on_cursor_moved)
        self._last_curline: Optional[str] = None
        self._big_text = False
        self._line_h: Optional[int] = None
        self._hl_range: Optional[Tuple[int, int]] = None  # 最近一次高亮的可视行范围
        self._skip_modified = False   # set_text 程序回写时抑制 on_text_changed

    def set_text(self, s: str) -> None:
        yview = self.text.yview()
        self.text.delete("1.0", "end")
        self.text.insert("1.0", s)
        self.text.edit_modified(False)
        self._skip_modified = True   # 本次 insert 引发的 <<Modified>> 不触发解析调度
        self._big_text = len(s) > HIGHLIGHT_LIMIT
        self._last_curline = None
        self._hl_range = None
        self._schedule_highlight()
        self.text.yview_moveto(yview[0])
        self.after_idle(self._draw_linenums)
        self.after_idle(self._update_curline)

    def get_text(self) -> str:
        return self.text.get("1.0", "end-1c")

    def _on_modified(self, _event=None) -> None:
        self.text.edit_modified(False)
        skip = self._skip_modified
        self._skip_modified = False
        self._schedule_highlight()
        self._draw_linenums()
        self._big_text = self._text_len() > HIGHLIGHT_LIMIT
        self._update_curline()
        if not skip:   # 程序回写（set_text）引发的 Modified：已同步，不重复解析
            self.app.on_text_changed()

    def _schedule_highlight(self) -> None:
        if self._hl_job:
            self.after_cancel(self._hl_job)
        self._hl_job = self.after(200, self._highlight)

    def _on_vscroll(self, *args: Any) -> None:
        """纵向滚动 → 同步滚动条并防抖重亮可视区（增量高亮）。"""
        self._vs.set(*args)
        self._schedule_highlight()

    def _highlight(self) -> None:
        """可视区语法高亮：只高亮可见行（±3 行缓冲），滚动时增量重亮。

        整文档一次性 tag_add 在 1MB+ 文本上会让 Tk 重写大范围 tag 结构
        （实测 >12s，主线程假死）。按视口行范围切片后开销只与视口相关，
        无论文档多大单次高亮均为毫秒级。"""
        self._hl_job = None
        t = self.text
        hl_tags = ("key", "string", "number", "boolean", "null", "punct")
        try:
            height = max(t.winfo_height(), 1)
            first = int(t.index("@0,0").split(".")[0])
            last = int(t.index("@0,%d" % height).split(".")[0]) + 3
        except tk.TclError:
            return
        try:
            total = int(t.index("end-1c").split(".")[0])
        except tk.TclError:
            return
        if total <= 0:
            return
        last = min(last, total + 1)
        if last - first <= 1:
            # 压缩视图/单行超长：跳过高亮，避免 Tk 对超长行做 tag 布局
            return
        # 只清理上一次高亮过的区域（避免对整文档 tag_remove）
        if self._hl_range:
            a, b = self._hl_range
            for tag in hl_tags:
                t.tag_remove(tag, "%d.0" % a, "%d.0" % b)
        self._hl_range = (first, last)
        seg = t.get("%d.0" % first, "%d.0" % last)
        if not seg:
            return
        n = len(seg)
        ranges: dict = {k: [] for k in hl_tags}
        for m in _TOKEN_RE.finditer(seg):
            tok = m.group()
            c = tok[0]
            if c == '"':
                j = m.end()
                while j < n and seg[j] in " \t\r\n":
                    j += 1
                tag = "key" if j < n and seg[j] == ":" else "string"
            elif c in "(){}[]:,":
                tag = "punct"
            elif tok in ("true", "false"):
                tag = "boolean"
            elif tok == "null":
                tag = "null"
            else:
                tag = "number"
            # Tk "+Nc" 是游标式字符前移，跨行自动；以可视首行为基准即可
            ranges[tag].extend(("%d.0+%dc" % (first, m.start()),
                                "%d.0+%dc" % (first, m.end())))
        for tag, rs in ranges.items():
            for i in range(0, len(rs), 10000):
                t.tag_add(tag, *rs[i:i + 10000])

    def _text_len(self) -> int:
        """文本字符数（Tk count，兼容 macOS Tk 返回值差异）。"""
        try:
            return int(self.tk.call(self.text._w, "count", "-chars",
                                    "1.0", "end-1c"))
        except tk.TclError:
            return 0

    def _on_cursor_moved(self, _e=None) -> None:
        """V1.1：光标移动后刷新当前行高亮、括号匹配与状态栏 Ln·Col。"""
        self._update_curline()
        self._update_bracket_match()
        self.app._update_cursor_pos()

    def _update_curline(self) -> None:
        """当前行背景高亮：行号变化才重绘，避免全文本重扫。"""
        t = self.text
        try:
            line = t.index("insert").split(".")[0]
        except tk.TclError:
            return
        if line == self._last_curline:
            return
        self._last_curline = line
        t.tag_remove("curline", "1.0", "end")
        t.tag_add("curline", "%s.0" % line, "%s.end" % line)
        t.tag_lower("curline")  # 保持在底层，不压过语法高亮/错误行

    def _update_bracket_match(self) -> None:
        """括号匹配：光标前一字符为括号时，在视口内 token 级配平扫描。
        >4MB 大文件跳过（与语法高亮边界一致）。"""
        t = self.text
        t.tag_remove("bmatch", "1.0", "end")
        t.tag_remove("bunmatch", "1.0", "end")
        if self._big_text:
            return
        try:
            pos = t.index("insert")
            if pos == "1.0":
                return
            # 光标所在行超长（压缩视图单行）：跳过，避免对超长行做 token 扫描
            if len(t.get(pos.split(".")[0] + ".0",
                         pos.split(".")[0] + ".end")) > MAX_HL_LINE:
                return
            prev = t.index("%s -1c" % pos)
        except tk.TclError:
            return
        ch = t.get(prev, pos)
        if ch not in "()[]{}":
            return
        if "string" in t.tag_names(prev):  # 括号位于字符串/键内，不匹配
            return
        w, h = max(t.winfo_width(), 1), max(t.winfo_height(), 1)
        top = t.index("@0,0")
        bot = t.index("@%d,%d lineend" % (w, h))
        # 光标在视口外（滚动后）：不在屏幕外寻找匹配
        if t.compare(prev, "<", top) or t.compare(prev, ">", bot):
            return
        if ch in "([{":
            m = self._match_bracket(t.get(prev, bot), ch, prev, forward=True)
        else:
            m = self._match_bracket(t.get(top, prev), ch, top, forward=False)
        if m is None:
            t.tag_add("bunmatch", prev, pos)
            return
        t.tag_add("bmatch", prev, pos)
        t.tag_add("bmatch", m, "%s+1c" % m)

    @staticmethod
    def _punct_tokens(text: str) -> list:
        """提取文本中的独立括号 token（(token, 偏移)，字符串内容天然排除）。"""
        return [(m.group(), m.start())
                for m in _TOKEN_RE.finditer(text) if m.group() in "()[]{}"]

    @staticmethod
    def _match_bracket(text: str, ch: str, base: str,
                       forward: bool) -> Optional[str]:
        """在 text 中配平括号，返回匹配位置的绝对索引；无匹配返回 None。
        forward=True：text 以 ch 开头向后找闭合；否则从右往左找配对。"""
        if forward:
            open_c, close_c = ch, {"(": ")", "[": "]", "{": "}"}[ch]
            depth = 0
            for tok, off in TextPane._punct_tokens(text):
                if tok == open_c:
                    depth += 1
                elif tok == close_c:
                    depth -= 1
                    if depth == 0:
                        return "%s+%dc" % (base, off)
        else:
            open_c, close_c = {")": "(", "]": "[", "}": "{"}[ch], ch
            depth = 1  # ch 自身需要一个配对开括号
            for tok, off in reversed(TextPane._punct_tokens(text)):
                if tok == close_c:
                    depth += 1
                elif tok == open_c:
                    depth -= 1
                    if depth == 0:
                        return "%s+%dc" % (base, off)
        return None

    def _draw_linenums(self) -> None:
        c = self.linenums
        c.delete("all")
        t = self.text
        total = int(t.index("end-1c").split(".")[0])
        if total <= 0:
            return
        first = int(t.index("@0,0").split(".")[0])
        last = first + int(t.winfo_height() // 20) + 2
        # V1.1：行高缓存（首次 bbox 一次，此后按固定行距绘制）。
        # 避免对超长单行反复调用 bbox——Tk 需布局整行，可致数百毫秒卡顿
        if self._line_h is None:
            # 超长行（压缩视图单行）不做 bbox——Tk 需布局整行，可致数百毫秒卡顿
            if len(t.get("%d.0" % first, "%d.end" % first)) > MAX_HL_LINE:
                self._line_h = 20
            else:
                bb = t.bbox("%d.0" % first)
                if not bb:
                    return
                self._line_h = bb[3]
        y = 1
        i = first
        while i <= min(total, last):
            c.create_text(46, y + self._line_h // 2 - 1, text=str(i),
                          anchor="e", font=(MONO_FONT, 11))
            i += 1
            y += self._line_h

    def mark_error(self, line: int, col: int) -> None:
        self.text.tag_remove("errline", "1.0", "end")
        if line <= 0:
            return
        self.text.tag_add("errline", "%d.0" % line, "%d.end" % line)
        self.text.see("%d.%d" % (max(line, 1), max(col - 1, 0)))

    def clear_error(self) -> None:
        self.text.tag_remove("errline", "1.0", "end")

    def apply_theme(self, th: dict) -> None:
        self.text.configure(background=th["field"], foreground=th["fg"],
                            insertbackground=th["fg"])
        self.linenums.configure(background=th["panel"])
        self.text.tag_configure("key", foreground=th["key"])
        self.text.tag_configure("string", foreground=th["string"])
        self.text.tag_configure("number", foreground=th["number"])
        self.text.tag_configure("boolean", foreground=th["boolean"])
        self.text.tag_configure("null", foreground=th["null"])
        self.text.tag_configure("punct", foreground=th["punct"])
        self.text.tag_configure("errline", background=th["error"])
        self.text.tag_configure("curline", background=th["curline"])
        self.text.tag_configure("bmatch", background=th["select_bg"])
        self.text.tag_configure("bunmatch", background=th["error"])


# ---------------------------------------------------------------------------
# TreePane —— 结构树（惰性加载 / 就地编辑 / 右键操作）
# ---------------------------------------------------------------------------

PH_SUFFIX = "__ph__"  # 懒加载占位子节点 iid 后缀
PG_SUFFIX = "__pg__"  # 分页占位 iid 后缀（超大子集合：双击加载更多）


class TreePane(ttk.Frame):
    def __init__(self, master: "App"):
        super().__init__(master)
        self.app = master
        self.tree = ttk.Treeview(self, columns=("type", "value"),
                                 selectmode="browse", show="tree headings")
        self.tree.heading("#0", text="键 / 索引", anchor="w")
        self.tree.heading("type", text="类型", anchor="w")
        self.tree.heading("value", text="值", anchor="w")
        self.tree.column("#0", width=280, minwidth=120, stretch=True)
        self.tree.column("type", width=80, minwidth=60, stretch=False)
        self.tree.column("value", width=420, minwidth=120, stretch=True)
        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self._editor: Optional[tk.Entry] = None
        self._edit_target: Optional[Tuple[str, str]] = None

        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<<TreeviewClose>>", self._on_close)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Return>", self._on_return)
        self.tree.bind("<Button-3>", self._on_menu)
        self.tree.bind("<Button-2>", self._on_menu)
        self.tree.bind("<Configure>", self._on_resize)
        self.tree.bind("<MouseWheel>", self._on_resize)
        self.tree.bind("<Motion>", self._on_motion)          # V1.1 Tooltip
        self.tree.bind("<Leave>", lambda e: self._hide_tooltip())
        self._tip: Optional[tk.Toplevel] = None
        self._tip_item: Optional[str] = None
        self._tip_col: Optional[str] = None
        self.menu = self._build_menu()

    # -- 构建 / 重建 -----------------------------------------------------------

    def rebuild(self) -> None:
        """全量重建可见树。惰性：只实例化 open_paths 中已展开的分支。"""
        self._cancel_edit()
        t = self.tree
        t.delete(*t.get_children(""))
        app = self.app
        root_obj = app.model.root
        is_container = isinstance(root_obj, (dict, list))
        root_open = is_container and (app.filter_paths is not None
                                      or () in app.open_paths)
        t.insert("", "end", iid=path_to_iid(()), text="root", open=root_open,
                 values=(type_name(root_obj),
                         self._row_display(root_obj, app.show_quotes)),
                 tags=("t_" + type_name(root_obj),))
        if is_container:
            self._insert_children((), path_to_iid(()))

    def _insert_children(self, path: Path, parent_iid: str) -> None:
        app = self.app
        try:
            obj = app.model.get_by_path(path)
        except (KeyError, IndexError):
            return
        t = self.tree
        if app.filter_paths is not None:
            # 过滤模式：可见性本就基于全量计算，可接受 O(N) 遍历
            children = self._children_of(path, obj)
            matched = [c for c in children if c[0] in app.filter_paths]
            if not matched:
                return
            t.item(parent_iid, open=True)
            self._insert_page(path, parent_iid, matched, 0, len(matched))
        elif path in app.open_paths:
            total = self._child_count(obj)
            if total > TREE_PAGE_SIZE:
                # 超大子集合：只实例化一页 + 分页占位（V1.1.1）
                self._insert_page(path, parent_iid,
                                  self._iter_children(path, obj), 0, total)
            else:
                for cp, _lbl in self._iter_children(path, obj):
                    self._insert_node(cp, parent_iid)
                t.item(parent_iid, open=True)
        elif self._child_count(obj):
            self._insert_placeholder(parent_iid)

    def _children_of(self, path: Path, obj: Any) -> List[Tuple[Path, str]]:
        """一次性构建全部子节点（仅过滤等本就全量的场景使用）。"""
        out: List[Tuple[Path, str]] = []
        if isinstance(obj, dict):
            for k in obj.keys():
                out.append((path + (k,), str(k)))
        elif isinstance(obj, list):
            for i in range(len(obj)):
                out.append((path + (i,), "[%d]" % i))
        return out

    def _child_count(self, obj: Any) -> int:
        """容器子项数（O(1)，不构造任何 Path）。"""
        if isinstance(obj, dict):
            return len(obj)
        if isinstance(obj, list):
            return len(obj)
        return 0

    def _iter_children(self, path: Path, obj: Any) -> Iterator[Tuple[Path, str]]:
        """流式生成子节点 (Path, label)（V1.1.1）：只生成需要的部分，
        绝不一次性构造全部 Path——打开 5 万子项只需前 2000 个 Path。"""
        if isinstance(obj, dict):
            for k in obj.keys():
                yield (path + (k,), str(k))
        elif isinstance(obj, list):
            for i in range(len(obj)):
                yield (path + (i,), "[%d]" % i)

    def _row_display(self, obj: Any, show_quotes: bool) -> str:
        """行值显示：容器显示计数 {n}/[n]（V1.1 视觉层级），字符串截断 80（hover 看完整）。"""
        if isinstance(obj, dict):
            return "{%d}" % len(obj)
        if isinstance(obj, list):
            return "[%d]" % len(obj)
        return scalar_label(obj, show_quotes, 80)

    def _update_parent_count(self, parent_path: Path) -> None:
        """局部增删后刷新父容器行的计数显示（object {n} / array [n]）。"""
        piid = path_to_iid(parent_path)
        t = self.tree
        if not t.exists(piid):
            return
        try:
            parent = self.app.model.get_by_path(parent_path)
        except (KeyError, IndexError):
            return
        if isinstance(parent, (dict, list)):
            t.item(piid, values=(type_name(parent),
                                 self._row_display(parent, self.app.show_quotes)))

    def _insert_node(self, path: Path, parent_iid: str) -> None:
        app = self.app
        try:
            obj = app.model.get_by_path(path)
        except (KeyError, IndexError):
            return
        t = self.tree
        seg = path[-1]
        label = "[%d]" % seg if isinstance(seg, int) else str(seg)
        tn = type_name(obj)
        try:
            t.insert(parent_iid, "end", iid=path_to_iid(path), text=label,
                     values=(tn, self._row_display(obj, app.show_quotes)),
                     tags=("t_" + tn,), open=False)
        except tk.TclError:
            return
        if isinstance(obj, (dict, list)):
            self._insert_children(path, path_to_iid(path))

    def _insert_placeholder(self, parent_iid: str) -> None:
        try:
            self.tree.insert(parent_iid, "end", iid=parent_iid + PH_SUFFIX,
                             text="⋯", values=("", ""))
        except tk.TclError:
            pass

    # -- 分页虚拟化（V1.1.1：超大子集合只实例化一页 + 「双击加载更多」占位） --------

    def _has_page(self, piid: str) -> bool:
        """父行下是否存在分页占位（子项被截断未完全实例化）。"""
        return any(PG_SUFFIX in c for c in self.tree.get_children(piid))

    def _insert_page(self, path: Path, parent_iid: str, children: Any,
                     start: int, total: int) -> None:
        """插入 children[start:start+TREE_PAGE_SIZE]（children 可为 list 或生成器），
        仍有剩余则补分页占位行。生成器路径不会构造整页之外的任何 Path。"""
        t = self.tree
        end = min(start + TREE_PAGE_SIZE, total)
        for cp, _lbl in itertools.islice(children, start, end):
            if not t.exists(path_to_iid(cp)):
                self._insert_node(cp, parent_iid)
        if end < total:
            self._insert_page_row(path, parent_iid, end, total)
        t.item(parent_iid, open=True)

    def _insert_page_row(self, path: Path, parent_iid: str,
                         next_start: int, total: int) -> None:
        try:
            self.tree.insert(parent_iid, "end",
                             iid=path_to_iid(path) + PG_SUFFIX + str(next_start),
                             text="⋯",
                             values=("", "还有 %d 项 · 双击加载更多" % (total - next_start)),
                             tags=("t_page",))
        except tk.TclError:
            pass

    def _load_more(self, page_iid: str) -> None:
        """双击分页占位：删除占位、插入下一页，仍超则再补占位。"""
        base, _, page = page_iid.rpartition(PG_SUFFIX)
        t = self.tree
        if not t.exists(page_iid):
            return
        path = iid_to_path(base)
        item = path_to_iid(path)
        t.delete(page_iid)
        try:
            obj = self.app.model.get_by_path(path)
        except (KeyError, IndexError):
            return
        total = self._child_count(obj)
        start = int(page)
        end = min(start + TREE_PAGE_SIZE, total)
        for cp, _lbl in itertools.islice(self._iter_children(path, obj),
                                         start, end):
            if not t.exists(path_to_iid(cp)):
                self._insert_node(cp, item)
        if end < total:
            self._insert_page_row(path, item, end, total)
        self.app.set_status("ok", "已加载 %d/%d 项" % (end, total))

    # -- 局部刷新（V1.1：单节点修改不重建整棵可见投影） ---------------------------

    def refresh_path(self, path: Path) -> None:
        """单行局部更新（改值/改名）。iid 未实例化（惰性未展开）则跳过。"""
        iid = path_to_iid(path)
        t = self.tree
        if not t.exists(iid):
            return
        app = self.app
        try:
            obj = app.model.get_by_path(path)
        except (KeyError, IndexError):
            return
        seg = path[-1]
        label = "[%d]" % seg if isinstance(seg, int) else str(seg)
        tn = type_name(obj)
        t.item(iid, text=label,
               values=(tn, self._row_display(obj, app.show_quotes)),
               tags=("t_" + tn,))

    def _refresh_parent_children(self, parent_path: Path) -> None:
        """局部重建父节点下的直接子行（Rename / list 的增删移用），
        保持父 item 自身与展开态；父未实例化则跳过。"""
        piid = path_to_iid(parent_path)
        t = self.tree
        if not t.exists(piid):
            return
        for child in t.get_children(piid):
            t.delete(child)
        self._insert_children(parent_path, piid)

    def refresh_insert(self, parent_path: Path) -> None:
        """父节点已实例化时局部插入。list 父（下标级联）→ 重建父子行；
        dict 父 → 单行追加（新键在末尾）。"""
        piid = path_to_iid(parent_path)
        t = self.tree
        if not t.exists(piid):
            return
        app = self.app
        try:
            parent = app.model.get_by_path(parent_path)
        except (KeyError, IndexError):
            return
        if isinstance(parent, list):
            self._refresh_parent_children(parent_path)
            return
        children = self._children_of(parent_path, parent)
        if not children:
            return
        if self._has_page(piid):
            self._refresh_parent_children(parent_path)  # 分页截断 → 重建父行
            return
        new_path = children[-1][0]  # dict 新增键必然在末尾
        if t.item(piid, "open"):
            self._insert_node(new_path, piid)
        elif not t.get_children(piid):
            self._insert_placeholder(piid)
        self._update_parent_count(parent_path)

    def refresh_delete(self, parent_path: Path, child_path: Path) -> None:
        """删除单行（dict 无下标级联）；list 父 → 重建父子行；占位随孩子有无增删。"""
        piid = path_to_iid(parent_path)
        t = self.tree
        if not t.exists(piid):
            return
        app = self.app
        try:
            parent = app.model.get_by_path(parent_path)
        except (KeyError, IndexError):
            return
        if isinstance(parent, list):
            self._refresh_parent_children(parent_path)
            return
        if self._has_page(piid):
            self._refresh_parent_children(parent_path)  # 分页截断 → 重建父行
            return
        iid = path_to_iid(child_path)
        if t.exists(iid):
            t.delete(iid)
        ph = piid + PH_SUFFIX
        if t.exists(ph):
            if not parent:
                t.delete(ph)  # 删空 → 移除占位
        elif parent and not t.get_children(piid):
            self._insert_placeholder(piid)  # 未展开且仍有孩子 → 补占位
        self._update_parent_count(parent_path)

    def refresh_move(self, path: Path, delta: int) -> None:
        """相邻元素移动局部刷新。两 iid 均已实例化且无子 item → 局部重插；
        否则重建父子行。选中跟随移动后的目标位置（V1.1：不丢选中/滚动）。"""
        parent = path[:-1]
        piid = path_to_iid(parent)
        t = self.tree
        if not t.exists(piid):
            return
        j = path[-1] + delta
        iid = path_to_iid(path)
        nid = path_to_iid(parent + (j,))
        if (t.exists(iid) and t.exists(nid)
                and not t.get_children(iid) and not t.get_children(nid)):
            t.delete(iid, nid)
            for k in sorted((path[-1], j)):
                self._insert_node(parent + (k,), piid)
        else:
            self._refresh_parent_children(parent)
        sel = path_to_iid(parent + (j,))
        if t.exists(sel):
            t.selection_set(sel)
            t.focus(sel)
            t.see(sel)

    # -- 惰性加载 ---------------------------------------------------------------

    def _on_open(self, _event=None) -> None:
        item = self.tree.focus()
        if not item or item.endswith(PH_SUFFIX) or PG_SUFFIX in item:
            return
        self.app.open_paths.add(iid_to_path(item))
        self._populate(item)

    def _on_close(self, _event=None) -> None:
        item = self.tree.focus()
        if item and not item.endswith(PH_SUFFIX):
            self.app.open_paths.discard(iid_to_path(item))

    def _populate(self, item: str) -> None:
        """首次展开：删除占位，按分页插入真实子节点（超大集合截断一页）。"""
        ph = item + PH_SUFFIX
        if self.tree.exists(ph):
            self.tree.delete(ph)
        path = iid_to_path(item)
        try:
            obj = self.app.model.get_by_path(path)
        except (KeyError, IndexError):
            return
        total = self._child_count(obj)
        self._insert_page(path, item, self._iter_children(path, obj), 0, total)

    # -- 就地编辑 -----------------------------------------------------------------

    def _on_double(self, event) -> str:
        item = self.tree.identify_row(event.y)
        if not item:
            return ""
        if PG_SUFFIX in item:
            self._load_more(item)
            return "break"
        col = self.tree.identify_column(event.x)
        self._start_edit(item, col)
        return "break"

    def _on_return(self, _event) -> str:
        item = self.tree.focus()
        if item and not item.endswith(PH_SUFFIX) and PG_SUFFIX not in item:
            self._start_edit(item, "#2")
        return "break"

    def _start_edit(self, item: str, col: str) -> None:
        if self._editor or item.endswith(PH_SUFFIX) or PG_SUFFIX in item:
            return
        app = self.app
        path = iid_to_path(item)
        try:
            obj = app.model.get_by_path(path)
        except (KeyError, IndexError):
            return
        if col in ("#0", ""):
            if not path:
                app.set_status("error", "根节点不可改名")
                return
            if not isinstance(path[-1], str):
                return  # 数组下标不可改名（用移动调整顺序）
            initial = self.tree.item(item, "text")
            hint = "键名"
        elif col == "#2":
            if isinstance(obj, (dict, list)):
                return
            if isinstance(obj, bool):
                app.run_command(SetValueCommand(path, obj, not obj), select_path=path)
                return
            if obj is None:
                app.set_status("error", "null 值不可编辑（可删除后新增其他类型）")
                return
            initial = obj if isinstance(obj, str) else json.dumps(obj)
            hint = "值"
        else:
            return
        bbox = self.tree.bbox(item, column=col)
        if not bbox:
            self.tree.see(item)
            self.tree.update_idletasks()
            bbox = self.tree.bbox(item, column=col)
            if not bbox:
                return
        x, y, w, h = bbox
        entry = ttk.Entry(self.tree, font=(MONO_FONT, 12))
        entry.place(x=x, y=y, width=max(w, 80), height=h)
        entry.insert(0, initial)
        entry.select_range(0, "end")
        entry.focus_set()
        entry.bind("<Return>", lambda e: self._commit_edit())
        entry.bind("<Escape>", lambda e: self._cancel_edit())
        entry.bind("<FocusOut>", lambda e: self._commit_edit())
        self._editor = entry
        self._edit_target = (item, col)
        app.set_status("info", "编辑%s，Enter 提交，Esc 取消" % hint)

    def _commit_edit(self) -> None:
        entry, target = self._editor, self._edit_target
        if not entry or not target:
            return
        self._editor = None
        self._edit_target = None
        item, col = target
        value = entry.get()
        entry.destroy()
        app = self.app
        path = iid_to_path(item)
        try:
            obj = app.model.get_by_path(path)
        except (KeyError, IndexError):
            return
        try:
            if col in ("#0", ""):
                old_key = path[-1]
                if value == old_key:
                    return
                app.run_command(RenameKeyCommand(path[:-1], old_key, value),
                                select_path=path[:-1] + (value,))
            else:
                new_val = parse_scalar_input(value, obj)
                if new_val == obj:
                    return
                app.run_command(SetValueCommand(path, obj, new_val),
                                select_path=path)
        except ValueError as e:
            app.set_status("error", str(e))

    def _cancel_edit(self) -> None:
        if self._editor:
            self._editor.destroy()
            self._editor = None
            self._edit_target = None

    def _on_resize(self, _event=None) -> None:
        if self._editor:
            self._commit_edit()

    # -- 右键菜单 ------------------------------------------------------------------

    def _build_menu(self) -> tk.Menu:
        m = tk.Menu(self, tearoff=0)
        add = tk.Menu(m, tearoff=0)
        for kind, label in (("string", "字符串"), ("number", "数字"),
                            ("boolean", "布尔"), ("null", "空值"),
                            ("object", "对象"), ("array", "数组")):
            add.add_command(label="新增 %s" % label,
                            command=lambda k=kind: self.app.on_add(k))
        m.add_cascade(label="新增", menu=add)
        m.add_command(label="删除", command=self.app.on_delete)
        m.add_separator()
        m.add_command(label="上移", command=lambda: self.app.on_move(-1))
        m.add_command(label="下移", command=lambda: self.app.on_move(1))
        m.add_separator()
        m.add_command(label="复制路径", command=self._copy_path)
        m.add_command(label="复制值", command=self._copy_value)
        m.add_command(label="复制 JSON", command=self._copy_json)
        return m

    def _sel_path(self) -> Optional[Path]:
        sel = self.tree.selection()
        if not sel:
            return None
        if PH_SUFFIX in sel[0] or PG_SUFFIX in sel[0]:
            return None
        return iid_to_path(sel[0])

    def _copy_path(self) -> None:
        path = self._sel_path()
        if path is None:
            return
        text = format_path(path)  # JSONPath 风格：$.users[3].address.city
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.set_status("ok", "已复制路径 %s" % text)

    def _copy_value(self) -> None:
        """复制原始值文本：字符串节点不带引号，其他为 JSON 字面量。"""
        path = self._sel_path()
        if path is None:
            return
        try:
            obj = self.app.model.get_by_path(path)
        except (KeyError, IndexError):
            return
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.set_status("ok", "已复制值")

    def _copy_json(self) -> None:
        """复制 JSON 字面量：字符串带引号（"Alice"），可粘贴回编辑区。"""
        path = self._sel_path()
        if path is None:
            return
        try:
            obj = self.app.model.get_by_path(path)
        except (KeyError, IndexError):
            return
        text = json.dumps(obj, ensure_ascii=False)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.set_status("ok", "已复制 JSON")

    def _on_menu(self, event) -> str:
        item = self.tree.identify_row(event.y)
        if item and not item.endswith(PH_SUFFIX) and PG_SUFFIX not in item:
            self.tree.selection_set(item)
            self.tree.focus(item)
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()
        return "break"

    def _on_motion(self, event) -> Optional[str]:
        """V1.1 Tooltip：hover 显示被截断的完整值（节流：命中不变不重建；大文档禁用）。"""
        if (self.app._node_count or 0) > 200_000:
            return None
        item = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if (item, col) == (self._tip_item, self._tip_col):
            if self._tip is not None:
                self._tip.geometry("+%d+%d" % (event.x_root + 14,
                                               event.y_root + 16))
            return None
        self._hide_tooltip()
        self._tip_item, self._tip_col = item, col
        if not item or item.endswith(PH_SUFFIX) or col != "#3":
            return None
        path = iid_to_path(item)
        if path is None:
            return None
        try:
            obj = self.app.model.get_by_path(path)
        except (KeyError, IndexError):
            return None
        if isinstance(obj, (dict, list)):
            return None  # 容器显示计数，无需 tooltip
        shown = self.tree.item(item, "values")
        if not shown or not str(shown[1]).endswith("…"):
            return None  # 未截断不显示
        full = scalar_label(obj, self.app.show_quotes, 10 ** 9)
        th = THEMES[self.app.theme_name]
        tip = tk.Toplevel(self)
        tip.withdraw()
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=full, background=th["field"], foreground=th["fg"],
                 relief="solid", bd=1, padx=7, pady=4, font=(UI_FONT, 11),
                 wraplength=520).pack()
        tip.deiconify()
        tip.geometry("+%d+%d" % (event.x_root + 14, event.y_root + 16))
        self._tip = tip
        return None

    def _hide_tooltip(self) -> None:
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None
        self._tip_item = None
        self._tip_col = None

    def apply_theme(self, th: dict) -> None:
        style = ttk.Style(self)
        style.configure("Treeview", background=th["panel"], foreground=th["fg"],
                        fieldbackground=th["panel"], rowheight=24,
                        font=(UI_FONT, 12))
        style.configure("Treeview.Heading", background=th["bg"],
                        foreground=th["muted"], font=(UI_FONT, 11, "bold"))
        style.map("Treeview",
                  background=[("selected", th["select_bg"])],
                  foreground=[("selected", th["fg"])])
        # V1.1：类型语义色（仅列前景色，轻微提示，不整行染色）
        for tn, color in (("object", th["muted"]), ("array", th["muted"]),
                          ("string", th["string"]), ("number", th["number"]),
                          ("integer", th["number"]), ("boolean", th["boolean"]),
                          ("null", th["null"])):
            try:
                self.tree.tag_configure("t_" + tn, foreground=color)
            except tk.TclError:
                pass
        try:
            self.tree.tag_configure("t_page", foreground=th["muted"])
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# SearchPanel —— 查找（计数/上一个/下一个）/ 过滤 / 替换
# ---------------------------------------------------------------------------

_SKIP = object()  # 替换类型转换失败标记


class SearchPanel(ttk.Frame):
    def __init__(self, master: "App"):
        super().__init__(master)
        self.app = master
        self.matches: List[Tuple[Path, str, Any]] = []  # (path, label, value)
        self.cur = -1
        self._job: Optional[str] = None

        pad = dict(padx=4, pady=2)
        row1 = ttk.Frame(self)
        row1.pack(fill="x", **pad)
        ttk.Label(row1, text="查找:").pack(side="left")
        self.query = tk.StringVar()
        ent = ttk.Entry(row1, textvariable=self.query, width=36)
        ent.pack(side="left", padx=4)
        ent.bind("<Return>", lambda e: self.goto(1))
        ent.bind("<Shift-Return>", lambda e: self.goto(-1))
        ent.bind("<KeyRelease>", self._on_query_changed)
        ttk.Button(row1, text="↑", width=3,
                   command=lambda: self.goto(-1)).pack(side="left")
        ttk.Button(row1, text="↓", width=3,
                   command=lambda: self.goto(1)).pack(side="left")
        self.count_label = ttk.Label(row1, text="0/0", width=10)
        self.count_label.pack(side="left", padx=6)
        ttk.Button(row1, text="✕", width=3,
                   command=self.app.hide_search).pack(side="right")

        row2 = ttk.Frame(self)
        row2.pack(fill="x", **pad)
        self.case_var = tk.BooleanVar(value=False)
        self.word_var = tk.BooleanVar(value=False)
        self.regex_var = tk.BooleanVar(value=False)
        self.filter_var = tk.BooleanVar(value=False)
        self.scope_var = tk.StringVar(value="两者")
        for text, var in (("大小写敏感", self.case_var), ("全词", self.word_var),
                          ("正则", self.regex_var), ("过滤模式", self.filter_var)):
            ttk.Checkbutton(row2, text=text, variable=var,
                            command=self._on_option_changed).pack(side="left", padx=3)
        ttk.Label(row2, text="范围:").pack(side="left", padx=(10, 2))
        cb_scope = ttk.Combobox(row2, textvariable=self.scope_var, width=8,
                                values=["两者", "仅键名", "仅值"], state="readonly")
        cb_scope.pack(side="left")
        cb_scope.bind("<<ComboboxSelected>>", lambda e: self._on_option_changed())

        row3 = ttk.Frame(self)
        row3.pack(fill="x", **pad)
        ttk.Label(row3, text="替换:").pack(side="left")
        self.replace_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.replace_var, width=36).pack(side="left", padx=4)
        ttk.Button(row3, text="替换当前", command=self.replace_current).pack(side="left")
        ttk.Button(row3, text="全部替换", command=self.replace_all).pack(side="left", padx=4)
        ttk.Label(row3, text="全部替换为一步撤销；键名命中不参与替换",
                  font=(UI_FONT, 10)).pack(side="left", padx=6)

        self.entry = ent

    # -- 匹配 -----------------------------------------------------------------

    def _pattern(self) -> Optional[re.Pattern]:
        q = self.query.get()
        if not q:
            return None
        flags = 0 if self.case_var.get() else re.IGNORECASE
        try:
            if self.regex_var.get():
                pat = r"\b(?:%s)\b" % q if self.word_var.get() else q
            else:
                pat = re.escape(q)
                if self.word_var.get():
                    pat = r"\b%s\b" % pat
            return re.compile(pat, flags)
        except re.error as e:
            self.app.set_status("error", "正则错误: %s" % e)
            return None

    def _update_matches(self) -> None:
        self._job = None
        pat = self._pattern()
        self.matches = []
        self.cur = -1
        if pat:
            scope = self.scope_var.get()
            search_key = scope in ("两者", "仅键名")
            search_val = scope in ("两者", "仅值")
            for path, label, tname, value in self.app.model.iter_nodes():
                if search_key and path and pat.search(label):
                    self.matches.append((path, label, value))
                elif search_val and tname not in ("object", "array"):
                    if isinstance(value, str):
                        vtext = value
                    elif value is None:
                        vtext = "null"
                    elif value is True:
                        vtext = "true"
                    elif value is False:
                        vtext = "false"
                    else:
                        vtext = json.dumps(value)
                    if pat.search(vtext):
                        self.matches.append((path, label, value))
        self._update_count()
        self.apply_filter()

    def _update_count(self) -> None:
        if not self.query.get():
            self.count_label.config(text="0/0")
        elif self.matches:
            pos = (self.cur + 1) if self.cur >= 0 else 0
            self.count_label.config(text="%d/%d" % (pos, len(self.matches)))
        else:
            self.count_label.config(text="无匹配")

    def _on_query_changed(self, _event=None) -> None:
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(250, self._update_matches)

    def _on_option_changed(self) -> None:
        self._update_matches()

    # -- 导航 / 过滤 / 替换 --------------------------------------------------------

    def goto(self, delta: int) -> None:
        if not self.matches:
            return
        self.cur = (self.cur + delta) % len(self.matches)
        self._update_count()
        self.app.locate_path(self.matches[self.cur][0])

    def apply_filter(self) -> None:
        if self.filter_var.get() and self.query.get():
            allowed = {()}
            for path, _l, _v in self.matches:
                for i in range(1, len(path) + 1):
                    allowed.add(path[:i])
            self.app.set_filter(allowed)
        elif not self.filter_var.get() and self.app.filter_paths is not None:
            self.app.set_filter(None)

    def _convert(self, old: Any, text: str) -> Any:
        """替换文本按原值类型转换，失败返回 _SKIP（跳过该项）。"""
        if isinstance(old, bool):
            low = text.strip().lower()
            return (low == "true") if low in ("true", "false") else _SKIP
        if isinstance(old, int):
            try:
                return int(text, 10)
            except ValueError:
                return _SKIP
        if isinstance(old, float):
            try:
                v = float(text)
                return _SKIP if v != v or v in (float("inf"), float("-inf")) else v
            except ValueError:
                return _SKIP
        if old is None:
            return _SKIP
        return text

    def replace_current(self) -> None:
        if not self.matches:
            self.app.set_status("info", "没有匹配项")
            return
        if self.cur < 0:
            self.cur = 0   # 首次点击：定位第一个匹配并直接替换（P1-7）
            self._update_count()
            self.app.locate_path(self.matches[self.cur][0])
        self._do_replace([self.matches[self.cur]])

    def replace_all(self) -> None:
        if not self.matches:
            self.app.set_status("info", "没有匹配项")
            return
        self._do_replace(list(self.matches))

    def _do_replace(self, targets: List[Tuple[Path, str, Any]]) -> None:
        repl = self.replace_var.get()
        pat = self._pattern()
        if pat is None:
            self.app.set_status("error", "请输入查找内容")
            return
        pairs: List[Tuple[Path, Any, Any]] = []
        skipped = 0
        q = self.query.get()
        for path, _label, old in targets:
            if isinstance(old, (dict, list)):
                continue  # 键名命中：不参与替换
            if isinstance(old, str):
                src = old
            elif old is None:
                src = "null"
            elif old is True:
                src = "true"
            elif old is False:
                src = "false"
            else:
                src = json.dumps(old)
            try:
                if self.regex_var.get():
                    # 正则模式：替换串原样传入，支持 \1 \2 捕获组反向引用
                    new_text = pat.sub(repl, src)
                else:
                    flags = 0 if self.case_var.get() else re.IGNORECASE
                    new_text = re.sub(re.escape(q), repl.replace("\\", "\\\\"), src,
                                      flags=flags)
            except re.error:
                return
            new_val = self._convert(old, new_text)
            if new_val is _SKIP:
                skipped += 1
                continue
            if new_val != old:
                pairs.append((path, old, new_val))
        if not pairs:
            self.app.set_status("info", "没有可替换的值%s"
                                % ("（%d 项类型转换失败已跳过）" % skipped if skipped else ""))
            return
        if self.app.run_command(ReplaceAllCommand(pairs)):
            self.app.set_status("ok", "已替换 %d 处%s"
                                % (len(pairs),
                                   ("（%d 项类型不匹配跳过）" % skipped) if skipped else ""))

    def refresh(self) -> None:
        """模型变更后重算（保持过滤/计数一致）。"""
        if self.winfo_ismapped():
            self._update_matches()


# ---------------------------------------------------------------------------
# App —— 主窗口（接线顺序遵循 design-log §10 的 13 步）
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("%s %s" % (APP_NAME, APP_VERSION))
        self.geometry("1200x760")
        self.minsize(860, 560)

        # 状态变量（design-log §6.3-A：model / draft / view 三者分离）
        self.model = JsonModel({})
        self.history = History()
        self.open_paths: set = {()}
        self.filter_paths: Optional[set] = None
        self.show_quotes = True
        self.pretty = True                      # 视图格式
        self.indent: Any = 4
        self.theme_name = "dark"
        self.draft_valid = True                 # 文本区当前内容能否解析
        self.view_dirty = False                 # 显示格式是否不同于磁盘
        self.had_comments = False               # 原文件含注释（保存前需确认）
        self._node_count = 0                    # 打开时缓存（Q6）
        self._saved_format: Tuple[bool, Any] = (True, 4)  # 磁盘上的格式
        self._from_tree = False
        self._text_job: Optional[str] = None
        self._pending_parse = False             # >8MB：暂停实时解析，待显式时机 commit
        self._text_txn_open = False             # Q2：文本事务开关
        self._last_synced_text: Optional[str] = None
        self._text_rebuild_job: Optional[str] = None  # V1.1.1：大文件 Text 重建防抖
        self._pending_text: Optional[str] = None      # 待落盘的 Text 内容（>1MB 合并）
        self._text_stale = False                      # 大文件：Text 已过期待重建
        self._big_text_rebuild = False                # 当前文档是否启用 Text 重建防抖
        self._text_dirty = False                      # 用户是否编辑过文本（免读 6MB 全文比较）
        self._saved_stat: Optional[Tuple[float, int]] = None
        self._status_job: Optional[str] = None

        self._build_menu()
        self._build_toolbar()
        self.search_panel = SearchPanel(self)
        self.paned = ttk.PanedWindow(self, orient="horizontal")
        self.tree_pane = TreePane(self)
        self.text_pane = TextPane(self)
        self.paned.add(self.tree_pane, weight=1)
        self.paned.add(self.text_pane, weight=1)
        self._build_statusbar()

        self.toolbar.pack(side="top", fill="x")
        self.paned.pack(side="top", fill="both", expand=True)
        self.statusbar.pack(side="bottom", fill="x")
        self.apply_theme()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self._refresh_text_now()
        self.update_status()

    # -- 界面构建 ---------------------------------------------------------------

    def _build_menu(self) -> None:
        mb = tk.Menu(self)
        mod = "Cmd" if IS_MAC else "Ctrl"
        m_file = tk.Menu(mb, tearoff=0)
        m_file.add_command(label="打开…", accelerator="%s+O" % mod,
                           command=self.open_file_dialog)
        m_file.add_command(label="保存", accelerator="%s+S" % mod,
                           command=self.save_file)
        m_file.add_command(label="另存为…", accelerator="Shift+%s+S" % mod,
                           command=lambda: self.save_file(save_as=True))
        m_file.add_separator()
        m_file.add_command(label="退出", command=self._on_close_request)
        mb.add_cascade(label="文件", menu=m_file)

        m_edit = tk.Menu(mb, tearoff=0)
        m_edit.add_command(label="撤销", accelerator="%s+Z" % mod, command=self.do_undo)
        m_edit.add_command(label="重做",
                           accelerator=("Shift+%s+Z" % mod) if IS_MAC else "%s+Y" % mod,
                           command=self.do_redo)
        m_edit.add_separator()
        m_edit.add_command(label="查找 / 过滤 / 替换", accelerator="%s+F" % mod,
                           command=self.show_search)
        mb.add_cascade(label="编辑", menu=m_edit)

        m_view = tk.Menu(mb, tearoff=0)
        m_view.add_command(label="格式化（美化）", accelerator="Shift+%s+F" % mod,
                           command=lambda: self.do_format(True))
        m_view.add_command(label="压缩", accelerator="Shift+%s+M" % mod,
                           command=lambda: self.do_format(False))
        m_view.add_separator()
        m_view.add_command(label="全部展开", accelerator="Shift+%s+E" % mod,
                           command=self.expand_all)
        m_view.add_command(label="全部收缩", accelerator="Shift+%s+K" % mod,
                           command=self.collapse_all)
        m_view.add_separator()
        self._quotes_var = tk.BooleanVar(value=True)
        m_view.add_checkbutton(label="显示字符串引号（仅视图，不影响数据）",
                               variable=self._quotes_var,
                               accelerator="Shift+%s+Q" % mod,
                               command=self.toggle_quotes)
        m_indent = tk.Menu(m_view, tearoff=0)
        self._indent_var = tk.StringVar(value="4")
        for v, label in (("2", "2 空格"), ("4", "4 空格"),
                         ("8", "8 空格"), ("tab", "Tab")):
            m_indent.add_radiobutton(label=label, variable=self._indent_var,
                                     value=v, command=self._on_indent_changed)
        m_view.add_cascade(label="缩进", menu=m_indent)
        m_theme = tk.Menu(m_view, tearoff=0)
        self._theme_var = tk.StringVar(value="dark")
        m_theme.add_radiobutton(label="深色", variable=self._theme_var,
                                value="dark", command=self.apply_theme)
        m_theme.add_radiobutton(label="浅色", variable=self._theme_var,
                                value="light", command=self.apply_theme)
        m_view.add_cascade(label="主题", menu=m_theme)
        mb.add_cascade(label="视图", menu=m_view)

        m_help = tk.Menu(mb, tearoff=0)
        m_help.add_command(label="快捷键说明", command=self._show_help)
        m_help.add_separator()
        m_help.add_command(label="关于 %s" % APP_NAME, command=self._show_about)
        mb.add_cascade(label="帮助", menu=m_help)
        self.config(menu=mb)

    def _build_toolbar(self) -> None:
        """V1.1：工具栏按组分区（文件 | 撤销重做 | 格式化▼ | 展开▼ | 查找）。"""
        tb = ttk.Frame(self, padding=(6, 4))
        self.toolbar = tb

        def sep():
            ttk.Separator(tb, orient="vertical").pack(side="left", fill="y", padx=7)

        for text, cmd in (("打开", self.open_file_dialog), ("保存", self.save_file)):
            ttk.Button(tb, text=text, command=cmd,
                       padding=(8, 2)).pack(side="left", padx=2)
        sep()
        for text, cmd in (("撤销", self.do_undo), ("重做", self.do_redo)):
            ttk.Button(tb, text=text, command=cmd,
                       padding=(8, 2)).pack(side="left", padx=2)
        sep()
        fmt = ttk.Menubutton(tb, text="格式化 ▾")
        fmt_menu = tk.Menu(fmt, tearoff=0)
        fmt_menu.add_command(label="美化", command=lambda: self.do_format(True))
        fmt_menu.add_command(label="压缩", command=lambda: self.do_format(False))
        fmt.configure(menu=fmt_menu)
        fmt.pack(side="left", padx=2)
        exp = ttk.Menubutton(tb, text="展开 ▾")
        exp_menu = tk.Menu(exp, tearoff=0)
        exp_menu.add_command(label="全部展开", command=self.expand_all)
        exp_menu.add_command(label="全部收缩", command=self.collapse_all)
        exp_menu.add_separator()
        exp_menu.add_checkbutton(label="显示字符串引号",
                                 variable=self._quotes_var,
                                 command=self.toggle_quotes)
        exp.configure(menu=exp_menu)
        exp.pack(side="left", padx=2)
        sep()
        ttk.Button(tb, text="查找", command=self.show_search,
                   padding=(8, 2)).pack(side="left", padx=2)

    def _build_statusbar(self) -> None:
        self.statusbar = ttk.Frame(self, padding=(8, 3))
        self.st_left = ttk.Label(self.statusbar, text="未命名", anchor="w")
        self.st_pos = ttk.Label(self.statusbar, text="", anchor="w")  # Ln/Col
        self.st_mid = ttk.Label(self.statusbar, text="", anchor="w")
        self.st_right = ttk.Label(self.statusbar, text="", anchor="e")
        self.st_left.pack(side="left", fill="x", expand=True)
        self.st_pos.pack(side="left", padx=12)
        self.st_mid.pack(side="left", padx=12)
        self.st_right.pack(side="right")

    # -- 快捷键（双套绑定：Ctrl 与 Cmd 同时生效） ---------------------------------

    def _bind_keys(self) -> None:
        mod = "Command" if IS_MAC else "Control"

        def bind(seqs: List[str], fn) -> None:
            for s in seqs:
                self.bind_all(s, fn)

        bind(["<%s-s>" % mod], lambda e: (self.save_file(), "break")[1])
        bind(["<%s-o>" % mod], lambda e: (self.open_file_dialog(), "break")[1])
        bind(["<%s-S>" % mod], lambda e: (self.save_file(save_as=True), "break")[1])
        bind(["<%s-z>" % mod], self._key_undo)
        bind(["<%s-y>" % mod], self._key_redo)
        if IS_MAC:
            bind(["<%s-Z>" % mod], self._key_redo)  # Shift+Cmd+Z
        bind(["<%s-f>" % mod], lambda e: (self.show_search(), "break")[1])
        bind(["<%s-F>" % mod], lambda e: (self.do_format(True), "break")[1])
        bind(["<%s-M>" % mod], lambda e: (self.do_format(False), "break")[1])
        bind(["<%s-E>" % mod], lambda e: (self.expand_all(), "break")[1])
        bind(["<%s-K>" % mod], lambda e: (self.collapse_all(), "break")[1])
        bind(["<%s-Q>" % mod], lambda e: (self.toggle_quotes(), "break")[1])
        bind(["<%s-Return>" % mod],  # Ctrl/Cmd+Enter：大文件下立即解析
             lambda e: (self._flush_text_parse(), "break")[1])
        bind(["<Escape>"], self._key_escape)

        # 树导航键必须绑定在 Treeview 自身（widget bindtag 先于 class 执行）并
        # return "break"：否则 aqua 的 class binding `<Up>/<Down>` → Keynav 会先
        # 移动选中项，导致随后执行的 handler 移动"新的选中行"（表现为移动错行）。
        t = self.tree_pane.tree

        def bind_tree(seqs: List[str], action) -> None:
            def handler(_e, a=action):
                a()
                return "break"
            for s in seqs:
                t.bind(s, handler)

        bind_tree(["<Control-Left>", "<%s-Left>" % mod], self.tree_collapse)
        bind_tree(["<Control-Right>", "<%s-Right>" % mod], self.tree_expand)
        bind_tree(["<Control-Up>", "<%s-Up>" % mod], lambda: self.on_move(-1))
        bind_tree(["<Control-Down>", "<%s-Down>" % mod], lambda: self.on_move(1))
        bind_tree(["<F2>"], lambda: self._edit_focused("#2"))
        bind_tree(["<Delete>", "<BackSpace>"], self.on_delete)

    def _key_undo(self, _e) -> Optional[str]:
        if isinstance(self.focus_get(), tk.Entry):
            return None
        self.do_undo()
        return "break"

    def _key_redo(self, _e) -> Optional[str]:
        if isinstance(self.focus_get(), tk.Entry):
            return None
        self.do_redo()
        return "break"

    def _key_escape(self, _e) -> Optional[str]:
        if self.search_panel.winfo_ismapped():
            self.hide_search()
            return "break"
        return None

    def _edit_focused(self, col: str) -> None:
        item = self.tree_pane.tree.focus()
        if item:
            self.tree_pane._start_edit(item, col)

    # -- 搜索面板显隐 -------------------------------------------------------------

    def show_search(self) -> None:
        # P0：搜索基于权威 Model，先 flush pending 草稿保证结果与所见一致
        # （非法 Draft 不拦截——搜索照常基于最后有效数据）
        self._flush_text_parse()
        self.search_panel.pack(side="top", fill="x", before=self.paned)
        self.search_panel.entry.focus_set()
        self.search_panel.entry.select_range(0, "end")

    def hide_search(self) -> None:
        self.search_panel.pack_forget()
        if self.filter_paths is not None:
            self.search_panel.filter_var.set(False)
            self.set_filter(None)
        self.tree_pane.tree.focus_set()

    # -- 非法 Draft 防护（硬规则：编辑前必须处理 Draft） ----------------------------

    def _ensure_draft_ok(self) -> bool:
        if self.draft_valid:
            return True
        ans = messagebox.askyesno(
            APP_NAME,
            "文本区当前存在无法解析的 JSON，未应用的文本修改尚未写入数据模型。\n\n"
            "「是」= 放弃文本区修改，继续当前操作\n「否」= 返回文本区先修复")
        if ans:
            self._refresh_text_now()  # 文本恢复为模型序列化
            self.set_status("info", "已放弃文本区未应用的修改")
        else:
            self.text_pane.text.focus_set()
        return ans

    # -- 命令执行与视图刷新 ----------------------------------------------------------

    def run_command(self, cmd: Command, select_path: Optional[Path] = None) -> bool:
        """树/搜索替换编辑的唯一入口（保证所有修改经 Command → History）。
        rebuild_text=False：连击编辑不强制落盘大 Text，由 500ms 防抖合并落盘一次。"""
        if not self._flush_or_guard_draft(rebuild_text=False):
            return False
        try:
            cmd.do(self.model)
        except (ValueError, HistoryError) as e:
            self.set_status("error", str(e))
            return False
        self.history.push(cmd)
        self._close_text_txn()  # 树命令断开文本事务（Q2）
        self.after_model_change(select_path, cmd=cmd, direction="do")
        return True

    def after_model_change(self, select_path: Optional[Path] = None,
                           cmd: Optional[Command] = None,
                           direction: str = "do",
                           text_urgent: bool = False) -> None:
        """模型变更后同步视图。

        提供 cmd 且非过滤模式 → 局部刷新（只更新受影响的行）；
        否则（文档级/过滤/批量/未知命令）全量 rebuild_tree（保守兜底）。
        text_urgent=True：立即落盘 Text（打开/新建等首次填充，不防抖）。
        """
        if cmd is not None and self.filter_paths is None \
                and self._apply_local(cmd, direction, select_path):
            pass
        else:
            self.rebuild_tree(select_path)
        self._refresh_text_now(text_urgent)
        self.update_status()
        self.search_panel.refresh()

    def _apply_local(self, cmd: Command, direction: str,
                     select_path: Optional[Path]) -> bool:
        """局部刷新分发。返回 False 表示需回退全量 rebuild_tree。"""
        kind = cmd.affected_paths(direction)
        tp = self.tree_pane
        k = kind[0]
        if k == "update":
            tp.refresh_path(kind[1])
        elif k == "update_multi":
            for p in kind[1]:
                tp.refresh_path(p)
        elif k == "rename":
            tp._refresh_parent_children(kind[1])
        elif k == "insert":
            tp.refresh_insert(kind[1])
        elif k == "delete":
            tp.refresh_delete(kind[1][0], kind[1][1])
        elif k == "move":
            tp.refresh_move(kind[1], kind[2])
        else:
            return False
        self._reselect(select_path)
        return True

    def _reselect(self, path: Optional[Path]) -> None:
        if path is None:
            return
        t = self.tree_pane.tree
        iid = path_to_iid(path)
        if t.exists(iid):
            t.selection_set(iid)
            t.focus(iid)
            t.see(iid)

    def rebuild_tree(self, select_path: Optional[Path] = None) -> None:
        if select_path is None:
            sel = self.tree_pane.tree.selection()
            select_path = iid_to_path(sel[0]) if sel else None
        self.tree_pane.rebuild()
        if select_path:
            t = self.tree_pane.tree
            iid = path_to_iid(select_path)
            if t.exists(iid):
                t.selection_set(iid)
                t.focus(iid)
                t.see(iid)

    def _refresh_text_now(self, urgent: bool = False) -> None:
        """模型 → 文本视图（按当前 view_format 渲染；Undo/Redo 后同样遵循）。
        V1.1.1 分档：小文件（<=TEXT_REBUILD_LIMIT）立即同步；大文件仅标记 stale，
        由 500ms 防抖合并「序列化 + 落盘」一次——连续树编辑不再每次重建 6MB 文档。
        urgent=True（打开/新建/保存后）：立即落盘，不防抖。"""
        self._from_tree = True
        try:
            if urgent or not self._big_text_rebuild:
                text = self.model.to_text(self.pretty, self.indent)
                self._schedule_text_set(text, urgent)
            else:
                self._mark_text_stale()
        finally:
            self._from_tree = False

    def _mark_text_stale(self) -> None:
        """大文件：树编辑后仅标记 Text 过期，500ms 防抖内合并为一次重建。"""
        if self._text_rebuild_job:
            self.after_cancel(self._text_rebuild_job)
            self._text_rebuild_job = None
        self._pending_text = None
        self._text_stale = True
        self._text_rebuild_job = self.after(TEXT_REBUILD_DELAY,
                                            self._flush_text_rebuild)

    def _schedule_text_set(self, text: str, urgent: bool = False) -> None:
        """按大小分档落盘 Text：<=TEXT_REBUILD_LIMIT 立即；更大则 500ms 防抖合并。"""
        if self._text_rebuild_job:
            self.after_cancel(self._text_rebuild_job)
            self._text_rebuild_job = None
        self._pending_text = text
        self._text_stale = False
        if urgent or len(text) <= TEXT_REBUILD_LIMIT:
            self._flush_text_rebuild()
            return
        self._text_rebuild_job = self.after(TEXT_REBUILD_DELAY,
                                            self._flush_text_rebuild)

    def _flush_text_rebuild(self) -> None:
        """把待重建 Text 真正写入，并同步 _last_synced_text / draft_valid。
        大文件 stale 路径在落盘时才序列化（连击编辑只序列化一次）。
        用户切到文本区（FocusIn）或任何即将使用 Model/Text 的时机前调用。"""
        if self._text_rebuild_job:
            self.after_cancel(self._text_rebuild_job)
            self._text_rebuild_job = None
        text = self._pending_text
        if text is None:
            if not self._text_stale:
                return
            text = self.model.to_text(self.pretty, self.indent)
        self._pending_text = None
        self._text_stale = False
        self._text_dirty = False
        self.text_pane.set_text(text)
        self._last_synced_text = text
        self.draft_valid = True
        self.text_pane.clear_error()
        if len(text) > TEXT_WARN_LIMIT:
            self.set_status("warn", "文本较大（%.1f MB），文本视图可能较慢，建议用树视图编辑"
                            % (len(text) / 1048576))

    # -- 文本 → 模型（三状态：Draft 可暂时非法；事务 merge） ---------------------------

    def on_text_changed(self) -> None:
        """文本编辑后按文件大小分档调度解析（V1.1）：
        <2MB 实时（300ms）；2~8MB 中档（600ms）；>8MB 暂停实时解析，
        仅在保存 / Ctrl+Enter / 离开编辑区 / 打开/关闭前由 _flush_text_parse 解析。"""
        if self._from_tree:
            return
        if self._text_job:
            self.after_cancel(self._text_job)
            self._text_job = None
        # V1.1.1：用户接管文本编辑 → 丢弃待落盘的 Text 重建（以用户输入为准）
        if self._text_rebuild_job:
            self.after_cancel(self._text_rebuild_job)
            self._text_rebuild_job = None
        self._pending_text = None
        self._text_stale = False
        self._text_dirty = True
        try:
            # Text.count 在不同 Tk 版本参数顺序/返回值不一致，直接用 -chars 选项
            n = int(self.tk.call(self.text_pane.text._w, "count",
                                 "-chars", "1.0", "end-1c"))
        except tk.TclError:
            n = 0
        if n > TEXT_WARN_LIMIT:
            if not self._pending_parse:
                self._pending_parse = True
                self.set_status("info", "大文件：已暂停实时解析，保存/离开编辑区时解析")
            return
        delay = PARSE_DEBOUNCE_MID if n > PARSE_MID_LIMIT else PARSE_DEBOUNCE_SMALL
        self._text_job = self.after(delay, self._commit_text)

    def _flush_text_parse(self, rebuild_text: bool = True) -> None:
        """立即解析 pending 草稿（保存/打开/关闭前、Ctrl+Enter、离开编辑区）。
        V1.1.1：默认先落盘待重建的 Text（保证 Text 与最新 Model 一致），再处理 Draft。
        rebuild_text=False（树命令连击路径）：跳过 Text 落盘——树命令只碰 Model，
        落盘交给 500ms 防抖合并，避免每次编辑都 delete+insert 整个大文档。"""
        if rebuild_text:
            self._flush_text_rebuild()
        if self._text_job:
            self.after_cancel(self._text_job)
            self._text_job = None
        if self._pending_parse:
            self._pending_parse = False
        self._commit_text()

    def _close_text_txn(self) -> None:
        self._text_txn_open = False

    def _push_text_cmd(self, cmd: ReplaceDocumentCommand) -> None:
        """文本命令入栈：事务开启且栈顶是文本命令 → merge；否则新入栈。"""
        top = self.history.undo_stack[-1] if self.history.undo_stack else None
        if self._text_txn_open and isinstance(top, ReplaceDocumentCommand):
            top.merge(cmd)
        else:
            self.history.push(cmd)
        self._text_txn_open = True

    def _commit_text(self) -> None:
        self._text_job = None
        if self._from_tree:
            return
        if not self._text_dirty:
            return  # V1.1.1：用户未编辑文本 → 免读大文件全文比较（Model 与 Text 已同步）
        text = self.text_pane.get_text()
        if text == self._last_synced_text:
            self._text_dirty = False
            return
        r = parse_json_text(text)
        if r.ok:
            cmd = ReplaceDocumentCommand(self.model.root, r.data)
            cmd.do(self.model)          # 模型更新（mutation_scope 由命令基类管理）
            self._push_text_cmd(cmd)    # merge 入历史
            self._last_synced_text = text
            self._text_dirty = False
            self.draft_valid = True
            self.text_pane.clear_error()
            self.rebuild_tree()
            self.update_status()
            self.search_panel.refresh()
            if r.had_comments:
                self.set_status("warn", "已剥离注释解析；保存后注释不会保留")
            elif r.dup_keys:
                self.set_status("warn", "检测到重复键 %s，保存后只保留最后一个"
                                % ", ".join(dict.fromkeys(r.dup_keys)[:5]))
        else:
            self.draft_valid = False    # Model 不动，Draft 保持非法
            self.text_pane.mark_error(r.lineno, r.colno)
            self.set_status("error", "JSON 无效（行 %d 列 %d）: %s —— 树保留最后一次有效数据"
                            % (r.lineno, r.colno, r.error))

    # -- undo / redo（硬约束②③：Save 不清历史；View 操作不进历史） --------------------

    def _flush_or_guard_draft(self, rebuild_text: bool = True) -> bool:
        """Model Boundary（P0 统一入口）：任何即将读取/修改/保存 Model 或覆盖
        Text 的操作（树命令/undo/redo/保存/打开/格式化）之前必须调用。
        ① pending 防抖 commit（含 >8MB 暂停模式）→ 立即同步 commit（合法草稿不丢）；
        ② Draft 非法 → 询问放弃/返回修复。
        rebuild_text=False：树命令连击路径——不落盘待重建的 Text（防抖合并负责）。
        返回 False 表示用户中止，调用方不得继续。"""
        self._flush_text_parse(rebuild_text)
        if not self.draft_valid:
            return self._ensure_draft_ok()
        return True

    def do_undo(self) -> None:
        if not self._flush_or_guard_draft():
            return
        sel = self._selected_path()   # 记录撤销前选中，局部刷新后恢复
        try:
            cmd = self.history.undo(self.model)
        except HistoryError as e:
            self.set_status("error", str(e))
            return
        self._close_text_txn()
        self.after_model_change(select_path=sel, cmd=cmd, direction="undo")
        self.set_status("ok", "已撤销")

    def do_redo(self) -> None:
        if not self._flush_or_guard_draft():
            return
        sel = self._selected_path()   # 记录重做前选中，局部刷新后恢复
        try:
            cmd = self.history.redo(self.model)
        except HistoryError as e:
            self.set_status("error", str(e))
            return
        self._close_text_txn()
        self.after_model_change(select_path=sel, cmd=cmd, direction="do")
        self.set_status("ok", "已重做")

    # -- 树操作 -------------------------------------------------------------------

    def _selected_path(self) -> Optional[Path]:
        """以 focus item 为准（Treeview 键盘操作围绕 focus），selection 兜底。"""
        tree = self.tree_pane.tree
        item = tree.focus()
        if not item or item.endswith(PH_SUFFIX) or PG_SUFFIX in item:
            sel = tree.selection()
            item = sel[0] if sel else None
        if not item or item.endswith(PH_SUFFIX) or PG_SUFFIX in item:
            return None
        return iid_to_path(item)

    def _add_target(self) -> Optional[Path]:
        """新增目标容器：选中容器 → 其自身；标量 → 其父容器。"""
        path = self._selected_path()
        if path is None:
            return () if isinstance(self.model.root, (dict, list)) else None
        obj = self.model.get_by_path(path)
        if isinstance(obj, (dict, list)):
            return path
        return path[:-1] or None

    _ADD_DEFAULTS = {"string": "", "number": 0, "boolean": False, "null": None,
                     "object": {}, "array": []}

    def on_add(self, kind: str) -> None:
        parent = self._add_target()
        if parent is None:
            self.set_status("error", "标量根文档无法新增子节点")
            return
        cmd = InsertCommand(parent, None, self._ADD_DEFAULTS[kind])
        if self.run_command(cmd):
            child = cmd.path()
            self.set_status("ok", "已新增 %s" % kind)
            if child:
                item = path_to_iid(child)
                col = "#0" if isinstance(child[-1], str) else "#2"
                self.tree_pane.tree.after_idle(
                    lambda: self.tree_pane._start_edit(item, col))

    def on_delete(self) -> None:
        path = self._selected_path()
        if not path:
            self.set_status("info", "请先选中要删除的节点")
            return
        if self.run_command(DeleteCommand(path), select_path=path[:-1]):
            self.set_status("ok", "已删除（可撤销）")

    def on_move(self, delta: int) -> None:
        path = self._selected_path()
        if not path:
            self.set_status("info", "请先选中要移动的节点")
            return
        # 选中跟随移动节点：数组元素移动后路径下标变化，对象键序不变（P1-6）
        target = path
        try:
            parent = self.model.get_by_path(path[:-1])
            if isinstance(parent, list):
                j = path[-1] + delta
                if 0 <= j < len(parent):
                    target = path[:-1] + (j,)
        except (KeyError, IndexError, TypeError):
            pass
        if self.run_command(MoveCommand(path, delta), select_path=target):
            self.set_status("ok", "已%s移（可撤销）" % ("上" if delta < 0 else "下"))

    def tree_collapse(self) -> None:
        t = self.tree_pane.tree
        item = t.focus()
        if not item:
            return
        path = iid_to_path(item)
        if t.get_children(item) and t.item(item, "open"):
            t.item(item, open=False)
            self.open_paths.discard(path)
        else:
            parent = path[:-1]
            piid = path_to_iid(parent)
            if t.exists(piid):
                t.selection_set(piid)
                t.focus(piid)
                t.see(piid)

    def tree_expand(self) -> None:
        t = self.tree_pane.tree
        item = t.focus()
        if not item:
            return
        path = iid_to_path(item)
        try:
            obj = self.model.get_by_path(path)
        except (KeyError, IndexError):
            return
        if isinstance(obj, (dict, list)):
            self.open_paths.add(path)
            t.item(item, open=True)
            self.tree_pane._populate(item)
        t.see(item)

    def expand_all(self) -> None:
        total = self._node_count or self.model.count_nodes()
        depth_limit: Optional[int] = None
        if total > EXPAND_CONFIRM_LIMIT:
            ans = messagebox.askyesnocancel(
                "全部展开",
                "文档共 %d 个节点，全部展开可能导致界面响应变慢。\n\n"
                "「是」= 全部展开\n「否」= 仅展开前 3 层\n「取消」= 不展开" % total)
            if ans is None:
                return
            if ans is False:
                depth_limit = 3
        containers = {()}
        for path, _l, tname, _v in self.model.iter_nodes():
            if tname in ("object", "array"):
                if depth_limit is None or len(path) <= depth_limit:
                    containers.add(path)
        self.open_paths = containers
        self.rebuild_tree(self._selected_path())
        self.set_status("ok", "已展开（%d 个节点%s）"
                        % (total, "，仅前 3 层" if depth_limit else ""))

    def collapse_all(self) -> None:
        self.open_paths = {()}
        self.rebuild_tree(self._selected_path())

    def toggle_quotes(self) -> None:
        self.show_quotes = self._quotes_var.get()
        self.rebuild_tree(self._selected_path())

    def _on_indent_changed(self) -> None:
        # P0：先处理 Draft，确认操作允许后再修改内部缩进，避免取消后状态已变
        if not self._flush_or_guard_draft():
            v = self.indent
            self._indent_var.set("tab" if v == "\t" else str(v))
            return
        v = self._indent_var.get()
        self.indent = "\t" if v == "tab" else int(v)
        self.do_format(self.pretty)

    # -- 过滤（只改显示，不改 Model） ---------------------------------------------

    def set_filter(self, allowed: Optional[set]) -> None:
        self.filter_paths = allowed
        self.rebuild_tree(self._selected_path())
        if allowed is not None:
            self.set_status("info", "过滤模式：仅显示命中节点及祖先链"
                                    "（取消勾选「过滤模式」恢复）")

    def locate_path(self, path: Path) -> None:
        """搜索定位：先重新 resolve（fail-stop，不猜相邻节点）。"""
        try:
            self.model.get_by_path(path)
        except (KeyError, IndexError):
            self.set_status("warn", "搜索结果已因数据变化失效，已重新搜索")
            self.search_panel.refresh()
            return
        if self.filter_paths is None:
            for i in range(1, len(path) + 1):
                self.open_paths.add(path[:i])
        self.rebuild_tree(select_path=path)

    # -- 格式化 / 压缩（View 操作：不进撤销历史，只影响显示与保存格式） ----------------

    def do_format(self, pretty: bool) -> None:
        if not self._flush_or_guard_draft():   # P0：pending 草稿先 commit 再重排
            return
        self.pretty = pretty
        self.view_dirty = (self.pretty, self.indent) != self._saved_format
        try:
            text = serialize_json(self.model.root, pretty, self.indent)
        except ValueError as e:
            messagebox.showerror(APP_NAME, "无法序列化: %s" % e)
            return
        self._from_tree = True
        try:
            self.text_pane.set_text(text)
            self._last_synced_text = text
            self.draft_valid = True
            self.text_pane.clear_error()
        finally:
            self._from_tree = False
        self.update_status()
        self.set_status("ok", "已%s（仅改变文本显示，不影响数据，不进入撤销历史）"
                        % ("格式化" if pretty else "压缩"))

    # -- 文件：打开 ----------------------------------------------------------------

    def open_file_dialog(self) -> None:
        path = filedialog.askopenfilename(
            title="打开 JSON 文件",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        # P0：先 flush pending 草稿（commit 后 model_dirty 自动正确），再走放弃确认
        if not self._flush_or_guard_draft():
            return
        if (self.model.model_dirty or not self.draft_valid) \
                and not self._confirm_discard():
            return
        try:
            text, enc = read_text_file(path)
        except OSError as e:
            messagebox.showerror(APP_NAME, "无法读取文件: %s" % e)
            return
        r = parse_json_text(text)
        if not r.ok:
            messagebox.showerror(APP_NAME, "JSON 解析失败\n行 %d 列 %d: %s"
                                 % (r.lineno, r.colno, r.error))
            return
        if r.dup_keys and not messagebox.askyesno(
                "重复键警告",
                "文件中存在重复键：%s\n按标准行为将只保留最后一个值，\n"
                "保存后重复键无法恢复。\n\n仍要打开吗？"
                % ", ".join(list(dict.fromkeys(r.dup_keys))[:8])):
            return
        if r.had_comments:
            messagebox.showwarning(
                "检测到注释",
                "文件包含 // 或 /* */ 注释，已剥离后解析。\n保存时注释不会被保留。")
        self.model = JsonModel(r.data)   # 取消路径不会走到这里（内存未被污染）
        self.model.file_path = path
        self.model.file_encoding = enc
        self.had_comments = r.had_comments
        self._saved_stat = file_stat(path)
        self._node_count = self.model.count_nodes()
        self._big_text_rebuild = len(text) > TEXT_REBUILD_LIMIT  # V1.1.1：大文件启用 Text 防抖
        self._saved_format = (self.pretty, self.indent)
        self.view_dirty = False
        self.history.clear()
        self.open_paths = {()}
        self.filter_paths = None
        self.search_panel.filter_var.set(False)
        self._text_txn_open = False
        self._last_synced_text = None
        self._text_dirty = False
        self.after_model_change(text_urgent=True)   # 首次填充立即落盘，不走防抖
        self.set_status("ok", "已打开 %s（%s，%d 个节点%s）"
                        % (os.path.basename(path), enc, self._node_count,
                           "，含注释" if r.had_comments else ""))

    # -- 文件：保存（外部检测 → 注释确认 → 原子写 → baseline 更新，不清 Undo） ----------

    def save_file(self, save_as: bool = False) -> bool:
        """返回 True = 保存完成/用户明确接受当前状态；False = 取消或失败。
        取消/失败必须阻止关闭与打开新文件的流程（P0-4）。"""
        if not self._flush_or_guard_draft():   # P0：pending 草稿先 commit，非法则询问
            return False
        # 未修改且格式未变且 Draft 合法 → 不重写文件（避免"打开→保存"无意义改变文件）
        if (not self.model.model_dirty and not self.view_dirty
                and self.model.file_path and not save_as
                and not self.had_comments and self.draft_valid):
            self.set_status("info", "未检测到修改，文件未重写")
            return True
        path = self.model.file_path
        if save_as or not path:
            path = filedialog.asksaveasfilename(
                title="保存 JSON 文件", defaultextension=".json",
                initialfile=os.path.basename(self.model.file_path or "untitled.json"),
                filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
            if not path:
                return False
        stat = file_stat(path)
        if stat and self._saved_stat and stat != self._saved_stat and not save_as:
            if not messagebox.askyesno(
                    "文件已被外部修改",
                    "磁盘上的文件在编辑期间被其他程序修改过。\n\n"
                    "「是」= 用当前内容覆盖磁盘文件\n「否」= 取消保存"):
                return False
        if self.had_comments and not messagebox.askyesno(
                "保存并移除注释",
                "原文件包含 // 或 /* */ 注释，注释不会写入保存的文件。\n\n"
                "「是」= 保存（注释将被移除）    「否」= 取消"):
            return False
        try:
            text = serialize_json(self.model.root, self.pretty, self.indent)
        except ValueError as e:
            messagebox.showerror(APP_NAME, "保存失败（非严格 JSON）: %s" % e)
            return False
        backup_before_save(path)
        try:
            write_atomic(path, text)
        except OSError as e:
            messagebox.showerror(APP_NAME, "保存失败: %s" % e)
            return False
        self.model.file_path = path
        self.model.set_saved_baseline()      # 硬约束②：baseline 更新，Undo 历史保留
        self._saved_stat = file_stat(path)
        self._saved_format = (self.pretty, self.indent)
        self.view_dirty = False
        self.had_comments = False
        self._close_text_txn()
        self.update_status()
        if self.draft_valid:
            self._refresh_text_now(urgent=True)  # Q3：Draft 非法时不覆盖文本区；保存后立即落盘
            self._last_synced_text = text
            self.set_status("ok", "已保存 %s（已生成 .bak 备份）" % os.path.basename(path))
        else:
            self.set_status("warn", "已保存（基于最后有效数据）· 文本区仍有未应用的修改")
        return True

    def _confirm_discard(self) -> bool:
        ans = messagebox.askyesnocancel(APP_NAME, "当前文件已修改，是否保存？")
        if ans is None:
            return False
        if ans:
            # P0-4：保存被取消/失败必须阻止关闭/打开（不能只看 model_dirty）
            return self.save_file()
        return True

    def _on_close_request(self) -> None:
        self._flush_text_parse()  # 关闭前解析 pending 草稿（含 >8MB 暂停模式）
        if (self.model.model_dirty or not self.draft_valid) \
                and not self._confirm_discard():
            return
        self.destroy()

    # -- 状态栏 / 标题 / 帮助 --------------------------------------------------------

    def _update_cursor_pos(self, _e=None) -> None:
        """V1.1：状态栏 Ln/Col（Text 光标位置，KeyRelease/ButtonRelease 时更新）。"""
        try:
            idx = self.text_pane.text.index("insert")
        except tk.TclError:
            return
        line, col = idx.split(".")
        self.st_pos.configure(text="Ln %s, Col %d" % (line, int(col) + 1))

    def set_status(self, kind: str, msg: str) -> None:
        th = THEMES[self.theme_name]
        color = {"ok": "ok", "error": "error", "warn": "warn"}.get(kind)
        self.st_right.configure(text=msg,
                                foreground=th[color] if color else th["muted"])
        if self._status_job:
            self.after_cancel(self._status_job)
        self._status_job = self.after(8000, lambda: self.st_right.configure(
            text="", foreground=th["muted"]))

    def update_status(self) -> None:
        name = os.path.basename(self.model.file_path) if self.model.file_path else "未命名"
        marks = (" *" if self.model.model_dirty else "") \
            + ("（显示格式已改）" if self.view_dirty else "")
        self.st_left.configure(text=name + marks)
        sel = self._selected_path()
        self.st_mid.configure(
            text="%s  ·  %d 节点" % (format_path(sel) if sel is not None else "—",
                                     self._node_count or self.model.count_nodes()))
        self._refresh_title()

    def _refresh_title(self) -> None:
        name = os.path.basename(self.model.file_path) if self.model.file_path else "未命名"
        self.title("%s %s — %s%s" % (APP_NAME, APP_VERSION, name,
                                     " *" if self.model.model_dirty else ""))

    def _show_about(self) -> None:
        """关于对话框：版本号与运行时信息（与 --version 同源 APP_VERSION）。"""
        tkver = self.tk.call("info", "patchlevel")
        messagebox.showinfo(
            "关于 %s" % APP_NAME,
            "%s %s\n\nJSON 语义保真编辑器：结构树 + 文本双视图，\n"
            "单节点局部刷新、大文件分档解析、零第三方依赖。\n\n"
            "Python %s · Tk %s" % (APP_NAME, APP_VERSION,
                                   sys.version.split()[0], tkver))

    def _show_help(self) -> None:
        mod = "Cmd" if IS_MAC else "Ctrl"
        messagebox.showinfo("快捷键说明", "\n".join([
            "文件",
            "  %s+O 打开    %s+S 保存    Shift+%s+S 另存为" % (mod, mod, mod),
            "",
            "编辑",
            "  %s+Z 撤销    %s+Y 重做%s" % (mod, mod,
                ("（或 Shift+%s+Z）" % mod) if IS_MAC else ""),
            "  %s+F 查找/过滤/替换面板，Esc 关闭，Enter 下一个，Shift+Enter 上一个" % mod,
            "",
            "树操作（焦点在结构树时）",
            "  %s+← 折叠节点    %s+→ 展开节点" % (mod, mod),
            "  %s+↑ 节点上移    %s+↓ 节点下移" % (mod, mod),
            "  F2/Enter 编辑    Delete 删除    右键菜单：新增六种类型",
            "",
            "视图",
            "  Shift+%s+F 格式化    Shift+%s+M 压缩" % (mod, mod),
            "  Shift+%s+E 全部展开    Shift+%s+K 全部收缩    Shift+%s+Q 引号开关" % (mod, mod, mod),
            "",
            "说明：格式化/压缩仅改变文本显示，不影响数据，不进入撤销历史；",
            "保存时按当前显示格式写盘，自动生成 .bak 备份。"]))

    # -- 主题 ---------------------------------------------------------------------

    def apply_theme(self) -> None:
        self.theme_name = self._theme_var.get() if hasattr(self, "_theme_var") else "dark"
        th = THEMES[self.theme_name]
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=th["bg"], foreground=th["fg"],
                        fieldbackground=th["field"], bordercolor=th["border"])
        style.configure("TFrame", background=th["bg"])
        style.configure("TLabel", background=th["bg"], foreground=th["fg"])
        style.configure("TButton", background=th["panel"], foreground=th["fg"])
        style.map("TButton",
                  background=[("active", th["select_bg"])],
                  foreground=[("active", th["fg"])])
        style.configure("TCheckbutton", background=th["bg"], foreground=th["fg"])
        style.configure("TEntry", fieldbackground=th["field"], foreground=th["fg"])
        style.configure("TPanedwindow", background=th["border"])
        style.configure("TSeparator", background=th["border"])
        self.configure(background=th["bg"])
        self.tree_pane.apply_theme(th)
        self.text_pane.apply_theme(th)


def _ensure_utf8_stdio() -> None:
    """Windows 控制台默认 cp1252/gbk，直接 print 中文会 UnicodeEncodeError。

    GUI 模式下 stdout 也可能被重定向到管道（默认 ANSI 代码页），故一律兜底。
    """
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if enc == "utf8":
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: List[str]) -> int:
    _ensure_utf8_stdio()
    if "--version" in argv or "-V" in argv:
        print("%s %s" % (APP_NAME, APP_VERSION))
        return 0
    if "--selftest" in argv:
        return _selftest()
    _run_gui(argv)
    return 0


def _run_gui(argv: List[str]) -> None:
    app = App()
    for a in argv:  # 命令行传入文件路径（文件关联的入口）
        if a and a not in ("--selftest", "--version", "-V") \
                and os.path.isfile(a):
            app.open_file(a)
            break
    app.mainloop()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
