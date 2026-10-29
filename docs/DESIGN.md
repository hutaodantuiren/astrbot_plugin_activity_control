# 设计说明

## 1. 核心目标

在 QQ 群里用三个 owner 指令控制机器人“读不读群、回不回、听谁的”：

- `/活跃`：每 2 分钟读群并回复；@/唤醒词即时回复。
- `/潜水`：平时完全安静；只有被 @ 时读取上下文回复。
- `/睡眠`：只认 owner 的 @ 与 owner 的指令。

“owner”取 AstrBot 管理员身份，不是 QQ 群主。

## 2. 事件流

```
QQ 群消息
   │
   ▼
AstrBot WakingCheck
   ├─ 设置 event.role（admins_id → admin）
   ├─ 计算 activated_handlers（指令是否命中）
   └─ 判断本消息是不是 @/唤醒
   │
   ▼
本插件 on_group_message（priority 10000）
   ├─ 合成事件？→ 放行（定时任务自己排入的事件）
   ├─ 解析：群号、发送者、@、唤醒词、是否指令、是否 owner
   ├─ 纯逻辑 decide_action()
   │    ├─ allow_command  → 放行给 AstrBot 指令处理器
   │    ├─ block_command  → stop_event（静默）
   │    ├─ reply          → 打标记，让原生 LLM 管线回复
   │    └─ ignore         → stop_event，只留在内存缓存
   └─ 缓存最近消息（仅内存）
```

### 即时回复（@/唤醒）

不自己拼模型请求，而是：

1. 给当前真实事件打 `_activity_ctx` 标记；
2. 设置 `is_at_or_wake_command=True`、`should_call_llm(True)`；
3. 在 `on_llm_request` 里把聊天记录作为**临时** `TextPart` 注入；
4. 走 AstrBot 原生 LLM 管线，保留人格、技能、工具、会话和发送链路。

这样做的好处：人格/上下文/语音/TTS/工具链都不会被绕过。

### 定时回复

定时器没有真实消息事件，使用 `CronMessageEvent` 的合成事件：

- 从 `context.get_event_queue()` 投递；
- 事件里带 `_activity_ctx`；
- `on_llm_request` 同样注入聊天记录；
- 平台发送仍走 `context.send_message(session, chain)`。

合成事件会设置真实机器人 `self_id`，避免平台开启“忽略机器人自己的消息”时被丢掉。

## 3. 为什么不是每条消息都调用模型

活跃模式如果每条消息都请求 LLM，会：

- 费用高；
- 刷屏；
- 打断群聊节奏。

因此普通消息只进内存缓存；每 `interval_seconds`（默认 120 秒）批量读取一次。
@ 和唤醒词是例外，立即触发。

## 4. owner 识别

### 默认：merge

```
AstrBot admins_id  ∪  插件 owner_ids
```

`admins_id` 是 AstrBot 全局管理员配置，也是 AstrBot 判断 `role=admin` 的来源。
插件直接读配置，不依赖群昵称、群名片或 QQ 群主身份。

### 覆盖：plugin

```
插件 owner_ids
```

适合 AstrBot 全局管理员另有其人，或者不想改全局配置的情况。

### 额外兜底

`event.is_admin()` 为真时，当前发送者也会被视为 owner（仅当 `owner_source` 允许 AstrBot 配置时）。
这能覆盖 AstrBot 后续版本调整 `role` 赋值时的情况。

### 为什么不用 QQ 群主

QQ 群主是平台侧身份，通常在 `group_owner` 字段里，和 AstrBot 的 `admins_id` 没有关系。
本插件按需求读取的是 AstrBot 的 owner 设定，所以不使用 QQ 群主。

## 5. 每个模式的精确规则

| 事件 | 活跃 | 潜水 | 睡眠 |
|------|------|------|------|
| 普通成员普通消息 | 缓存，不回复 | 缓存，不回复 | 缓存，不回复 |
| 唤醒词 | 立即回复 | 忽略 | 忽略 |
| 普通成员 @ | 立即回复 | 立即回复 | 忽略 |
| owner @ | 立即回复 | 立即回复 | 立即回复 |
| owner 普通消息 | 缓存 | 缓存 | 忽略 |
| owner 指令 | 放行 | 放行 | 放行 |
| 非 owner 指令 | 默认放行 | 静默拦截 | 静默拦截 |

“缓存”仅指内存中的最近消息，用于之后被 @ 时补上下文；不写插件状态文件。

## 6. 状态与并发

- 模式按群持久化到 `data/plugin_data/astrbot_plugin_activity_control/state.json`。
- 聊天缓存只存在 `deque`，每个群最多 200 条。
- 每个群同一时间只允许一条回复在途（`_tokens` / `_busy_until`）。
- 回复完成后消费本轮消息；生成期间新来的消息留给下一轮。
- 模式切换会让旧 token 失效，在途的合成回复发送前会被丢弃。

## 7. 已知边界

1. AstrBot 的 `CronMessageEvent` / `on_llm_request` API 需要 `>=4.9.0`。
2. QQ 官方等平台如果限制主动消息，定时回复可能发送失败；这是平台能力边界。
3. 模型输出被其他插件改成图片/语音时，本插件记录的是模型原始文本；聊天缓存仍能保持上下文。
4. 非 owner 指令在潜水/睡眠下会被 `stop_event` 静默拦截，不会提示“权限不足”。
5. 同时启用其他“主动接话”插件可能产生重复回复，建议只保留一个主动回复方案。
