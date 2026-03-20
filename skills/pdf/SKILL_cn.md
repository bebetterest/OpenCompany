# PDF Skill

当任务涉及 PDF 的读取、生成或审查，而且版式和渲染效果很重要时，使用这个 skill。

## 工作流程

1. 优先把 PDF 渲染成图片做目检。
2. 需要稳定版式时，优先用 `reportlab` 生成 PDF。
3. 用 `pdfplumber` 或 `pypdf` 做提取和快速检查，但不要把提取结果当成版式真相。
4. 每次有实质修改后，都重新渲染并检查留白、裁切、分页和可读性。

## 依赖

优先使用 `uv`：

```bash
uv pip install reportlab pdfplumber pypdf
```

若没有 `uv`：

```bash
python3 -m pip install reportlab pdfplumber pypdf
```

页面渲染通常需要 Poppler：

```bash
# macOS
brew install poppler

# Ubuntu/Debian
sudo apt-get install -y poppler-utils
```

## 路径约定

- 临时文件放在当前任务自己的临时目录中，例如项目下的 `tmp/`，不要写死到某个仓库专用路径。
- 最终输出写到用户指定的位置，或写到当前项目里命名清晰的路径。
- 文件名保持稳定、可读，便于多轮渲染比对。

## 渲染命令

```bash
pdftoppm -png $INPUT_PDF $OUTPUT_PREFIX
```

## 质量要求

- 保持字体、间距、页边距和层级一致。
- 避免文字裁切、重叠、乱码、模糊图片和破损表格。
- 不交付存在视觉缺陷的 PDF。
