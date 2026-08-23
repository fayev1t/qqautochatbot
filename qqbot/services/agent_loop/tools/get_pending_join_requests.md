# 工具：`get_pending_join_requests`

## 功能

`get_pending_join_requests` 查询当前群在 napcat 系统消息中的入群申请，对应
`get_group_system_msg`。该调用为只读操作，只返回当前群的待处理申请和近期已
处理数量；其他群的申请及机器人入群邀请会被过滤。

## 参数

```json
{}
```

本工具不接收业务参数。`group_id` 从当前 `scope_key` 注入，参数中不存在
`group_id`。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=GUEST`。
- `required_bot_role="admin"`，群主同时满足该条件。

## 返回

成功返回：

```json
{
  "group_id": 100,
  "pending_count": 2,
  "requests": [
    {"user_id": 456, "nickname": "小明", "comment": "申请说明"}
  ],
  "handled_recent_count": 1,
  "may_be_incomplete": true
}
```

- `pending_count` 是当前响应中属于本群的待处理申请数。
- `requests` 最多返回 50 条；`nickname` 和 `comment` 可以为 `null`。
- 返回结果不包含 napcat `flag`。
- `handled_recent_count` 是响应窗口中属于本群且已处理的申请数。
- `may_be_incomplete` 固定为 `true`，表示平台只返回近期系统消息，结果不保证
  覆盖全部历史积压。

本工具的结果不包含 `request_event_id`。审批操作
`respond_to_group_join_request` 使用时间线
`<join_request>` 行的 `ev:` 值。没有对应时间线事件的申请
不能通过该审批工具处理。

上游响应结构无法识别时返回 `upstream_payload_invalid`；OneBot 调用失败时
返回 `upstream_action_failed`。
