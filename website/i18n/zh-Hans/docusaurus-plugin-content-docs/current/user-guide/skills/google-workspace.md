---
sidebar_position: 2
sidebar_label: "Google Workspace"
title: "Google Workspace — Linxio Gmail、Calendar 与 Drive"
description: "最小权限的 Gmail 草稿、需批准的自有日历事件及集成创建的 Drive 文件"
---

# Google Workspace Skill

Linxio 配置允许 Hermes 读取 Gmail 和 Calendar、仅创建 Gmail 草稿、在明确批准后创建自有 Calendar 事件，并通过 `drive.file` 管理集成创建的文件。

普通工作流不提供邮件发送功能。

## OAuth 权限

授权仅请求以下五项权限：

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.compose
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/calendar.readonly
https://www.googleapis.com/auth/calendar.events.owned
```

凭据默认位于 `${HERMES_HOME:-$HOME/.hermes}/secrets/google/`。部署可通过 `GOOGLE_OAUTH_CLIENT_FILE` 和 `GOOGLE_OAUTH_TOKEN_FILE` 指定绝对路径。

## 授权

```bash
GSETUP="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/setup.py"
$GSETUP --service-profile linxio --check
$GSETUP --service-profile linxio --auth-url
$GSETUP --service-profile linxio --auth-code '完整的本地重定向 URL'
```

浏览器登录、同意及返回重定向 URL 必须由用户完成。切勿输出或复制凭据与 token 内容。

## Gmail

```bash
GAPI="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/google_api.py"
EMAIL="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/email_learning.py"
$GAPI gmail search 'from:customer@example.com newer_than:30d' --max 10
$GAPI gmail get MESSAGE_ID
$GAPI gmail thread THREAD_ID
$EMAIL draft-preview --draft-file PRIVATE_0600_JSON
$EMAIL draft-create STATE_ID --draft-file PRIVATE_0600_JSON --approval-token TOKEN
$GAPI gmail draft-delete DRAFT_ID
```

草稿必须绑定 Cal 明确选择的消息或线程、四类结构化上下文及一次性精确预览批准。不存在 send、reply-send 或 draft-send 命令。

写作比较是独立步骤：Cal 手动发送已批准的邮件后，`comparison-preview` 只接受 Cal 明确选择且带有 Gmail `SENT` 标签的消息。Hermes 永远不会发送邮件。

## Calendar

```bash
$GAPI calendar list
$GAPI calendar create --summary 'Review' --start 2026-08-01T14:00:00Z --end 2026-08-01T14:30:00Z --dry-run
$GAPI calendar create --summary 'Review' --start 2026-08-01T14:00:00Z --end 2026-08-01T14:30:00Z --approval-token TOKEN
$GAPI calendar delete EVENT_ID
```

批准 token 有效期短、仅可使用一次，并与预览内容严格绑定。事件为私有，不支持参与者，也不会发送通知。

## Drive 与冒烟测试

```bash
$GAPI drive create-file --kind linxio --name 'Linxio Knowledge' --content 'Text'
$GAPI drive create-file --kind cogitator --name 'Cogitator Knowledge' --content-file /path/to/content.txt
$GAPI drive delete FILE_ID

GSMOKE="python ${HERMES_HOME:-$HOME/.hermes}/skills/productivity/google-workspace/scripts/smoke_test.py"
$GSMOKE --dry-run
$GSMOKE --approval-token TOKEN
```

Drive 删除只会移至回收站。冒烟测试不会发送邮件或添加参与者，并始终尝试清理草稿、Drive 文件及私有日历事件。
