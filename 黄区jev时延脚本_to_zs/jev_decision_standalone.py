#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jev 决策独立测试脚本 —— 与 SysAgent 服务端 jev 评估同口径（请求/解析/决策逐字节复刻）。

用途：对形如 sampled_intent1_100.xlsx 的测试集逐条调用 jev（Clef 决策模型），
输出表 = 数据集原始列 + decision_source/jev_q1/jev_q2/jev_q3_choice/jev_q3_confidence/
jev_q3_probs/jev_q4/jev_q5/jev_decision/jev决策耗时ms（发起 jev 调用 → 三种决策之一出炉）。

上下文口径：与服务端测试脚本一致——context_user/context_assistant 在**客户端**按
"以下是之前的对话：\n用户：…\n助手：…\n当前用户问题：…" 拼进发送语料（SysAgent 本身
无上下文能力），jev 看到的就是这份拼接文本；无上下文用例发送纯 cur_query。

依赖：python3 + openpyxl（pip install openpyxl），其余全部标准库。

用法：
  export JEV_ENDPOINT="https://api.cloudflare.com/client/v4/accounts/.../ai/run/@cf/cloudflare/clef"
  export JEV_API_KEY="cfut_..."
  python3 jev_decision_standalone.py --dataset sampled_intent1_100.xlsx [--limit 10] [--only SS_0001]

可配置项（环境变量，括号为默认）：
  JEV_ENDPOINT / JEV_API_KEY        （必配；也可 --jev-endpoint/--jev-api-key）
  JEV_MODEL（clef）                 JEV_TIMEOUT_MS（30000）  JEV_CONNECT_TIMEOUT_MS（5000）
  JEV_VERIFY_TLS（1；0=关闭校验）    JEV_CA_BUNDLE（自定义 CA 文件）
  JEV_ROUTE_MIN_CONFIDENCE（0.6）   JEV_ROUTE_MIN_MAX_PROB（0.8）
  JEV_ROUTE_MIN_NOUL_VALUE（0.8）   JEV_ROUTE_MIN_NOUL_VALUE_CTX（0.7）
  --device-category（默认 1）       设备类别过滤：gui-control [1,2]、film-tv [3]

判读口径：
  - 全零行（q1-q5=0、choice 空、decision=none）= jev 评估失败（调用失败/超时/响应解析失败），
    服务端 fail-closed 回退 LLM——不是路由结果；
  - decision_source 只有 "jev"（直答/直转）与 "other"（none 回退）；提参救援（jev_fp）
    依赖前端 LLM 链路，不在本脚本范围。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

# 7 个可转发技能（name, description, categories）——description 与 SKILL.md 逐字一致
SKILLS = [
    ("home-habits-query", '处理只读的家电习惯与已有自动模式查询。只要用户的主谓关系是查询或检查设备习惯及已有配置，即使宾语里出现设备名和打开、关闭、调节、自动、设置、离家、回家等动作词，也应选择本 Skill。典型正例：“想查一下目前有没有根据作息设置自动开关的习惯”“check what automatic modes are currently set up”“التحقق مما إذا كانت الأجهزة تعمل على التشغيل أو الإيقاف التلقائي بناءً على العادات”。不处理要求现在改变设备状态的实时控制；查询与实时控制并列时，本 Skill 只承接查询部分。', []),
    ("iot-control", '仅处理用户明确要求系统现在执行并改变设备状态的一次性实时控制，如“打开客厅灯”“关闭空调”“把温度调到26度”。只读判断准则：只要用户的主谓关系是查询或检查设备习惯及已有配置，该目标就属于查询通道；即使宾语里出现设备名和动作词，CChannel 也必须不选 iot-control、不得为其生成 task。反例：“想查一下目前有没有根据作息设置自动开关的习惯”“check what automatic modes are currently set up”“التحقق مما إذا كانت الأجهزة تعمل على التشغيل أو الإيقاف التلقائي بناءً على العادات”。查询与实时控制并列时，仅承接明确要求执行的控制部分。不处理新增或修改定时、场景和自动化规则。', []),
    ("general-query", '处理未指定App/平台的通用信息问答（如时间、天气、周边、列车、航班、新闻资讯等实时信息和通用知识），推荐、写作、攻略制定、总结等内容生成与规划意图；无需查询信息的闲聊（如问候、寒暄、致谢、告别）由 SysAgent 直接回答，不调用本 Skill；不处理任何执行类意图，如导航等。', []),
    ("liantong-query", '仅处理联通相关查询，如联通云手机、联通宽带、套餐、号码和业务规则；不处理非联通问题，不处理查询话费、充值话费意图。', []),
    ("film-tv", '仅处理影视内容相关的推荐、搜索和信息查询类意图，包括电影、电视剧、综艺等内容的推荐、片名搜索等，以及播放影视等执行类意图；不处理推荐音乐、查询演唱会歌单意图，不处理单纯查询影视评分口碑、剧情解析、导演作品、演员信息、续集等影视知识问答。', [3]),
    ("gui-control", '处理需要在移动端完成的具体操作/执行流程类意图，包括导航、打车、外卖、购物、订票、订酒店、播放音视频、设定提醒（除吃药提醒外）、查询话费、充值话费、开发票等，以及明确指定App/平台的意图（如在某App内搜索xx）。不处理未指定App/平台的各类查询和推荐意图，不处理健康执行类意图（挂号、吃药提醒、健康档案操作等）。', [1, 2]),
    ("health-query", '仅处理健康咨询类意图，如症状、疾病、用药知识和健康建议问答，以及挂号、挂号查询、吃药提醒、吃药提醒查询、药品信息获取、健康档案查询和创建等健康执行类意图。', []),
]
NO_SKILL_DESC = '无需调用技能，可直接回答：仅限寒暄、情感互动，或依据对话上下文即可解答的追问。'
# 问题顺序与题目原文与 BuildJevRequest 逐字一致（q1,q2,q5,q3,q4）
QUESTIONS = [('q1', 'noul', '针对用户当前这句话，结合上下文，判断其意图是否清晰（无错别字、无转录缺字、无无法理解的表述）。'), ('q2', 'noul', '当前输入提供的上下文是否充分，足以直接处理而无需向用户追问补充。'), ('q5', 'noul', '用户当前这个问题，对应需要完成的任务数量是否为 1。'), ('q3', 'choice', '针对用户当前这个问题，如果它需要调用技能，应选择哪个技能？'), ('q4', 'noul', '用户当前这个问题本身，是否可以直接作为所选技能的任务内容转发，无需做额外的信息补全。')]


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_utterance(case: dict) -> str:
    """与单轮测试脚本同款上下文拼接；空上下文返回纯语料。"""
    cu = str(case.get("context_user") or "").strip()
    ca = str(case.get("context_assistant") or "").strip()
    cq = str(case.get("cur_query") or "").strip()
    if not cu and not ca:
        return cq
    lines = ["以下是之前的对话："]
    if cu:
        lines.append("用户：" + cu)
    if ca:
        lines.append("助手：" + ca)
    lines.append("当前用户问题：" + cq)
    return "\n".join(lines)


def build_state(user_input: str, history_turns: str = "") -> str:
    """BuildJevState 同款：history 为空 → state 即 userInput 原文。"""
    if not history_turns:
        return user_input
    try:
        doc = json.loads(history_turns)
        turns = doc.get("RecentTurns") if isinstance(doc, dict) else None
        if isinstance(turns, list):
            if not turns:
                return user_input
            hist = "（以下为历史对话，仅供上下文参考）\n"
            for t in turns:
                if not isinstance(t, dict):
                    continue
                if t.get("UserText"):
                    hist += "用户：" + str(t["UserText"]) + "\n"
                if t.get("ReplyText"):
                    hist += "助手：" + str(t["ReplyText"]) + "\n"
            if len(hist) > 2000:
                hist = hist[:2000]
            return hist + "当前需要判断的用户问题：" + user_input
    except json.JSONDecodeError:
        pass
    hist = history_turns
    if len(hist) > 2000:
        hist = hist[:2000]
    return "（以下为历史对话，仅供上下文参考）\n" + hist + "\n当前需要判断的用户问题：" + user_input


def candidate_skills(device_category: str) -> list:
    """候选集构造（与 ChannelStart 同款设备类别过滤；stoi 异常兜底不过滤）。"""
    try:
        dc = int(device_category)
    except (TypeError, ValueError):
        dc = -1
    offered = []
    for name, desc, cats in SKILLS:
        if cats and dc >= 0 and dc not in cats:
            continue
        offered.append((name, desc or name))
    return offered


def build_request(state: str, offered: list, model: str) -> dict:
    criteria = {"no_skill": NO_SKILL_DESC}
    for name, desc in offered:
        criteria[name] = desc
    questions = {}
    for qname, qtype, qinstr in QUESTIONS:
        q = {"type": qtype, "instructions": qinstr}
        if qtype == "choice":
            q["criteria"] = criteria
        questions[qname] = q
    return {"model": model, "state": state, "questions": questions}


def call_jev(endpoint: str, api_key: str, body: dict, timeout_ms: int,
             connect_ms: int, verify_tls: bool, ca_bundle: str):
    """JevClient::Call 同款同步调用：POST + Bearer；失败抛异常（调用方落全零行）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    if ca_bundle:
        ctx = ssl.create_default_context(cafile=ca_bundle)
    elif not verify_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx = ssl.create_default_context()
    # 只装 HTTPSHandler（不带 ProxyHandler → 绕过系统代理），context 传 TLS 上下文
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    with opener.open(req, timeout=connect_ms / 1000.0) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def parse_response(payload):
    """双 envelope 解析（与 latency 分支 codec 同款兼容策略）：
    ① Clef 格式：{"result": {"answers": {...}, ...}}
    ② 顶层 answers 格式（如 StartLux-Decision 等模型）：{"answers": {...}, ...}
    任一异常返回 (False, ...)。"""
    try:
        if not isinstance(payload, dict):
            return False, {}
        result = payload.get("result")
        if isinstance(result, dict):
            src = result
        else:
            src = payload
        answers_obj = src.get("answers")
        if not isinstance(answers_obj, dict) or not answers_obj:
            return False, {}
        answers = {}
        for name, a in answers_obj.items():
            if not isinstance(a, dict) or not isinstance(a.get("type"), str):
                return False, {}
            t = a["type"]
            if t == "noul":
                v = a.get("noul")
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    return False, {}
                answers[name] = {"type": "noul", "value": float(v)}
            elif t == "choice":
                choice = a.get("choice")
                if not isinstance(choice, str) or not choice:
                    return False, {}
                probs = a.get("probabilities")
                if probs is None:
                    probs = {}
                if not isinstance(probs, dict) or any(
                        not isinstance(v, (int, float)) or isinstance(v, bool)
                        for v in probs.values()):
                    return False, {}
                conf = a.get("confidence")
                if conf is not None and (not isinstance(conf, (int, float))
                                         or isinstance(conf, bool)):
                    return False, {}
                answers[name] = {
                    "type": "choice", "choice": choice,
                    "probabilities": {k: float(v) for k, v in probs.items()},
                    "confidence": float(conf) if conf is not None else None,
                }
            else:
                return False, {}
        return True, answers
    except Exception:
        return False, {}


def map_assessment(answers: dict) -> dict:
    """MapJevResponse 同款：五题全须在且题型匹配，否则 valid=False（全零落盘）。"""
    a = {"valid": True,
         "q1": 0.0, "q2": 0.0, "q4": 0.0, "q5": 0.0,
         "q3_choice": "", "q3_confidence": None, "q3_probs": {}}
    for qname in ("q1", "q2", "q5", "q3", "q4"):
        if qname not in answers:
            a["valid"] = False
            continue
        ans = answers[qname]
        if qname == "q3":
            if ans["type"] != "choice":
                a["valid"] = False
                continue
            a["q3_choice"] = ans["choice"]
            a["q3_confidence"] = ans["confidence"]
            a["q3_probs"] = ans["probabilities"]
        else:
            if ans["type"] != "noul":
                a["valid"] = False
                continue
            a[qname] = ans["value"]
    return a


def resolve_route(a: dict, offered_names: list, thresholds: dict) -> str:
    """ResolveJevRoute 同款决策树。"""
    if not a["valid"]:
        return "none"
    conf_ok = (a["q3_confidence"] is not None
               and a["q3_confidence"] >= thresholds["conf"]
               and bool(a["q3_choice"])
               and max(a["q3_probs"].values(), default=0.0) >= thresholds["maxp"])
    if not conf_ok:
        return "none"
    if a["q3_choice"] == "no_skill":
        return ("direct_reply"
                if a["q1"] >= thresholds["noul"] and a["q2"] >= thresholds["ctx"]
                else "none")
    if a["q3_choice"] not in offered_names:
        return "none"
    if (a["q1"] >= thresholds["noul"] and a["q4"] >= thresholds["ctx"]
            and a["q5"] >= thresholds["noul"]):
        return "direct_forward"
    return "none"


def format_probs(probs: dict) -> str:
    """与帧内 q3_probs 同款：按值降序 "名=值; "（尾随空格保留）。"""
    return "".join("%s=%s; " % (k, v) for k, v in sorted(probs.items(), key=lambda kv: -kv[1]))


def load_cases(path: Path):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        headers = [str(h).strip() if h is not None else "" for h in rows[0]]
        if "case_id" not in headers or "cur_query" not in headers:
            raise ValueError("缺少 case_id / cur_query 列")
        cases = [dict(zip(headers, r)) for r in rows[1:] if any(x is not None for x in r)]
    finally:
        wb.close()
    return cases, headers


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, required=True)
    ap.add_argument("--only", default="", help="只跑指定用例，逗号分隔（SS_0001 或 0001）")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--device-category", default="1")
    ap.add_argument("--jev-endpoint", default=env("JEV_ENDPOINT", ""))
    ap.add_argument("--jev-api-key", default=env("JEV_API_KEY", ""))
    ap.add_argument("--jev-model", default=env("JEV_MODEL", "clef"))
    ap.add_argument("--timeout-ms", type=int, default=int(env("JEV_TIMEOUT_MS", "30000")))
    ap.add_argument("--connect-timeout-ms", type=int, default=int(env("JEV_CONNECT_TIMEOUT_MS", "5000")))
    ap.add_argument("--verify-tls", type=int, default=int(env("JEV_VERIFY_TLS", "1")), choices=[0, 1])
    ap.add_argument("--ca-bundle", default=env("JEV_CA_BUNDLE", ""))
    ap.add_argument("--min-confidence", type=float, default=float(env("JEV_ROUTE_MIN_CONFIDENCE", "0.6")))
    ap.add_argument("--min-max-prob", type=float, default=float(env("JEV_ROUTE_MIN_MAX_PROB", "0.8")))
    ap.add_argument("--min-noul", type=float, default=float(env("JEV_ROUTE_MIN_NOUL_VALUE", "0.8")))
    ap.add_argument("--min-noul-ctx", type=float, default=float(env("JEV_ROUTE_MIN_NOUL_VALUE_CTX", "0.7")))
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.jev_endpoint or not args.jev_api_key:
        print("错误：需配置 JEV_ENDPOINT / JEV_API_KEY（环境变量或 --jev-endpoint/--jev-api-key）",
              file=sys.stderr)
        return 2

    cases, headers = load_cases(args.dataset)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases
                 if str(c.get("case_id") or "").strip() in wanted
                 or str(c.get("case_id") or "").strip().split("_")[-1] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("没有要跑的用例", file=sys.stderr)
        return 2

    offered = candidate_skills(args.device_category)
    offered_names = [n for n, _ in offered]
    thresholds = {"conf": args.min_confidence, "maxp": args.min_max_prob,
                  "noul": args.min_noul, "ctx": args.min_noul_ctx}
    print("== jev 决策独立测试：%d 条 | 模型=%s 设备类别=%s" % (len(cases), args.jev_model, args.device_category))
    print("   阈值 conf>=%s maxp>=%s noul>=%s ctx>=%s" %
          (args.min_confidence, args.min_max_prob, args.min_noul, args.min_noul_ctx))
    print("   候选技能: %s" % offered_names)

    out_cols = headers + ["decision_source", "jev_q1", "jev_q2", "jev_q3_choice",
                          "jev_q3_confidence", "jev_q3_probs", "jev_q4", "jev_q5",
                          "jev_decision", "jev决策耗时ms"]
    rows_out = []
    results = []
    dec_cnt = Counter()
    lat = []
    for idx, case in enumerate(cases, 1):
        cid = str(case.get("case_id") or "").strip()
        t0 = time.monotonic()
        try:
            state = build_state(build_utterance(case))
            body = build_request(state, offered, args.jev_model)
            payload = call_jev(args.jev_endpoint, args.jev_api_key, body,
                               args.timeout_ms, args.connect_timeout_ms,
                               bool(args.verify_tls), args.ca_bundle)
            ok, answers = parse_response(payload)
            if not ok:
                raise ValueError("response parse failed")
            a = map_assessment(answers)
            decision = resolve_route(a, offered_names, thresholds)
        except Exception as exc:
            a = {"valid": False, "q1": 0.0, "q2": 0.0, "q4": 0.0, "q5": 0.0,
                 "q3_choice": "", "q3_confidence": None, "q3_probs": {}}
            decision = "none"
            print("[%d/%d] %s jev调用失败(%s: %s) → 全零/none" %
                  (idx, len(cases), cid, type(exc).__name__, exc))
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        src = "jev" if decision in ("direct_reply", "direct_forward") else "other"
        dec_cnt[decision] += 1
        lat.append(elapsed_ms)
        row = [case.get(h) for h in headers] + [
            src, a["q1"], a["q2"], a["q3_choice"],
            ("" if a["q3_confidence"] is None else a["q3_confidence"]),
            format_probs(a["q3_probs"]), a["q4"], a["q5"], decision, elapsed_ms]
        rows_out.append(row)
        results.append({"case_id": cid, "decision_source": src,
                        "jev_decision": decision, "jev_decision_ms": elapsed_ms,
                        "jev_q1": a["q1"], "jev_q2": a["q2"], "jev_q4": a["q4"],
                        "jev_q5": a["q5"], "jev_q3_choice": a["q3_choice"],
                        "jev_q3_confidence": a["q3_confidence"],
                        "jev_q3_probs": format_probs(a["q3_probs"])})
        print("[%d/%d] %s decision=%s src=%s 耗时=%dms" %
              (idx, len(cases), cid, decision, src, elapsed_ms))

    def pct(vals, p):
        s = sorted(vals)
        return s[max(0, int(len(s) * p / 100 + 0.999) - 1)] if s else None

    print("\n== 汇总 ==")
    print("  jev_decision 分布:", dict(dec_cnt))
    print("  jev决策耗时ms: n=%d min=%d avg=%.1f p50=%s p90=%s max=%d" %
          (len(lat), min(lat), sum(lat) / len(lat), pct(lat, 50), pct(lat, 90), max(lat)))

    out_dir = args.dataset.parent
    stem = args.dataset.stem
    csv_path = out_dir / (stem + "_jev决策.csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(out_cols)
        w.writerows(rows_out)
    print("CSV:", csv_path)
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "jev决策"
        ws.append(out_cols)
        for row in rows_out:
            ws.append(row)
        xlsx_path = out_dir / (stem + "_jev决策.xlsx")
        wb.save(xlsx_path)
        print("XLSX:", xlsx_path)
    except ImportError:
        pass
    if args.report is not None:
        report_path = args.report
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(
            {"columns": out_cols,
             "summary": {"total": len(cases), "jev_decision": dict(dec_cnt)},
             "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("JSON:", report_path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        sys.exit(1)
