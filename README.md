# 活跃度控制 · 交付包

GitHub：<https://github.com/hutaodantuiren/astrbot_plugin_activity_control>

这是给 AstrBot 用的群聊参与度控制插件，插件目录名：

```
astrbot_plugin_activity_control
```

## 先看效果

| 群状态 | owner 发 `/活跃` | owner 发 `/潜水` | owner 发 `/睡眠` |
|--------|------------------|------------------|------------------|
| 普通消息 | 每 2 分钟读取并回复 | 不读、不回 | 不读、不回 |
| 唤醒词 | 立即读取并回复 | 不响应 | 不响应 |
| 任何人 @ | 立即回复 | 立即读取上下文并回复 | 非 owner 忽略 |
| owner @ | 立即回复 | 立即回复 | 立即回复 |
| owner 指令 | 执行 | 执行 | 执行 |
| 非 owner 指令 | 默认放行其他插件指令 | 静默拦截 | 静默拦截 |

## 安装（最短路径）

1. 解压压缩包，得到 `astrbot_plugin_activity_control/` 文件夹。
2. 把**这个文件夹**放到 AstrBot 的插件目录：

   ```
   <AstrBot>/data/plugins/astrbot_plugin_activity_control/
   ```

   放好后应存在：

   ```
   <AstrBot>/data/plugins/astrbot_plugin_activity_control/main.py
   ```

3. 重启 AstrBot，或在 WebUI 插件管理里重载。
4. 打开 WebUI → 插件管理 →「活跃度控制」：
   - 在 `owner_ids` 填 owner 的 QQ 号；或者
   - 在 AstrBot 全局配置的 `admins_id` 填 owner 的 QQ 号。
5. owner 在群里发送 `/活跃`。

> `owner` 指 AstrBot 管理员，不是 QQ 群主。  
> 默认 `owner_source=merge`，会同时读取 AstrBot 的 `admins_id` 和插件 `owner_ids`。

## 详细文档

- [插件完整说明](astrbot_plugin_activity_control/README.md)：指令、三态行为、owner 识别、全部配置、排错。
- [设计说明](docs/DESIGN.md)：为什么这么实现、事件流、状态持久化、边界条件。

## 最低要求

- AstrBot `>= 4.9.0`
- 一个能正常收发群消息的 QQ 适配器（NapCat / aiocqhttp / QQ 官方等）
- 不需要额外 Python 依赖

## 目录

```
activity-control/
├─ astrbot_plugin_activity_control/   # 直接复制到 data/plugins 的插件目录
├─ docs/设计说明.md
├─ tests/                             # 纯逻辑单元测试（不参与运行）
├─ README.md
└─ LICENSE
```

## 测试

在项目根目录执行：

```bash
python -m unittest discover -s tests -v
```

测试覆盖 owner 合并、唤醒词匹配、三态判定、聊天记录裁剪和状态持久化。
