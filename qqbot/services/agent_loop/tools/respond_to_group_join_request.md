# 工具：`respond_to_group_join_request`

## 功能

`respond_to_group_join_request` 审批当前群的一条入群申请，对应 OneBot V11
`set_group_add_request`。目标申请必须已作为
`<join_request>ev:...` 行出现在当前群时间线中。

该工具不处理好友申请或机器人入群邀请。

## 参数

```json
{
  "request_event_id": "EV_123",
  "approve": true,
  "reason": ""
}
```

- `request_event_id`：必填字符串，取目标 `group.add` 请求事件的 `event_id`。
  工具会根据该事件读取 napcat `flag`；调用参数不包含 `flag`。
- `approve`：必填布尔值。`true` 表示同意，`false` 表示拒绝。
- `reason`：可选字符串。`approve=false` 时作为拒绝原因传给平台；
  `approve=true` 时忽略。

事件类型必须为 `external.request.group.add`，且事件所属群必须等于当前
`scope_key` 中的群。

## 权限与作用域

- `allowed_scopes=("group",)`。
- `required_permission=ADMIN`。`triggered_by_event_id` 所指消息的发送者会在
  调用时按实时群角色校验。
- `required_bot_role="admin"`，群主同时满足该条件。

## 返回

成功返回：

```json
{
  "request_event_id": "EV_123",
  "group_id": 100,
  "user_id": 456,
  "approve": true,
  "applied": true
}
```

事件不存在、类型不匹配、事件不属于当前群或缺少 `flag` 时返回
`invalid_arguments`。权限不满足时返回 `permission_denied_user_tier` 或
`permission_denied_bot_role`；OneBot 调用失败时返回
`upstream_action_failed`。
