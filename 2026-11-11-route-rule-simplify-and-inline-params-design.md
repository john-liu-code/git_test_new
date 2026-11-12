# 路由规则简化 + 决策同步提参 设计（修订）

> 日期：2026-11-11 ｜ 状态：设计草案待评审 ｜ 分支：`personal/ljw/dev-qwen3-jev-llm-ttseparation`
> 修订对象：`2026-11-06-llm-route-memory-transition-design.md` 的 §1/§2/§3 相关部分
> 基线：`2f8f3ea`（含远端 304c9e1 承接语优化、34cc8b0 按路由前缀提前生成承接语）

## 0. 背景与目标

四项改动（用户已确认）：

1. **Q5 题干极性反转**：从"任务数量是否为 1（Y=单任务）"改为"任务数量是否大于 1（Y=多任务）"——q1/q2/q5 三个"Y"统一为复杂信号；
2. **Q4 题干补指代**：增加"任务要素清晰，无'这个''那个'类不清晰指代"；
3. **路由规则简化重排**：q1/q2 先行（意图/记忆层）→ q5（任务数）→ q3/q4（技能层）；1N2Y 与 1Y 统一走澄清（带记忆澄清）；
4. **决策同步提参**：Q4=N 时决策 LLM 在同一次输出中给出提参结果（params + self_score），替代"决策后再调 FP LLM 救援提参"的第二轮调用。

已确认的配套决策：

- 提参三 gate（解析/schema/防复读/自评分≥0.7）+ 白名单 6 技能 + fail-closed **全部保留**，校验对象改为决策输出的 params；
- 决策 LLM 超时 3000ms → **5000ms**（一次调用要判断+提参）；
- 澄清**不带 q3 方向**（q1=Y 时 q3 参考意义不大）：BuildClarifyPrompt 去技能名引导，改通用追问；
- 承接语侧与 34cc8b0 的"q1/q2 双 N 才生成"gate 天然一致，**不改动**；
- 帧口径连锁：`jev_q5` 值 1.0=Y=多任务（与旧"1.0=单任务"相反），文档/分析口径同步反转。

## 1. 决策 prompt 改动（RouteLlmAssessor）

五问 prompt 中 q5、q4 题干替换，输出格式段扩展提参要求：

```
q5：用户当前这个问题，对应需要完成的任务数量是否大于 1。Y=任务数大于1，N=任务数小于等于1

q4：用户当前这个问题本身，是否可以直接作为所选技能（即你在 q3 中选出的技能）的任务内容转发，
任务要素清晰，无需做额外的信息补全，如当前这个问题中没有类似"这个""那个"这类不清晰的指代。
Y=可直接转发，N=需要补全

q4=N 时，还需输出 params：把用户问题改写成所选技能可直接执行的内容。
- 技能为 iot-control：整理成编号命令文本（"1.xxx；2.xxx"，单条也带编号），只保留用户明确要求的动作；
- 其他技能：改写/补全成一条自包含的可直接处理文本（补全上下文中的指代，不臆造用户未提供的信息）；
- self_score：0-1 之间，你对该改写忠实于用户意图的自评分。

输出格式（字段顺序固定）：
{"q1":"Y或N","q2":"Y或N","q5":"Y或N","q3":"技能名或no_skill","q4":"Y或N"}
q4=N 时：{"q1":"Y或N","q2":"Y或N","q5":"Y或N","q3":"技能名或no_skill","q4":"N",
          "params":{"command"或"intent":"...","self_score":0.0-1.0}}
```

要点：

- q1/q2 前缀位置不变（34cc8b0 的 `ParseRouteTransitionGate` 正则不受影响）；
- params 为嵌套对象（iot-control 用 `command` 键、其他技能用 `intent` 键），与救援提参格式**同构**；
- q4=Y 时不输出 params（保持输出短）。

## 2. 解析改动（ParseRouteLlmResponse）

- q5 语义反转：`q5=="Y"` → `q5MultiTask=true`（字段重命名，原 `q5SingleTask` 删除）；
- 新增解析 `params` 子对象：仅 q4=N 时允许存在（q4=Y 时忽略）；解析出 `command`/`intent`（string）与 `self_score`（number），把 params 子对象**原样序列化回 JSON 字符串**存入 `LlmRouteAssessment.paramsJson` + `paramsSelfScore`；
- params 缺失/非法不判决策失败（fail-soft）：决策本身有效，仅应用层"提参直转"不可用 → 慢路径；
- 其余字段解析不变；任何字段缺失/非法 → valid=false（fail-closed）。

## 3. 新路由决策树（ResolveLlmRoute 重写）

```
!valid / q3 空 / q3 非 no_skill 且 ∉ 候选集     → kNone（慢路径）
q1=Y（无论 q2）                                  → kDirectClarify（withMemory=q2）★ 澄清优先
q1=N 且 q2=Y                                     → kDirectClarify（withMemory=true）   ★ 1N2Y 也澄清（用户定稿）
q1=N 且 q2=N：
    q5=Y（多任务）                                → kNone（慢路径）
    q5=N（单任务）：
        q3=no_skill                              → kDirectReply
        q3=技能：
            q4=Y                                 → kDirectForward（零 LLM 直转）
            q4=N                                 → kNone（应用层决策 params 提参直转）
```

与旧树的差异：

| 项 | 旧 | 新 |
|---|---|---|
| 判定顺序 | q3 先行（no_skill 分支）→ q5 → q1/q2 → q4 | q1/q2 先行 → q5 → q3/q4 |
| 1N2Y | no_skill→带记忆直答；技能→带记忆提参直转 | **统一带记忆澄清**（确认式追问） |
| 5N+1Y | 慢路径（旧裁决） | **澄清**（q1/q2 先行截获，已确认反转） |
| Q4=N 提参 | 决策后 FP LLM 救援（第二轮调用） | **决策同步提参**（同一次输出） |

四值决策语义不变（kNone/kDirectReply/kDirectForward/kDirectClarify），withMemory 标记语义不变（应用层先召回记忆）。

## 4. 应用层改动（ChannelStart 4b 块）

### 4.1 带记忆澄清（kDirectClarify + withMemory）

与现状一致：先 `FetchContextInfo(userInput, userName, "", true)` 注入 ctx.longMemory → 澄清 prompt 带记忆上下文（确认式追问："您说的是昨晚去的 XX 餐厅吗？"）。澄清 prompt 本身按 §6 改为**通用追问**（不带技能方向）。

### 4.2 决策同步提参（替代 FP 救援）

kNone 且 q5=N 且 q3∈白名单 6 技能 且 q4=N 时：

```
decision.paramsJson 存在？
  ├─ 是 → 三 gate 校验（复用 ValidateFpParamsRescue，喂 paramsJson 作为 LLM 输出）
  │       过 → 建 task（params=paramsJson 内容）→ 直转（decision_source=llm_fp，fp_params_ms 打点）
  │       不过 → fail-closed → 慢路径
  └─ 否 / 白名单外（如 home-habits-query）→ 慢路径
```

- 三 gate 校验函数复用 `ValidateFpParamsRescue(skillName, paramsJson, userInput, queryLanguage)`——paramsJson 与 FP 输出同构，校验逻辑零改动；
- 失败原因留档（`fp_params_fail_reason`，沿用现有字段与观测口径）；
- FP 救援的旧路径（`ShouldAttemptFpParamsRescueLlm` + `BuildFpParamsRescuePrompt` 调用）**删除**（决策同步提参全覆盖 4N 场景）；`BuildFpParamsRescuePrompt`/`ShouldAttemptFpParamsRescueLlm` 若无其他调用点则移除，`ValidateFpParamsRescue` 保留复用。

### 4.3 决策超时

`ROUTE_LLM_TIMEOUT_MS` 默认 3000 → 5000（LoadRouteLlmConfigFromEnv + start.sh 同步）。

## 5. 澄清 prompt 去 q3 方向

`BuildClarifyPrompt(ctx)` 签名去掉 skillName 参数；prompt 中"系统判断用户大概率想使用技能「X」"及"结合所选技能的方向提问"改为通用追问措辞：

```
你是语音助手的意图澄清员。用户的当前输入意图不够清晰。请生成一句自然、口语化的澄清追问，
帮助用户把意图说清楚。要求：
1. 只问一个问题，不连环追问；
2. 若上下文（如长期记忆）能提示用户可能指代的对象，可做确认式追问（"您说的是…吗？"）；
3. 不确定时礼貌地请用户重述（"抱歉，我没听清，能再说一遍吗？"）；
4. 回复需简短（不超过 20 个字/词），与用户输入使用同一语言。
只输出追问文本，不要解释、不要 JSON。
```

`FastPathClarify` 相应去掉 skillName 参数（Coordinator 澄清分支不再传 q3 技能名）。

## 6. 帧口径连锁（q5 反转）

| 项 | 旧口径 | 新口径 |
|---|---|---|
| 帧内 `jev_q5` | 1.0=任务数为 1 | **1.0=任务数大于 1（多任务）** |
| 测试脚本列 `jev_q5` | 同上 | 同上（列名不变，语义反转） |
| 使用说明/分析脚本 | "q5=1 单任务" | "q5=1 多任务" |

q1/q2/q4 口径不变。历史结果文件的 q5 解读全部反转——分析时注意批次区分（新代码部署前后）。

## 7. 承接语侧衔接（不改动）

34cc8b0 的承接语 gate（决策流式输出中 q1/q2 双 N 才启动承接语生成）与新树天然一致：q1=Y/q2=Y 的轮（澄清/带记忆澄清）不生成承接语 ✓。承接语 prompt（304c9e1 版本）、发送时序、哨兵观测帧全部保持现状。

## 8. 观测与评估口径

- 帧内 q5 新语义 + params 相关观测（`fp_params_ms`/`fp_params_fail_reason` 沿用；决策提参结果可从 `llm_route_output` 原始输出反查）；
- 决策解析成功率（params 字段加入后 JSON 变长，监控 valid 率变化）；
- 提参直转成功率对比（旧 FP 救援黄区数据 13/19 ≈ 68% 作基线）；
- 决策耗时分布（5000ms 超时下 p50/p90 变化）；
- 澄清轮占比与澄清问句质量（通用追问后人工抽检）。

## 9. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 决策输出变长 → 解析失败率上升 | params 解析 fail-soft（不判决策失败）；黄区对比 valid 率；必要时 params 精简格式 |
| 决策提参质量低于 FP 救援 | 三 gate 保留；黄区 A/B（若提参直转成功率显著低于 68% 基线，回退 FP 救援路径） |
| 超时 5000ms 拖慢决策轮 | 黄区时延对比；流式 gate 下承接语仍提前，用户感知影响小 |
| 1N2Y 全澄清 → 多一轮交互 | 确认式追问（带记忆）；黄区统计 1N2Y 轮占比与追问闭环成功率 |
| q5 口径反转的分析混乱 | 部署批次区分；文档/脚本同步 |

## 10. 实现要点（文件清单）

| 文件 | 改动 |
|---|---|
| `utils/route_llm/RouteLlmPolicy.h` | `LlmRouteAssessment` 字段重命名 q5MultiTask、新增 paramsJson/paramsSelfScore；`ResolveLlmRoute` 按新树重写 |
| `utils/route_llm/RouteLlmAssessor.cpp` | q5/q4 题干替换、提参要求段、params 解析（fail-soft）、超时默认 5000 |
| `utils/llm/LLMClient.h` | LoadRouteLlmConfigFromEnv 超时默认 3000→5000 |
| `nodes/channel/ChannelStart.cpp` | 4b 应用层：决策 params 三 gate 提参直转（替代 FP 救援）、删除旧救援调用、带记忆澄清不变 |
| `utils/fast_path/FastPathRescue.h` | `ShouldAttemptFpParamsRescueLlm`/`BuildFpParamsRescuePrompt` 移除（无调用点后）；`ValidateFpParamsRescue` 保留复用 |
| `utils/prompt/PromptUtils.h/.cpp` | BuildClarifyPrompt 去 skillName、通用追问措辞 |
| `utils/fast_path/FastPathShared.h/.cpp` | FastPathClarify 去 skillName 参数 |
| `nodes/coordinator/Coordinator.cpp` | 澄清分支不再传 q3 技能名 |
| `deployment/scripts/start.sh` | ROUTE_LLM_TIMEOUT_MS 默认 5000（CRLF） |
| 测试 | TestRouteLlmPolicy（新树全矩阵）、TestRouteLlmAssessor（题干/params 解析/q5 极性）、TestFastPathConfig（超时默认）、澄清测试更新 |
| 文档 | 接口规范（q5 口径、params 帧内说明）、使用说明（jev_q5 语义反转） |
