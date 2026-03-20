# OpenAI 文档

当用户询问如何构建 OpenAI 产品或 API，并且答案需要基于最新官方资料时，使用这个 skill。

## 工作流程

1. 优先使用最新的 OpenAI 官方文档，而不是仓库内的参考笔记。
2. 需要联网检索时，只使用 OpenAI 官方域名。OpenCompany 中应把浏览/搜索限制在官方 OpenAI 站点。
3. 只有在问题与之直接相关时，才读取 `resources/references/` 下的参考笔记；其中任何易变内容都要再次对照官方文档确认。
4. 最终回答保持简洁，并给出实际使用的官方来源。

## 参考文件

- `resources/references/latest-model.md`
  用于模型选择类问题，但结论必须再对照当前官方文档确认。
- `resources/references/upgrading-to-gpt-5p4.md`
  用于 GPT-5.4 升级规划。
- `resources/references/gpt-5p4-prompting-guide.md`
  用于 prompt 改写或 GPT-5.4 提示词迁移。

## 规则

- 以 OpenAI 官方文档为准。
- 引用保持简短，优先转述并附来源。
- 如果仓库内参考笔记和当前官方文档不一致，要显式指出并以官方文档为准。
- 如果官方文档没有覆盖用户的问题，要明确说明，再给出下一步建议。
