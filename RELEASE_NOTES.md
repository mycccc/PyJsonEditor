# PyJsonEditor v1.1.0

First stable, practically usable release of PyJsonEditor.

PyJsonEditor is a lightweight desktop JSON editor built with Python and Tkinter.
It provides both tree-based and text-based JSON editing, formatting,
compression, search/replace, undo/redo, node reordering and safe file saving
with backups.

## Highlights

- Tree + Text dual view, synchronized Model / Draft / View
- Paged virtualized TreeView (lazy loading), local refresh on single-node edits
- Format / minify, search / replace with regex & capture groups, filter
- Undo / Redo (command pattern, depth 200)
- Comment-stripping JSON parser (JSONC)
- Safe save with automatic `.bak` backups, external change detection
- Cross-platform: macOS / Windows / Linux (Python 3.10+, Tkinter)

## Known Limitations

> Large JSON documents containing many visible TreeView nodes may experience
> noticeable rendering latency.
>
> This is a known limitation of the current Tkinter/TreeView implementation.
> Performance optimization is planned for a future release.

## Assets

- `PyJsonEditor-v1.1.0-macos-arm64.zip` — macOS (Apple Silicon)
- `PyJsonEditor-v1.1.0-macos-x64.zip` — macOS (Intel)

## Running from source

```bash
python3 pyjsoneditor.py                # empty document
python3 pyjsoneditor.py config.json    # open a file
```
