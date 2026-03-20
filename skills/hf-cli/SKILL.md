# HF CLI Skill

Use this skill when a task needs Hugging Face Hub operations from the terminal.

## Scope

- Manage authentication (`hf auth ...`) for Hugging Face Hub access.
- Inspect and query models, datasets, papers, and Spaces.
- Download/upload files or repositories.
- Perform repo-level maintenance (create, move, settings, tags).

## Setup

1. Ensure the `hf` command exists:

```bash
hf version
```

2. If missing, install the CLI:

```bash
curl -LsSf https://hf.co/cli/install.sh | bash -s
```

3. Authenticate (preferred: env var, fallback: interactive login):

```bash
export HF_TOKEN="<your_token>"
hf auth whoami
```

## Common command patterns

```bash
# Discover assets
hf models list
hf datasets list
hf spaces list

# Inspect metadata
hf models info <namespace/model>
hf datasets info <namespace/dataset>

# Transfer files
hf download <repo_id>
hf upload <repo_id> <local_path>

# Manage repositories
hf repos create <repo_id>
hf repos settings <repo_id>
```

## Working rules

- Prefer `hf` over deprecated `huggingface-cli`.
- Never print tokens in logs or command output.
- Use explicit repo IDs (`namespace/name`) to avoid writing to wrong targets.
- Check command help before destructive operations:

```bash
hf <subcommand> --help
```

## Reference

- Upstream source: <https://github.com/huggingface/skills/tree/main/skills/hf-cli>
