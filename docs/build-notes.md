# 打包说明（V2 阶段实施，本期仅记录）

## 通用

```bash
pip install pyinstaller
pyinstaller --noconfirm --clean --windowed --name "PyJsonEditor" pyjsoneditor.py
```

- 单文件模式（`--onefile`）启动慢且易被杀软误报，建议用目录模式（默认）。
- 无数据文件依赖，无需 `--add-data`。

## macOS .app

1. 产物 `dist/PyJsonEditor.app`；未签名会被 Gatekeeper 拦截：
   - 本机自用：右键打开，或 `codesign --force --deep -s - dist/PyJsonEditor.app`（ad-hoc）
   - 分发：需 Apple Developer 账号签名 + 公证（notarytool）
2. 关联 .json：编辑 `Info.plist` 加入 `CFBundleDocumentTypes`（LSItemContentTypes = public.json，
   CFBundleTypeRole = Editor），并实现打开事件（`sys.argv` 或 tkinter 不自动收 ODOC，
   需补 `tkinter` 的 open-file 处理或用 pyobj）
3. universal2 需在对应架构机器上分别构建再 `lipo` 合并；建议只出 arm64 + x86_64 各一份

## Windows .exe

1. 高 DPI：代码已处理（`SetProcessDpiAwareness(1)`）
2. 文件关联：需安装程序写注册表（Inno Setup / NSIS）：
   `HKCU\Software\Classes\PyJsonEditor.json\shell\open\command` → `"...\PyJsonEditor.exe" "%1"`
   再把 `.json` 的 OpenWithProgids 指过去（不要直接抢注 .json）
3. 杀软误报：目录模式 + 代码签名证书可缓解

## Linux

- 目录模式产物直接可用；文件关联通过 `.desktop` 文件（`MimeType=application/json;`）+
  `update-desktop-database`
- 注意目标机需 `python3-tk`（打包时 PyInstaller 会带上 tkinter 依赖，一般无需目标机安装）

## 版本策略

- 开发/打包环境 Python ≥ 3.10；代码不使用 Tk 9.0 特有 API
- 交付物命名：`PyJsonEditor-<ver>-<platform>-<arch>`
