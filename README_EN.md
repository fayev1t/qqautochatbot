<div align="center">

# 🌟 XiaoZou-Bot (XiaoZou)

<p align="center">
  <em>"Toradora!"</em>
</p>

![Python](https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python)
![NoneBot](https://img.shields.io/badge/NoneBot-2.0+-red?style=flat-square)
![PostgreSQL](https://img.shields.io/badge/Database-PostgreSQL-336791?style=flat-square&logo=postgresql)
![VLM](https://img.shields.io/badge/LLM-VLM%20native-purple?style=flat-square)

</div>

<p align="center">
  <a href="README.md">简体中文</a> | <a href="README_EN.md">English</a>
</p>

## 😚 Introduction

<table border="0" width="100%">
  <tr>
    <td valign="middle" style="border: none; vertical-align: middle;">
      This project is built on <a href="https://github.com/NapNeko/NapCatQQ">NapCatQQ</a> and <a href="https://nonebot.dev/">NoneBot2</a>, aiming to create XiaoZou, a bot capable of <b>continuous operation</b>, <b>self-awakening</b>, and driving <b>complex tasks</b> on the QQ event timeline.<br>
      XiaoZou is driven and awakened by upstream real-time events from NapCat as well as system internal event streams, operating on a <b>Tick-Based</b> <b>AgentLoop</b> architecture, and executing tool calls inside an isolated sandbox via a restricted <b>Python DSL</b>.<br>
      In addition, the system is deeply specialized for scenarios such as <b>cross-tick speech confirmation</b>, <b>group chat memory</b>, and <b>cross-tick reflection</b>, fully maintaining XiaoZou's interactive experience as an independent entity in group chats.<br>
      Finally, I am continuously enhancing XiaoZou's behavioral capabilities and expressive traits as an LLM / VLM, hoping XiaoZou can become an agent with richer capabilities and continuous learning, participating deeply in group chat communication in a more natural and human-like manner.
    </td>
    <td width="200" align="center" valign="middle" style="border: none; vertical-align: middle; text-align: center;">
      <img src="assets/imgs/xiaozou.png" alt="XiaoZou Character" width="180" style="max-width: 100%; height: auto;">
    </td>
  </tr>
</table>

## 🤓 System Design

This system is built around **event-driven mechanisms**, a **Tick-Based state machine**, and **programmatic tool calling**, forming a complete closed-loop execution system:

- **🌊 Unified Event Stream & Projection**  
  All external OneBot events (group messages, recalls, join requests, member leaves, etc.) and internal system events (program terminals, background facts, scheduled callbacks, etc.) are uniformly persisted to PostgreSQL. They are formatted into a flat natural Chinese timeline via the Envelope projection mechanism, significantly reducing context loss and token consumption for the model.

- **🔄 Tick-Based AgentLoop Architecture**  
  Ticks are awakened when events arrive or internal timers trigger. The loop operates through `Event Ingestion -> State Folding (Projection) -> LLM Code Generation -> Later-Tick Program Dispatch -> Domain Event/Terminal Closure`. Each successful model response is preflighted once; invalid source is returned as `agent.invalid_action`, while provider/network failures are handled by the model routing layer. Crash or exception events close as `interrupted` / `uncertain` during startup recovery.

- **🐍 Programmatic Tool Calling Sandbox (Program-Shaped Tool Calling / Python DSL)**  
  Abandoning traditional JSON Function Calling, the Planner directly generates restricted Python code on each tick (supporting conditional branching, loop control, and multi-tool orchestration). The code runs inside an isolated, restricted Python AST sandbox with security controls on built-in access and quotas.

## 🥰 Current Capabilities

| Category | Core Feature | Corresponding Program API |
| --- | --- | --- |
| **🎭 Human-like Interaction** | Cross-tick speech confirmation | `send_messages` (bubble delivery); a newly written program is dispatched only by a later tick |
| **🖼️ Multimodal & Memes** | Meme collection & visual comprehension | `meme_collection` (collection only), meme bubbles in `send_messages`, and `look_at_image` (multimodal vision understanding & transcription) |
| **🌐 Search & History Retrieval** | Real-time search, web fetching & history lookup | `websearch` (Exa/Tavily), `webfetch` (webpage text distillation), and `search_history` (historical context) |
| **🛡️ Group Moderation** | Member management, join request approval & leaving | `get_pending_join_requests`, `respond_to_group_join_request`, `kick`, `leave_group` |
| **📌 Task & Control** | Cross-tick note & self-scheduling | `task` (single-column latest-wins note) and `wait` (delayed wake-up and reflection scheduling) |



## 😴 TODO

- [x] Programmatic tool calling based on a restricted Python AST sandbox (Program API)
- [x] Native NapCat / OneBot event ingestion and group moderation tool wrappers
- [x] Cross-tick speech confirmation and message bubble delivery control (`send_messages`)
- [x] Single-column cross-tick note (`task`) and program decision pipeline
- [x] Optional long-term memory system and dynamic context compaction (Memory Compaction, feature-flagged)
- [ ] Emotional state machine and proactive group chat social decision-making

## 🤣 Group Chat Demos

<div align="center">
  <table border="0" style="border-collapse: collapse; margin: 20px 0;">
    <tr>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message1.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">1. Start Task & Launch Count</p>
      </td>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message2.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">2. Interleaved Chat & Dynamic Task Adjustment</p>
      </td>
      <td align="center" style="padding: 10px; border: none; vertical-align: top;">
        <img src="assets/imgs/message3.jpg" width="260" style="border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); border: 1px solid #e2e8f0;" />
        <p style="margin-top: 10px; font-size: 13px; color: #64748b;">3. Task Completion & Autonomous Multimodal Reply</p>
      </td>
    </tr>
  </table>
</div>


## 😄 Quick Start

Simply invite XiaoZou (1005089717) to your group chat.


## 😭 Slow Start

```bash
# 1. Start dependency containers (PostgreSQL & NapCat)
docker compose -f docker/postgres/compose.yml up -d
docker compose -f docker/napcat/compose.yml up -d

# 2. Initialize configuration and launch
cp .env.example .env
cp config/model_providers.example.json config/model_providers.json
pip install -r requirements.txt && python -m qqbot
```

> **📌 Configuration Notes**:
> - **NapCat Reverse WS**: Add a client connection on the panel pointing to `ws://<bot-host>:7500/onebot/v11/ws`.
> - **Model Configuration (`config/model_providers.json`)**: Fill in API Keys and configure models for roles: `planner`, `vision`, `caption`, `memory`, and `web_digest`.
> - **Protocol Probe**: Run `python -m qqbot.main_test` to independently test NapCat / OneBot API connectivity.




## 🥳 Community Group

Feel free to join for any questions.
**610662657**
<div align="left">
  <img src="assets/imgs/qqgroup_info.png" width="240" />
</div>


## 🤤 Will It Hit Top Trending Someday?
<a href="https://www.star-history.com/?repos=fayev1t%2FXiaoZou-Bot&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=fayev1t/XiaoZou-Bot&type=date&legend=top-left" />
 </picture>
</a>
