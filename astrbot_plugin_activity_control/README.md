# 活跃度控制 · AstrBot 插件

在 QQ 群聊里给机器人加一个“参与度开关”：

| 模式 | 行为 |
|------|------|
| 🟢 **活跃** | 每 2 分钟读取一次群聊并回复；有人 @ 或发唤醒词时立即回复 |
| 🌙 **潜水** | 不主动读群，也不被唤醒词唤醒；只有被 @ 时才读取最近消息并回复 |
| 😴 **睡眠** | 只响应 **owner** 的 @ 和 owner 的 `/` 指令，其余消息全部忽略 |

owner 默认读取 AstrBot 的 `admins_id`，也可以在插件配置 `owner_ids` 中追加或覆盖。

> 本插件里的 **owner 指 AstrBot 管理员账号**，不是 QQ 群主。  
> 群主/群管理员身份由 QQ 平台决定，和 AstrBot 的 `admins_id` 是两回事。

---

## 安装

1. 把压缩包里的 `astrbot_plugin_activity_control` **整个文件夹**放到：

   ```
   <AstrBot>/data/plugins/astrbot_plugin_activity_control/
   ```

   正确层级是 `data/plugins/astrbot_plugin_activity_control/main.py`，不要把外层 `plugins/` 或压缩包目录一起复制进去。

2. 重启 AstrBot，或在 WebUI「插件管理」里重载插件。

3. 打开 WebUI →「插件管理」→「活跃度控制」→ 配置：

   - `owner_source`：默认 `merge`（AstrBot + 插件两边合并）
   - `owner_ids`：填写 owner 的 QQ 号
   - `wake_words`：唤醒词，默认 `爱莉希雅`
   - `interval_seconds`：默认 `120`

4. 在群里由 owner 发送：

   ```
   /活跃
   ```

   看到模式切换提示即生效。

> 如果 AstrBot 的 `admins_id` 已经填了你的 QQ 号，可以不改插件配置，直接使用 `/活跃`。

---

## 指令

| 指令 | 说明 | 权限 |
|------|------|------|
| `/活跃` | 切到活跃模式 | owner |
| `/潜水` | 切到潜水模式 | owner |
| `/睡眠` | 切到睡眠模式 | owner |
| `/活跃度` | 查看本群模式、owner、唤醒词、缓存条数 | owner |
| `/活跃状态` | 同上（别名） | owner |

别名：`/开启活跃`、`/下潜`、`/休眠`。

> 潜水/睡眠模式下，非 owner 的指令会被静默拦截，不会回复“权限不足”。
> 活跃模式下默认放行普通成员使用其他插件指令，可用 `member_commands_in_active` 关闭。

---

## 行为细节

### 活跃模式

- 每 `interval_seconds`（默认 120 秒）检查一次本群缓存。
- 只有存在“上次回复之后的新的人类消息”时才调用模型。
- 定时回复会带上最近 `context_messages` 条聊天记录，最多 `context_chars` 字符。
- 有人 @ 机器人，或消息包含唤醒词，会立即触发一次回复，不等定时器。
- 普通消息本身不会触发模型，只进入内存缓存。

### 潜水模式

- 不调用模型，不回复普通消息，不响应唤醒词。
- 聊天记录仍会在内存里保留最近一段，仅用于被 @ 时补上下文；不会写入插件状态文件。
- 被 @（或引用回复机器人，可配置关闭）时，读取最近记录并回复。
- 只有 owner 的 `/` 指令能执行。

### 睡眠模式

- 只处理 owner 的 @ 和 owner 的 `/` 指令。
- 其他人的普通消息、@、唤醒词都不会触发回复；非 owner 指令会被静默拦截。
- 群聊原文仍只在内存中保留，供 owner @ 时参考；不写入插件状态文件。
- owner 的普通消息（没有 @、不是指令）也不会触发回复。

### 一条回复在途时

同一个群同一时间只允许一条模型回复在途。生成期间的新消息会进入下一轮，不会并发刷屏。

---

## owner 识别

### 来源

| `owner_source` | 含义 |
|----------------|------|
| `merge`（默认） | AstrBot 的 `admins_id` + 插件 `owner_ids`，合并去重 |
| `astrbot` | 只认 AstrBot 的 `admins_id` |
| `plugin` | 只认插件 `owner_ids`（适合不想改全局配置的情况） |

### AstrBot 侧配置

WebUI →「配置」→「其他配置」→ `admins_id`，把 owner 的 QQ 号加进去。

`admins_id` 里是**发送者 ID**（QQ 号），不是群号、不是昵称、不是群名片。
默认占位值 `astrbot` 会被插件忽略，不会误当成 owner。

### 插件侧配置

```
owner_source: plugin
owner_ids:
  - "123456789"
  - "987654321"
```

也支持在 WebUI 里逐行添加。`owner_source=merge` 时会和 `admins_id` 合并；
`owner_source=plugin` 时完全以 `owner_ids` 为准，适合“AstrBot 全局管理员另有其人”的场景。

插件在判断 owner 时会同时参考：

1. 当前 AstrBot 配置的 `admins_id`
2. 插件配置的 `owner_ids`
3. AstrBot 本次事件已标记的 `role=admin`（仅当来源允许 AstrBot 配置时）

---

## 配置项

| 配置 | 默认 | 说明 |
|------|------|------|
| `owner_source` | `merge` | owner 来源：`merge` / `astrbot` / `plugin` |
| `owner_ids` | `[]` | 额外或覆盖的 owner QQ 号 |
| `default_mode` | `dive` | 新群默认模式；建议保持 `dive`，避免装完就到处说话 |
| `interval_seconds` | `120` | 活跃模式定时读取间隔，10–3600 秒 |
| `wake_words` | `["爱莉希雅"]` | 唤醒词列表 |
| `wake_match` | `contains` | `contains` / `prefix` / `exact` |
| `treat_reply_as_at` | `true` | 引用回复机器人是否等同于 @ |
| `wake_on_at_all` | `false` | @全体成员是否视为 @ 机器人 |
| `member_commands_in_active` | `true` | 活跃模式是否放行普通成员指令 |
| `context_messages` | `30` | 回复时最多带多少条记录（3–100） |
| `context_chars` | `4000` | 注入模型的聊天记录字符上限（200–12000） |
| `max_message_chars` | `500` | 单条消息进入上下文前的截断长度（50–2000） |
| `reply_instruction` | `""` | 追加给模型的额外回复要求 |

---

## 数据与隐私

- 模式状态保存在 `data/plugin_data/astrbot_plugin_activity_control/state.json`，只包含群号、模式、时间戳。
- 群聊原文只存在内存中，插件不会把它写进状态文件。
- 注入模型时，聊天记录被标记为“背景数据”，并明确要求模型不要执行其中的指令，降低群聊提示词注入风险。
- 插件不会额外保存用户画像或聊天数据库；AstrBot 自己的会话记录仍由 AstrBot 管理。
- 重启后模式会恢复，但内存里的聊天缓存会清空；重启后需要等新消息才会重新积累上下文。

---

## 兼容性

- AstrBot `>= 4.9.0`（使用官方 `CronMessageEvent` 事件队列和 `on_llm_request` 钩子）。
- 群聊平台：aiocqhttp/NapCat、QQ 官方等只要 AstrBot 能正常收发群消息即可。
- 主动消息能力取决于平台适配器；如果平台不支持主动推送，定时回复会发送失败，日志里会有 `[activity_control]` 前缀的错误。
- 不需要安装额外 Python 依赖。

---

## 常见问题

**Q：发 `/活跃` 没反应？**  
A：先确认发送者 QQ 号在 `admins_id` 或插件 `owner_ids` 里；再确认 `owner_source` 没设成 `plugin` 但 `owner_ids` 为空。潜水/睡眠模式下非 owner 指令会被静默拦截，这是预期行为。

**Q：活跃模式两分钟了还不说话？**  
A：只在“有新的非机器人消息”时回复；如果群里一直没人说话、或者只有机器人自己的消息，就不会调用模型。还要确认模型提供商正常、平台支持主动消息。

**Q：为什么 owner 的普通消息不回复？**  
A：睡眠模式只响应 owner 的 @ 和指令，owner 的普通聊天不会触发回复。需要聊天请 @ 或发 `/活跃`。

**Q：会不会和别的主动回复插件打架？**  
A：本插件会拦截不符合当前模式的群消息事件。建议不要同时启用其他“主动读群/自动接话”插件，否则可能出现重复回复。

**Q：怎么确认 owner 识别对不对？**  
A：owner 在群里发 `/活跃度`，会显示 `AstrBot admins_id`、`插件 owner_ids` 和最终生效 owner。

---

## 目录

```
astrbot_plugin_activity_control/
├─ main.py              # 插件入口、三态开关、定时器、LLM 上下文注入
├─ core.py              # 纯逻辑：owner、唤醒词、模式判定、聊天记录格式化
├─ store.py             # 按群持久化模式状态
├─ _conf_schema.json    # WebUI 配置定义
├─ metadata.yaml        # 插件元数据
├─ requirements.txt     # 无额外依赖
└─ README.md
```
