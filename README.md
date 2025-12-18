
一个基于 **NoneBot2 + OneBot + LangChain** 的全功能 QQ 机器人框架。
感谢 **napcat** 和 **nonebot** 两个优秀的开源项目提供的强大基础，让我可以专注于对话逻辑、数据库和 RAG 知识库的开发。

---

## 🎯 核心功能

### ✅ 已实现功能

#### 📊 数据采集与存储
- **群组/成员信息采集** - 实时同步 QQ 群基本信息、成员列表
- **消息事件驱动** - 自动捕获聊天内容、群员加入/退出等事件
- **数据库持久化** - 群组信息、成员数据、聊天消息存储至 PostgreSQL（支持动态分表）

#### 🤖 AI 对话系统
- **个性化 AI 回复** - 完整的提示词管理和角色定制
- **自主群聊对话** - 自然融入群聊，支持自动判断是否需要回复
- **支持多个 AI 服务商** - DeepSeek、OpenAI 等灵活切换

### 📸 功能演示

![QQ Bot 演示]（郭楠群友展示）(./img/屏幕截图%202025-12-18%20165501.png)



### 📋 计划开发方向

#### 🔍 知识与联网能力
1. **RAG 知识库** - 向量数据库集成，支持私有知识库查询
2. **AI 联网搜索** - 增强实时信息获取
3. **MCP 接口调用** - 扩展机器人功能（欢迎功能建议！）
4. **多模态理解** - 图像识别和分析

#### 🛠️ 定制化功能
5. **群管理工具** - 群管理、禁言、反垃圾等功能
6. **爬虫工具集** - 数据采集能力

#### 📦 部署与发行
- 每个大版本完成 Docker 容器化并发布发行版

---

## 🏗️ 技术架构

### 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    QQ 消息入口                          │
│            (OneBot 协议 via napcat)                    │
└────────────────────┬────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        │            │            │
        ▼            ▼            ▼
   事件处理器    群聊处理器    定时任务
   (Priority 100) (Priority 50) (APScheduler)
        │            │            │
        │  ┌──────────┴──────────┐ │
        │  ▼                     │ │
        │ 消息判断器 (一层 AI)   │ │
        │  │                    │ │
        │  └──────────┬─────────┘ │
        │             ▼            │
        │  对话生成器 (二层 AI)   │
        │  (LangChain + LLM)      │
        │             │            │
        ├─ 消息存储   ├──────────┐ │
        │  (DB)       │          │ │
        │             └──────────┤ │
        └─────────────────────────┼─┘
                                  │
              ┌───────────────────┘
              ▼
         服务层 (Services)
         ├─ ConversationService    (对话生成)
         ├─ MessageJudger          (消息判断)
         ├─ GroupService           (群组管理)
         ├─ UserService            (用户管理)
         ├─ PromptManager          (提示词)
         └─ ContextManager         (上下文)
              │
              ▼
         数据库层 (PostgreSQL)
         ├─ users                  (全局用户表)
         ├─ groups                 (全局群组表)
         ├─ group_members_{id}     (动态分表)
         └─ group_messages_{id}    (动态分表)
```

### 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **框架** | NoneBot2 v2.4+ | 异步 Python Bot 框架 |
| **协议** | OneBot V11 | QQ 通信标准协议 |
| **AI 编排** | LangChain | AI 调用和编排框架 |
| **LLM 提供商** | DeepSeek / OpenAI | 大语言模型服务 |
| **数据库** | PostgreSQL + SQLAlchemy | 关系数据存储 + ORM |
| **异步驱动** | asyncpg | 异步 PostgreSQL 驱动 |
| **任务调度** | APScheduler | 定时任务管理 |

### 核心依赖

```toml
# Bot 框架
nonebot2[fastapi]>=2.4.4
nonebot-adapter-onebot>=2.4.6

# AI 能力
langchain>=0.1.0
langchain-openai>=0.0.5

# 数据库
sqlalchemy>=2.0.0
sqlalchemy[asyncio]
asyncpg>=0.29.0

# 任务调度
apscheduler>=3.10.0

# 开发工具
pyright[nodejs]
ruff
```

---

## 📂 项目结构

```
qqbot/
├── qqbot/
│   ├── __main__.py                    # 应用入口
│   │
│   ├── plugins/                       # NoneBot 插件（事件驱动）
│   │   ├── event_handlers.py          # 事件处理（消息保存、成员同步）
│   │   ├── group_chat.py              # 群聊 AI 回复处理
│   │   ├── sync_nicknames.py          # 昵称同步服务
│   │   └── startup.py                 # 启动和初始化钩子
│   │
│   ├── core/                          # 核心基础设施
│   │   ├── database.py                # PostgreSQL 异步连接管理
│   │   ├── llm.py                     # LLM 配置和工厂方法
│   │   └── scheduler.py               # APScheduler 集成
│   │
│   ├── services/                      # 业务逻辑服务层
│   │   ├── conversation.py            # 对话生成服务（二层 AI）
│   │   ├── message_judge.py           # 消息判断服务（一层 AI）
│   │   ├── group.py                   # 群组 CRUD 操作
│   │   ├── group_member.py            # 群成员 CRUD 操作
│   │   ├── group_message.py           # 群消息 CRUD 操作
│   │   ├── user.py                    # 用户 CRUD 操作
│   │   ├── context.py                 # 上下文和会话管理
│   │   ├── prompt.py                  # 提示词模板管理
│   │   ├── silence_mode.py            # 群组沉默模式控制
│   │   └── __init__.py
│   │
│   ├── models/                        # SQLAlchemy ORM 模型
│   │   ├── base.py                    # 基础模型（时间戳等）
│   │   ├── messages.py                # 用户、群组、消息等模型定义
│   │   └── __init__.py
│   │
│   ├── ai/                            # AI 相关工具和集成
│   │   └── __init__.py
│   │
│   └── utils/                         # 工具函数（可选）
│       └── __init__.py
│
├── pyproject.toml                     # 项目配置、依赖声明
├── .env                               # NoneBot2 核心配置（版本控制）PS：暂不上传至仓库
├── .env.dev                           # 开发环境覆盖（不提交）
├── .env.prod                          # 生产环境配置（不提交）
├── .gitignore                         # Git 忽略规则
├── README.md                          # 本文件
├── CLAUDE.md                          # 开发指南和规范
├── ARCHITECTURE.md                    # 详细架构说明
└── logs/                              # 日志输出目录（运行时生成）
```

---

## 🚀 快速开始

### 前置条件

- **Python** 3.9 或更高版本
- **PostgreSQL** 16 或更高版本
- **napcat** Docker 容器已部署并运行

### 环境配置

#### 1. 创建 `.env.dev` 文件

```env
# ========== NoneBot2 核心配置 ==========
ENVIRONMENT=dev
DRIVER=~fastapi
HOST=0.0.0.0
PORT=8080

# OneBot 适配器配置
ONEBOT_ACCESS_TOKEN=your_onebot_token
SUPERUSERS=["123456789"]        # 管理员 QQ 号
NICKNAME=["机器人名称"]
COMMAND_START=["/"]

# ========== LLM 配置 ==========
LLM_PROVIDER=deepseek           # deepseek 或 openai
LLM_API_KEY=sk-xxx              # API 密钥
LLM_MODEL=deepseek-chat         # 模型名称
LLM_TEMPERATURE=0.7             # 温度（0.0-1.0）

# ========== 数据库配置 ==========
DATABASE_URL=postgresql+asyncpg://username:password@localhost:5432/qqbot

# ========== 日志配置 ==========
LOG_LEVEL=DEBUG
```

#### 2. 初始化数据库

```bash
# 创建数据库
psql -U postgres -c "CREATE DATABASE qqbot;"

# 创建表（首次运行或升级时）
# 系统会在启动时自动创建表结构
```

### 安装与运行

```bash
# 1. 安装依赖（从项目根目录）
pip install -e ".[dev]"

# 2. 启动（开发模式，自动重载）
nb run --reload

# 3. 生产模式启动
nb run

# 启动成功标志：应看到日志 "Performing startup..."
```


---

## 🔧 开发工具

### 代码质量检查

```bash
# Ruff 代码检查（style + bugs）
ruff check .

# Ruff 自动格式化
ruff format .

# Pyright 类型检查
pyright

# 完整检查流程
ruff check . && ruff format . && pyright
```

### 开发规范

- **类型注解** - 所有公开函数必须有完整类型提示（强制执行）
- **异步优先** - 禁止阻塞 I/O，所有网络/数据库操作必须 `async`
- **结构化日志** - 包含用户 ID、群组 ID 等上下文信息
- **错误处理** - 永不静默失败，总是提供用户反馈
- **导入顺序** - `typing` → stdlib → 第三方 → 本地（自动排序）

---

## ⚙️ 配置详解

### `.env` - NoneBot2 核心配置

```env
ENVIRONMENT=dev|prod            # 运行环境
DRIVER=~fastapi                 # HTTP 驱动（FastAPI）
HOST=0.0.0.0                    # 监听地址
PORT=8080                       # 监听端口
ONEBOT_ACCESS_TOKEN=token       # OneBot 访问令牌
SUPERUSERS=["123"]              # 管理员 QQ
NICKNAME=["bot"]                # 机器人昵称
COMMAND_START=["/"]             # 命令前缀
```

### `.env.dev` - 开发环境（覆盖）

```env
LOG_LEVEL=DEBUG                 # 调试模式
LLM_PROVIDER=deepseek           # AI 服务商
LLM_API_KEY=sk-xxx              # API 密钥
LLM_MODEL=deepseek-chat         # 模型选择
DATABASE_URL=postgresql+asyncpg://...
```

### `.env.prod` - 生产环境

根据实际部署环境配置，**不应提交到版本控制**。

---

## 📚 工作阶段规划

### Phase 1: 基础完善 ✅
- [x] NoneBot2 + OneBot 框架集成
- [x] PostgreSQL 数据持久化
- [x] LangChain + 多 LLM 支持
- [x] 事件驱动消息采集
- [x] 两层 AI 决策系统

### Phase 2: 功能优化 🔄
- [ ] 优化数据库查询性能
- [ ] 增强 AI 对话上下文管理
- [ ] 完善错误处理和重试机制
- [ ] 扩展事件处理类型

### Phase 3: 高级特性 📋
- [ ] **RAG 知识库** - 向量数据库集成
- [ ] **AI 联网搜索** - 实时信息检索
- [ ] **MCP 接口** - 第三方功能扩展
- [ ] **多模态理解** - 图像识别能力
- [ ] **群管理工具** - 自动化管理功能
- [ ] **爬虫工具** - 数据采集

### Phase 4: 生产部署 🚀
- [ ] 性能优化和监控
- [ ] Docker 镜像打包
- [ ] 完整部署文档
- [ ] 发行版和更新机制

---

## 📦 依赖变更日志

所有依赖变化（新增/删除/更新）记录如下，用于 Docker 镜像构建和部署。

| 日期 | 依赖包 | 操作 | 说明 |
|------|--------|------|------|
| - | nonebot2 | 新增 | Bot 框架核心 |
| - | nonebot-adapter-onebot | 新增 | OneBot V11 协议适配 |
| - | langchain | 新增 | AI 编排框架 |
| - | langchain-openai | 新增 | OpenAI API 兼容层 |
| - | sqlalchemy | 新增 | ORM 框架 |
| - | sqlalchemy[asyncio] | 新增 | 异步 SQLAlchemy |
| - | asyncpg | 新增 | 异步 PostgreSQL 驱动 |
| - | apscheduler | 新增 | 任务调度器 |
| - | pydantic-settings | 新增 | 配置管理 |
| - | pyright | 新增 (dev) | 类型检查 |
---

## 📖 相关文档

- **[ARCHITECTURE.md](./ARCHITECTURE.md)** - 系统架构深度分析、数据模型设计
- **[NoneBot2 文档](https://nonebot.dev/)** - 官方框架文档
- **[LangChain 文档](https://python.langchain.com/)** - AI 编排框架文档

---

## 📄 许可证

无

---
