# jev 评估 Prompt 构成说明（含完整示例）

> jev = Cloudflare Clef 决策模型（`@cf/cloudflare/clef`，Workers AI API）。
> 一次 Clef Call = **state + 5 问**，无额外 system prompt。

## 1. 总览

| 组成 | 内容 | 构造位置 |
|---|---|---|
| state | 上下文状态（当前输入 + 最近对话历史） | `JevAssessor::BuildJevState` |
| q1 | noul：意图是否清晰 | `JevAssessor::BuildJevRequest` |
| q2 | noul：上下文是否充分 | 同上 |
| q3 | choice：技能选择（含 `no_skill`） | 同上（选项集由 ChannelStart 动态构造） |
| q4 | noul：query 是否可直接作为任务内容转发 | 同上 |
| q5 | noul：任务数是否为 1 | 同上 |

## 2. state 构造规则（BuildJevState）

| historyTurns 形态 | state 内容 |
|---|---|
| 空（TEXT 模式） | 纯当前输入 `userInput` |
| 快照 JSON 且 `RecentTurns` 空数组（FD 首轮） | 纯当前输入（不注入包装噪音） |
| 快照 JSON 且 RecentTurns 非空 | `最近对话：\n用户：U1\n助手：R1\n…\n用户当前输入：{userInput}` |
| 非 JSON（旧格式兜底） | `最近对话：\n{原样透传}\n用户当前输入：{userInput}` |

历史段按 **2000 字符先截断、再追加当前输入**——当前输入永不截断。

## 3. 5 问原文（BuildJevRequest）

| 题 | 题型 | 原文 |
|---|---|---|
| q1 | noul | 结合上下文，判断用户这句话是否意图清晰（无错别字、无转录缺字、无无法理解的表述）。 |
| q2 | noul | 当前输入提供的上下文是否充分，足以直接处理而无需向用户追问补充。 |
| q3 | choice | 如果这个问题需要调用技能，应选择哪个技能？ |
| q4 | noul | 用户问题本身是否可以直接作为所选技能的任务内容转发，无需做额外的信息补全。 |
| q5 | noul | 该问题对应需要完成的任务数量是否为 1。 |

### q3 选项集

动态构造：`no_skill` + 候选技能（`IsJevForwardableSkill` 白名单 ∩ 设备品类过滤）。
选项文本 = 各技能 SKILL.md 的 description（为空时用技能名兜底）：

- **no_skill**：无需技能，可直接回答
- **home-habits-query**：处理只读的家电习惯与已有自动模式查询。只要用户的主谓关系是查询或检查设备习惯及已有配置，即使宾语里出现设备名和打开、关闭、调节、自动、设置、离家、回家等动作词，也应选择本 Skill。典型正例：“想查一下目前有没有根据作息设置自动开关的习惯”“check what automatic modes are currently set up”“التحقق مما إذا كانت الأجهزة تعمل على التشغيل أو الإيقاف التلقائي بناءً على العادات”。不处理要求现在改变设备状态的实时控制；查询与实时控制并列时，本 Skill 只承接查询部分。
- **iot-control**：仅处理用户明确要求系统现在执行并改变设备状态的一次性实时控制，如“打开客厅灯”“关闭空调”“把温度调到26度”。只读判断准则：只要用户的主谓关系是查询或检查设备习惯及已有配置，该目标就属于查询通道；即使宾语里出现设备名和动作词，CChannel 也必须不选 iot-control、不得为其生成 task。查询与实时控制并列时，仅承接明确要求执行的控制部分。不处理新增或修改定时、场景和自动化规则。
- **general-query**：处理未指定App/平台的通用信息问答（如时间、天气、周边、列车、航班、新闻资讯等实时信息和通用知识），推荐、写作、攻略制定、总结等内容生成与规划意图；无需查询信息的闲聊（如问候、寒暄、致谢、告别）由 SysAgent 直接回答，不调用本 Skill；不处理任何执行类意图，如导航等。
- **liantong-query**：仅处理联通相关查询，如联通云手机、联通宽带、套餐、号码和业务规则；不处理非联通问题，不处理查询话费、充值话费意图。
- **film-tv**：仅处理影视内容相关的推荐、搜索和信息查询类意图，包括电影、电视剧、综艺等内容的推荐、片名搜索等，以及播放影视等执行类意图；不处理推荐音乐、查询演唱会歌单意图，不处理单纯查询影视评分口碑、剧情解析、导演作品、演员信息、续集等影视知识问答。
- **gui-control**：处理需要在移动端完成的具体操作/执行流程类意图，包括导航、打车、外卖、购物、订票、订酒店、播放音视频、设定提醒（除吃药提醒外）、查询话费、充值话费、开发票等，以及明确指定App/平台的意图（如在某App内搜索xx）。不处理未指定App/平台的各类查询和推荐意图，不处理健康执行类意图（挂号、吃药提醒、健康档案操作等）。
- **health-query**：仅处理健康咨询类意图，如症状、疾病、用药知识和健康建议问答，以及挂号、挂号查询、吃药提醒、吃药提醒查询、药品信息获取、健康档案查询和创建等健康执行类意图。

## 4. 完整示例

**场景**：设备品类 1，同会话第 2 轮。上一轮用户说"打开客厅的灯"，系统回复"已为您打开客厅的灯"。

### 实际提交给 jev 的内容

```
state：
最近对话：
用户：打开客厅的灯
助手：已为您打开客厅的灯
用户当前输入：把亮度调低一点

q1（noul）：结合上下文，判断用户这句话是否意图清晰（无错别字、无转录缺字、无无法理解的表述）。

q2（noul）：当前输入提供的上下文是否充分，足以直接处理而无需向用户追问补充。

q3（choice）：如果这个问题需要调用技能，应选择哪个技能？
选项：
  no_skill —— 无需技能，可直接回答
  home-habits-query —— 处理只读的家电习惯与已有自动模式查询。……
  iot-control —— 仅处理用户明确要求系统现在执行并改变设备状态的一次性实时控制，如"打开客厅灯""关闭空调""把温度调到26度"。……
  general-query —— 处理未指定App/平台的通用信息问答……（内容生成与规划意图；闲聊由 SysAgent 直接回答）
  liantong-query —— 仅处理联通相关查询……
  gui-control —— 处理需要在移动端完成的具体操作/执行流程类意图……
  health-query —— 仅处理健康咨询类意图……
  （注：本例设备品类 1，film-tv 为品类 3 技能，不在候选集内）

q4（noul）：用户问题本身是否可以直接作为所选技能的任务内容转发，无需做额外的信息补全。

q5（noul）：该问题对应需要完成的任务数量是否为 1。
```

### jev 返回（示意数值）→ 决策

```
q1 = 0.95
q2 = 0.90   （"它"指灯，历史里有，无需追问）
q3 = iot-control, confidence = 0.97
     概率: iot-control=0.97; no_skill=0.01; gui-control=0.01; …
q4 = 0.85
q5 = 0.98
```

全部过门槛（q3 置信度 ≥ 0.8，noul value ≥ 0.8）→ **DirectForward**：

- 跳过 LLM 选 skill，直接下发任务 `{"command": "把亮度调低一点", "query_language": "zh-CN"}` 给 iot-control
- RouteDecision 帧带 `DecisionSource:"jev"` + `ResponsePolicy: SHORT_WAIT`

## 5. 决策规则速查

```
ChoicePass(q3) 不达标（confidence < 0.8 / 无置信度）→ kNone（现有 LLM 路由）
q3 == no_skill 且 q1、q2 ≥ 0.8                 → DirectReply（LLM 直接回答）
q3 ∈ 候选技能 且 q1、q4、q5 ≥ 0.8              → DirectForward（直接转发技能）
其余（含评估失败/超时/答案缺失）                → kNone（fail-closed 回退 LLM）
```

| decision_source | ResponsePolicy | 含义 |
|---|---|---|
| jev | DIRECT_RESPONSE | DirectReply |
| jev | SHORT_WAIT | DirectForward（白名单 4 项） |
| jev | LONG_WAIT | DirectForward（委托类 3 项） |
| other（无字段） | 任意 | 非 jev 路径（LLM/PreMatch/RAG 拒答） |

## 6. 备注

- **无 system prompt**：问题文案与选项描述即全部指令。
- **queryLanguage 不进 prompt**：仅用于直转成功时构造任务参数（`BuildJevTaskParams`）。
- **技能描述是调优杠杆**：q3 的技能认知完全来自 SKILL.md 的 description 文案，改文案即可影响选择准确率（无需改代码，但需重新评估效果）。
- **模型波动**：Clef 决策模型存在 ±0.2 左右数值波动，阈值边缘用例有非确定性。
