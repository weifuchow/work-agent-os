# 飞书机器人接入配置指南

## 能力说明

| 场景 | 是否支持 | 所需权限 |
|------|---------|---------|
| 监听个人账号私聊 | **不支持** | 飞书不允许 |
| 别人私聊机器人 | 支持 | `im:message.p2p_msg:readonly` |
| 群聊中 @机器人 | 支持 | `im:message.group_at_msg:readonly` |
| 群聊中所有消息（无需@） | 支持 | `im:message.group_msg`（敏感权限，需审批） |
| 机器人主动发消息 | 支持 | `im:message:send_as_bot` |

> 飞书开放平台只允许通过**自建应用机器人**接收消息，无法拦截个人账号的消息流。

---

## 第一步：创建飞书自建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app)，登录你的飞书账号
2. 点击右上角 **「创建企业自建应用」**
3. 填写：
   - 应用名称：`WorkAgent`（或你喜欢的名字）
   - 应用描述：`个人工作助理机器人`
4. 点击 **创建**

---

## 第二步：获取凭证

在应用详情页，左侧导航 → **「凭证与基础信息」**：

| 字段 | 说明 | 对应 .env 变量 |
|------|------|---------------|
| App ID | 应用唯一标识 | `FEISHU_APP_ID` |
| App Secret | 应用密钥 | `FEISHU_APP_SECRET` |

复制这两个值，填入 `.env` 文件：

```env
FEISHU_APP_ID=cli_xxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_BOT_NAME=WorkAgent
```

> `FEISHU_VERIFICATION_TOKEN` 和 `FEISHU_ENCRYPT_KEY` 在长连接模式下**不需要**，可以留空。长连接只在建连时鉴权，后续事件为明文推送。

---

## 第三步：添加机器人能力

1. 左侧导航 → **「添加应用能力」**
2. 选择 **「机器人」**，点击添加
3. 在 **「应用能力 → 机器人」** 页面配置：
   - 机器人名称：`WorkAgent`（要和 `FEISHU_BOT_NAME` 一致）

---

## 第四步：配置事件订阅（长连接模式）

1. 左侧导航 → **「事件与回调」**
2. 在 **「事件配置」** 页签：
   - 订阅方式选择 **「使用长连接接收事件」**
   - 点击 **保存**
3. 点击 **「添加事件」**，搜索并添加：
   - `im.message.receive_v1` — 接收消息

> 长连接模式的优势：
> - 无需公网 IP 或域名，本地开发即可接收事件
> - 无需内网穿透工具
> - 无需处理解密和验签

---

## 第五步：配置权限

左侧导航 → **「权限管理」**，搜索并开通以下权限：

### 必选权限

| 权限 | 说明 |
|------|------|
| `im:message.p2p_msg:readonly` | 接收私聊消息 |
| `im:message.group_at_msg:readonly` | 接收群聊 @机器人 的消息 |
| `im:message:send_as_bot` | 以机器人身份发送消息 |

### 可选权限（按需开通）

| 权限 | 说明 |
|------|------|
| `im:message.group_msg` | 接收群里所有消息（无需@），**敏感权限需审批** |
| `im:message:send_as_bot` | 主动发消息 |
| `contact:user.base:readonly` | 读取用户基本信息（用于获取发送者姓名） |

---

## 第六步：发布应用

1. 左侧导航 → **「版本管理与发布」**
2. 点击 **「创建版本」**
3. 填写版本号（如 `1.0.0`）和更新说明
4. 点击 **保存** → **申请发布**
5. 企业管理员在飞书管理后台审批通过

> 如果你是企业管理员，可以在「免审发布」设置中将自己添加为免审人员。

---

## 第七步：启动 Worker

```bash
# 确保 .env 已配置好
python -m apps.worker.feishu_worker
```

看到以下日志说明连接成功：

```
Starting Feishu WebSocket connection...
connected to wss://xxxxx
```

---

## 第八步：测试

### 测试私聊

1. 在飞书中搜索你的机器人名称（如 `WorkAgent`）
2. 直接给它发一条消息
3. 检查日志和数据库中是否收到消息

### 测试群聊 @

1. 创建一个测试群
2. 把机器人添加到群中
3. 在群里发送 `@WorkAgent 测试消息`
4. 检查日志

### 验证管线

消息接收后会自动触发处理管线：
1. 入库 → `messages` 表
2. 分类 → `classified_type` 更新
3. 路由 → `session_id` 关联
4. 摘要 → `data/sessions/{id}/summary.md` 生成

可在管理后台 `/messages` 页面查看管线状态。

---

## 使用建议

### 推荐用法

由于无法监听个人账号消息，推荐以下工作流：

1. **工作群模式**：把机器人拉进各个工作群，开通 `im:message.group_msg` 权限后，机器人自动接收所有消息并处理
2. **私聊指令模式**：需要机器人帮忙时，直接私聊它（类似 ChatGPT 对话）
3. **@触发模式**：群里 @机器人 + 问题，机器人分析并回复

### 常见问题

| 问题 | 解决方案 |
|------|---------|
| Worker 启动报错 `FEISHU_APP_ID must be set` | 检查 `.env` 文件配置 |
| 连接成功但收不到消息 | 检查事件订阅是否添加了 `im.message.receive_v1` |
| 群消息收不到 | 检查是否开通了 `group_at_msg` 或 `group_msg` 权限 |
| 应用未发布 | 必须在「版本管理」中发布并审批通过才能接收消息 |
| 长连接保存失败 | 确保先添加了机器人能力 |

---

## .env 完整配置参考

```env
# Feishu
FEISHU_APP_ID=cli_a5xxxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_BOT_NAME=WorkAgent

# 长连接模式下以下两项可留空
FEISHU_VERIFICATION_TOKEN=
FEISHU_ENCRYPT_KEY=
```
