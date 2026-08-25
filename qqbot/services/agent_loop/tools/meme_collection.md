# 工具：`meme_collection`

## 功能

`meme_collection` 管理当前账号内跨聊天 scope 共享的表情包收藏夹，支持收录、删除和重新生成
描述。该工具不发送消息，也不存在发送 action；收藏表情包的发送由
`send_messages` 的 `{"meme": "hash12"}` 气泡完成。

收藏记录保存图片 hash、生成的中文描述、可选上下文和媒体类型。图片文件复用
EventIngest 已落盘的内容寻址文件，不由本工具复制或删除。

## 参数

```json
{
  "action": "save",
  "image_hash": "<12 位 hash 前缀>",
  "context_note": "可选上下文"
}
```

- `action`：必填，支持 `save`、`delete`、`recaption`。
- `image_hash`：必填。`save` 接收一个 12–64 位 hash 前缀字符串，或最多 10 个
  字符串的数组；`delete` 和 `recaption` 仅接收单个字符串。
- `context_note`：可选字符串，最多 300 个字符，仅适用于 `save` 和
  `recaption`。该字段作为描述生成的附加上下文，不直接展示给用户。
  `recaption` 省略该字段时复用收录时保存的上下文。

`save` 的 hash 对应时间线 `[img …]` 段里的 12 位前缀；`delete` 和
`recaption` 的 hash 对应表情包收藏 `<meme>` 行里的 12 位前缀，原样照抄即可。
hash 会归一化为小写。

## action 语义

### save

读取指定图片文件并调用 caption 模型生成检索描述，然后写入收藏记录。单个
hash 已存在时返回 `already_saved`，不重复生成描述。数组输入会保序去重并逐项
处理；结构错误会拒绝整次调用，单张内容错误会记录在对应项回执中。

单张成功返回 `action`、`file_hash`、`saved` 和 `description`。批量输入返回
`batch=true`、逐项 `results` 以及 `saved_count`、
`already_saved_count`、`failed_count`。

### delete

删除指定 hash 的收藏元数据，不删除磁盘图片文件。成功返回被删除记录的
`description`。

### recaption

重新读取图片并生成描述，只更新 `description` 和 `context_note`。生成失败时
原描述保持不变。成功返回 `description` 和 `previous_description`。

## 权限与作用域

`allowed_scopes=("group","private")`，`required_permission=GUEST`，不要求
机器人群角色。收藏夹在当前账号的所有聊天 scope 之间共享；system scope 不暴露本工具。

## 失败

常见错误包括：`bad_action`、`bad_image_hash`、`empty_batch`、
`too_many_images`、`batch_not_supported`、`image_not_found`、
`unknown_meme`、`media_file_missing` 和 `caption_failed`。
这些错误只描述收藏管理结果，不代表任何消息已发送。
