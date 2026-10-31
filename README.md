# AutoResearch Lab

本机运行的科研编排工作台，使用 Python 3.12、FastAPI、SQLite、LangGraph 和 React / TypeScript。通过研究统筹、架构实现、文献审查等角色，组织文献阅读、方案讨论、代码实现、实验比较和报告生成。

## 主要功能

- 研究任务与阶段编排、暂停恢复、持久化上下文和检查点。
- 文献检索、PDF 阅读、引用定位、阅读笔记和本地检索库。
- 架构文档、实现交接、独立审查与本机 Codex 集成。
- 数据集配置、实验方案、单因素参数搜索、动态 YAML 超参数和资源约束。
- 前端研究指挥台、代码与架构视图、实验比较、调用状态及费用展示。

真实模型调用与演示流程分别标识来源；演示输出不代表真实科研发现。预算、硬件限制、共识和执行授权属于运行门禁。默认价格是预算估算，须按实际供应方配置，不能视为官方报价。代码运行成功不等于科研结论成立。

## Windows 安装与启动

准备 Python 3.12、Node.js 和 npm。在仓库根目录的 CMD 中运行：

```cmd
py -3.12 -m venv .venv
copy .env.example .env.local
setup.cmd
```

在本地 `.env.local` 中填写供应方凭据，并在界面的智能体设置中核对接口、模型、角色及价格。仓库只提供空白凭据模板。

```cmd
start.cmd
```

访问 http://127.0.0.1:8000 。后端同时提供构建后的前端页面。服务默认只监听本机；同一数据目录只运行一个后端工作进程。

```cmd
start.cmd status
start.cmd stop
start.cmd --foreground
```

首次启动后新建研究任务，选择真实调用或演示模式，配置预算、资源限制和执行权限。需要本机 Codex 时，先安装并完成本机登录，再在界面选择实现目录、模型及推理配置。

## 开发

后端：

```cmd
.venv\Scripts\python.exe -m uvicorn autoresearch.api:app --host 127.0.0.1 --port 8000
```

另一个终端启动前端：

```cmd
cd frontend
npm ci
npm run dev
```

开发页面位于 http://127.0.0.1:5173 ，`/api` 请求代理到后端。执行 `npm run build` 生成 `frontend/dist/`。

可选文献处理依赖：

```cmd
.venv\Scripts\python.exe -m pip install -e ".[literature]"
```

## 目录

```text
autoresearch/      FastAPI 后端、研究编排、文献工具和实验执行控制
frontend/src/     React / TypeScript 页面和交互
frontend/public/  前端静态资源
.env.example      空白环境配置模板
pyproject.toml    Python 依赖与打包配置
setup.cmd         Windows 安装与前端构建入口
start.cmd         Windows 服务启动、状态与停止入口
AGENTS.md         项目开发约束
```

## 源码发布范围

此仓库快照包含主要应用代码、依赖配置和启动入口。测试文件、历史执行记录、研究数据库、数据集、实验产物、截图、缓存及本地凭据不随源码发布。程序运行后会在本地创建 `data/` 和 `.autoresearch/` 等目录；可用 `AUTORESEARCH_DATA_DIR` 指定其他数据目录。
