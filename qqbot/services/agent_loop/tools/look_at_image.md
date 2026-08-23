# 工具：`look_at_image`

## 功能

`look_at_image` 针对一张已下载图片执行一次视觉问答。时间线中已有的
`desc="..."` 是图片接收时生成的通用转录；本工具会新建一次视觉模型调用，并
针对 `question` 返回答案，不修改原有 `desc`。

## 参数

```json
{
  "image_hash": "<12 位 hash 前缀>",
  "question": "需要从图片中识别的具体信息"
}
```

- `image_hash`：必填，时间线 `[img …]` 段中的 12 位 hash 前缀（原样照抄，也接受完整 64 位）。
  没有 hash 的图片未落盘，不能调用本工具查询。
- `question`：必填非空字符串，最长 500 个字符。视觉模型仅接收图片和该问题，
  不接收群聊时间线；问题中需要包含回答所需的上下文。

## 权限与作用域

`required_permission=GUEST`，`allowed_scopes` 不限，不要求机器人群角色。

## 返回

成功返回：

```json
{
  "image_hash": "<sha256>",
  "question": "...",
  "answer": "视觉模型回答"
}
```

hash 格式错误或问题为空时返回 `invalid_arguments`；图片文件不存在时返回
`image_not_found`；视觉模型调用失败时返回 `upstream_action_failed`。
