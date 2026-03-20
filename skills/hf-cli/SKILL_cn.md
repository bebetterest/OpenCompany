# HF 命令行 Skill

当任务需要通过终端操作 Hugging Face Hub 时，使用这个 skill。

## 适用范围

- 管理认证（`hf auth ...`）。
- 查询 models、datasets、papers、Spaces 信息。
- 下载与上传仓库文件。
- 进行 repo 级维护（创建、迁移、设置、标签等）。

## 环境准备

1. 先确认 `hf` 命令可用：

```bash
hf version
```

2. 若不存在，安装 CLI：

```bash
curl -LsSf https://hf.co/cli/install.sh | bash -s
```

3. 认证（优先环境变量，其次交互登录）：

```bash
export HF_TOKEN="<your_token>"
hf auth whoami
```

## 常用命令模式

```bash
# 资产发现
hf models list
hf datasets list
hf spaces list

# 元信息查询
hf models info <namespace/model>
hf datasets info <namespace/dataset>

# 文件传输
hf download <repo_id>
hf upload <repo_id> <local_path>

# 仓库管理
hf repos create <repo_id>
hf repos settings <repo_id>
```

## 使用规则

- 优先使用 `hf`，不要再用已废弃的 `huggingface-cli`。
- 不要在日志或输出中泄露 token。
- 使用明确的 `namespace/name`，避免误操作到错误仓库。
- 涉及潜在破坏性操作前，先看帮助：

```bash
hf <subcommand> --help
```

## 参考

- 上游来源：<https://github.com/huggingface/skills/tree/main/skills/hf-cli>
