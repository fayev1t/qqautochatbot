<div align="center">

# 🌟 XiaoZou-Bot (小奏)

<p align="center">
  <em>「龙与虎」</em>
</p>

![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python)
![NoneBot](https://img.shields.io/badge/NoneBot-2.0+-red?style=flat-square)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-336791?style=flat-square&logo=postgresql)
![VLM](https://img.shields.io/badge/LLM-VLM%20native-purple?style=flat-square)

</div>

<p align="center">
  <a href="README.md">简体中文</a> | <a href="README_EN.md">English</a>
</p>

## 😚 项目简介

<table border="0" width="100%">
  <tr>
    <td valign="middle" style="border: none; vertical-align: middle;">
      本项目基于 <a href="https://github.com/NapNeko/NapCatQQ">NapCatQQ</a> 与 <a href="https://nonebot.dev/">NoneBot2</a> 构建，旨在完善一个能在 QQ 事件轴上<b>持续运转</b>、<b>自我唤醒</b>，并且能够推进<b>复杂任务</b>的机器人小奏。<br>
      小奏由 NapCat 上游实时事件与系统内部事件流共同驱动唤醒，采用 <b>Tick-Based</b> 的 <b>AgentLoop</b> 架构维持运转，并在隔离沙盒内通过受限 <b>Python DSL</b> 执行工具调用。<br>
      此外，本系统还针对<b>跨拍确认发言</b>、<b>群聊记忆</b>、<b>跨拍反思</b>等场景进行了深度特化，充分维护小奏作为群聊独立主体的交互体验。<br>
      最后，我还在持续提升小奏作为 LLM / VLM 的行为能力以及表达特点，希望小奏能成为一个能力更加丰富且持续学习的智能体，以更自然拟人的方式深度参与群聊沟通。
    </td>
    <td width="200" align="center" valign="middle" style="border: none; vertical-align: middle; text-align: center;">
      <img src="assets/imgs/xiaozou.png" alt="XiaoZou Character" width="180" style="max-width: 100%; height: auto;">
    </td>
  </tr>
</table>

## 🤓 系统设计

本系统围绕**事件驱动**、**Tick-Based 状态机**与**程序化工具调用**构建，形成完整的闭环运行机制：

- **🌊 统一事件轴与状态折叠 (Unified Event Stream & Projection)**  
  所有外部 OneBot 事件（群消息、撤回、入群申请、退群等）与系统内部事件（程序终态、背景事实、后台任务回调等）统一持久化至 PostgreSQL。通过 Envelope 投影机制将其格式化为平铺的自然中文时间线，富含语义并且大幅降低模型上下文损耗与 Token 消耗。

- **🔄 Tick-Based AgentLoop 驱动架构**  
  在事件到达或内部定时器到点时唤醒 Tick 运转。按 `事件摄入 -> 状态折叠 -> LLM 代码生成 -> 后续 Tick 指名执行 -> 领域事件/Terminal 收口` 流程闭环。Planner 响应只做一次预检；内容错误回灌为 `agent.invalid_action`，提供层的网络失败则由模型路由处理。崩溃或异常事件在启动恢复期收口为 `interrupted` / `uncertain`。

- **🐍 程序化工具调用沙盒 (Program-Shaped Tool Calling / Python DSL)**  
  摒弃传统的 JSON Function Call，Planner 在每拍直接生成受限 Python 代码（支持条件分支、循环控制与多工具协同）。代码运行在受限 Python AST 隔离沙盒中，安全管控 built-in 访问与配额。

## 🥰 当前能力

| 分类 | 核心功能 | 对应 Program API |
| --- | --- | --- |
| **🎭 拟人交互** | 跨拍确认发言 | `send_messages`（气泡发送）；写下程序的 Tick 不直接出站，后续 Tick 指名后才执行 |
| **🖼️ 多模态与表情包** | 表情包收藏 & 视觉理解 | `meme_collection`（只管理收藏）与 `send_messages` 的 meme 气泡，以及 `look_at_image`（图文理解与转录） |
| **🌐 联网与历史检索** | 实时搜索、网页提炼与历史查阅 | `websearch`（Exa/Tavily）、`webfetch`（网页提炼）与 `search_history`（历史上下文） |
| **🛡️ 自动化群务** | 群成员管理、入群审批与退群 | `get_pending_join_requests`、`respond_to_group_join_request`、`kick`、`leave_group` |
| **📌 任务与控制** | 跨拍便签与自主唤醒 | `task`（单栏 latest-wins 便签）与 `wait`（延时唤醒与回想安排） |



## 😴 待办

- [x] 基于受限 Python AST 沙盒的程序化工具调用 (Program API)
- [x] NapCat / OneBot 原生事件摄入与群务治理工具封装
- [x] 跨拍确认发言与气泡送达控制 (`send_messages`)
- [x] 单栏跨拍便签（`task`）与程序决策流水线
- [x] 可选长期记忆系统与动态上下文压缩（Memory Compaction，按开关灰度）
- [ ] 情绪状态机与主动群聊社交决策

## 🤣 群聊表现

<div align="center">
  <table border="0" style="border-collapse: collapse; margin: 20px 0;">
    <tr>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message1.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">1. 开启任务 & 发起报数</p>
      </td>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message2.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">2. 多轮插话与任务动态调整</p>
      </td>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message3.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">3. 任务结束与自主多模态回复</p>
      </td>
    </tr>
  </table>
</div>


## 😄 快速开始

把小奏（1005089717）拉到群里。


## 😭 慢速开始

```bash
# 1. 启动依赖容器 (PostgreSQL & NapCat)
docker compose -f docker/postgres/compose.yml up -d
docker compose -f docker/napcat/compose.yml up -d

# 2. 初始化配置与启动
cp .env.example .env
cp config/model_providers.example.json config/model_providers.json
pip install -r requirements.txt && python -m qqbot
```

> **📌 配置要点**：
> - **NapCat 反向 WS**：面板添加客户端连接至 `ws://<bot-host>:7500/onebot/v11/ws`。
> - **模型配置 (`config/model_providers.json`)**：填入 API Key 及 `planner`、`vision`、`caption`、`memory`、`web_digest` 角色模型。
> - **协议探针**：运行 `python -m qqbot.main_test` 独立调试 NapCat / OneBot 接口连通性。




## 🥳 交流群

任何问题，欢迎加入。
**610662657**
<div align="left">
  <img src="assets/imgs/qqgroup_info.png" width="240" />
</div>


## 🤤 难道有一天上热榜了？
<a href="https://www.star-history.com/?repos=fayev1t%2FXiaoZou-Bot&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&legend=top-left" />
 </picture>
</a>
