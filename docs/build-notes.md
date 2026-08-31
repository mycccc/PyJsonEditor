# 打包说明

V1.1 正式产物仅 macOS（arm64 + x86_64 双 zip）；Windows/Linux 仅在 CI 中做构建冒烟验证（不产出正式物，v1.2 扩展）。

## 通用

```bash
pip install pyinstaller
pyinstaller --noconfirm --clean --windowed --name "PyJsonEditor" pyjsoneditor.py
```

- 目录模式（默认，不用 `--onefile`）：启动快且不易被杀软误报
- 无数据文件依赖，无需 `--add-data`

## macOS .app（V1.1 正式产物）

1. 在对应架构的 runner 上分别构建（arm64 用 macos-latest，x86_64 用 macos-13）：
   ```bash
   pyinstaller --noconfirm --clean --windowed --name PyJsonEditor pyjsoneditor.py
   mkdir -p "dist/PyJsonEditor-macos-<arch>"
   mv "dist/PyJsonEditor.app" "dist/PyJsonEditor-macos-<arch>/"
   cd dist && zip -r "PyJsonEditor-v1.1.0-macos-<arch>.zip" "PyJsonEditor-macos-<arch>/"
   ```
   - 产物命名：`PyJsonEditor-v1.1.0-macos-arm64.zip`、`PyJsonEditor-v1.1.0-macos-x64.zip`
   - universal2 需两种架构分别构建再 `lipo` 合并；V1.1 采用双产物方案，不合并
2. 未签名产物会被 Gatekeeper 拦截：
   - 本机自用：右键 →「打开」；或 ad-hoc 签名
     `codesign --force --deep -s - "dist/PyJsonEditor.app"`
   - 对外分发：需 Apple Developer 账号签名 + 公证（notarytool）；v1.1.0 暂不执行，README 提示用户自行解除拦截
3. 命令行入口已支持 `--version` / `--selftest`，.app 内可从终端运行
   `"PyJsonEditor.app/Contents/MacOS/PyJsonEditor" --version` 验证

## Windows .exe（CI 冒烟，v1.2 正式化）

1. 高 DPI：代码已处理（`SetProcessDpiAwareness(1)`）
2. 文件关联：需安装程序写注册表（Inno Setup / NSIS）：
   `HKCU\Software\Classes\PyJsonEditor.json\shell\open\command` → `"...\PyJsonEditor.exe" "%1"`
   再把 `.json` 的 OpenWithProgids 指过去（不要直接抢注 .json）
3. 杀软误报：目录模式 + 代码签名证书可缓解

## Linux（CI 冒烟，v1.2 正式化）

- 目录模式产物直接可用；文件关联通过 `.desktop` 文件（`MimeType=application/json;`）+
  `update-desktop-database`
- 注意目标机需 `python3-tk`（打包时 PyInstaller 会带上 tkinter 依赖，一般无需目标机安装）

## 版本策略

- 语义化版本：v1.0.0 / v1.1.0 / v1.2.0（旧 `v1` 标签已删除，不再使用）
- 开发/打包环境 Python ≥ 3.10；代码不使用 Tk 9.0 特有 API
- 交付物命名：`PyJsonEditor-<ver>-<platform>-<arch>`
