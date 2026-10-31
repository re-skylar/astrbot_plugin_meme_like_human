# 「像人一样玩梗」插件 · 设计全面评估与审计

> 审计时间：2026-10-31 20:10
> 审计对象：交接方案（memory 中 `mem_20261031_9f156a2ebb99` + 后续修订）
> 审计方法：源码实测 + 服务器实机验证 + 成本测算
> 结论：**原方案有 1 个致命冲突、3 个定位偏差、7 个工程缺陷，需修正后实施**

---

## 一、致命冲突：命令名已被占用

**实测**（服务器 `/opt/qqbot/astrbot/data/plugins/` 现有 12 个插件）：

| 插件 | 已注册命令 |
|---|---|
| **memewiki** | **`梗` `梗档案` `网搜` `黑话`** |
| affection | mystatus / reset_all_emotions / set_emotion … |
| diary_qzone | 写日记 / 日记状态 / 日记预览 |
| humanlike | 拟人 |
| livingmemory | lmem |
| owner_guard | owner |
| parser | 关闭解析 / 开启解析 / 登录B站 |
| pokepro | 戳 |
| sticker_pack | 存表情 / 删表情 / 图库 / 来一张 / 发收藏 |

**问题**：原方案的核心命令 `/梗` 与 `/黑话` **已被 memewiki 全部占用**。
AstrBot 多插件注册同名命令的行为未定义（可能只触发其一，可能都触发），**必须改名**。

**修正**：

| 用途 | 新命令 | 理由 |
|---|---|---|
| 查梗 | `/玩梗 <词>` | 与 memewiki 的 `/梗` 区分 |
| 梗库管理 | `/玩梗库` | 备用 |
| 黑话列表 | `/黑话库` | 与 memewiki 的 `/黑话` 区分 |
| 黑话增删 | `/黑话库 add\|del\|global` | 子命令 |

> 注：AstrBot **支持中文命令名**（memewiki 已在用 `梗`、`写日记` 等），无需担心。

---

## 二、定位偏差（最需要修）

### 2.1 【严重】「查梗命令」不是「像人一样玩梗」

原方案功能 A 只有一个 `/梗 <词>` 查询命令。**这本质上是 memewiki 的功能子集**，装上后与 memewiki 重复，用户没有理由再装一个。

「像人一样玩梗」的真正差异点应是：**让 bot 在自然对话中听懂并用上梗**，而不是等用户来查。

**修正方案**：

| 触发方式 | 说明 | 优先级 |
|---|---|---|
| **被动注入（主）** | `on_llm_request` 时检测用户消息里命中的梗 → 查库 → 把**极简释义**注入 system_prompt，模型自然理解 | ⭐核心 |
| 主动查询（辅） | `/玩梗 <词>` 返回完整释义 | 辅助 |
| 查库未命中 | 走 DeepSeek 搜索 → 写回 → 本次注入 | 自学习 |

### 2.2 【中】功能 A / B 耦合过紧

两个功能共享 Star 类与数据层，一个出错会拖垮另一个。

**修正**：各自独立开关、独立 try/except、独立降级；A 失败不影响 B。

### 2.3 【中】注入冲突未处理

服务器上 humanlike / livingmemory / memewiki / 本插件都会改 `req.system_prompt`。追加顺序不确定，且历史上出过 **prompt_tokens=37807 淹没 204 字提示词** 的事故（见 SHARED-STATE 5.2）。

**修正**：
- 注入块加**唯一边界标记**：`<!-- meme-like-human:BEGIN -->` … `<!-- END -->`
- **硬性长度上限**：梗注入 ≤ 120 字，黑话注入 ≤ 150 字
- 配置项可关

---

## 三、功能 A（全网梗）审计

### 3.1 【重要】词库应打包进插件，而非首次运行在线拉取

**原方案**：首次运行在线拉取 6668 条。
**已实测推翻**：
- cnmeme.wiki：根域名 30s、API 18.7s、IPv6 only、cron 跑 2 次全失败
- 服务器 `raw.githubusercontent.com`：HTTP 000 不可达
- 本地 `raw.githubusercontent.com`：依赖 Clash 代理，代理抖动时 0/5 成功

**新方案（因为词库改由我们自己生成）**：
- 我们自己生成的词库是 **AI 原创文本 + CC0**，**无版权问题**，可自由打包
- 10,000 条 ≈ **2MB JSON**（gzip 约 700KB）→ 远低于 zip 16MB 限制
- **打包做内置快照**，零网络依赖，开箱即用
- 同时提供**可选在线更新**（jsDelivr 主 / GitHub API 备）

### 3.2 【严重】首次拉取不能放在 `initialize()` 里

AstrBot 加载插件有超时限制，`initialize()` 内做 2MB 下载会**导致插件加载失败**。

**修正**：
- `initialize()` 只做本地初始化（读内置词库、建表）
- 在线更新用 `asyncio.create_task` 后台跑，失败静默
- 保存 task 引用，`terminate()` 时取消

### 3.3 【中】缓存表设计缺失字段

原方案只有「查询+写回」。缺少：

| 缺字段 | 用途 |
|---|---|
| `hit_count` | LRU 淘汰依据 |
| `last_hit_at` | 淘汰 + 新鲜度 |
| `source` | 区分 `builtin` / `search` / `jargon`，词库更新时不冲掉自学习数据 |
| `updated_at` | 词库版本对比 |

**修正**：词库更新时按 `(slug, source)` 合并，`source=search` 的条目**永不覆盖**。

### 3.4 【严重】成本与限频完全缺失

每次 DeepSeek 搜索 ¥0.0003。若群内刷屏或有人恶意刷梗：
- 100 次/天 = ¥0.03（可接受）
- 但无上限时，10,000 次/天 = ¥3/天 = **¥90/月**

**修正（必须）**：

| 限制 | 默认值 |
|---|---|
| 每会话每分钟最多搜索 | 3 次 |
| 全局每日最多搜索 | 200 次 |
| 结果缓存 TTL | 30 天 |
| 未命中记录（避免重复搜同一个空结果） | 7 天 |

---

## 四、功能 B（群内黑话）审计

### 4.1 【最严重】LLM 成本会爆炸

原方案：`learn_frequency=40`，即每 40 条消息触发 1 次提取；每个候选词触发 3 次推断 LLM。

**测算**（deepseek-chat，输入 ¥1/M token、输出 ¥2/M token）：

| 场景 | 日消息量 | 提取次数/天 | 提取 token/天 | 推断次数/天 | 日成本 |
|---|---|---|---|---|---|
| 1 个 50 人群 | 1,000 | 25 | ≈5 万 | ≈30 | ≈¥0.15 |
| 5 个群 | 5,000 | 125 | ≈25 万 | ≈150 | ≈¥0.75 |
| **20 个群** | 20,000 | **500** | **≈100 万** | ≈600 | **≈¥3/天 ≈ ¥90/月** |

**修正（必须加三重上限）**：

| 限制 | 默认值 | 说明 |
|---|---|---|
| 每群每日提取次数上限 | 30 | 防刷屏触发 |
| 单次提取候选上限 | 10 | 原方案 30 太多 |
| 每群每日推断次数上限 | 20 | 推断最贵 |
| 提取 prompt 消息条数上限 | 40 | 防止 learn_frequency 设大后 prompt 爆炸 |

> **保留** MaiBot 的阈值设计（4/8/25/100）——它本身就是很好的成本控制：只有高频词才推断。

### 4.2 【已确认】阈值与「随机丢弃证据」

| 项 | 交接文档 | 源码实测 | 采用 |
|---|---|---|---|
| 阈值 | 2/4/8/12/24/60/100 | **`[4, 8, 25, 100]`** | ✅ 用 4 档 |
| 随机丢一半证据 | 有 | **全文 grep `random/shuffle/sample/half` 零命中** | ❌ 不实现 |

`_should_infer_meaning` 完整逻辑：
```
manual → False
is_complete → False
count < 4 → False
count <= last_inference_count → False
next_t = 第一个 > last 的阈值
return count >= next_t
```
`is_complete = count >= 100`

### 4.3 【缺失】人名过滤没设计出来

MaiBot 有 `person_name_filter`，但原方案没写**怎么实现**。否则群友昵称会被当黑话学习。

**修正**：
- 从群消息的 `sender_name` 累积**本群成员昵称集合**（内存，最多 500 个）
- 候选词若命中昵称集合（包含/被包含）→ 丢弃
- 另加：候选词若 = 常见中文词（用内置高频词典）→ 丢弃

### 4.4 【缺失】「听懂」还是「会用」

原方案只说「注入 system_prompt 供理解」。
但「像人一样玩梗」应该**适度会用**。全都会用会显得假，全都不会用等于没学。

**修正**：注入时区分两档
- `is_jargon && count >= 8` → 标注「可偶尔自然使用」
- `is_jargon && count < 8` → 标注「仅需理解，不要主动使用」

### 4.5 保留的优秀逻辑（来自 jargon，已重写）

| # | 逻辑 |
|---|---|
| 1 | 纯机械子串匹配（归一化后 `in`），**不调 LLM、不用向量** |
| 2 | 权重打分 `score = count - index*0.01` |
| 3 | 证据回溯 `source_id` → 本消息+前后各 1 条，≤200 字 |
| 4 | `no_info` 不瞎猜（更新 `last_inference_count` 后放弃） |
| 5 | `created_by=manual` 的词条 AI 永不覆盖 |
| 6 | 会话作用域 `chat_scopes` + `is_global` |
| 7 | 专用分析模型 `provider_id` |
| 8 | 每会话一把学习锁 + 全局 Semaphore 限推断 |
| 9 | 宽松 JSON 解析（去围栏 + 截首尾括号） |
| 10 | 黑名单群 + 不学自己发的消息 |

---

## 五、工程风险

| # | 风险 | 修正 |
|---|---|---|
| 1 | `asyncio.create_task` 泄漏（插件卸载时任务仍在跑） | 保存 task set，`terminate()` 全部 cancel + await |
| 2 | SQLite 异步并发写冲突 | `WAL` 模式 + 用 `asyncio.to_thread` 包装写操作 |
| 3 | DeepSeek key 获取：`provider_sources` 结构可能变 | 三级回退：插件配置 → 自动探测 → 禁用功能A（降级为纯本地） |
| 4 | 多平台 `event.message_obj.group_id` 不存在 | try/except 回退到 `umo` 解析 |
| 5 | `context.register_web_api` 旧版没有 | try/except 降级，不报错 |
| 6 | `_conf_schema.json` 类型写错会导致插件加载失败 | 严格对照官方格式，逐项本地校验 |
| 7 | 中文命令与 memewiki 冲突 | 已改名（见第一节） |

---

## 六、隐私与合规

| 项 | 处理 |
|---|---|
| 聊天记录会上传给 LLM 分析 | **README 必须显著声明**，配置项可关闭学习 |
| 黑名单群 | 默认支持逐个群屏蔽 |
| 不学自己发的消息 | 默认开启 |
| 不收集 QQ 号/昵称入库 | 只存证据片段文本，昵称仅用于过滤，**不落库** |
| 许可证 | 插件 **AGPL-3.0**（与官方模板一致） |
| 词库 | **CC0**（AI 原创，无第三方转载） |
| 参考来源致谢 | README 注明「双路对照思路来自 MaiBot(GPL-3.0)」，代码全新编写 |

---

## 七、修正后的最终设计

```
用户消息
  │
  ├─【A. 全网梗】被动注入为主 + 命令为辅
  │   ①内置词库 10,000 条（打包，本地 SQLite，0ms）
  │   ②on_llm_request 检测消息中命中的梗 → 注入 ≤120 字极简释义
  │   ③未命中 → DeepSeek 搜索(0.4s) → 写回缓存 → 本次注入
  │   ④/玩梗 <词> 主动查询完整释义
  │   ⑤后台可选：从 jsDelivr 拉词库更新（不阻塞）
  │
  └─【B. 群内黑话】被动学习 + 注入
      ①deque(200) 缓冲消息，攒够 40 条触发提取
      ②LLM 提取候选（≤10 个，带 source_id）
      ③过滤：单字 / 昵称 / 常见词 / 黑名单群
      ④阈值 4/8/25/100 触发双路对照（≤20 次/群/天）
      ⑤确认黑话 → 入库（manual 永不被覆盖）
      ⑥注入 ≤150 字，按 count 分「会用」/「仅理解」两档
```

### 文件结构

| 文件 | 行数 | 职责 |
|---|---|---|
| `main.py` | ~280 | Star 主类、命令、事件钩子、注入编排 |
| `meme_kb.py` | ~230 | 功能A：词库加载/查询/DeepSeek/缓存/更新 |
| `jargon_miner.py` | ~240 | 功能B：提取 + 双路对照推断 |
| `jargon_store.py` | ~170 | 黑话 SQLite 存储 |
| `jargon_match.py` | ~80 | 机械匹配 + 权重排序 |
| `limits.py` | ~70 | 限频/预算控制（新增） |
| `data/memes.json` | — | 内置词库快照（~2MB） |
| `metadata.yaml` / `_conf_schema.json` / `README.md` / `LICENSE` | — | 配置与文档 |

### 配置项（新增限流类）

| 分组 | 项 | 默认 |
|---|---|---|
| A | `meme_enabled` / `inject_enabled` / `deepseek_api_key` / `search_daily_max`(200) / `search_per_min`(3) / `inject_max_chars`(120) | — |
| B | `jargon_enabled` / `learn_frequency`(40) / `extract_max`(10) / `infer_daily_max`(20) / `learn_daily_max`(30) / `inject_max`(5) / `inject_max_chars`(150) / `provider_id` / `listen_private`(false) / `blacklist_groups` | — |
| 通用 | `auto_update_dict`(true) / `dict_update_url` | — |

---

## 八、API 实测验证结果（已在 AstrBot v4.29.3 容器内核验）

| # | 验证项 | 结果 | 证据 |
|---|---|---|---|
| 1 | `@filter.on_llm_request()` 签名 | ✅ **`async def f(self, event: AstrMessageEvent, req: ProviderRequest)`** | `builtin_stars/astrbot/main.py:320` 实际用法 |
| 2 | `req.system_prompt` 字段 | ✅ **`system_prompt: str = ""`** | `core/provider/entities.py:113` |
| 3 | `req.prompt` / `req.contexts` | ✅ 存在，`contexts: list[dict]`（OpenAI 格式） | `entities.py:95-112` |
| 4 | 中文命令名 | ✅ 支持，`command_name: str` 无限制 | `filter/command.py:36`；memewiki 已在用 `梗`/`写日记` |
| 5 | 命令组/子命令 | ✅ `CommandGroupFilter(group_name)` 支持嵌套 | `filter/command_group.py:14` |
| 6 | 剩余参数接收 | ✅ `GreedyStr` 类型标注可接收全部剩余文本 | `filter/command.py:15` |
| 7 | 数据目录 API | ✅ `get_astrbot_data_path()`；`StarTools.get_data_dir()` 亦可 | `core/utils/astrbot_path.py:38`；实测可导入 |
| 8 | `@register` 装饰器 | ⚠️ **已废弃**（`[DEPRECATED]`），继承 `Star` 即自动识别 | `core/star/register/star.py` |
| 9 | `context.register_web_api` | ✅ 存在，但需 `try/except` 兼容旧版 | jargon 插件实测用法 |

### 仍需编码时验证

| # | 项 | 影响 |
|---|---|---|
| 1 | `event.message_obj.group_id` 在 aiocqhttp 的实际取值 | 黑话作用域 |
| 2 | 插件 `initialize()` 的超时时长 | 决定能否放下载 |
| 3 | `req.system_prompt` 为空串时追加行为 | 注入边界 |
| 4 | 10,000 条词库加载耗时/内存 | 性能（预计 100ms / 3-5MB） |

---

## 九、审计结论

| 维度 | 原方案 | 审计后 |
|---|---|---|
| 命令冲突 | ❌ `/梗` `/黑话` 均被占用 | ✅ 改 `/玩梗` `/黑话库` |
| 产品定位 | ⚠️ 等于 memewiki 子集 | ✅ 被动注入为主，差异化明显 |
| 词库策略 | ❌ 在线拉取（源已死） | ✅ 打包自研 CC0 词库 + 可选更新 |
| 加载安全性 | ❌ 下载放 initialize 会失败 | ✅ 后台任务 |
| 成本控制 | ❌ 完全缺失 | ✅ 三重限流 + 预算上限 |
| 人名过滤 | ❌ 未设计 | ✅ 群成员昵称集合 |
| 注入冲突 | ⚠️ 未处理 | ✅ 边界标记 + 硬长度上限 |
| 任务泄漏 | ⚠️ 未处理 | ✅ task set + cancel |
