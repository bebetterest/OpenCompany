# 文档索引

OpenCompany 文档按当前运行时实现分层组织，方便按模块快速定位。

## 推荐阅读顺序

1. `README.md`：环境准备、CLI/Web 使用方式、运行安全模型。
2. `docs/technical_route.md`：技术路线与演进重点。
3. `docs/architecture.md`：系统架构与运行时执行链路。
4. `docs/modules/*.md`：日常开发使用的子系统说明。
5. `docs/message_flow.md`：模型可见消息与 UI 流式事件协议细节。
6. `docs/message_stream_map.md`：按界面拆分的实时内容来源映射。

## 文档地图

### 入口与架构

- `docs/technical_route.md` / `docs/technical_route_cn.md`
- `docs/architecture.md` / `docs/architecture_cn.md`
- `docs/message_flow.md` / `docs/message_flow_cn.md`
- `docs/message_stream_map.md` / `docs/message_stream_map_cn.md`

### 模块参考（`docs/modules/`）

- `runtime_core.md`：会话生命周期、预算限制、root/worker 边界。
- `orchestration_pipeline.md`：循环引擎、上下文组装、强制总结行为。
- `tool_runtime.md`：工具注册、执行器、`tool_run` 生命周期语义，以及远程 SSH sandbox transport 行为。
- `workspace_sync.md`：`direct` / `staged` 工作区模式、`direct` 下本地/远程根目录选择、工作区分叉/合并、diff artifact、暂存 apply/undo。
- `skills.md`：skill 来源发现、项目内物化、resume 替换语义，以及 prompt / runtime 集成。
- `persistence_observability.md`：SQLite、JSONL 日志、checkpoint、诊断。
- `llm_prompts.md`：OpenRouter 流式路径与 prompt/tool 定义加载。
- `ui_surfaces.md`：Web UI 与 TUI 的 setup 流程（`direct` 下本地/远程目录切换）、会话模式选择/锁定、接口与 Tool/Steer Runs 面板。
- `testing_debugging.md`：测试矩阵、验证命令与排障流程。

每个模块文档均有同结构中文镜像（`*_cn.md`）。

## 文档维护规则

- 英文与中文镜像保持同结构与同一事实基线。
- 新增/删除文档或调整导航时，必须更新本索引。
- 优先记录 `src/opencompany` 中已存在的实现行为，避免“设计先行但未落地”的描述。
- 架构变更时，同步更新 `README.md`、`docs/architecture*.md` 与受影响模块文档。
