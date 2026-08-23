输入信封格式规范（行文法）

本文档规定输入信封的全部行型与字段，是这套语法的唯一出处。
记法：<x> 为行首或行内的字面标记，[…] 表示可选出现，a|b 为取值枚举。
通则一：字段或槽位缺失一律表示"未知或不适用"，绝不表示"否"。
通则二：一切结构标记以 < 开头，且动态内容中的 < > & 已转义为 &lt; &gt; &amp;——
正文里出现的 &lt;m&gt; 等字样是被引述的字面文本，不是结构；理解语义时把
&lt; &gt; &amp; 还原为字面字符即可。
通则三：只有渲染器写列 0。以两个空格缩进的行从属于其上方最近的列 0 行。
通则四：名字、头衔、摘要等短字段内的全角（）／：＃＠、『』是半角原文的
净化替身，语义同原字符。
通则五：全部时间均为北京时间（Asia/Shanghai），不逐处标注时区。

结构总览（节按固定顺序出现）：

  # 决策输入 <scope>
  本账号(QQ)                              （可缺）

  <task>                                  （便签为空时整块不出现）
  <memes>                                 （空收藏时整块不出现）
  ## 时间线

  <now>YYYY-MM-DD HH:MM:SS

ID 记号：记号标识值域，各值域互不通用，也不可互相推导。
  名字(N) / (N)   QQ 用户号。与出站 at 段的 data.qq、工具的 user_id 参数同域。
                  N 后缀 * 表示该号即本账号（服务端标注；引用他人时无此
                  后缀，不会出现"非本账号"标记）。
  #N              OneBot 消息 ID。与出站 reply 段的 data.id、以消息为目标的
                  工具参数 message_id 同域。
  ev:X            内部事件存储 ID，命名时间线上一个**已发生的事实事件**。
                  出现在决策行、程序终态行与加群申请行。
                  respond_to_group_join_request 的 request_event_id、以及一切
                  effect 的 triggered_by_event_id= 都取此值。
                  实参写裸值，不带 ev: 前缀。
  程序 hash        12 位十六进制，命名一**段代码**本身（不是写下它的那一拍）。
                  出现在 <action> 的 next_action{…} 与 <program_result>
                  行头。execute_program 的 program_hash 取此值。
                  同样的代码永远是同一个 hash，与 ev: 互不相通、不可互推。
  12 位十六进制    图片内容 sha256 的展示前缀。[img] 段与 <meme> 行同域；
                  工具参数 image_hash 按前缀原样照抄即可（也接受完整 64 位）。
  等待 ID          原样字面，仅出现在旧记录行里。


═══ 头部 ═══

# 决策输入 <scope>
  scope 取 group:<群号> | private:<QQ号> | system。group 群聊，private 一对一
  私聊，system 无聊天面的系统 loop。运行时内部标识，不属于对外可见信息。

本账号(QQ)
  本账号自己的用户号，与行内 * 标注同源；后端未连上的最初若干拍可能缺失。
  头部只有这一项与 scope；群名、群号、本账号在群里的昵称与群角色都在时间线的
  <background> 行上。


═══ <task> ═══

<task>
  <缩进的便签正文>
  当前还没办完的事，你自己写下的原文，逐字保留（整段缩进两空格呈现）。
  整块不出现 = 当前没有未竟之事。
  它只有一栏：没有编号、没有状态、没有条目切分——要记两件事就在一段话里写两件。
  每次 task(content=…) 整段覆盖上一版，只显示最新一版；先前各版不在这里。


═══ <memes> ═══

<meme>hash12 (日期): 描述
  一张已收藏的表情包，最新在前。描述是系统生成的图片描述（画面内容、图上
  文字、情绪、适用情形），是选图时能看到的全部信息，像素不随之传递。
  hash12 是内容 sha256 前缀，meme_collection 与 send_messages 以此定位一张
  收藏；日期为收藏日（同年省年）。本节是可选清单而不是待发送队列。


═══ ## 时间线 ═══

按时间升序排列的事件流。直接内容 = <t> 时刻头与事件行。

<t>HH:MM:SS 或 <t>YYYY-MM-DD HH:MM:SS
  时刻头，是时间轴本身。一个事件行的发生时刻 = 其上方最近的 <t>；同一秒的
  事件行共享一个头。首个时刻头与跨日的时刻头带完整日期，同日内只到秒。
  与 <now> 相减即得"距今多久"。

── 消息行 ──

<msg>名字(QQ[/身份][/匿名][/「头衔」]) #消息ID [回复标记]: 正文
  收到的一条用户消息。行头到第一个": "（半角冒号+空格）为止，其后是正文。
  名字      发送者显示名。有群名片取名片，否则取昵称。
  QQ        发送者用户号；本账号缀 *。
  身份      管理员 | 群主。仅这两值渲染；缺失 = 普通成员或角色未知。
  匿名      匿名群消息：名字是匿名马甲而非真实成员身份，括号内 QQ 若存在也
            是匿名伪 ID，两者都不指向稳定的人。
  「头衔」  发送者的群专属头衔，后端上报时才有。
  回复标记  回复#ID[(作者)][「摘要」]。发送者引用回复了某条消息。被引用的
            内容属于作者，冒号后的新文本属于本行发送者。
            作者   名(QQ) 或 (QQ)，被引消息作者；QQ 缀 * 即被引的是本账号
                   自己发出的消息（服务端标注，头部本账号行缺失时仍有效）。
            「摘要」被引消息的 40 字以内摘要；纯文本原样保留，富媒体折为
                   ［语义］占位（全角方括号——摘要里不会有真的内联段）。
            标记在消息到达时解析，被引消息早于可见窗口时仍然存在；全部缺失
            = 平台亦无法解析被引用的消息。回复#? 表示引用目标未知。

── 消息正文的内联段 ──

内联段一律写作 [类型 …]，出现在消息正文里。**方括号只有渲染器写得出**：
正文里用户自己打的 [ ] 会被转义成 &lsqb; &rsqb;，引用摘要里的写作全角
［ ］。所以看见半角 [ 开头的就是真的内联段，不可能是别人打出来的字。

[@ 名字(QQ)] / [@ (QQ)]
                    @ 某个用户。QQ 等于头部本账号（或缀 *）即 @ 的是本账号。
[@ 全体]            @ 全体成员，不与具体 QQ 并用。
[img [hash12] [照片|贴图] [: 描述]]
                    一张图片。像素不进入本信封，描述是该图内容的唯一表示：
                    优先为图片到达时生成的客观转录（画面内容与图上文字，
                    生成于到达时刻、不含此后的对话语境），转录未成功时退为
                    QQ 自身的外显文案。hash12 是内容 sha256 前缀，
                    look_at_image / meme_collection / send_messages 以此定位；
                    缺失 = 图片未下载成功。照片=照片或截图，贴图=表情贴。
[face N [: 名]]     QQ 原生黄豆表情。N 是 QQ 内部表情 ID，与通知行 face:N、
                    出站 face 段的 data.id 同域。无 N 的 [face : 名] 为商城/
                    魔法表情，无可回填 ID。
[voice [Ns]]        语音消息。N 是时长秒数，取不到时只有 [voice]。
                    **语音内容不在信封内，也没有转录**——只知道有人发了段
                    多长的语音。
[video]             视频消息，内容不在信封内。
[file [名] [(大小)] [id:X]]
                    聊天中发送的文件。大小为人性化写法；id 是平台侧文件
                    凭证，供文件类工具回填。
[poke [目标(QQ)]]   消息内的拍一拍。目标 QQ 缀 * 即拍的是本账号；缺失 =
                    无特定对象。
[dice N]            掷骰子结果，1 至 6。
[rps 石头|剪刀|布]  猜拳结果。
[markdown]正文      Markdown 富文本消息，正文超 500 字截断（末尾 …）。
[forward [id:X]]    合并转发的聊天记录包，包内消息不展开。
[card [app] [「外显」] [标题] [描述] [url]]
                    富文本分享卡片：链接分享、小程序、公众号文章、音乐、
                    位置、名片推荐。app 为卡片应用 ID；「外显」是 QQ 自身的
                    单行文案；标题/描述为卡片自带（小程序上标题常是应用名、
                    描述是实际内容标题）。[card 原始json|原始xml|原始share]
                    表示未能解析，内容未知。
[unknown 类型]      运行时未识别的段，内容未知。

── 工具行 ──

<tool>名 已调用|完成|失败[ kind [k=v …]]
  参数 <JSON>
  结果 <JSON>[（截断）]
  原因 <文字>
  一次工具调用及其结果。工具结果只在此处呈现，信封内没有另外的结果区。
  所属时刻头 = 做出该调用那一拍的观察时刻，不是调用执行或结束的时刻。
  已调用    意图已落库，终态尚未写出；不要把它读成失败，也不要当成已经成功。
  完成      调用成功；结果行为返回值 JSON，超过 6144 字符时尾部截去并加
            （截断）。
  失败      调用失败；kind 与结构化附加字段在行头，人类可读原因在原因行。
            partial / uncertain / interrupted 不等于“动作没有发生”，只按回执
            字面理解，是否采取新动作由当前拍重新判断。kind 常见取值：
            permission_denied_user_tier  发起请求的用户等级不足
                                         附 required_tier= actual_tier=
            permission_denied_bot_role   本账号群角色不足
                                         附 required_bot_role=
                                         actual_bot_role=；目标约束导致时
                                         另附 target_role=
            invalid_arguments            参数不合法或缺失，附 reason_code=；
                                         涉及消息段时另附 segment_index=
                                         segment_type=
            tool_unavailable_in_scope    该工具在当前 scope 不可用，附
                                         allowed_scopes= actual_scope=
            target_scope_mismatch        目标与当前 scope 不符，附
                                         expected_scope= actual_target_kind=
                                         actual_target_id=
            unknown_tool                 该名字不在注册表中
            no_bot_available             临时基础设施故障
            upstream_action_failed       平台拒绝该动作，附 retcode= action=
                                         upstream_wording=
            internal_tool_error          非预期的工具缺陷
            program_timeout              调用超时；effect 附 status=uncertain
            interrupted                  进程退出或关停留下半截调用，附
                                         status=uncertain；系统不会自动重放

<tool>send_messages 的特例：参数/结果不渲 JSON，改为逐气泡一行——
  「内容」[→回执] 或 <meme hash12>[→回执]
  原因 <文字>（仅失败）
  气泡内容按发送顺序排列，文字气泡按 回复#消息ID / [@ (QQ)] / text 原文 /
  [face N] 的固定顺序拼接（缺省的部分不出现）。
  回执三态：→#消息ID 送达（该气泡在 QQ 上的消息 ID）；
  →失败；→存疑（送达与否无法确认）。行头 失败 后先跟 error kind，再以
  status=partial|failed|uncertain 给出整体发送状态：partial 部分送达，failed
  全部失败，uncertain 至少一条无法确认。这一行块是该次发送的唯一记录；
  回执缺失的历史半截调用按 interrupted / uncertain 呈现。

── 决策行与程序终态 ──

<action> ev:X
  execute_program: hash12
  next_action {hash12}:
    源码全文

<program_result> [hash12] ev:X status:ok
  result: <JSON>[（截断）]

<program_result> [hash12] ev:X status:failed kind [k=v …]
  reason: <文字>

  `<action>` 是某一拍的完整输出，两层各自可缺（都缺时显示「（空程序）」）：
    ev:X            这一拍本身。`<program_result>` 的 ev: 回指的就是它。
    execute_program 那一拍下的调度令，指名某段**已经写在流上的**代码去跑。
                    没有这行 = 那一拍没有指名任何东西。
    next_action     那一拍新起草的业务代码原文，含注释。{hash12} 是**这段
                    代码**的身份，你要指名它就照抄这串。
  **写出来不等于跑过。** next_action 里的代码当拍一个函数都没被调用；它只有
  在后来某一拍被 `execute_program(program_hash="hash12")` 指名之后才会真的
  执行。空程序与只下过调度令的拍没有 next_action（没留下可跑的代码，指名不到）。

  `<program_result>` 是执行终态，hash12 是跑的那段代码，ev: 回指**下令跑它的
  那一拍**。两者都要看：同一段代码可以被指名多次、也可以多段同时在跑，靠这一对
  而不是靠位置对上号。没有终态的 next_action 有三种情形：还没被指名执行、正在
  执行中、以及再也不会被执行。status:ok 时只有程序显式 `return` 了数据才有
  result 行；空程序在它自己那一拍就直接得到 status:ok。
  调用明细在各自的 `<tool>` 行上，不在这里用查询名摘要代替。失败 kind 除工具
  自身错误外，程序层常见取值：
    program_syntax_error        源码语法或最外层围栏不合法
    program_forbidden_construct 使用了受限子集之外的结构，附 construct=
    program_unknown_name        名字不存在或当前 scope 不可见，附 name=
    program_quota_exceeded      静态或动态配额超限，附 quota= actual= max=
    program_unknown_field       读取返回 schema 未声明字段，附 function= field=
    program_timeout             单调用或整程序超时，附 scope=call|program
    program_output_too_large    return JSON 超限，附 actual_bytes= max_bytes=
    program_not_found           execute_program 指的那段代码不在本 scope，附
                                target_program_hash=（空程序和只下过调度指令的
                                拍不留代码，指名它们即属此类）
    interrupted                 进程退出留下半截程序，附 status=uncertain；不重放

── 其余行型 ──

<background>
  group_name: <群名>
  group_id: <群号>
  self_group_nick_name: <本账号在这个群显示的名字>
  group_role: owner|admin|member
  date: YYYY-MM-DD 星期X
  当天的群聊环境。每天 00:00 注入一条，字段缺失即该项未知。
  它是**时间线上的一条事实**，不是常驻表头：说的是"注入那一刻这个群是这样"。
  群名、昵称、角色都可能在此后被改动，最新一条也可能已是昨天的；与实时对话
  冲突时以实时对话为准。真要动手时也不必据此判断权限——工具在调用时自己复查
  本账号的真实角色，角色写着 member 的调用照样可能通过。
  时间线滚动会把早先的 <background> 挤出窗口，届时这些信息暂时无从读起。

<invalid_action>
reason: 错误kind [line=N column=M]: 说明
raw_text:
  <被拒源码全文，缩进两格逐行原样>
  你此前某一拍写出的程序没有通过静态校验，那一拍因此没有产生任何决策，
  也没有任何函数被调用过。reason 是真实的错误 kind 与出错位置，raw_text
  是你当时写下的那段源码原文。看着它重写即可。
  校验不通过不重试、不换模型：一次响应就是一次结果，要么成为一段代码，
  要么成为这一行。

<notice>kind 模板句
  群内发生的一个事件的记录，不是发给本账号的消息。kind 为原始枚举词作锚，
  模板句中的人物为 名(QQ) 或 (QQ)（近期消息可反查到名字时带名），当事人为
  本账号时 QQ 缀 *。kind 与句式：
    group_increase  入群（含被邀请入群、由谁通过）
    group_decrease  退群或被移出（含 本账号被移出）
    group_recall    某人撤回了消息#N（自撤时写明"自己的"；被撤回的内容已
                    从平台消失）
    friend_recall   对方撤回了消息#N
    poke            拍一拍，动作文案按平台原语序（动作+目标+后缀）
    group_admin     被设为/被取消管理员
    group_ban       禁言 N秒 / 解除禁言 / 开启·关闭全员禁言
    group_card      群名片 「旧」→「新」（「」空引号 = 名片被清空，与整句
                    退化为"修改了群名片"的未知不同）
    group_upload    上传了 文件名 (大小)
    essence         消息#N 被设为/被移出精华
    emoji_like      对消息#N 回应 表情×人数[,…]（表情为字面 emoji 字符，或
                    face:N，与 [face N] 同域）
    honor           获得群荣誉（talkative 龙王 / performer / emotion，原值
                    透传）
    lucky_king      成为红包运气王
    friend_add      新增好友
    input_status    "对方正在输入"指示，不是一条消息
    bot_offline     本账号掉线
  未列出的 kind 或明细缺失时正文为载荷 JSON，按字面理解。

<reflection>
  <缩进的正文>
  本账号自己写下的一版自我认识，由 reflect 留下。它是时间线上的一条普通事实：
  发生时刻由上方最近的 <t> 承载，与 <now> 相减即得这版认识距今多久，据此判断
  它是否仍然成立。历史各版都留在流上，早写的在上、晚写的在下，可以看出这套
  认识是怎么变过来的；后写的一版不会抹掉前一版，只是更晚。
  正文为写下时的完整文本，逐字保留、不经压缩或改写。
  内容出自本账号自己，不是他人的评价，也不是运行时的判定。

<join_request>ev:X 申请人(QQ) [留言「…」]
  一条处于待处理状态的加群申请，平台正在等待管理员裁决（仅群聊申请；好友
  申请与入群邀请在别处自动处理，不出现于此）。ev:X 属于事件存储值域，
  respond_to_group_join_request 的 request_event_id 取此值，不是消息 ID。
  申请人尚非群成员，无法被 @。留言为申请人填写的验证消息，缺失 = 未填写。
  目标群恒为当前群，不再标注。

<wait_ended>等待ID [rN]
  仅旧记录：已删除的 reply 等待链路中一段等待到点的事实。现行链路不产生本行，
  也不再有 reply 这个函数。

<system>kind [载荷]
  运行时发出的一条提示，载荷按 kind 与正文字面理解。kind 常见取值：
    wait_elapsed          此前调度的 wait 已到点，载荷含当时留下的 note 原文。
    silence_elapsed       当前群自最后一条可见事件起已静默满载荷 seconds 秒，
                          系统据此开了这一拍。它陈述群内无人活动这一事实，
                          不表示有人在等待回应，也不指定本拍该做什么。
    bot_role_observed     系统观测到本账号的群角色。
    event_ingest_failed   平台输入已经到达，但接收器在映射、媒体落盘或图片
                          描述阶段失败。载荷只给安全摘要：source、actor、原消息
                          ID、失败阶段/错误码、原因及仍可读取的文本；原始 NapCat
                          报文不进入信封。该行本身就是一次已提交的终态内部事件。

<recall>[起止区间] [共N条]
  <缩进的摘要正文与脚注>
  滚动记忆摘要。早于该行的事件已从时间线移除，缩进正文的摘要即其全部残留。
  恒位于时间线首位。其内容为压缩产物，与实时对话冲突时以实时对话为准。

<legacy_reply>[等待ID] sent|partial|failed|empty|uncertain
  <缩进的逐气泡行，形态同 send_messages 特例>
  原因 <文字>
  仅旧记录：历史链路中一次回复投递的最终结果。现行发送不产生本行——一次
  发送的记录是它自己的 <tool>send_messages 行块。


═══ 时钟 ═══

<now>YYYY-MM-DD HH:MM:SS
  本次运行的时钟，位于信封末尾附近。判定 <t> 的新旧以此为基准。
