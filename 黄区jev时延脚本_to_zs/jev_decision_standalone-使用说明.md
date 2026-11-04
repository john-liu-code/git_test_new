# jev_decision_standalone.py 使用说明

独立运行 SysAgent 的 jev（Clef 决策模型）评估——对形如 `sampled_intent1_100.xlsx` 的测试集逐条调用 jev，输出 5 问评分与路由决策，**与服务端帧内输出同口径**（请求构造/响应解析/决策规则逐字节复刻，已用 SS_0328、SS_0058 两条真实用例与服务端比对一致）。

## 1. 快速上手

```bash
# 依赖：openpyxl（其余标准库）
python3 -m pip install openpyxl

# 配置 API（必配）
export JEV_ENDPOINT="https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/run/@cf/cloudflare/clef"
export JEV_API_KEY="cfut_..."

# 运行（全量）
python3 jev_decision_standalone.py --dataset sampled_intent1_100.xlsx

# 先试 10 条 / 只跑指定用例
python3 jev_decision_standalone.py --dataset sampled_intent1_100.xlsx --limit 10
python3 jev_decision_standalone.py --dataset sampled_intent1_100.xlsx --only SS_0328,SS_0058

# 输出完整报告 json（含每轮 5 问明细）
python3 jev_decision_standalone.py --dataset sampled_intent1_100.xlsx --report result.json
```

## 2. 参数与配置

### 命令行参数

| 参数 | 说明 |
|---|---|
| `--dataset <xlsx>` | 必填，测试集（要求含 `case_id`、`cur_query` 列；`context_user`/`context_assistant` 可选） |
| `--limit N` | 只跑前 N 条 |
| `--only SS_0001,SS_0002` | 只跑指定用例（支持完整编号或末段编号） |
| `--device-category` | 设备类别（默认 `1`）——候选技能过滤：gui-control [1,2]、film-tv [3] |
| `--jev-endpoint` / `--jev-api-key` | 覆盖环境变量的端点/密钥 |
| `--report <json>` | 输出报告 json（每轮 5 问明细 + 决策） |

### 环境变量（括号为默认值，与服务端同款）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `JEV_ENDPOINT` / `JEV_API_KEY` | — | 必配 |
| `JEV_MODEL` | `clef` | 请求体里的 model 字段 |
| `JEV_TIMEOUT_MS` | `30000` | 请求超时（对应服务端 JEV_TIMEOUT_MS） |
| `JEV_CONNECT_TIMEOUT_MS` | `5000` | 连接超时 |
| `JEV_VERIFY_TLS` | `1` | `0` 关闭 TLS 校验 |
| `JEV_CA_BUNDLE` | 空 | 自定义 CA 文件（容器环境需指定 RHEL bundle 时用） |
| `JEV_ROUTE_MIN_CONFIDENCE` | `0.6` | q3 置信度门槛 |
| `JEV_ROUTE_MIN_MAX_PROB` | `0.8` | q3 最大项概率门槛 |
| `JEV_ROUTE_MIN_NOUL_VALUE` | `0.8` | noul 严格档（q1/q5） |
| `JEV_ROUTE_MIN_NOUL_VALUE_CTX` | `0.7` | noul 上下文档（q2/q4） |

## 3. 输出物与列说明

输出文件（与 `--dataset` 同目录）：`<数据集名>_jev决策.csv`、`<数据集名>_jev决策.xlsx`、报告 json（`--report` 时）。

输出表 = **数据集原始列** + 以下 10 列：

| 列 | 含义 |
|---|---|
| `decision_source` | `jev`（直答/直转）或 `other`（none 回退）。**注意：本脚本只做 jev 决策，`jev_fp`（提参救援直转）依赖前端 LLM 链路，不在范围** |
| `jev_q1` / `jev_q2` / `jev_q4` / `jev_q5` | 四道 noul 题评分（0-1） |
| `jev_q3_choice` | q3 选中的技能（`no_skill` = 无需技能） |
| `jev_q3_confidence` | q3 选择置信度 |
| `jev_q3_probs` | q3 全选项概率，按值降序 `名=值; ` 文本（与服务端帧内格式一致） |
| `jev_decision` | `direct_reply` / `direct_forward` / `none` |
| `jev决策耗时ms` | 发起 jev 调用 → 三种决策之一出炉（含请求构造与响应解析） |

**全零行判读**：q1-q5=0、choice 空、decision=none = **jev 评估失败**（调用失败/超时/响应解析失败），对应服务端 fail-closed 回退 LLM——不是路由结果。

## 4. 与服务端的一致性口径（复刻点）

| 环节 | 口径 |
|---|---|
| 上下文 | `context_user`/`context_assistant` 按服务端测试脚本同款格式**客户端拼接**（"以下是之前的对话：\n用户：…\n助手：…\n当前用户问题：…"）；无上下文用例发送纯 `cur_query` |
| 候选技能 | 7 个可转发技能（description 与 SKILL.md 逐字一致）+ 设备类别过滤（gui-control [1,2]、film-tv [3]） |
| 请求体 | `{"model","state","questions"}`；问题顺序 q1,q2,q5,q3,q4 与题目原文逐字一致；q3 为 choice 题型带 criteria |
| 响应解析 | 双 envelope 兼容：① Clef `result` 包裹格式；② **顶层 `answers` 格式**（如 StartLux-Decision 等模型直出 answers）；任一字段缺失/题型不符 → 整轮判无效（全零落盘） |
| 决策 | 与 ResolveJevRoute 同款：q3 门槛（conf≥0.6 且 maxp≥0.8）→ no_skill+直答条件 → 直转条件（q1≥0.8、q4≥0.7、q5≥0.8）→ 其余 none |

## 5. 与服务端对比验证方法

1. 用**同一份 xlsx** 分别跑：服务端批测（测试脚本，帧内取 5 问值）与本脚本（直连 Clef）；
2. 逐条对比 `jev_q1~q5 / q3_choice / q3_confidence / q3_probs / jev_decision` 应完全一致；
3. 注意：两边都可能出现"jev 评估失败"的全零行（Clef 偶发响应异常），属正常 fail-closed，对比时归为同类即可。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| `ModuleNotFoundError: openpyxl` | `python3 -m pip install openpyxl` |
| 连接超时/证书错误 | 确认网络可达 Cloudflare；容器/内网环境用 `JEV_VERIFY_TLS=0` 或 `JEV_CA_BUNDLE` 指定 CA |
| 全部全零行 | 检查 endpoint/key 是否正确；Clef 偶发异常时可对同一用例重跑对比。**换用其他模型端点时**：若响应是顶层 `answers` 格式（无 `result` 包裹），脚本已兼容；若格式再变（如 answers 在别的键下），把原始响应贴出来即可定位 |
| 脚本绕过系统代理 | 已内置（不带 ProxyHandler），与测试环境惯例一致 |

## 7. 冒烟验证记录

- 用例 SS_0328 / SS_0058：脚本输出与服务端帧内值（q1-q5、q3 选择/置信度、decision）逐项一致；
- SS_0328 判 none 因 q3 置信度 0.5825 < 0.6；SS_0058 判 none 因 q4=0.4318 < 0.7——两边同判。
