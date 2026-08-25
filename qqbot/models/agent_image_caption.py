"""agent_image_captions 表 —— timeline 图片的客观描述缓存（2026-07-28）。

为什么存在：
  2026-07-28 起 Planner / Replyer 降级为纯文本模型，不再收到图片像素。群里
  出现的每张图在 EventIngest 落盘后由专用 VLM（role="vision"）看一次、写一段
  客观描述，描述随 payload.segments 的 image 段一起进事件正文，投影渲染成
  <image ... desc="..."/>。本表是那次调用的**写时缓存**：同一张图（sha256 内容
  寻址）在任何群、任何时间再次出现都命中本表，不重复调 VLM —— QQ 群里的图以
  重复表情包为主，这层缓存是整个方案的成本来源。

与 append-only 硬规矩的关系：
  agent_events 仍是唯一事件真相源。本表与 agent_memes / agent_delivery_claim
  同类，是**可变派生表**（不是事件）：内容可重算、可整表清空，清空只会让后续
  新出现的图重新付一次 VLM 调用，不影响任何历史事件的可回放性 —— 因为投影
  读的是事件正文里那份 desc，不是本表。**投影层永不查本表**（每 tick 每图一次
  查询的代价换不来任何东西），本表只在 ingest 写路径上被读写。

描述的不可变性（有意为之）：
  事件正文里的 desc 写下即定，日后换 VLM 或改 prompt 都不会追溯修改 —— 回放
  契约要的正是"当时模型真看到了什么"。本表的 model 列记下产出这份描述的端点，
  将来想按模型筛选重跑（只影响之后新出现的图）有据可依。

媒体文件生命周期：
  本表**不**钉住磁盘文件 —— 描述是文件的替代品而非引用，文件被 GC 掉之后描述
  依然有效（只有 look_at_image 重看会失败）。将来的媒体 GC 无需 join 本表，这
  与 agent_memes 的钉住语义相反，见 agent_meme.py。
"""

from sqlalchemy import Column, DateTime, Integer, Text

from qqbot.models.base import Base


class AgentImageCaption(Base):
    __tablename__ = "agent_image_captions"

    # sha256 内容寻址，与 EventIngest 落盘布局、时间线 `<图 hash12 …>`、
    # 收藏节 `<meme>hash12` 同一值空间（信封只展示 12 位前缀，库里存完整
    # 64 位）。不带 scope：同一张图在当前账号内共用一份描述。
    file_hash = Column(Text, primary_key=True)
    # VLM 产出的客观描述（画面内容 + 图上文字逐字转录）。不含语境判断——
    # 语境由 Planner 从 timeline 自己合成，见 image_description.py 模块 docstring。
    description = Column(Text, nullable=False)
    # 送进 VLM 时的 mime（GIF 已在 normalize_image_for_llm 里转成 PNG，故这里
    # 记的是转换后的值，不是 ingest 嗅探到的原始 mime）。
    mime = Column(Text, nullable=False, default="image/png")
    # 送进 VLM 的字节数（base64 编码前）。排查"图太大被网关拒"时用。
    byte_size = Column(Integer, nullable=True)
    # 产出这份描述的端点 spec（provider/model）。换模型后想筛选重跑的依据；
    # 取不到（stub / 未暴露 model_name）时为 None，不猜。
    model = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<AgentImageCaption({self.file_hash[:12]}…)>"
