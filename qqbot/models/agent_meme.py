"""agent_memes 表 —— 表情包收藏（meme 工具的操作状态表）。

为什么存在（开发文档/v2.0/30-工具设计/表情包工具黑盒设计.md）：
  LLM 在聊天里看到值得收藏的图片时，经 meme 工具（action=save）把它收录为
  表情包。图片文件本身复用 EventIngest 落盘的
  runtime_data/media/img/<hash[:2]>/<hash>
  （EventIngest契约.md §5.1，sha256 内容寻址，**不复制文件**），本表只存一层
  元数据 —— 其中 description 由 meme 工具内部的 caption LLM 调用看图生成，
  是选图的唯一依据（投影把收藏夹渲染成信封的 `## 表情包收藏` 一节，每 tick
  注入；选哪张、发不发在 send_messages 组稿时现场决定）。

与 append-only 硬规矩的关系：
  agent_events 仍是唯一事件真相源。本表与 agent_delivery_claim 同类，是**可变
  操作状态表**（不是事件），INSERT/UPDATE/DELETE 由 meme 工具执行（save /
  recaption / delete）；各动作本身已作为 agent.tool_called / tool_result 事件
  留痕，审计链不依赖本表。

共享语义（2026-07-06 起，事件系统设计 §11.3 例外）：
  收藏夹在当前账号内跨聊天 scope 共用 —— 收藏以公共值 file_hash 为键、
  不携带 scope 上下文，与图片文件按 hash 落盘缓存同类，属于 scope 隔离明确
  允许并要求显式标注的例外。实现：scope_key 列固定写 meme_store 的
  MEME_SCOPE_GLOBAL 哨兵（'global'），主键 (scope_key, file_hash) 退化为
  全局一图一条；列保留以便将来恢复分域。历史分群行需一次性迁移合并（见
  表情包工具黑盒设计.md §2）。跨会话投递不因此放开：meme 气泡的发送目标
  仍只从当前 loop 的 scope_key 解析。

媒体文件生命周期约束：
  本表的 file_hash **钉住**对应磁盘文件。当前 runtime_data/media 没有任何清理
  任务；将来若引入媒体 GC，必须 join 本表排除被收藏的 hash，否则发送前
  读盘失败（media_file_missing）。见 表情包工具黑盒设计.md §媒体生命周期。
"""

from sqlalchemy import Column, DateTime, Text

from qqbot.models.base import Base


class AgentMeme(Base):
    __tablename__ = "agent_memes"

    # 联合主键。当前账号内全局共享后 scope_key 恒为 MEME_SCOPE_GLOBAL 哨兵，主键退化
    # 为全局一图一条（重复保存 = ON CONFLICT DO NOTHING，工具层折
    # already_saved）；取收藏夹按 scope_key 哨兵走主键前缀，无需额外索引。
    scope_key = Column(Text, primary_key=True)
    file_hash = Column(Text, primary_key=True)
    # caption LLM 生成的中文描述（画面内容/图上文字/情绪/适用场景）。
    # `## 表情包收藏` 节的渲染与组稿选图都只看它。
    description = Column(Text, nullable=False)
    # 保存时 planner 附带的群聊语境（谁的名场面/本群怎么用）——caption 的输入
    # 之一，原文留档便于将来重生成描述。None = 未提供。
    context_note = Column(Text, nullable=True)
    # 保存时从文件 magic bytes 嗅探（磁盘文件按 hash 命名无扩展名；ingest 时的
    # mime 在事件 payload 里，现场嗅探比查事件流可靠）。
    mime = Column(Text, nullable=False, default="image/png")
    # 保存动作的 agent.tool_called event_id —— 因果锚点，凭它能从事件流找回
    # 谁、在哪个 tick 收藏的（本表不冗余存发起人）。
    source_event_id = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<AgentMeme({self.scope_key} {self.file_hash[:12]}…)>"
