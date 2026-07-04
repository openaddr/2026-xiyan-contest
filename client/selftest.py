#!/usr/bin/env python3
"""离线自测：不连服务端，验证框架各层。

1. 官方 framing 帧编解码：粘包 / 半包 / 中文跨包；
2. 用仓库里的 start消息.json / inquire消息.json 驱动 GameState + 策略;
3. 寻路合理性检查。
"""
import json
import os
import random
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lychee import protocol as P
from lychee.planner import (TaskPlanner, marginal_task_value,
                            task_component_score, Plan, RUSH_EARLIEST)
from lychee.state import GameState
from lychee.strategy import BaselineStrategy, PlannerStrategy
from lychee_basic_client.framing import read_frame, write_frame

DOC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))
    return cond


def test_codec():
    """验证官方 lychee_basic_client.framing 在粘包/半包/中文跨包下的行为。"""
    ok = True
    msg1 = {"msg_name": "action",
            "msg_data": {"matchId": "m", "round": 1, "playerId": 1, "actions": []}}
    msg2 = {"msg_name": "ready",
            "msg_data": {"matchId": "岭南贡队测试中文", "round": 1, "playerId": 1}}

    # 粘包：两条消息一次写入，连续 read_frame 应各得一条
    a, b = socket.socketpair()
    write_frame(a, msg1)
    write_frame(a, msg2)
    r1, r2 = read_frame(b), read_frame(b)
    ok &= check("framing 粘包拆两条",
                r1["msg_name"] == "action" and r2["msg_name"] == "ready")
    ok &= check("framing 中文完整", r2["msg_data"]["matchId"] == "岭南贡队测试中文")
    a.close(); b.close()

    # 半包：另起线程逐字节慢发（中文必然跨包），read_frame 应完整收齐
    a, b = socket.socketpair()
    body = json.dumps(msg2, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload = f"{len(body):05d}".encode() + body

    def drip():
        for i in range(len(payload)):
            a.sendall(payload[i:i + 1])
            time.sleep(0.0005)
        a.close()

    t = threading.Thread(target=drip)
    t.start()
    r = read_frame(b)
    t.join()
    ok &= check("framing 逐字节半包", r["msg_data"]["matchId"] == "岭南贡队测试中文")
    ok &= check("framing 长度前缀口径", int(payload[:5]) == len(body),
                f"prefix={int(payload[:5])}")
    b.close()
    return ok


def test_state_and_strategy():
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    gs = GameState(1001)
    gs.on_start(start)
    ok &= check("start: matchId", gs.match_id == start["matchId"], gs.match_id)
    ok &= check("start: 识别语义点",
                gs.start_node == "S01" and gs.gate_node == "S14" and gs.terminal_node == "S15",
                f"{gs.start_node}->{gs.gate_node}->{gs.terminal_node}")
    ok &= check("start: 地图边数", len(gs.graph.edges) == 21, f"{len(gs.graph.edges)} edges")
    ok &= check("start: 资源配置回退 gameplay", len(gs.resource_config) > 0,
                f"{len(gs.resource_config)} entries")

    # 寻路：S01 -> S14 存在路径且帧数合理
    frames, path = gs.graph.shortest_path("S01", "S14")
    ok &= check("寻路 S01->S14", 0 < frames < 600, f"{frames} 帧, path={'>'.join(path)}")
    # 到站帧数公式抽查：E01 ROAD 距离30 => ceil(30*1380/1000)=42 帧
    e01 = gs.graph.edges["E01"]
    ok &= check("E01 帧数公式", gs.graph.edge_frames(e01) == 42,
                f"{gs.graph.edge_frames(e01)} 帧")

    # inquire 帧
    gs.on_inquire(inquire)
    me = gs.me
    ok &= check("inquire: round/phase", gs.round == 142 and gs.phase == "NORMAL")
    ok &= check("inquire: 定位自己", me.get("playerId") == 1001,
                f"pos={me.get('currentNodeId')} state={me.get('state')}")
    ok &= check("inquire: 增益识别", gs.has_move_buff() and gs.my_speed() == P.SPEED_RUSH,
                f"speed={gs.my_speed()}")

    # 策略：样例中自己 PROCESSING，主车队应不出动作；有窗口则出牌
    st = BaselineStrategy()
    st.on_start(gs)
    actions = st.decide(gs)
    kinds = [a["action"] for a in actions]
    ok &= check("策略: PROCESSING 时不发主车队动作",
                all(a["action"] == "WINDOW_CARD" for a in actions), str(kinds))
    contests = gs.my_open_contests()
    if contests:
        ok &= check("策略: 窗口出牌带 contestId",
                    actions and actions[0].get("contestId") == contests[0]["contestId"],
                    json.dumps(actions[0], ensure_ascii=False) if actions else "no action")

    # 构造 IDLE 场景：应该赶路
    idle = json.loads(json.dumps(inquire))
    for p in idle["players"]:
        if p["playerId"] == 1001:
            p.update(state="IDLE", routeEdgeId=None, nextNodeId=None,
                     currentProcess=None, currentNodeId="S09")
    idle["contests"] = []
    gs.on_inquire(idle)
    st2 = BaselineStrategy()
    a = st2.decide(gs)
    # 样例中 S10/S11 有障碍，S09 出发合理动作是 MOVE 或对相邻障碍 CLEAR
    ok &= check("策略: IDLE 时向宫门推进(MOVE/CLEAR)",
                len(a) == 1 and (
                    a[0]["action"] == "MOVE"
                    or (a[0]["action"] == "CLEAR" and gs.has_obstacle(a[0]["targetNodeId"]))),
                json.dumps(a, ensure_ascii=False))

    # 无阻挡场景：放在 S12，应直接 MOVE 去 S13
    clean = json.loads(json.dumps(idle))
    for p in clean["players"]:
        if p["playerId"] == 1001:
            p["currentNodeId"] = "S12"
    gs.on_inquire(clean)
    a = BaselineStrategy().decide(gs)
    ok &= check("策略: 无阻挡时 MOVE 下一跳",
                len(a) == 1 and a[0]["action"] == "MOVE" and a[0]["targetNodeId"] == "S13",
                json.dumps(a, ensure_ascii=False))

    # 构造 RUSH + 在宫门：应该验核
    rush = json.loads(json.dumps(idle))
    rush["phase"] = "RUSH"
    for p in rush["players"]:
        if p["playerId"] == 1001:
            p["currentNodeId"] = "S14"
    gs.on_inquire(rush)
    a = BaselineStrategy().decide(gs)
    ok &= check("策略: RUSH 在宫门发验核",
                len(a) == 1 and a[0]["action"] == "VERIFY_GATE",
                json.dumps(a, ensure_ascii=False))

    # 构造已验核 + 在终点：应该交付
    dlv = json.loads(json.dumps(idle))
    for p in dlv["players"]:
        if p["playerId"] == 1001:
            p.update(currentNodeId="S15", verified=True)
    gs.on_inquire(dlv)
    a = BaselineStrategy().decide(gs)
    ok &= check("策略: 验核后在终点发交付",
                len(a) == 1 and a[0]["action"] == "DELIVER",
                json.dumps(a, ensure_ascii=False))
    return ok


def test_planner():
    ok = True
    # ---- 分数模型 ----
    # 基础分 0->90：送达 120->240、任务 0->125、用时 0->25，共 +270
    ok &= check("模型: 0->90 边际收益",
                marginal_task_value(0, 90) == 270, f"{marginal_task_value(0, 90)}")
    # 90 档解锁后单个 30 分任务边际收益骤降（90->120: 任务 125->170，共 +45）
    ok &= check("模型: 90 后收益衰减",
                marginal_task_value(90, 30) == 45 and marginal_task_value(60, 30) == 99,
                f"90+30={marginal_task_value(90, 30)}, 60+30={marginal_task_value(60, 30)}")
    ok &= check("模型: 基础分 90 拿满送达+用时",
                task_component_score(90, 25) == 240 + 125 + 25)

    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def make_state(round_no=142, node="S07", task_score=45, contests=False):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        if not contests:
            d["contests"] = []
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", routeEdgeId=None, nextNodeId=None,
                         currentProcess=None, currentNodeId=node,
                         taskScore=task_score, buffs=[],
                         freshness=95.0, resources={})  # 需要测冰的场景显式设置
            else:
                # 对手放在身后（样例中它在前方，阴影惩罚会诚实压低 slack，
                # 干扰这些与走廊竞争无关的场景）
                p.update(state="IDLE", currentNodeId="S03", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None)
        gs.on_inquire(d)
        return gs

    # ---- 场景1: 站在可领任务点（T_003@S07，15分，保护期归我）应领任务 ----
    gs = make_state()
    a = PlannerStrategy().decide(gs)
    kinds = {x["action"]: x for x in a}
    ok &= check("规划: 站在任务点发 CLAIM_TASK",
                kinds.get("CLAIM_TASK", {}).get("taskId") == "T_003",
                json.dumps(a, ensure_ascii=False))
    # 早期不探宫门（验核 ~390 帧才开放，标记 45 帧就过期）；任务点在脚下也不探
    ok &= check("探路: 早期不浪费人手探宫门", "SQUAD_SCOUT" not in kinds,
                json.dumps(a, ensure_ascii=False))

    # ---- 场景1b: 355 帧后接近宫门时才派探路 ----
    gs = make_state(round_no=360, node="S13")
    a = PlannerStrategy().decide(gs)
    kinds = {x["action"]: x for x in a}
    ok &= check("探路: 355帧后近宫门派探路",
                kinds.get("SQUAD_SCOUT", {}).get("targetNodeId") == "S14",
                json.dumps(a, ensure_ascii=False))

    # ---- 场景1c: 首关前零坏果时不抢在 90 阈值前吃冰 ----
    gs = make_state()
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["freshness"] = 88.0
            p["resources"] = {"ICE_BOX": 1}
            p["badFruit"] = 0
    gs.tasks = []  # 排除任务干扰
    a = PlannerStrategy().main_action(gs)
    ok &= check("保鲜: 首关前零坏果不急用冰",
                not (a and a["action"] == "USE_RESOURCE"
                     and a["resourceType"] == "ICE_BOX"),
                str(a))

    # ---- 场景1c2: 已有 1 个坏果弹药后，鲜度到 86 左右再补冰 ----
    gs = make_state()
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["freshness"] = 85.5
            p["resources"] = {"ICE_BOX": 1}
            p["badFruit"] = 1
    gs.tasks = []
    a = PlannerStrategy().main_action(gs)
    ok &= check("保鲜: 有坏果后 86 附近补冰",
                a and a["action"] == "USE_RESOURCE"
                and a["resourceType"] == "ICE_BOX",
                str(a))

    # ---- 场景1d: 已持有 1 个冰鉴仍可再领（上限 2） ----
    gs = make_state()
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["resources"] = {"ICE_BOX": 1}
            p["freshness"] = 95.0  # 高于用冰阈值，测领取分支
    gs.tasks = []
    gs.nodes["S07"]["resourceStock"] = {"ICE_BOX": 1}
    gs.nodes["S07"].pop("processType", None)
    a = PlannerStrategy().main_action(gs)
    ok &= check("保鲜: 持1个冰鉴仍再领",
                a and a["action"] == "CLAIM_RESOURCE" and a["resourceType"] == "ICE_BOX",
                str(a))

    # ---- 场景1e: RUSH 在宫门用坏果破关令代替护果令（V3.12：坏果 12 篓近乎
    # 白送，破关令绑验核省 3 帧且几乎零成本，优先于要烧鲜度损耗才见效的护果令）----
    gs = make_state(node="S14")
    gs.phase = "RUSH"
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["rushTacticUsedCount"] = 0  # 样例里已用过急策，重置以测该分支
            # 样例默认 goodFruit=88 badFruit=12：坏果充足，应该走破关令分支
    a1 = PlannerStrategy().main_action(gs)
    ok &= check("急策: 坏果充足优先破关令绑验核",
                a1 and a1["action"] == "VERIFY_GATE" and a1.get("rushTactic") == "BREAK_ORDER",
                str(a1))

    # ---- 场景1e2: 坏果不足、好果紧张时回落护果令 ----
    gs = make_state(node="S14")
    gs.phase = "RUSH"
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["rushTacticUsedCount"] = 0
            p["goodFruit"], p["badFruit"] = 5, 0  # 好果只够留底仓，坏果为 0
    st = PlannerStrategy()
    a1 = st.main_action(gs)
    a2 = st.main_action(gs)
    ok &= check("急策: 坏果不足回落护果令",
                a1 and a1["action"] == "RUSH_PROTECT"
                and a2 and a2["action"] == "VERIFY_GATE",
                f"{a1} -> {a2}")

    # ---- 场景1e3: RUSH 移动中截止吃紧优先疾行令（唯一能在 MOVING 里提交的急策）----
    def rushing_state(good=88):
        gs = make_state(round_no=580, node="S09")
        gs.phase = "RUSH"
        for p in gs.players.values():
            if p["playerId"] == 1001:
                p.update(state="MOVING", currentNodeId="S09", nextNodeId="S10",
                         routeEdgeId="E09", buffs=[], resources={},
                         rushTacticUsedCount=0, goodFruit=good)
        return gs

    a = PlannerStrategy().decide(rushing_state())
    kinds = {x["action"]: x for x in a}
    mains = [x for x in a if x["action"] in P.MAIN_ACTION_TYPES]
    ok &= check("急策: 截止吃紧移动中优先疾行令",
                kinds.get("RUSH_SPEED") is not None,
                json.dumps(a, ensure_ascii=False))
    # 疾行令按主车队动作提交（4.1）：同帧绝不允许再补 MOVE，否则双主动作
    # 全部作废 + 记非法，急策名额还被本地标记锁死
    ok &= check("急策: 疾行令同帧不补 MOVE（单主车队动作）",
                len(mains) == 1 and "MOVE" not in kinds,
                json.dumps(a, ensure_ascii=False))

    # 好果不足 3（疾行令费 2 且交付要求好果 >0）：不发，名额不被拒绝锁死
    st_poor = PlannerStrategy()
    a = st_poor.decide(rushing_state(good=2))
    kinds = {x["action"]: x for x in a}
    ok &= check("急策: 好果不足不发疾行令（名额留给停靠三选一）",
                "RUSH_SPEED" not in kinds and not st_poor._rush_tactic_tried,
                json.dumps(a, ensure_ascii=False))

    # ---- 场景2: 截止临近（r560）应放弃任务直奔交付线 ----
    gs = make_state(round_no=560, node="S09")
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["resources"] = {}  # 清空背包，排除「临交付用冰鉴」正确分支的干扰
    st = PlannerStrategy()
    plan = st.planner.plan(gs)
    ok &= check("规划: 截止临近直奔交付", plan.kind == "deliver", repr(plan))
    a = st.decide(gs)
    main_acts = [x for x in a if x["action"] in ("MOVE", "CLEAR", "WAIT")]
    ok &= check("规划: 截止临近在赶路", len(main_acts) == 1,
                json.dumps(a, ensure_ascii=False))

    # ---- 场景3: 任务分已满 130，15 分小任务不值得再绕 ----
    gs = make_state(task_score=130)
    plan = PlannerStrategy().planner.plan(gs)
    # T_003 就在脚下（绕路 0 帧 + 3 帧读条），封顶后仍可能为正收益; 只验证不崩溃且可解释
    ok &= check("规划: 高基础分下有明确决策",
                plan.kind in ("task", "deliver", "resource"), repr(plan))

    # ---- 场景4: 无移动增益时出牌应为可负担的有效牌（混合策略，非恒定） ----
    gs = make_state(contests=True)
    st = PlannerStrategy()
    contests = gs.my_open_contests()
    if contests:
        random.seed(7)
        cards = {st.pick_card(gs, contests[0]) for _ in range(30)}
        ok &= check("窗口: 出牌均为可负担合法牌",
                    cards <= {P.CARD_BING_ZHENG, P.CARD_XIAN_GONG,
                              P.CARD_YAN_DIE, P.CARD_ABSTAIN} and len(cards) >= 1,
                    f"cards={sorted(cards)}")

    # ---- 场景5: 已验核后规划为 deliver ----
    gs = make_state(node="S14")
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["verified"] = True
    plan = PlannerStrategy().planner.plan(gs)
    ok &= check("规划: 已验核直奔交付", plan.kind == "deliver", repr(plan))
    return ok


def test_contention():
    """镜像死锁回归：错峰处理 / 混合出牌 / 移动中补显式 MOVE。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def mirror_state(my_id, round_no=44):
        """双方同帧空闲停在 S02（固定处理站，前段交接）——镜像死锁现场。"""
        gs = GameState(my_id)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"] = []
        d["tasks"] = []
        for p in d["players"]:
            p.update(state="IDLE", routeEdgeId=None, nextNodeId=None,
                     currentProcess=None, currentNodeId="S02", buffs=[],
                     taskScore=0, freshness=95.0, resources={})
        for n in d["nodes"]:
            if n["nodeId"] == "S02":
                n.update(processType="TRANSFER", processRound=4)
        gs.on_inquire(d)
        return gs

    # ---- V3.7 改策：固定处理站不让行（对不让行的对手让行=白送5帧先手，
    #      S02 先手决定整条冰链归属）。同帧撞车打 DOCK 窗口，混合出牌破平局 ----
    for rnd in (44, 45):
        acts = {}
        for pid in (1001, 2002):
            a = PlannerStrategy().main_action(mirror_state(pid, rnd))
            acts[pid] = a["action"]
        ok &= check(f"抢先手: r{rnd} 双方都立即开始处理",
                    sorted(acts.values()) == ["PROCESS", "PROCESS"],
                    f"{acts}")

    # ---- V3.43：S02 一旦发生 DRAW，退回奇偶错峰，避免镜像窗口永动 ----
    acts = {}
    for pid in (1001, 2002):
        st = PlannerStrategy()
        st._window_draw_pressure[("S02", P.CONTEST_DOCK)] = (1, 45)
        a = st.main_action(mirror_state(pid, 46))
        acts[pid] = a["action"]
    ok &= check("抢先手: S02 DRAW 后奇偶错峰",
                sorted(acts.values()) == ["PROCESS", "WAIT"],
                f"{acts}")

    st = PlannerStrategy()
    st._window_draw_pressure[("S02", P.CONTEST_DOCK)] = (2, 57)
    st._window_suppress_until[("S02", P.CONTEST_DOCK)] = 71
    a = st.main_action(mirror_state(1001, 60))
    ok &= check("抢先手: S02 重复平局抑制期不空发 PROCESS",
                a["action"] == "WAIT", str(a))

    # ---- 对手读条中：排队等待，不重复提交 ----
    gs = mirror_state(1001, 44)
    opp = gs.players[2002]
    opp.update(state="PROCESSING",
               currentProcess={"action": "PROCESS", "type": "PROCESS",
                               "targetNodeId": "S02", "remainRound": 3})
    a = PlannerStrategy().main_action(gs)
    ok &= check("错峰: 对手读条中我方排队", a["action"] == "WAIT", str(a))

    # ---- best-response 出牌（V3.16）：镜像局面主打期望最优牌；严格劣势不混 ----
    random.seed(42)
    gs = mirror_state(1001, 44)
    contest = {"contestId": "C_X", "contestType": "DOCK",
               "targetNodeId": "S02",
               "redPlayerId": 1001, "bluePlayerId": 2002}
    st = PlannerStrategy()
    picks = [st.pick_card(gs, contest) for _ in range(200)]
    distinct = set(picks)
    # 镜像开局对手池 {弃权,兵争,献贡}：献贡胜弃权+兵争、仅平献贡 → 期望最优
    ok &= check("出牌: 镜像局主打期望最优的献贡",
                picks.count(P.CARD_XIAN_GONG) > 120,
                f"xian={picks.count(P.CARD_XIAN_GONG)}/200")
    ok &= check("出牌: 严格优势不混劣势牌",
                distinct == {P.CARD_XIAN_GONG}, f"{sorted(distinct)}")
    ok &= check("出牌: 不随机弃权",
                P.CARD_ABSTAIN not in distinct,
                f"abstain={picks.count(P.CARD_ABSTAIN)}/200")
    ok &= check("出牌: 全部为合法牌",
                distinct <= {P.CARD_YAN_DIE, P.CARD_QIANG_XING, P.CARD_XIAN_GONG,
                             P.CARD_BING_ZHENG, P.CARD_ABSTAIN})

    # ---- 拍分数学锁定即弃权（V3.14）：三拍两胜，先到 2 分胜负已定 ----
    # replay36 实锤：对手三拍全弃权，我们 2:0 后第三张献贡纯白烧 1 好果
    st = PlannerStrategy()
    locked_win = dict(contest, redPoint=2, bluePoint=0)   # 我(red) 2:0 进第三拍
    picks = {st.pick_card(gs, locked_win) for _ in range(20)}
    ok &= check("出牌: 2:0 锁定后第三拍弃权", picks == {P.CARD_ABSTAIN},
                f"{sorted(picks)}")
    locked_loss = dict(contest, redPoint=0, bluePoint=2)  # 0:2 已数学出局
    picks = {st.pick_card(gs, locked_loss) for _ in range(20)}
    ok &= check("出牌: 0:2 出局后认负省牌", picks == {P.CARD_ABSTAIN},
                f"{sorted(picks)}")
    live = dict(contest, redPoint=1, bluePoint=1)         # 1:1 决胜拍照常出牌
    random.seed(3)
    picks = {st.pick_card(gs, live) for _ in range(40)}
    ok &= check("出牌: 1:1 决胜拍照常出牌",
                any(c != P.CARD_ABSTAIN for c in picks), f"{sorted(picks)}")

    # replay99：对手连续献贡且我方没有强行，献贡是唯一非负响应；决胜拍
    # 不得被软混合掷到兵争/弃权。
    st = PlannerStrategy()
    st._opp_card_hist = {P.CARD_XIAN_GONG: 5}
    live = dict(contest, redPoint=0, bluePoint=0)
    picks = {st.pick_card(gs, live) for _ in range(80)}
    ok &= check("出牌: 对手献贡画像下不随机偏离献贡",
                picks == {P.CARD_XIAN_GONG}, f"{sorted(picks)}")
    st = PlannerStrategy()
    st._window_draw_pressure[("S02", P.CONTEST_DOCK)] = (1, gs.round - 1)
    picks = {st.pick_card(gs, live) for _ in range(20)}
    ok &= check("出牌: S02 首个DRAW后第二窗止损弃权",
                picks == {P.CARD_ABSTAIN}, f"{sorted(picks)}")

    # ---- 移动中只有小分队动作时补显式 MOVE（防服务端暂停推进） ----
    # 场景: r360 在 E09 上移动、接近宫门 -> 触发探路，同包必须补 MOVE 保持推进
    gs = GameState(1001)
    gs.on_start(start)
    d = json.loads(json.dumps(inquire))
    d["contests"], d["tasks"] = [], []
    d["round"] = 360
    for p in d["players"]:
        if p["playerId"] == 1001:
            p.update(state="MOVING", currentNodeId="S13", nextNodeId="S14",
                     routeEdgeId="E09", currentProcess=None, buffs=[],
                     resources={})
    gs.on_inquire(d)
    a = PlannerStrategy().decide(gs)
    kinds = [x["action"] for x in a]
    ok &= check("移动中: 触发探路", "SQUAD_SCOUT" in kinds, f"{kinds}")
    mv = [x for x in a if x["action"] == "MOVE"]
    ok &= check("移动中: 小分队动作伴随显式 MOVE 保持推进",
                len(mv) == 1 and mv[0]["targetNodeId"] == "S14",
                json.dumps(a, ensure_ascii=False))
    return ok


def test_breakthrough():
    """平台败局回归：S09 面对 S10 敌卡不再干等风化（曾等 175 帧导致未交付）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def blocked_state(defense=6, good=90, bad=2, squad=6, round_no=330):
        """我方(RED)在 S09，S10 有蓝方设卡，其余阻挡清空。"""
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", routeEdgeId=None, nextNodeId=None,
                         currentProcess=None, currentNodeId="S09", buffs=[],
                         goodFruit=good, badFruit=bad, squadAvailable=squad,
                         freshness=95.0, resources={}, taskScore=90)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            if n["nodeId"] == "S10":
                n["guard"] = {"ownerTeamId": "BLUE", "defense": defense,
                              "maxDefense": 7, "active": True}
        gs.on_inquire(d)
        return gs

    # 1) 坏果够破：2 坏果攻坚值 6 >= 防守 6，好果一个不花
    st = PlannerStrategy()
    a = st.decide(blocked_state(defense=6, bad=2))
    brk = next((x for x in a if x["action"] == "BREAK_GUARD"), None)
    ok &= check("突破: 坏果优先一击必破",
                brk and brk["targetNodeId"] == "S10"
                and brk["goodFruit"] == 0 and brk["badFruit"] == 2,
                json.dumps(a, ensure_ascii=False))

    # 2) 防守低时最小好果投入
    a = st.decide(blocked_state(defense=2, bad=0))
    brk = next((x for x in a if x["action"] == "BREAK_GUARD"), None)
    ok &= check("突破: 低防守最小投入",
                brk and brk["goodFruit"] == 1 and brk["badFruit"] == 0,
                json.dumps(a, ensure_ascii=False))

    # 3) 果品不够破且余量充足 -> 主车队等待 + 小分队削弱同帧出发
    #    （r330 余量已为负会直接强通，所以取 r200 测削弱分支）
    a = PlannerStrategy().decide(blocked_state(defense=6, bad=0, round_no=200))
    kinds = {x["action"]: x for x in a}
    ok &= check("突破: 破不动先派削弱",
                kinds.get("SQUAD_WEAKEN", {}).get("targetNodeId") == "S10"
                and "WAIT" in kinds,
                json.dumps(a, ensure_ascii=False))

    # 4) 无人手可削弱 -> 强制通行兜底
    a = PlannerStrategy().decide(blocked_state(defense=6, bad=0, squad=1))
    ok &= check("突破: 无人手走强制通行",
                any(x["action"] == "FORCED_PASS" and x["targetNodeId"] == "S10"
                    for x in a),
                json.dumps(a, ensure_ascii=False))

    # 5) V3.8 改策：截止吃紧也按真实耗时选（削弱 32 帧 < 强通税 45），
    #    人手够就削弱 —— replay25 曾因 slack<0 选强通吃了 100 帧
    a = PlannerStrategy().decide(blocked_state(defense=6, bad=0, round_no=520))
    ok &= check("突破: 截止吃紧仍选更快的削弱",
                any(x["action"] == "SQUAD_WEAKEN" for x in a)
                and not any(x["action"] == "FORCED_PASS" for x in a),
                json.dumps(a, ensure_ascii=False))

    # 6) 规则限制（6.3.2，V3.18 修正语义）：限制绑定在【发起节点】——
    #    "主车队停在该节点时不能再次提交强制通行"，与目标无关
    st = PlannerStrategy()
    st._last_forced_node = "S09"   # 我们此刻就站在上次强通到达的 S09
    a = st.decide(blocked_state(defense=6, bad=0, squad=0))
    ok &= check("突破: 停在上次强通到达节点时禁发强通",
                not any(x["action"] == "FORCED_PASS" for x in a),
                json.dumps(a, ensure_ascii=False))

    # 6b) 反向验证：上次强通到达的是 S10（目标本身），现在从 S09 发起——
    #     规则允许（限制不在目标）。旧代码在这里自我禁足，双咽喉局
    #     （强通 S10 → 站 S10 想强通 S11）则反向漏判吃 FORCED_PASS_REPEAT
    st = PlannerStrategy()
    st._last_forced_node = "S10"
    a = st.decide(blocked_state(defense=6, bad=0, squad=0))
    ok &= check("突破: 从别处再次强通进同一节点合法放行",
                any(x["action"] == "FORCED_PASS" and x["targetNodeId"] == "S10"
                    for x in a),
                json.dumps(a, ensure_ascii=False))

    # ---- V3.14 蹲点补卡对策（replay36: 拆一次它 ≤3 好果原地补一次）----
    def camped_state(**kw):
        gs = blocked_state(**kw)
        for p in gs.players.values():
            if p["playerId"] != 1001:  # 卡主停靠在卡节点 S10 上
                p.update(state="IDLE", currentNodeId="S10", nextNodeId=None,
                         routeEdgeId=None, delivered=False, retired=False)
        return gs

    def camped_ordinary_state(**kw):
        gs = blocked_state(**kw)
        for p in gs.players.values():
            if p["playerId"] == 1001:
                p.update(currentNodeId="S07", taskScore=60)
            else:
                p.update(state="IDLE", currentNodeId="S09", nextNodeId=None,
                         routeEdgeId=None, delivered=False, retired=False)
        gs.nodes["S10"]["guard"] = None
        gs.nodes["S09"]["guard"] = {"ownerTeamId": "BLUE", "defense": kw.get("defense", 6),
                                    "maxDefense": 6, "active": True}
        return gs

    # 7) 卡主在场且补得起卡：先给 CAMPER_GRACE 帧宽限（语料 6/6 它读完临别卡
    #    次帧就走），赖着不走才认定真蹲点转强通（免试探——拆掉即被补满）
    st = PlannerStrategy()
    a = None
    for i in range(st.CAMPER_GRACE + 2):
        a = st.decide(camped_state(defense=6, bad=2, round_no=330 + i))
    ok &= check("突破: 蹲点者赖过宽限窗后直接强通（免试探）",
                any(x["action"] == "FORCED_PASS" and x["targetNodeId"] == "S10"
                    for x in a)
                and not any(x["action"] == "BREAK_GUARD" for x in a),
                json.dumps(a, ensure_ascii=False))

    # 7a) lose(5) vs2814：普通合流点过路卡未坐实为 camper，且当前弹药
    #     可秒破时，不能误走强通税（S09 防6 强通锁了 122 帧）。
    st = PlannerStrategy()
    a = None
    for i in range(st.CAMPER_GRACE + 2):
        a = st.decide(camped_ordinary_state(defense=6, good=96, bad=1,
                                            round_no=330 + i))
    ok &= check("突破: 普通过路卡可秒破时不强通",
                any(x["action"] == "BREAK_GUARD" and x["targetNodeId"] == "S09"
                    for x in a)
                and not any(x["action"] == "FORCED_PASS" for x in a),
                json.dumps(a, ensure_ascii=False))

    # 7a.1) 强通前离场预判：卡主正在做任务且剩 1 帧，先等短窗让它走，
    #       避免强通窗口刚锁死它就离场；短窗耗尽后仍不走才拆/强通。
    st = PlannerStrategy()
    for i in range(st.CAMPER_GRACE):
        st.decide(camped_ordinary_state(defense=6, good=96, bad=1,
                                        round_no=330 + i))
    gs_leave = camped_ordinary_state(defense=6, good=96, bad=1,
                                     round_no=330 + st.CAMPER_GRACE)
    for p in gs_leave.players.values():
        if p["playerId"] != 1001:
            p["state"] = P.ST_PROCESSING
            p["currentProcess"] = {"action": "CLAIM_TASK", "type": "CLAIM_TASK",
                                   "targetNodeId": "S08", "taskId": "T_X",
                                   "remainRound": 1}
    a = st.decide(gs_leave)
    ok &= check("突破: 卡主快离场时先等不锁强通窗口",
                any(x["action"] == "WAIT" for x in a)
                and not any(x["action"] in ("BREAK_GUARD", "FORCED_PASS") for x in a),
                json.dumps(a, ensure_ascii=False))

    # 7b) 临别卡宽限：卡主在宽限窗内离开 → 站在节点上白菜价攻坚
    #     （reports 局实锤：S10 本可 2好果+1坏果秒拆，实付 117 帧强通）
    st = PlannerStrategy()
    st.decide(camped_state(defense=6, bad=2, round_no=330))   # 宽限第 1 帧：等
    gs2 = blocked_state(defense=6, bad=2, round_no=332)       # 它走了（不在 S10）
    a = st.decide(gs2)
    ok &= check("突破: 宽限窗内卡主离开则节点攻坚",
                any(x["action"] == "BREAK_GUARD" and x["targetNodeId"] == "S10"
                    for x in a),
                json.dumps(a, ensure_ascii=False))

    # 8) 卡主在场但好果见底（关键关隘补卡底价 1 好果都掏不出）：放心攻坚
    gs = camped_state(defense=6, bad=2)
    for p_ in gs.players.values():
        if p_["playerId"] != 1001:
            p_["goodFruit"] = 0
    a = PlannerStrategy().decide(gs)
    ok &= check("突破: 蹲点者补不起卡则放心攻坚",
                any(x["action"] == "BREAK_GUARD" for x in a),
                json.dumps(a, ensure_ascii=False))

    # 9) 卡主在场且果品不够破：不派削弱喂饵（与中边冻结分支同一纪律），
    #    宽限后直接强通兜底
    st = PlannerStrategy()
    a = None
    for i in range(st.CAMPER_GRACE + 2):
        a = st.decide(camped_state(defense=6, bad=0, round_no=200 + i))
    ok &= check("突破: 卡主在场不削弱宽限后强通",
                not any(x["action"] == "SQUAD_WEAKEN" for x in a)
                and any(x["action"] == "FORCED_PASS" for x in a),
                json.dumps(a, ensure_ascii=False))

    # 10) 蹲点+强通被规则禁止（我们停在上次强通到达节点 S09）→ 老实等它离开
    st = PlannerStrategy()
    st._last_forced_node = "S09"
    a = None
    for i in range(st.CAMPER_GRACE + 2):
        a = st.decide(camped_state(defense=6, bad=2, round_no=330 + i))
    ok &= check("突破: 蹲点且强通被禁则等待",
                not any(x["action"] in ("BREAK_GUARD", "FORCED_PASS") for x in a),
                json.dumps(a, ensure_ascii=False))

    # 11) 首见帧吸收记录（V3.18）：老卡（首见已超宽限窗）+ 卡主在场 →
    #     到卡前第一帧就强通，不再重新起算 8 帧宽限。先在远处"看见"这张卡
    #     （吸收帧记录首见），再走到相邻节点测试
    st = PlannerStrategy()
    far = camped_state(defense=6, bad=2, round_no=318)
    for p_ in far.players.values():
        if p_["playerId"] == 1001:
            p_.update(currentNodeId="S07", nextNodeId=None)  # 还离卡两跳远
    st.decide(far)                                # r318 首见 S10 敌卡
    a = st.decide(camped_state(defense=6, bad=2, round_no=330))  # 到 S09 相邻
    ok &= check("突破: 老卡蹲点者到卡前立即强通（宽限只留给新卡）",
                any(x["action"] == "FORCED_PASS" and x["targetNodeId"] == "S10"
                    for x in a),
                json.dumps(a, ensure_ascii=False))

    # 12) 坐地户免宽限（V3.19）：对手起卡前已在节点驻扎 ≥20 帧 → 新卡也不给
    #     宽限，立即强通。宽限的依据是"临别卡"，坐地户不是过客
    st = PlannerStrategy()
    for i in range(24):    # r300~323 对手驻扎 S10、无卡（我们在 S09 防陷阱等待）
        st.decide(camped_state(defense=0, bad=2, round_no=300 + i))
    a = st.decide(camped_state(defense=6, bad=2, round_no=326))  # 新卡出现
    ok &= check("突破: 坐地户起新卡免宽限立即强通",
                any(x["action"] == "FORCED_PASS" and x["targetNodeId"] == "S10"
                    for x in a),
                json.dumps(a, ensure_ascii=False))

    # 12b) 反例：刚到就起卡的过客仍给宽限（V3.17 临别卡语料 6/6 不回退）
    st = PlannerStrategy()
    st.decide(camped_state(defense=0, bad=2, round_no=324))   # 对手刚停靠
    a = st.decide(camped_state(defense=6, bad=2, round_no=326))
    ok &= check("突破: 刚到的过客新卡仍给宽限",
                not any(x["action"] in ("FORCED_PASS", "BREAK_GUARD")
                        for x in a),
                json.dumps(a, ensure_ascii=False))
    return ok


def test_edge_block():
    """平台第二败局回归：半路被设卡冻结（S09->S10 边上冻 180 帧）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def edge_state(guard_def=6, squad=6, opp_setting=False, on_edge=True):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = 320
        d["contests"], d["tasks"] = [], []
        for p in d["players"]:
            if p["playerId"] == 1001:
                if on_edge:  # 挂在 E05 (S09->S10) 半路
                    p.update(state="MOVING", currentNodeId="S09", nextNodeId="S10",
                             routeEdgeId="E05", currentProcess=None, buffs=[],
                             squadAvailable=squad, resources={}, freshness=95.0,
                             goodFruit=96, badFruit=1)
                else:        # 停在 S09
                    p.update(state="IDLE", currentNodeId="S09", nextNodeId=None,
                             routeEdgeId=None, currentProcess=None, buffs=[],
                             squadAvailable=squad, resources={}, freshness=95.0,
                             goodFruit=96, badFruit=1)
            else:
                if opp_setting:
                    p["currentProcess"] = {"action": "SET_GUARD", "type": "SET_GUARD",
                                           "targetNodeId": "S10", "remainRound": 3}
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            if n["nodeId"] == "S10" and guard_def:
                n["guard"] = {"ownerTeamId": "BLUE", "defense": guard_def,
                              "maxDefense": 7, "active": True}
        gs.on_inquire(d)
        return gs

    # 1) 边上被冻结 -> 小分队削弱同帧出发，主车队不乱动
    a = PlannerStrategy().decide(edge_state())
    kinds = {x["action"]: x for x in a}
    ok &= check("边冻结: 派小分队削弱目标卡",
                kinds.get("SQUAD_WEAKEN", {}).get("targetNodeId") == "S10",
                json.dumps(a, ensure_ascii=False))
    ok &= check("边冻结: 主车队不提交无效动作",
                not any(x["action"] in ("MOVE", "BREAK_GUARD", "FORCED_PASS")
                        for x in a),
                json.dumps(a, ensure_ascii=False))

    # 2) 边上被冻结但无人手 -> 不崩溃，不发无效动作
    a = PlannerStrategy().decide(edge_state(squad=1))
    ok &= check("边冻结: 无人手不发无效动作",
                not any(x["action"] in ("SQUAD_WEAKEN", "MOVE", "BREAK_GUARD")
                        for x in a),
                json.dumps(a, ensure_ascii=False))

    # 3) 第一刀后仍冻在关键口边上：动用最后一组人手补第二刀，避免死等风化
    st = PlannerStrategy()
    st._weaken_sent["S10"] = 320 - PlannerStrategy.WEAKEN_RESEND_GAP
    a = st.decide(edge_state(guard_def=4, squad=3))
    ok &= check("边冻结: 关键口第一刀后低余量补第二刀",
                any(x["action"] == "SQUAD_WEAKEN" and x["targetNodeId"] == "S10"
                    for x in a),
                json.dumps(a, ensure_ascii=False))

    # 4) 停在节点、对手正读条设卡下一跳 -> 等卡成型后攻坚，不上边挨冻
    a = PlannerStrategy().decide(edge_state(guard_def=0, opp_setting=True,
                                            on_edge=False))
    main_acts = [x for x in a if x["action"] in P.MAIN_ACTION_TYPES]
    ok &= check("防冻结: 对手设卡读条中不上边",
                len(main_acts) == 1 and main_acts[0]["action"] == "WAIT",
                json.dumps(a, ensure_ascii=False))

    # 5) 卡成型后（停在节点）恢复正常攻坚
    a = PlannerStrategy().decide(edge_state(guard_def=6, on_edge=False))
    ok &= check("防冻结: 卡成型后节点攻坚",
                any(x["action"] == "BREAK_GUARD" and x["targetNodeId"] == "S10"
                    for x in a),
                json.dumps(a, ensure_ascii=False))
    return ok


def test_p0_audit():
    """策略自查修复回归：交付后静默 / 等待时用冰 / 任务资源前置。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def st_at(node, **me_kw):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["tasks"] = []
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", routeEdgeId=None, nextNodeId=None,
                         currentProcess=None, currentNodeId=node, buffs=[],
                         resources={}, freshness=95.0)
                p.update(me_kw)
        gs.on_inquire(d)
        return gs

    # 1) 交付后：即使有窗口在列，也一个动作不发（交付后违规每次 -5）
    gs = st_at("S15", delivered=True)
    a = PlannerStrategy().decide(gs)
    ok &= check("交付后: 完全静默(有窗口也不出牌)", a == [],
                json.dumps(a, ensure_ascii=False))

    # 2) 宫门等 RUSH 时鲜度低有冰鉴 -> 先用冰而不是干等
    gs = st_at("S14", freshness=84.0, resources={"ICE_BOX": 1})
    gs.contests = []
    a = PlannerStrategy().main_action(gs)
    ok &= check("等待中: 宫门等 RUSH 也用冰鉴",
                a and a["action"] == "USE_RESOURCE" and a["resourceType"] == "ICE_BOX",
                str(a))

    # 3) 缺前置资源的任务（T06 需马）不再被规划
    gs = st_at("S09", resources={})
    gs.contests = []
    gs.task_templates = {"T06": {"taskTemplateId": "T06",
                                 "requiredResourceTypes": ["FAST_HORSE", "SHORT_HORSE"]}}
    gs.tasks = [{"taskId": "T_X6", "taskTemplateId": "T06", "nodeId": "S09",
                 "processRound": 3, "score": 30, "expireRound": 0,
                 "active": True, "completed": False, "failed": False,
                 "ownerPlayerId": 0, "protectionPlayerId": 0}]
    st = PlannerStrategy()
    plan = st.planner.plan(gs)
    # 无马时不能把 T06 当任务目标（先领马属 resource 计划，是合理前置动作）
    ok &= check("任务前置: 无马不接 T06", plan.kind != "task", repr(plan))
    # 有马则接（就在脚下，净收益为正）
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["resources"] = {"SHORT_HORSE": 1}
    plan = PlannerStrategy().planner.plan(gs)
    ok &= check("任务前置: 有马就接 T06",
                plan.kind == "task" and plan.task["taskId"] == "T_X6", repr(plan))
    return ok


def test_horse_economy():
    """平台第4局回归：骑掉唯一的马导致 T06×2 做不了（任务分 60 封顶）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def moving_state(horses, round_no=91, task_score=30, phase="NORMAL"):
        gs = GameState(1001)
        gs.on_start(start)   # gameplay.taskCandidates 含 T06 -> 地图会刷马匹任务
        d = json.loads(json.dumps(inquire))
        d["round"], d["phase"] = round_no, phase
        d["contests"], d["tasks"] = [], []
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="MOVING", currentNodeId="S02", nextNodeId="S03",
                         routeEdgeId="E02", currentProcess=None, buffs=[],
                         resources=horses, freshness=95.0, taskScore=task_score)
        gs.on_inquire(d)
        return gs

    # 1) 只有 1 匹马 + 地图有 T06 候选 -> 不骑（留给任务）
    a = PlannerStrategy().decide(moving_state({"SHORT_HORSE": 1}))
    ok &= check("马匹: 唯一的马留给 T06 不骑",
                not any(x["action"] == "USE_RESOURCE" for x in a),
                json.dumps(a, ensure_ascii=False))

    # 2) 有 2 匹 -> 骑掉盈余的那匹
    a = PlannerStrategy().decide(moving_state({"SHORT_HORSE": 1, "FAST_HORSE": 1}))
    ok &= check("马匹: 盈余马正常骑",
                any(x["action"] == "USE_RESOURCE" for x in a),
                json.dumps(a, ensure_ascii=False))

    # 3) 任务分 >=110（里程碑拿满）-> 不再预留
    a = PlannerStrategy().decide(moving_state({"SHORT_HORSE": 1}, task_score=110))
    ok &= check("马匹: 里程碑拿满后不预留",
                any(x["action"] == "USE_RESOURCE" for x in a),
                json.dumps(a, ensure_ascii=False))

    # 4) RUSH 阶段 -> 不预留，全力赶路
    a = PlannerStrategy().decide(moving_state({"SHORT_HORSE": 1}, round_no=460,
                                              phase="RUSH"))
    ok &= check("马匹: RUSH 阶段全力赶路",
                any(x["action"] == "USE_RESOURCE" for x in a),
                json.dumps(a, ensure_ascii=False))

    # 5) 帧价值修正：绕路成本恒含用时分斜率（非竞速期取基准价；样例中对手
    #    已近 S10、我们还在 S02→S03，ETA 差远超竞争带 → race off）
    from lychee.planner import TaskPlanner, FRESH_VALUE_PER_FRAME, TIME_SCORE_PER_FRAME
    gs = moving_state({"SHORT_HORSE": 1})
    fv = TaskPlanner()._frame_value(gs, eta_direct=200)
    ok &= check("帧价值: 恒含用时斜率",
                abs(fv - (FRESH_VALUE_PER_FRAME + TIME_SCORE_PER_FRAME)) < 1e-9,
                f"fv={fv:.3f}")
    return ok


def test_watchdog():
    """人手账本兜底：squadAvailable 字段缺失也能派削弱。

    V3.12 删除了停滞看门狗改道：任务书 8.2 明确移动中只能 WAIT/续走当前
    目标/用马，中边 MOVE 改道是非法动作，该分支在真实服务端永远无法生效。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def frozen_inquire(round_no, guard_visible, squad_field):
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="MOVING", currentNodeId="S09", nextNodeId="S10",
                         routeEdgeId="E05", edgeProgressMs=6000, edgeTotalMs=55200,
                         currentProcess=None, buffs=[], resources={},
                         freshness=90.0, goodFruit=96, badFruit=0)
                if squad_field is None:
                    p.pop("squadAvailable", None)   # 平台字段缺失场景
                else:
                    p["squadAvailable"] = squad_field
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["resourceStock"] = {}
            n["guard"] = ({"ownerTeamId": "BLUE", "defense": 6, "active": True}
                          if (guard_visible and n["nodeId"] == "S10") else None)
        return d

    # 1) squadAvailable 字段缺失时：本地账本兜底，冻结仍派削弱
    gs = GameState(1001)
    gs.on_start(start)
    st = PlannerStrategy()
    gs.on_inquire(frozen_inquire(320, guard_visible=True, squad_field=None))
    a = st.decide(gs)
    ok &= check("账本: 字段缺失仍派削弱",
                any(x["action"] == "SQUAD_WEAKEN" and x["targetNodeId"] == "S10"
                    for x in a),
                json.dumps(a, ensure_ascii=False))

    # 2) V3.12 回归：中边不再提交非法改道 MOVE（8.2 移动中不能改道，
    #    该看门狗分支在真实服务端只会刷非法动作计数）
    gs2 = GameState(1001)
    gs2.on_start(start)
    st2 = PlannerStrategy()
    fired = False
    for i in range(12):
        gs2.on_inquire(frozen_inquire(320 + i, guard_visible=False, squad_field=6))
        a = st2.decide(gs2)
        if any(x["action"] == "MOVE" and x["targetNodeId"] != "S10" for x in a):
            fired = True
    ok &= check("看门狗: 中边不发非法改道", not fired)
    return ok


def test_active_guard():
    """V3 主动设卡：领先过咽喉时回手设卡挡对手。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_at(cur="S10", opp_pos="S07", round_no=200, phase="NORMAL",
              good=90, my_guards=(), node_guarded=False,
              opp_good=96, opp_bad=0, my_score=120, opp_score=90,
              opp_task=90, opp_squads=2):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"], d["phase"] = round_no, phase
        d["contests"], d["tasks"] = [], []
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=cur, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=95.0, goodFruit=good,
                         badFruit=0, taskScore=90, totalScore=my_score)
            else:
                p.update(state="MOVING" if opp_pos else "IDLE",
                         currentNodeId=opp_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None,
                         delivered=False, retired=False, goodFruit=opp_good,
                         badFruit=opp_bad, totalScore=opp_score,
                         taskScore=opp_task, squadAvailable=opp_squads)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["resourceStock"] = {}
            n["guard"] = None
            if n["nodeId"] in my_guards:
                n["guard"] = {"ownerTeamId": "RED", "defense": 4, "active": True}
            if node_guarded and n["nodeId"] == cur:
                n["guard"] = {"ownerTeamId": "BLUE", "defense": 4, "active": True}
        gs.on_inquire(d)
        return gs

    # 1) 我在 S10（关键关隘），对手在 S07 身后赶来 -> 设卡
    a = PlannerStrategy().main_action(gs_at())
    ok &= check("设卡: 领先过武关回手设卡",
                a and a["action"] == "SET_GUARD" and a["targetNodeId"] == "S10"
                and a.get("extraGoodFruit", 0) == 2,
                str(a))

    # 1b) 领先局若对手当前可一击破满防卡且 30+ 帧后才到，会把卡变成悬赏礼物
    st = PlannerStrategy()
    st._opp_profile = "farmer"
    a = st.main_action(gs_at(opp_bad=1))
    ok &= check("设卡: 领先局不送可秒破悬赏卡",
                not (a and a["action"] == "SET_GUARD"), str(a))

    # 1c) replay99：零设卡 farmer/跟随者攒着远程削弱弹药时，防 6 卡会被
    #     低成本拆掉，保好果优先。
    st = PlannerStrategy()
    st._opp_profile = "farmer"
    a = st.main_action(gs_at(opp_task=120, opp_squads=6))
    ok &= check("设卡: farmer 小分队充足时不喂防6卡",
                not (a and a["action"] == "SET_GUARD"), str(a))

    st = PlannerStrategy()
    st._opp_profile = "farmer"
    a = st.main_action(gs_at(opp_task=120, opp_squads=1))
    ok &= check("设卡: farmer 弹药不足时仍可兑现卡点",
                a and a["action"] == "SET_GUARD" and a["targetNodeId"] == "S10",
                str(a))

    st = PlannerStrategy()
    st._opp_profile = "farmer"
    st._own_guard_broken["S10"] = 196
    a = st.main_action(gs_at(opp_task=120, opp_squads=2))
    ok &= check("设卡: 同点卡刚被拆且对手仍有削弱弹药时不复立",
                not (a and a["action"] == "SET_GUARD"), str(a))

    # 2) 对手已过（在 S11，路线不再经过 S10）-> 不设
    a = PlannerStrategy().main_action(gs_at(opp_pos="S11"))
    ok &= check("设卡: 对手已过不白设",
                not (a and a["action"] == "SET_GUARD"), str(a))

    # 3) 已有 2 张有效卡 -> 不设（防顶掉旧卡）
    a = PlannerStrategy().main_action(gs_at(my_guards=("S03", "S08")))
    ok &= check("设卡: 已有2卡不再设",
                not (a and a["action"] == "SET_GUARD"), str(a))

    # 4) RUSH 阶段远离宫门的武关点仍专心交付；S13 起点二卡另有回归钉子
    a = PlannerStrategy().main_action(gs_at(round_no=460, phase="RUSH"))
    ok &= check("设卡: RUSH 远端不过度设卡",
                not (a and a["action"] == "SET_GUARD"), str(a))

    # 5) 该节点已有卡（谁的都算）-> 不设
    a = PlannerStrategy().main_action(gs_at(node_guarded=True))
    ok &= check("设卡: 节点已有卡不重复",
                not (a and a["action"] == "SET_GUARD"), str(a))

    # 6) 好果紧张 -> 不做对抗投资
    a = PlannerStrategy().main_action(gs_at(good=8))
    ok &= check("设卡: 好果紧张不投资",
                not (a and a["action"] == "SET_GUARD"), str(a))

    # 7) V3.12 回归：实战咽喉停靠 slack 分布 70~84（replay31 实测），旧闸门
    #    80 把整个档位拦掉（V3.7 后 4 局 0 触发的死分支）→ 65 后正常开卡
    a = PlannerStrategy().main_action(gs_at(round_no=300))
    ok &= check("设卡: slack 70 档位正常开卡",
                a and a["action"] == "SET_GUARD" and a["targetNodeId"] == "S10",
                str(a))

    # 8) 远距设卡风化门：对手从 S10 到 S14 约 138 帧，宫门卡到达时已残，
    #    且可能先挂悬赏；不应因全局 150 ETA 上限而提前白设。
    a = PlannerStrategy().main_action(gs_at(cur="S14", opp_pos="S10",
                                            round_no=260, my_score=700,
                                            opp_score=650))
    ok &= check("设卡: 对手太远时不到达残防不白设",
                not (a and a["action"] == "SET_GUARD"), str(a))
    return ok


def test_corridor():
    """V3.1 走廊竞争：对手阴影 / 处理站帧数 / 天气感知寻路。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_race(my_pos="S02", opp_pos="S07", weather=None, round_no=60,
                strip_process=True, opp_next=None, opp_edge=None,
                opp_progress=0, tasks=None):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["phase"] = P.PHASE_NORMAL
        d["contests"], d["tasks"] = [], list(tasks or [])
        d["weather"] = weather or {"active": [], "forecast": []}
        edge_total = (gs.graph.edge_total_move(gs.graph.edges[opp_edge])
                      if opp_edge else None)
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=my_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=95.0, goodFruit=95, badFruit=0)
            else:
                p.update(state="MOVING", currentNodeId=opp_pos,
                         nextNodeId=opp_next, routeEdgeId=opp_edge,
                         currentProcess=None, delivered=False, retired=False)
                if edge_total is not None:
                    p.update(edgeTotalMs=edge_total,
                             edgeProgressMs=opp_progress)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            if strip_process:   # 路线选择测试剥离处理站帧数干扰
                n.pop("processType", None)
                n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    # 1) 阴影集合：对手在 S03（官道前方）→ 官道下游 S07 被抢先；水路 S04/S05 干净
    gs = gs_race(my_pos="S02", opp_pos="S03")
    st = PlannerStrategy()
    shadow = st.planner._shadow_nodes(gs)
    ok &= check("阴影: 对手官道前方节点被标记",
                "S07" in shadow and "S04" not in shadow and "S05" not in shadow,
                f"shadow={sorted(shadow)}")

    # 2) 走廊选择：对手在官道前方(S03) → 我们从 S02 走水路 S04
    a = st.main_action(gs)
    ok &= check("走廊: 对手在官道前方走水路",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S04", str(a))

    # 2b) replay99：对手已用 E02 暴露官道承诺时，低位水路任务/直送兜底
    #     都不能再把 S02 岔口带去 S04。
    t_s04_water = {"taskId": "T_S04_WATER", "taskTemplateId": "T01",
                   "nodeId": "S04", "processRound": 4, "score": 30,
                   "expireRound": 221, "active": True, "completed": False,
                   "failed": False, "ownerPlayerId": 0,
                   "protectionPlayerId": 0, "routeBucket": P.WATER}
    gs = gs_race(my_pos="S02", opp_pos="S02", opp_next="S03",
                 opp_edge="E02", opp_progress=8000, tasks=(t_s04_water,))
    st = PlannerStrategy()
    plan = st.planner.plan(gs)
    a = st.main_action(gs, plan)
    ok &= check("走廊: 对手官道动身后不因低位水路切 S04",
                plan.kind != "task" and a and a["action"] == "MOVE"
                and a["targetNodeId"] == "S03",
                f"{plan} -> {a}")

    # 3) 天气改道：暴雨生效中（水路 x1.35）→ 从 S02 改走官道 S03
    rain_now = {"active": [{"type": "HEAVY_RAIN", "region": "WATER",
                            "remainRound": 50}], "forecast": []}
    gs2 = gs_race(my_pos="S02", opp_pos="S05", weather=rain_now)
    a = PlannerStrategy().main_action(gs2)
    ok &= check("走廊: 暴雨中改走官道",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S03", str(a))

    # 4) 处理站帧数计入惩罚：S04 登船 7 帧 / S05 水路换运 6 帧
    gs3 = gs_race(opp_pos=None, strip_process=False)
    for p in gs3.players.values():
        if p["playerId"] != 1001:
            p["currentNodeId"] = None
    pen = PlannerStrategy().planner._penalty_fn(gs3)
    ok &= check("ETA: 处理站帧数入惩罚",
                pen("S04") == 7 and pen("S05") == 6 and pen("S11") == 5,
                f"S04={pen('S04')} S05={pen('S05')} S11={pen('S11')}")

    # 5) 天气：暴雨生效中水路边成本 = 移动税x1.35 × 雨中鲜度因子(>1)；官道仅鲜度因子
    from lychee.planner import _FV
    from lychee import protocol as PP
    rain = {"active": [{"type": "HEAVY_RAIN", "region": "WATER", "remainRound": 40}],
            "forecast": []}
    gs4 = gs_race(weather=rain, opp_pos="S05")
    ec = PlannerStrategy().planner._edge_cost_fn(gs4)
    e12 = gs4.graph.edges["E12"]   # S04-S05 WATER
    e02 = gs4.graph.edges["E02"]   # S02-S03 ROAD
    f_water_rain = 1 + (0.045 * 1.3 - PP.IDLE_FRESH_DECAY) * 1.8 / _FV
    f_road = 1 + (0.055 - PP.IDLE_FRESH_DECAY) * 1.8 / _FV
    ok &= check("天气: 暴雨水路成本(移动税x鲜度)",
                abs(ec(e12, 100) - 135 * f_water_rain) < 1e-6
                and abs(ec(e02, 100) - 100 * f_road) < 1e-6,
                f"water={ec(e12, 100):.1f} (exp {135*f_water_rain:.1f}) "
                f"road={ec(e02, 100):.1f}")
    ok &= check("天气: 雨中水路鲜度反超基准", f_water_rain > 1.0,
                f"factor={f_water_rain:.3f}")

    # 6) 预告暴雨（近期窗口内）移动税按半额计（鲜度因子按无雨基准）
    fc = {"active": [], "forecast": [{"type": "HEAVY_RAIN", "region": "WATER",
                                      "startRound": 120, "durationRound": 60}]}
    gs5 = gs_race(weather=fc, round_no=60)
    ec = PlannerStrategy().planner._edge_cost_fn(gs5)
    f_water = 1 + (0.045 - PP.IDLE_FRESH_DECAY) * 1.8 / _FV
    ok &= check("天气: 预告暴雨半额计",
                abs(ec(gs5.graph.edges["E12"], 100) - 117.5 * f_water) < 1e-6,
                f"{ec(gs5.graph.edges['E12'], 100):.1f} (exp {117.5*f_water:.1f})")
    return ok


def test_ice_hunt():
    """V3.2 冰鉴猎手回归（败局13：水路竞速零冰鉴，75:91 鲜度输 27 分）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_ice(my_pos="S02", opp_pos="S02", my_ice=0, round_no=48,
               ice_nodes=("S03", "S07")):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=my_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={"ICE_BOX": my_ice} if my_ice else {},
                         freshness=95.0, goodFruit=95, badFruit=0, taskScore=60)
            else:
                p.update(state="IDLE", currentNodeId=opp_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None,
                         delivered=False, retired=False)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = ({"ICE_BOX": 1} if n["nodeId"] in ice_nodes else {})
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    # 1) 双方同在 S02 起跑：S03 有冰 → 规划资源目标，走官道抢冰（放弃纯水路）
    gs = gs_ice()
    st = PlannerStrategy()
    plan = st.planner.plan(gs)
    ok &= check("冰猎: 同位起跑规划抢 S03 冰",
                plan.kind == "resource" and plan.position == "S03"
                and plan.resource == "ICE_BOX", repr(plan))
    a = st.main_action(gs, plan)
    ok &= check("冰猎: 官道向 S03 进发",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S03", str(a))

    # 2) 对手已在 S03（会被扫空）→ 放弃抢冰走水路
    gs = gs_ice(opp_pos="S03")
    st = PlannerStrategy()
    plan = st.planner.plan(gs)
    a = st.main_action(gs, plan)
    ok &= check("冰猎: 对手先到放弃抢冰",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S04",
                f"{plan!r} -> {a}")

    # 3) 到位领取
    gs = gs_ice(my_pos="S03", opp_pos="S01")
    st = PlannerStrategy()
    plan = st.planner.plan(gs)
    a = st.main_action(gs, plan)
    ok &= check("冰猎: 到位 CLAIM_RESOURCE",
                a and a["action"] == "CLAIM_RESOURCE"
                and a["resourceType"] == "ICE_BOX", f"{plan!r} -> {a}")

    # 4) 已持 2 冰 → 不再规划资源目标
    gs = gs_ice(my_ice=2)
    plan = PlannerStrategy().planner.plan(gs)
    ok &= check("冰猎: 持满 2 冰不再绕路", plan.kind != "resource", repr(plan))

    # 5) 截止吃紧 → 不为资源冒险
    gs = gs_ice(round_no=520)
    plan = PlannerStrategy().planner.plan(gs)
    ok &= check("冰猎: 截止吃紧直奔交付", plan.kind == "deliver", repr(plan))
    return ok


def test_fresh_race():
    """V3.3 鲜度竞赛回归（败局14：山冰独食 80 鲜度 vs 官道双冰 93，输 16）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]
    from lychee.planner import ROUTE_FRESH_FACTOR

    # 1) 路线鲜度定价：山路 > 支路 > 官道 > 1 > 水路
    ok &= check("鲜度定价: 山>支>官>1>水",
                ROUTE_FRESH_FACTOR["MOUNTAIN"] > ROUTE_FRESH_FACTOR["BRANCH"]
                > ROUTE_FRESH_FACTOR["ROAD"] > 1.0 > ROUTE_FRESH_FACTOR["WATER"],
                json.dumps({k: round(v, 3) for k, v in ROUTE_FRESH_FACTOR.items()}))

    def gs_open(ice_nodes, my_pos="S01", opp_pos="S01", round_no=2):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=my_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=100.0, goodFruit=100,
                         badFruit=0, taskScore=0)
            else:
                p.update(state="IDLE", currentNodeId=opp_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None,
                         delivered=False, retired=False)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = ({"ICE_BOX": 1} if n["nodeId"] in ice_nodes else {})
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    # 2) 败局14开局重演：S03+S07 官道双冰(对手必经) vs S06 山冰独食
    #    拒止×1.5 + 链式半权 应选官道 S03，不再上山
    gs = gs_open(("S03", "S06", "S07"))
    st = PlannerStrategy()
    plan = st.planner.plan(gs)
    ok &= check("鲜度竞赛: 选官道双冰链而非山冰独食",
                plan.kind == "resource" and plan.position == "S03", repr(plan))

    # 3) 只剩 S06 山冰（官道冰没了）→ 仍去 S06（有比没有强）
    gs = gs_open(("S06",))
    plan = PlannerStrategy().planner.plan(gs)
    ok &= check("鲜度竞赛: 仅剩山冰仍去取",
                plan.kind == "resource" and plan.position == "S06", repr(plan))

    # 4) 链式估值：三冰在场时 S03（官道链头）估值严格最高
    st = PlannerStrategy()
    gs = gs_open(("S03", "S06", "S07"))
    pen, ec = st.planner._penalty_fn(gs), st.planner._edge_cost_fn(gs)
    to_gate = gs.graph.shortest_path("S01", "S14", 1000, pen, ec)[0]
    targets = {(n, r): v for n, r, v in st.planner._resource_targets(
        gs, "S01", to_gate, 400, 1000, pen, ec)}
    v_s03 = targets.get(("S03", "ICE_BOX"), 0)
    v_s06 = targets.get(("S06", "ICE_BOX"), 0)
    v_s07 = targets.get(("S07", "ICE_BOX"), 0)
    ok &= check("鲜度竞赛: 链头 S03 估值严格最高",
                v_s03 > v_s07 > 0 and v_s03 > v_s06 > 0,
                f"S03={v_s03:.1f} S07={v_s07:.1f} S06={v_s06:.1f}")
    return ok


def test_honest_eta():
    """V3.4 回归：交付截止用真实时间，价值定价不得吓熔断规划器。

    真实地图实测：鲜度因子+阴影混进 ETA 后开局估 542 帧（实际 454），
    slack=-26 → 第 2 帧进抢救模式，任务/资源全部熔断。
    """
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    gs = GameState(1001)
    gs.on_start(start)
    d = json.loads(json.dumps(inquire))
    d["round"] = 2
    d["contests"], d["tasks"] = [], []
    d["weather"] = {"active": [], "forecast": []}
    for p in d["players"]:
        p.update(state="IDLE", currentNodeId="S01", nextNodeId=None,
                 routeEdgeId=None, currentProcess=None, buffs=[],
                 delivered=False, retired=False)
        if p["playerId"] == 1001:
            p.update(resources={}, freshness=100.0, goodFruit=100,
                     badFruit=0, taskScore=0)
    gs.on_inquire(d)  # 保留样例中的障碍/处理站，模拟真实开局

    st = PlannerStrategy()
    pl = st.planner
    # 1) 时间成本 < 价值成本（山路边上鲜度因子只进价值侧）
    e15 = gs.graph.edges["E15"]  # S01-S06 MOUNTAIN
    tc = pl._time_edge_cost_fn(gs)(e15, 100)
    vc = pl._edge_cost_fn(gs)(e15, 100)
    ok &= check("诚实ETA: 山路鲜度因子只进价值侧", tc == 100 and vc > 110,
                f"time={tc} value={vc:.1f}")

    # 2) slack 按时间成本计（正值），且规划器不熔断
    from lychee.planner import (GATE_VERIFY_FRAMES, DELIVER_FRAMES, SAFETY_MARGIN)
    tg_t = gs.graph.shortest_path("S01", "S14", 1000,
                                  pl._time_penalty_fn(gs),
                                  pl._time_edge_cost_fn(gs))[0]
    g2t = gs.graph.shortest_path("S14", "S15", 1000)[0]
    expect = 600 - (2 + tg_t + GATE_VERIFY_FRAMES + g2t + DELIVER_FRAMES
                    + SAFETY_MARGIN)
    plan = pl.plan(gs)
    ok &= check("诚实ETA: slack=时间口径且为正",
                abs(plan.slack - expect) < 1e-6 and plan.slack > 0,
                f"slack={plan.slack:.0f} expect={expect:.0f}")
    ok &= check("诚实ETA: 开局不进抢救模式", "deadline" not in plan.detail,
                repr(plan))

    # 3) 对手合理走廊并集：在 S02 时官道 S03 与水路 S04 都在预测集内
    gs2 = GameState(1001)
    gs2.on_start(start)
    d2 = json.loads(json.dumps(d))
    for p in d2["players"]:
        if p["playerId"] != 1001:
            p["currentNodeId"] = "S02"
    gs2.on_inquire(d2)
    opp_path = PlannerStrategy().planner._opp_path_nodes(gs2)
    ok &= check("走廊预测: 官道水路双覆盖",
                "S03" in opp_path and "S04" in opp_path,
                f"{sorted(opp_path)}")
    return ok


def test_trap_proof():
    """V3.5 回归（replay20：S11 中边连环陷阱冻到终场，60:754 未交付）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_tail(opp_cur="S11", opp_next=None, opp_edge=None, round_no=380,
                guard_def=0):
        """我在 S10 空闲欲往 S11（必经之路），对手位置可配。"""
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId="S10", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=90.0, goodFruit=90,
                         badFruit=2, taskScore=90, squadAvailable=1)
            else:
                p.update(state="MOVING" if opp_edge else "IDLE",
                         currentNodeId=opp_cur, nextNodeId=opp_next,
                         routeEdgeId=opp_edge, currentProcess=None,
                         delivered=False, retired=False)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
            if n["nodeId"] == "S11" and guard_def:
                n["guard"] = {"ownerTeamId": "BLUE", "defense": guard_def,
                              "maxDefense": 6, "active": True}
        gs.on_inquire(d)
        return gs

    def gs_s09_opp_forced():
        """replay93: 我在 S09，要进 S10；对手已从 S10 强通离开。"""
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = 330
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId="S09", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=88.0, goodFruit=94,
                         badFruit=1, taskScore=90, squadAvailable=1)
            else:
                p.update(state=P.ST_FORCED_PASSING, currentNodeId="S10",
                         nextNodeId=None, routeEdgeId=None,
                         currentProcess={"action": "FORCED_PASS",
                                         "type": "FORCED_PASS",
                                         "targetNodeId": "S11",
                                         "remainRound": 30},
                         delivered=False, retired=False, goodFruit=94,
                         badFruit=1, taskScore=90)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    # 0) V3.12 删证据门回归：对手从未设卡，但它占着我们的咽喉下一跳 →
    #    照样等待。首卡必然没有前科（replay36: 2614 全场首卡掐上边冻 195 帧
    #    零交付），误伤上界为对手真实停留时长 << 冻结 180+ 帧
    a = PlannerStrategy().main_action(gs_tail())
    ok &= check("防陷阱: 首卡也设防(无前科照等)",
                a and a["action"] == "WAIT", str(a))

    # 0b) 地形门已删（V3.22，2839 复盘根因 C）：普通驿站同样设防——
    #     第一名的回手卡打的是走廊汇入点不限咽喉（实战第 4 局 S09 掐
    #     踏边 118 帧、离线 TollerBot 复现冻 60~120 帧）。replay27 想防
    #     的误伤由等待上界兜底：过客做完站务就走，几帧 << 冻结代价
    gs = gs_tail()
    for n in gs.nodes.values():
        if n["nodeId"] == "S11":
            n["nodeType"] = "STATION"
    a = PlannerStrategy().main_action(gs)
    ok &= check("防陷阱: 普通驿站同样设防(V3.22)",
                a and a["action"] == "WAIT", str(a))

    # 1) 对手正站在我们的下一跳（咽喉 S11）→ 不上边，等待
    a = PlannerStrategy().main_action(gs_tail())
    ok &= check("防陷阱: 对手占下一跳时等待",
                a and a["action"] == "WAIT", str(a))

    # 2) 对手在赶往 S11 且明显先到 → 同样等待
    a = PlannerStrategy().main_action(
        gs_tail(opp_cur="S12", opp_next="S11", opp_edge="E07"))
    ok &= check("防陷阱: 对手先到下一跳时等待",
                a and a["action"] == "WAIT", str(a))

    # 2b) replay235919：普通节点也有窄窗口收敛掐边风险。我方长边将进
    #     S09，对手很快先到 S09；多等十几帧可躲 100+ 帧临别卡税。
    gs = gs_tail(opp_cur="S12", opp_next="S11", opp_edge="E07")
    for n in gs.nodes.values():
        if n["nodeId"] == "S11":
            n["nodeType"] = "STATION"
    a = PlannerStrategy().main_action(gs)
    ok &= check("防陷阱: 普通长边收敛也短等让过客先离站",
                a and a["action"] == "WAIT", str(a))

    # 2c) 死线逃逸：咽喉已经等穿余量时，继续等=确定未交付，允许赌边。
    st_escape = PlannerStrategy()
    st_escape._trap_wait = ("S11", st_escape.TRAP_DEADLINE_ESCAPE_WAIT)
    gs = gs_tail()
    gs.phase = P.PHASE_RUSH
    a = st_escape.main_action(gs, Plan("deliver", slack=-30))
    ok &= check("防陷阱: 咽喉等待烧穿死线后赌边逃逸",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S11",
                str(a))

    # 3) 对手已离开（在 S12 且驶向 S13）→ 正常上边
    a = PlannerStrategy().main_action(
        gs_tail(opp_cur="S12", opp_next="S13", opp_edge="E08"))
    ok &= check("防陷阱: 对手离开后正常推进",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S11", str(a))

    # 3b) replay93：强通离开中的对手不能在原节点设卡，别把 S10 当蹲点
    a = PlannerStrategy().main_action(gs_s09_opp_forced())
    ok &= check("防陷阱: 对手强通离开原节点时不误等",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S10", str(a))

    # 4) 它留了卡 → 站在节点上攻坚拆（好果2×2+坏果2×3=10 ≥ 6）
    a = PlannerStrategy().main_action(
        gs_tail(opp_cur="S12", opp_next="S13", opp_edge="E08", guard_def=6))
    ok &= check("防陷阱: 留卡则节点攻坚瞬拆",
                a and a["action"] == "BREAK_GUARD" and a["targetNodeId"] == "S11",
                str(a))

    # 5) 对峙上限只适用于"正在赶来"的汇聚窗口（V3.14）：
    # 5a) 对手常驻下一跳（蹲点）→ 永不硬闯。设卡读条 4 帧比任何边都短，
    #     一上边它随手起卡就是必冻（replay36: 硬闯 71 帧长边冻 195 帧未交付）
    st = PlannerStrategy()
    last = None
    for i in range(35):
        last = st.main_action(gs_tail(round_no=380 + i))
    ok &= check("防陷阱: 蹲点者常驻下一跳永不硬闯",
                last and last["action"] == "WAIT", str(last))

    # 5b) 对手"正在赶来"同样不硬闯（V3.15 删对峙上限——replay56 直接死因：
    #     r305 上限到点硬闯 71 帧长边，对手 r310 到点 r314 起卡冻死）。
    #     汇聚窗口以对手到点自然收束：到点后离开→风险解除（用例 3）、
    #     设卡→enemy_guard 分支接管（用例 4）、干蹲→常驻情形（用例 5a）
    st = PlannerStrategy()
    last = None
    for i in range(40):
        last = st.main_action(
            gs_tail(opp_cur="S12", opp_next="S11", opp_edge="E07", round_no=380 + i))
    ok &= check("防陷阱: 汇聚中超过旧上限仍不硬闯",
                last and last["action"] == "WAIT", str(last))

    # 6) 截止吃紧也不赌：slack 越紧冻结越致命（等待 10~30 帧 vs 冻结 180+ 帧）
    a = PlannerStrategy().main_action(gs_tail(round_no=545))
    ok &= check("防陷阱: 截止吃紧仍不上险边",
                a and a["action"] == "WAIT", str(a))

    # 7) 对手已交付 → 无陷阱风险
    gs = gs_tail()
    for p in gs.players.values():
        if p["playerId"] != 1001:
            p["delivered"] = True
    a = PlannerStrategy().main_action(gs)
    ok &= check("防陷阱: 对手已交付不误等",
                a and a["action"] == "MOVE", str(a))
    return ok


def test_bundle():
    """V3.6 回归（replay21/22：等分任务二选一时，无视沿途双冰选了水路）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_fork(ice_nodes=("S03", "S07")):
        """replay21 开局重演：T01@S03(官道) 与 T08@S04(水路) 同为 30 分。"""
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = 2
        d["contests"] = []
        d["weather"] = {"active": [], "forecast": []}
        d["tasks"] = [
            {"taskId": "T_001", "taskTemplateId": "T01", "nodeId": "S03",
             "processRound": 3, "score": 30, "expireRound": 221,
             "active": True, "completed": False, "failed": False,
             "ownerPlayerId": 0, "protectionPlayerId": 0},
            {"taskId": "T_002", "taskTemplateId": "T08", "nodeId": "S04",
             "processRound": 4, "score": 30, "expireRound": 221,
             "active": True, "completed": False, "failed": False,
             "ownerPlayerId": 0, "protectionPlayerId": 0},
        ]
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId="S01", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=100.0, goodFruit=100,
                         badFruit=0, taskScore=0)
            else:
                p.update(state="IDLE", currentNodeId="S01", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None,
                         delivered=False, retired=False)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = ({"ICE_BOX": 1} if n["nodeId"] in ice_nodes else {})
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    # 1) 官道任务捆双冰 vs 水路裸任务 → 选官道 T_001@S03
    plan = PlannerStrategy().planner.plan(gs_fork())
    ok &= check("捆绑: 等分任务选带双冰的官道",
                plan.kind == "task" and plan.position == "S03"
                and plan.task["taskId"] == "T_001", repr(plan))

    # 2) 没有冰时退回纯净值比较（不误偏官道）
    plan = PlannerStrategy().planner.plan(gs_fork(ice_nodes=()))
    ok &= check("捆绑: 无资源时按裸净值决策",
                plan.kind == "task", repr(plan))

    # 3) 冰已持满（2个）→ 捆绑不再加分，回到裸净值
    gs = gs_fork()
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["resources"] = {"ICE_BOX": 2}
    plan = PlannerStrategy().planner.plan(gs)
    ok &= check("捆绑: 持满冰不重复计价", plan.kind == "task", repr(plan))
    return ok


def test_tempo_guard():
    """V3.7 回归（replay23：S02 让行送先手丢冰链；边上对手 ETA=0 设卡从未触发）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_guard_moment(opp_progress=0.6):
        """replay23 r287 重演：我在武关 S10 空闲，对手在 S09→S10 边上 60%。"""
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = 287
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId="S10", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=85.0, goodFruit=95,
                         badFruit=1, taskScore=120, totalScore=120)
            else:
                total = 55200
                p.update(state="MOVING", currentNodeId="S09", nextNodeId="S10",
                         routeEdgeId="E05", edgeTotalMs=total,
                         edgeProgressMs=int(total * opp_progress),
                         currentProcess=None, delivered=False, retired=False,
                         goodFruit=94, badFruit=1, totalScore=90)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    # 1) 对手边上 ETA 含剩余进度（不再是 0）
    gs = gs_guard_moment()
    eta = PlannerStrategy().planner._opp_eta(gs, "S10")
    ok &= check("对手ETA: 含边上剩余进度", 15 <= eta <= 30, f"eta={eta:.1f}")

    # 2) 武关设卡时机触发（replay23 r287 的教科书场景）
    st = PlannerStrategy()
    plan = st.planner.plan(gs)
    a = st.main_action(gs, plan)
    ok &= check("设卡: 武关回手卡触发",
                a and a["action"] == "SET_GUARD" and a["targetNodeId"] == "S10",
                f"plan={plan.kind} -> {a}")

    # 3) 对手已过半很久（progress 0.98，ETA<8）→ 来不及成卡，不设
    gs = gs_guard_moment(opp_progress=0.98)
    st = PlannerStrategy()
    a = st.main_action(gs, st.planner.plan(gs))
    ok &= check("设卡: 对手将至不硬设",
                not (a and a["action"] == "SET_GUARD"), str(a))
    return ok


def test_replay25():
    """V3.8 回归（replay25 三宗罪：冰被偷 / 回头 / 强通吃100帧）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def base_gs(my_pos, opp_pos, opp_next=None, opp_edge=None, opp_prog=0,
                round_no=86, ice_nodes=(), tasks=(), squad=6, bad=0):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"] = []
        d["tasks"] = list(tasks)
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=my_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=90.0, goodFruit=95,
                         badFruit=bad, taskScore=60, squadAvailable=squad)
            else:
                p.update(state="MOVING" if opp_edge else "IDLE",
                         currentNodeId=opp_pos, nextNodeId=opp_next,
                         routeEdgeId=opp_edge, edgeTotalMs=34500,
                         edgeProgressMs=opp_prog, currentProcess=None,
                         delivered=False, retired=False)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = ({"ICE_BOX": 1} if n["nodeId"] in ice_nodes else {})
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    T_S03 = {"taskId": "T_001", "taskTemplateId": "T01", "nodeId": "S03",
             "processRound": 3, "score": 30, "expireRound": 300, "active": True,
             "completed": False, "failed": False, "ownerPlayerId": 0,
             "protectionPlayerId": 0}

    # 1) 冰保卫：在 S03 有任务+冰，对手 S02→S03 边上快到 → 先领冰再做任务
    gs = base_gs("S03", "S02", opp_next="S03", opp_edge="E02",
                 opp_prog=28000, ice_nodes=("S03",), tasks=(T_S03,))
    st = PlannerStrategy()
    a = st.main_action(gs, st.planner.plan(gs))
    ok &= check("冰保卫: 对手将至先领冰",
                a and a["action"] == "CLAIM_RESOURCE"
                and a["resourceType"] == "ICE_BOX", str(a))

    # 2) 对手远（还在 S01）→ 正常先做任务
    gs = base_gs("S03", "S01", ice_nodes=("S03",), tasks=(T_S03,))
    st = PlannerStrategy()
    a = st.main_action(gs, st.planner.plan(gs))
    ok &= check("冰保卫: 对手远则任务优先",
                a and a["action"] == "CLAIM_TASK", str(a))

    # 3) 回头迟滞：刚从 S03 到 S02，回头目标被课税，前进免税
    gs = base_gs("S02", "S07", round_no=100, ice_nodes=("S03",))
    st = PlannerStrategy()
    st.planner.back_node = "S03"
    st.planner.back_until = 130
    tax = st.planner._backtrack_tax(gs, "S02", "S03")
    tax_fwd = st.planner._backtrack_tax(gs, "S02", "S04")
    ok &= check("回头迟滞: 回头课税/前进免税",
                tax == 25 and tax_fwd == 0, f"back={tax} fwd={tax_fwd}")
    st.planner.back_until = 90
    ok &= check("回头迟滞: 窗口过期免税",
                st.planner._backtrack_tax(gs, "S02", "S03") == 0, "")

    # 4) 突破选择：坏果破不动防6卡、人手够 → 削弱路径（即便 slack 为负）
    gs = base_gs("S09", "S12", round_no=520, squad=6, bad=0)
    for n in gs.nodes.values():
        if n["nodeId"] == "S10":
            n["guard"] = {"ownerTeamId": "BLUE", "defense": 6,
                          "maxDefense": 7, "active": True}
    st = PlannerStrategy()
    acts = st.decide(gs)
    ok &= check("突破: 削弱比强通快就削弱(不看slack)",
                any(x["action"] == "SQUAD_WEAKEN" and x["targetNodeId"] == "S10"
                    for x in acts)
                and not any(x["action"] == "FORCED_PASS" for x in acts),
                json.dumps(acts, ensure_ascii=False))

    # 5) 削到能拆即止（V3.17）：坏果 0 时好果攻坚上限 4，防 6 只需削 1 次
    #    到防 4 —— 2 人手足够（reports 局：人手 5 被"削到 0 要 6 人手"拒掉，
    #    白吃 70 帧强通税）
    gs = base_gs("S09", "S12", round_no=520, squad=2, bad=0)
    for n in gs.nodes.values():
        if n["nodeId"] == "S10":
            n["guard"] = {"ownerTeamId": "BLUE", "defense": 6,
                          "maxDefense": 7, "active": True}
    acts = PlannerStrategy().decide(gs)
    ok &= check("突破: 削到能拆即止（2 人手削防6卡）",
                any(x["action"] == "SQUAD_WEAKEN" for x in acts)
                and not any(x["action"] == "FORCED_PASS" for x in acts),
                json.dumps(acts, ensure_ascii=False))

    # 5b) 人手 1（连一次削弱都不够）→ 强通兜底不变
    gs = base_gs("S09", "S12", round_no=520, squad=1, bad=0)
    for n in gs.nodes.values():
        if n["nodeId"] == "S10":
            n["guard"] = {"ownerTeamId": "BLUE", "defense": 6,
                          "maxDefense": 7, "active": True}
    a = PlannerStrategy().main_action(gs)
    ok &= check("突破: 人手见底仍强通兜底",
                a and a["action"] == "FORCED_PASS", str(a))
    return ok


def test_tail_farm():
    """V3.10 回归（29/30/31：尾段任务饥荒，对手靠身后刷新农到 180）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_tail2(cur="S12", round_no=300, base=90):
        """S12 是 T13/T14 候选点；无可做任务，对手在前方（S13→S14）。

        V3.10.1 跟随者闸门：蹲刷是跟随者战术，领先时不蹲。"""
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"], d["phase"] = round_no, "NORMAL"
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=cur, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=90.0, goodFruit=95,
                         badFruit=0, taskScore=base)
            else:
                p.update(state="MOVING", currentNodeId="S13", nextNodeId="S14",
                         routeEdgeId="E09", edgeTotalMs=24840, edgeProgressMs=9000,
                         currentProcess=None, delivered=False, retired=False)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    # 1) 候选点 + 余量足 + 里程碑未满 + 对手在前方 → 蹲刷等待
    a = PlannerStrategy().main_action(gs_tail2())
    ok &= check("蹲刷: 候选点上等任务刷新",
                a and a["action"] == "WAIT", str(a))

    # 1b) 领先时不蹲（跟随者闸门）：对手在身后 → 保节奏，推进或设卡
    gs = gs_tail2()
    for p in gs.players.values():
        if p["playerId"] != 1001:
            p.update(currentNodeId="S09", nextNodeId="S10", routeEdgeId="E05",
                     edgeTotalMs=55200, edgeProgressMs=9000)
    a = PlannerStrategy().main_action(gs)
    ok &= check("蹲刷: 领先时保节奏不蹲",
                a and a["action"] in ("MOVE", "SET_GUARD"), str(a))

    # 2) 任务基础分已到 110 → 不蹲，直接推进
    a = PlannerStrategy().main_action(gs_tail2(base=110))
    ok &= check("蹲刷: 里程碑拿满不蹲",
                a and a["action"] == "MOVE", str(a))

    # 3) 余量不足（r470）→ 不蹲
    a = PlannerStrategy().main_action(gs_tail2(round_no=470))
    ok &= check("蹲刷: 余量不足直奔交付",
                a and a["action"] == "MOVE", str(a))

    # 4) 预算耗尽后放行
    st = PlannerStrategy()
    last = None
    for i in range(55):
        last = st.main_action(gs_tail2(round_no=300 + i))
    ok &= check("蹲刷: 预算耗尽后推进",
                last and last["action"] == "MOVE", str(last))

    # 5) 刷出任务立即接住：加一个 S12 的任务 → plan 变 task 且当帧领取
    gs = gs_tail2()
    gs.tasks = [{"taskId": "T_N", "taskTemplateId": "T13", "nodeId": "S12",
                 "processRound": 5, "score": 15, "expireRound": 420,
                 "active": True, "completed": False, "failed": False,
                 "ownerPlayerId": 0, "protectionPlayerId": 0}]
    st = PlannerStrategy()
    plan = st.planner.plan(gs)
    a = st.main_action(gs, plan)
    ok &= check("蹲刷: 刷出任务立即领取",
                plan.kind == "task" and a and a["action"] == "CLAIM_TASK",
                f"{plan.kind} -> {a}")
    return ok


def test_reject_join():
    """V3.12 P1 回归（replay20/36：ACTION_REJECTED 载荷无 action 字段，
    拉黑分支从未命中；S08 逐帧重试障碍已清的死 T04 达 38/27 帧）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_at(round_no, tasks, events=(), obstacle_nodes=()):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"] = []
        d["tasks"] = tasks
        d["events"] = list(events)
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId="S08", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=90.0, goodFruit=90,
                         badFruit=1, taskScore=120, squadAvailable=8)
            else:
                p.update(state="IDLE", currentNodeId="S01", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None,
                         delivered=True, retired=False)
        for n in d["nodes"]:
            n["hasObstacle"] = n["nodeId"] in obstacle_nodes
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    dead_t04 = {"taskId": "T_009", "taskTemplateId": "T04", "nodeId": "S08",
                "score": 30, "active": True, "completed": False, "failed": False,
                "ownerPlayerId": 0, "expireRound": 999, "processRound": 6}
    live_t04 = dict(dead_t04, taskId="T_010", nodeId="S06")

    # 1) 障碍已清的 T04 不再被选为目标（规划层可行性过滤）
    st = PlannerStrategy()
    gs = gs_at(260, [dead_t04])
    plan = st.planner.plan(gs)
    ok &= check("拒绝join: 死T04不入计划",
                plan.kind != "task" or (plan.task or {}).get("taskId") != "T_009",
                repr(plan))

    # 1b) 障碍仍在的 T04 正常可选（不要误杀活任务）
    gs = gs_at(260, [live_t04], obstacle_nodes=("S06",))
    plan = st.planner.plan(gs)
    ok &= check("拒绝join: 活T04仍可做",
                plan.kind == "task" and plan.task["taskId"] == "T_010",
                repr(plan))

    # 2) 载荷缺 action 字段的拒绝：按上一帧提交的主动作 join → 拉黑该任务
    st = PlannerStrategy()
    st._last_main_action = {"action": "CLAIM_TASK", "taskId": "T_777"}
    rej = {"type": "ACTION_REJECTED",
           "payload": {"playerId": 1001, "errorCode": "TASK_REQUIREMENT_NOT_MET"}}
    st.decide(gs_at(261, [dead_t04], events=[rej]))
    ok &= check("拒绝join: 无action字段也拉黑",
                st.planner.blacklist.get("T_777", 0) > 261,
                str(st.planner.blacklist))

    # 3) 上一帧主动作不是 CLAIM_TASK 时，缺字段拒绝不误伤拉黑
    st = PlannerStrategy()
    st._last_main_action = {"action": "MOVE", "targetNodeId": "S10"}
    st.decide(gs_at(262, [dead_t04], events=[rej]))
    ok &= check("拒绝join: 非任务动作不误拉黑",
                not st.planner.blacklist, str(st.planner.blacklist))
    return ok


def test_weaken_discipline():
    """V3.12 P2 回归（replay36: r315-317 连发 3 削弱清零防6，卡主站在 S10
    原地补卡，6 人手白烧还倒亏 16 帧；replay20: 人手烧光后 S11 二次冻结）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def frozen(round_no=320, squad=8, opp_at_guard=False):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="MOVING", currentNodeId="S09", nextNodeId="S10",
                         routeEdgeId="E05", currentProcess=None, buffs=[],
                         squadAvailable=squad, resources={}, freshness=95.0,
                         goodFruit=96, badFruit=1)
            elif opp_at_guard:
                p.update(state="IDLE", currentNodeId="S10", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None,
                         delivered=False, retired=False)
            else:
                p.update(state="MOVING", currentNodeId="S10", nextNodeId="S11",
                         routeEdgeId="E06", currentProcess=None,
                         delivered=False, retired=False)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["resourceStock"] = {}
            n["guard"] = ({"ownerTeamId": "BLUE", "defense": 6, "active": True}
                          if n["nodeId"] == "S10" else None)
        gs.on_inquire(d)
        return gs

    def weakens(actions):
        return [x for x in actions if x["action"] == "SQUAD_WEAKEN"]

    # 1) 卡主本人停靠在卡节点上 → 不削弱（它能原地补卡，削弱=喂饵）
    a = PlannerStrategy().decide(frozen(opp_at_guard=True))
    ok &= check("削弱纪律: 卡主在场不削弱", not weakens(a),
                json.dumps(a, ensure_ascii=False))

    # 2) 卡主已离开 → 正常削弱
    st = PlannerStrategy()
    a = st.decide(frozen(round_no=331))
    ok &= check("削弱纪律: 卡主离开后削弱",
                weakens(a) and weakens(a)[0]["targetNodeId"] == "S10",
                json.dumps(a, ensure_ascii=False))

    # 3) 重发间隔：同一张卡 12 帧内不连发（落地要 3-5 帧，连发白扣人手）
    burst = sum(len(weakens(st.decide(frozen(round_no=331 + i))))
                for i in range(1, PlannerStrategy.WEAKEN_RESEND_GAP))
    ok &= check("削弱纪律: 间隔内不连发", burst == 0, f"burst={burst}")
    a = st.decide(frozen(round_no=331 + PlannerStrategy.WEAKEN_RESEND_GAP))
    ok &= check("削弱纪律: 到间隔后续派", bool(weakens(a)),
                json.dumps(a, ensure_ascii=False))

    # 4) 人手保底：仅剩 3 人手（花 2 剩 1 < 2）→ 不削，留给第二张卡
    a = PlannerStrategy().decide(frozen(squad=3))
    ok &= check("削弱纪律: 人手保底不掏空", not weakens(a),
                json.dumps(a, ensure_ascii=False))
    return ok


def test_latent_mechanics():
    """V3.12：悬赏追分、情报空转帧顺手用、文书顺路领取、远程清障/续防。

    此前 bounties[]/totalScore/INTEL/PASS_TOKEN/OFFICIAL_PERMIT/SQUAD_CLEAR/
    SQUAD_REINFORCE 全部零引用（见策略体检）。这里逐条验证新逻辑，且每条都
    带一个"不该触发时不触发"的反例，防止重蹈"主动设卡因 ETA=0 从未触发"
    的覆辙。
    """
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def base_state(cur="S01", round_no=2, my_score=0, opp_score=0, resources=None,
                    bounties=None, clear_stock=True):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        d["bounties"] = bounties or []
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", routeEdgeId=None, nextNodeId=None,
                         currentProcess=None, currentNodeId=cur, buffs=[],
                         goodFruit=90, badFruit=4, squadAvailable=8,
                         freshness=95.0, resources=resources or {},
                         taskScore=0, totalScore=my_score, verified=False,
                         delivered=False, retired=False)
            else:
                p.update(state="IDLE", currentNodeId="S13", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None,
                         totalScore=opp_score, delivered=False, retired=False)
        for n in d["nodes"]:
            n["guard"] = None
            n["hasObstacle"] = False
            if clear_stock:
                n["resourceStock"] = {}
        gs.on_inquire(d)
        return gs

    # ---- 悬赏 1: 落后时专程绕路打敌方设卡拿悬赏（S08 不在 S01->S14 最短路上）----
    gs = base_state(cur="S01", my_score=600, opp_score=700)  # 落后 100 分
    gs.nodes["S08"]["guard"] = {"ownerTeamId": "BLUE", "defense": 4,
                                "maxDefense": 6, "active": True}
    gs.bounties = [{"bountyId": "B_S08", "bountyType": "NORMAL_BOUNTY",
                    "nodeId": "S08", "rewardScore": 18, "active": True,
                    "completed": False, "winnerPlayerId": 0}]
    st = PlannerStrategy()
    plan = st.planner.plan(gs)
    ok &= check("悬赏: 落后时专程绕路攻敌方带悬赏的卡",
                plan.kind == "bounty" and plan.position == "S08", repr(plan))

    # ---- 悬赏 2: 领先时同样的局面不追（追分口径，见 6.3.3） ----
    gs2 = base_state(cur="S01", my_score=700, opp_score=600)  # 领先 100 分
    gs2.nodes["S08"]["guard"] = {"ownerTeamId": "BLUE", "defense": 4,
                                 "maxDefense": 6, "active": True}
    gs2.bounties = [{"bountyId": "B_S08", "bountyType": "NORMAL_BOUNTY",
                     "nodeId": "S08", "rewardScore": 18, "active": True,
                     "completed": False, "winnerPlayerId": 0}]
    plan2 = st.planner.plan(gs2)
    ok &= check("悬赏: 领先时不追（打了也不计分）", plan2.kind != "bounty", repr(plan2))

    # ---- 悬赏 2b: 任务分封顶后，小分差终局不能把悬赏候选一刀切掉 ----
    gs2b = base_state(cur="S01", my_score=700, opp_score=680)
    gs2b.players[1001]["taskScore"] = 150
    gs2b.nodes["S08"]["guard"] = {"ownerTeamId": "BLUE", "defense": 4,
                                  "maxDefense": 6, "active": True}
    gs2b.bounties = [{"bountyId": "B_S08", "bountyType": "NORMAL_BOUNTY",
                      "nodeId": "S08", "rewardScore": 18, "active": True,
                      "completed": False, "winnerPlayerId": 0}]
    plan2b = PlannerStrategy().planner.plan(gs2b)
    ok &= check("悬赏: 封顶小分差终局仍追悬赏",
                plan2b.kind == "bounty" and plan2b.position == "S08",
                repr(plan2b))

    # ---- 悬赏 3: 到达相邻节点后，既有突破逻辑接管攻坚（复用 _breakthrough）----
    gs3 = base_state(cur="S06", my_score=600, opp_score=700)
    gs3.nodes["S08"]["guard"] = {"ownerTeamId": "BLUE", "defense": 4,
                                 "maxDefense": 6, "active": True}
    a = PlannerStrategy().main_action(gs3, Plan("bounty", position="S08", slack=200))
    ok &= check("悬赏: 到相邻节点自动转交攻坚破卡",
                a and a["action"] == "BREAK_GUARD" and a["targetNodeId"] == "S08",
                str(a))

    # ---- 情报 1: 排队等对手处理本站时，顺手用情报标自己这站(距离0) ----
    gs = base_state(cur="S02", resources={"INTEL": 1})
    for p in gs.players.values():
        if p["playerId"] != 1001:
            p["currentProcess"] = {"targetNodeId": "S02", "action": "PROCESS"}
    plan = st.planner.plan(gs)
    a = PlannerStrategy().main_action(gs, plan)
    ok &= check("情报: 排队空转帧顺手标记本站",
                a and a["action"] == "USE_RESOURCE" and a["resourceType"] == "INTEL"
                and a.get("targetNodeId") == "S02", str(a))

    # ---- 情报 2: 手里没有情报时，同样的局面老实 WAIT（不假装有资源） ----
    gs = base_state(cur="S02", resources={})
    for p in gs.players.values():
        if p["playerId"] != 1001:
            p["currentProcess"] = {"targetNodeId": "S02", "action": "PROCESS"}
    plan = st.planner.plan(gs)
    a = PlannerStrategy().main_action(gs, plan)
    ok &= check("情报: 没情报就老实 WAIT", a == {"action": "WAIT"}, str(a))

    # ---- 文书 1: 不再默认顺路领取文书（2 帧读条收益不稳定，小分差局反噬） ----
    gs = base_state(cur="S03", resources={})
    gs.me["taskScore"] = 90
    gs.nodes["S03"]["resourceStock"] = {"PASS_TOKEN": 1}
    plan = st.planner.plan(gs)
    a = PlannerStrategy().main_action(gs, plan)
    ok &= check("文书: 不默认顺路领过所",
                not (a and a.get("action") == "CLAIM_RESOURCE"
                     and a.get("resourceType") == "PASS_TOKEN"), str(a))

    # ---- 马匹 1: 直送阶段临门短路，马省不回 2 帧领取读条则不顺手领 ----
    gs = base_state(cur="S13", resources={})
    gs.nodes["S13"]["resourceStock"] = {"SHORT_HORSE": 1}
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=120))
    ok &= check("马匹: 临门短路不顺手领马",
                not (a and a.get("action") == "CLAIM_RESOURCE"
                     and a.get("resourceType") == "SHORT_HORSE"), str(a))

    # ---- 马匹 2: 剩余路程足够长时，顺路马仍可领取 ----
    gs = base_state(cur="S07", resources={})
    gs.nodes["S07"]["resourceStock"] = {"FAST_HORSE": 1}
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=160))
    ok &= check("马匹: 长路直送仍顺手领马",
                a and a.get("action") == "CLAIM_RESOURCE"
                and a.get("resourceType") == "FAST_HORSE", str(a))

    # ---- 情报 3: 不主动顺路领取情报；只有已持有时才利用空转/预热收益 ----
    gs = base_state(cur="S06", resources={})
    gs.me["taskScore"] = 90
    gs.nodes["S06"]["resourceStock"] = {"INTEL": 1}
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=200))
    ok &= check("情报: 不主动顺路领取",
                not (a and a.get("action") == "CLAIM_RESOURCE"
                     and a.get("resourceType") == "INTEL"), str(a))

    gs = base_state(cur="S03", round_no=90, resources={})
    gs.me["taskScore"] = 90
    gs.nodes["S03"]["resourceStock"] = {"INTEL": 1}
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=200))
    ok &= check("情报: S03 开局打包允许顺路领",
                a and a["action"] == "CLAIM_RESOURCE"
                and a["resourceType"] == "INTEL", str(a))

    gs = base_state(cur="S10", resources={})
    gs.me["taskScore"] = 120
    for p in gs.players.values():
        if p["playerId"] != 1001:
            p["currentNodeId"] = "S01"
    gs.nodes["S10"]["resourceStock"] = {"INTEL": 1}
    st_late_intel = PlannerStrategy()
    late_plan = Plan("deliver", slack=80)
    ok &= check("情报: 后段走廊拒止允许顺路领",
                st_late_intel._should_claim_intel_en_route(gs, late_plan, "S10"),
                str(st_late_intel._should_claim_intel_en_route(gs, late_plan, "S10")))

    gs = base_state(cur="S12", resources={})
    gs.me["taskScore"] = 90
    gs.nodes["S12"]["resourceStock"] = {"INTEL": 1}
    st_camper = PlannerStrategy()
    st_camper._opp_profile = "camper"
    st_camper.planner.opp_profile = "camper"
    a = st_camper.main_action(gs, Plan("deliver", slack=200))
    ok &= check("情报: camper 慢局允许顺路领",
                a and a["action"] == "CLAIM_RESOURCE"
                and a["resourceType"] == "INTEL", str(a))

    # ---- 远程清障 1: 路上非 T04 障碍派小分队清，不用主车队绕路/自己 CLEAR ----
    # 处理站帧数已计入寻路惩罚（V3.1），S01->S14 的惩罚后最短路实际走
    # S01-S06-S08-S10-S11-S12-S13-S14（绕开 S02/S04/S05/S09 的固定处理），
    # 障碍要挂在这条真实路径上才会被撞见，S08 正好在路上
    gs = base_state(cur="S01")
    gs.nodes["S08"]["hasObstacle"] = True
    a = PlannerStrategy().squad_action(gs, Plan("deliver", slack=200))
    ok &= check("远程清障: 路上非T04障碍派小分队",
                a == {"action": "SQUAD_CLEAR", "targetNodeId": "S08"}, str(a))

    gs = base_state(cur="S06")
    gs.nodes["S08"]["hasObstacle"] = True
    st_clear = PlannerStrategy()
    a_main = st_clear.main_action(gs, Plan("deliver", slack=200))
    a_squad = st_clear.squad_action(gs, Plan("deliver", slack=200))
    ok &= check("远程清障: 小分队可清时主车队不烧好果 CLEAR",
                a_main == {"action": "WAIT"}
                and a_squad == {"action": "SQUAD_CLEAR", "targetNodeId": "S08"},
                f"main={a_main} squad={a_squad}")

    # ---- 远程清障 2: 同一障碍若是我们自己在做的 T04 目标，绝不能碰 ----
    gs = base_state(cur="S01")
    gs.nodes["S08"]["hasObstacle"] = True
    t04_plan = Plan("task", task={"taskTemplateId": "T04"}, position="S01", slack=200)
    a = PlannerStrategy().squad_action(gs, t04_plan)
    ok &= check("远程清障: 自己的T04目标绝不代劳清障",
                a is None or a.get("action") != "SQUAD_CLEAR", str(a))

    gs = base_state(cur="S01")
    gs.players[1001]["taskScore"] = 60
    pressure = TaskPlanner()._resource_task_pressure(gs, P.ICE_BOX, 30)
    ok &= check("鲜度: 60-120 分阶段冰鉴绕路计任务机会成本",
                pressure > 0, f"{pressure:.1f}")

    # ---- 续防 1: 落后时给风化中的自家设卡续防守值 ----
    gs = base_state(cur="S13", my_score=600, opp_score=700)
    gs.nodes["S10"]["guard"] = {"ownerTeamId": "RED", "defense": 2,
                                "maxDefense": 6, "active": True}
    for p in gs.players.values():
        if p["playerId"] != 1001:  # S09->S10 = 56 帧，落在续防的 ETA 窗口内
            p.update(currentNodeId="S09", nextNodeId=None, routeEdgeId=None)
    st2 = PlannerStrategy()
    st2._guard_sent = {"S10": 1}
    a = st2.squad_action(gs, Plan("deliver", slack=200))
    ok &= check("续防: 落后时给自家风化中的卡续命",
                a == {"action": "SQUAD_REINFORCE", "targetNodeId": "S10"}, str(a))

    # ---- 续防 2: 领先常规不续——别把卡续到悬赏触发线上送对手追分分 ----
    gs = base_state(cur="S13", my_score=700, opp_score=600)
    gs.nodes["S10"]["guard"] = {"ownerTeamId": "RED", "defense": 2,
                                "maxDefense": 6, "active": True}
    for p in gs.players.values():
        if p["playerId"] != 1001:  # S09->S10 = 56 帧，落在续防的 ETA 窗口内
            p.update(currentNodeId="S09", nextNodeId=None, routeEdgeId=None)
    st3 = PlannerStrategy()
    st3._guard_sent = {"S10": 1}
    a = st3.squad_action(gs, Plan("deliver", slack=200))
    ok &= check("续防: 领先但未被攻坚时不续",
                a is None, str(a))
    # ---- 续防 3: 领先时若对手正在攻坚我方卡，用富余人手补防 ----
    gs = base_state(cur="S13", my_score=700, opp_score=600)
    gs.nodes["S10"]["guard"] = {"ownerTeamId": "RED", "defense": 4,
                                "maxDefense": 7, "active": True}
    for p in gs.players.values():
        if p["playerId"] != 1001:
            p.update(currentNodeId="S09", nextNodeId=None, routeEdgeId=None,
                     currentProcess={"action": "BREAK_GUARD",
                                     "targetNodeId": "S10"})
    st4 = PlannerStrategy()
    st4._guard_sent = {"S10": 1}
    a = st4.squad_action(gs, Plan("deliver", slack=200))
    ok &= check("续防: 领先且对手正攻坚时补防",
                a == {"action": "SQUAD_REINFORCE", "targetNodeId": "S10"}, str(a))
    # ---- 续防 4: 不补超上限；只剩 1 点空间也不花 2 人手换半次收益 ----
    gs = base_state(cur="S13", my_score=700, opp_score=600)
    gs.nodes["S10"]["guard"] = {"ownerTeamId": "RED", "defense": 6,
                                "maxDefense": 7, "active": True}
    for p in gs.players.values():
        if p["playerId"] != 1001:
            p.update(currentNodeId="S09", nextNodeId=None, routeEdgeId=None,
                     currentProcess={"action": "BREAK_GUARD",
                                     "targetNodeId": "S10"})
    st5 = PlannerStrategy()
    st5._guard_sent = {"S10": 1}
    a = st5.squad_action(gs, Plan("deliver", slack=200))
    ok &= check("续防: 不为超上限补防浪费人手", a is None, str(a))

    # ---- 宫门设卡成本: S14 防守值上限 4（6.2.1），extra=1 即拉满，
    # extra=2 超上限那篓不提防守且不返还，纯白烧 ----
    gs = base_state(cur="S14")
    for p in gs.players.values():
        if p["playerId"] != 1001:  # 对手在 S13，ETA ~25 帧落在设卡窗口 [8,150]
            p.update(currentNodeId="S13", nextNodeId=None, routeEdgeId=None)
    a = PlannerStrategy()._guard_opportunity(gs, "S14", Plan("task", position="S07", slack=200))
    ok &= check("设卡: 宫门上限4只投1篓（不投溢出）",
                a is not None and a["action"] == "SET_GUARD"
                and a["extraGoodFruit"] == 1, str(a))

    # ---- 情报时机门: 宫门等 RUSH 时 355 帧后才标宫门（标记只活 45 帧）----
    st4 = PlannerStrategy()
    gs = base_state(cur="S14", round_no=360, resources={"INTEL": 1})
    plan = st4.planner.plan(gs)
    a = st4.main_action(gs, plan)
    ok &= check("情报: 宫门等 RUSH 时 355 帧后标宫门",
                a and a["action"] == "USE_RESOURCE" and a["resourceType"] == "INTEL"
                and a.get("targetNodeId") == "S14", str(a))

    gs = base_state(cur="S14", round_no=300, resources={"INTEL": 1})
    plan = st4.planner.plan(gs)
    a = st4.main_action(gs, plan)
    ok &= check("情报: 355 帧前不标宫门（必然过期白扔）",
                a == {"action": "WAIT"}, str(a))
    return ok


def test_corridor_reserve():
    """V3.15 走廊人手预留：过验核前非削弱派遣给设卡战留 4 人手底仓。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_at(squad, verified=False, opp_delivered=False):
        """我在 S13（离宫门 ETA ~30 帧），r360 触发宫门探路窗口。"""
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = 360
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        for p_ in d["players"]:
            if p_["playerId"] == 1001:
                p_.update(state="IDLE", currentNodeId="S13", nextNodeId=None,
                          routeEdgeId=None, currentProcess=None, buffs=[],
                          resources={}, squadAvailable=squad, verified=verified,
                          goodFruit=90, badFruit=2, freshness=90.0)
            else:
                p_.update(delivered=opp_delivered, retired=False)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n["scouted"] = []
        gs.on_inquire(d)
        return gs

    plan = Plan("deliver", slack=200)

    # 1) 人手 5：花 1 剩 4 = 底仓，允许探宫门
    a = PlannerStrategy().squad_action(gs_at(5), plan)
    ok &= check("人手预留: 剩够底仓时探路照发",
                a and a["action"] == "SQUAD_SCOUT" and a["targetNodeId"] == "S14",
                str(a))

    # 2) 人手 4：花 1 就击穿底仓（= 少一次削弱），不发
    a = PlannerStrategy().squad_action(gs_at(4), plan)
    ok &= check("人手预留: 击穿底仓的探路不发（弹药留给设卡战）",
                a is None, str(a))

    # 3) 对手已交付：设卡威胁解除，敞开花
    a = PlannerStrategy().squad_action(gs_at(4, opp_delivered=True), plan)
    ok &= check("人手预留: 对手已交付则不再预留",
                a and a["action"] == "SQUAD_SCOUT", str(a))

    # 4) 已验核：走廊已过，敞开花
    a = PlannerStrategy().squad_action(gs_at(4, verified=True), plan)
    ok &= check("人手预留: 已验核则不再预留",
                a and a["action"] == "SQUAD_SCOUT", str(a))

    # 5) 削弱不受预留限制——预留攒的就是削弱弹药
    st = PlannerStrategy()
    st._weaken_target = "S14"
    a = st.squad_action(gs_at(2), plan)
    ok &= check("人手预留: 削弱不受底仓限制",
                a and a["action"] == "SQUAD_WEAKEN" and a["targetNodeId"] == "S14",
                str(a))

    # replay235919：临别卡已把我们挡在长边/边口，前两刀后只剩最后 2
    # 人手；此时底仓应转为救命弹药，不能账上留人、车队等风化到输。
    gs = gs_at(2)
    gs.players[1001].update(state="MOVING", currentNodeId="S07",
                            nextNodeId="S09", routeEdgeId="E04",
                            edgeProgressMs=20000, edgeTotalMs=63480,
                            verified=False)
    gs.nodes["S09"]["guard"] = {"ownerTeamId": "BLUE", "defense": 4,
                                "maxDefense": 6, "active": True}
    st = PlannerStrategy()
    st._weaken_sent["S09"] = 240
    st.main_action(gs, Plan("deliver", slack=40))
    a = st.squad_action(gs, Plan("deliver", slack=40))
    ok &= check("人手预留: 临别卡死线局动用最后2人削弱",
                a and a["action"] == "SQUAD_WEAKEN" and a["targetNodeId"] == "S09",
                str(a))

    gs = gs_at(8)
    gs.players[1001].update(currentNodeId="S02", nextNodeId=None,
                            routeEdgeId=None)
    st = PlannerStrategy()
    ok &= check("S02探路: 首个计划分支允许预探",
                st._s02_fork_scout_allowed(gs, "S02", "S03"), "")
    st._scout_sent["S04"] = 50
    ok &= check("S02探路: 未承诺前不反向预投互斥分支",
                not st._s02_fork_scout_allowed(gs, "S02", "S03"),
                str(st._scout_sent))
    st = PlannerStrategy()
    st._window_draw_pressure[("S02", P.CONTEST_DOCK)] = (1, gs.round - 1)
    ok &= check("S02探路: DRAW压力后不再预探分叉",
                not st._s02_fork_scout_allowed(gs, "S02", "S03"),
                str(st._window_draw_pressure))
    return ok


def test_lenient_frame():
    """V3.16.1 宽容读帧回归（replay61：服务端毒 JSON 杀死读循环 → 缺 60 帧强制退赛）。"""
    from lychee.session import lenient_loads, FrameDecodeError
    ok = True

    # 1) 平台实测毒样：破关令成本表把玩家 ID 序列化成裸整数键
    poison = ('{"round":503,"contests":[{"contestType":"GATE",'
              '"breakOrderCostTypes":{2744:"GOOD_FRUIT"},"redPoint":0}]}')
    d = lenient_loads(poison)
    ok &= check("宽容读帧: 裸整数键补引号修复",
                d["round"] == 503
                and d["contests"][0]["breakOrderCostTypes"] == {"2744": "GOOD_FRUIT"},
                str(d)[:120])

    # 2) 合法 JSON 原样通过（不触发修复路径）
    d = lenient_loads('{"round":1,"a":{"1":"x"},"s":"{2744:not-json-inside-string}"}')
    ok &= check("宽容读帧: 合法帧原样解析（字符串内容不受修复干扰）",
                d["s"] == "{2744:not-json-inside-string}", str(d))

    # 3) 修不回来的帧抛 FrameDecodeError 且携带原文（供跳帧兜底抠 round）
    try:
        lenient_loads('{"round":77,broken!!!')
        ok &= check("宽容读帧: 坏帧应抛 FrameDecodeError", False, "no raise")
    except FrameDecodeError as e:
        import re as _re
        m = _re.search(r'"round"\s*:\s*(\d+)', e.body)
        ok &= check("宽容读帧: 坏帧抛错且能抠出 round 兜底",
                    m and m.group(1) == "77", str(e.body))
    return ok


def test_race_tempo():
    """V3.18 竞速模式：漏斗竞争带内帧价上调、马匹豁免、蹲刷禁用、关隘热设卡。

    audit 缺口 1（领先没有被当成资产）的修复：所有对抗机制都是"被卡了
    怎么办"的事后反应，这里补"怎么不落到被卡的位置"。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]
    from lychee.planner import (TaskPlanner, RACE_FRAME_MULT,
                                FRESH_VALUE_PER_FRAME, TIME_SCORE_PER_FRAME)

    def gs_race(my_pos="S02", opp_pos="S03", my_moving=None, resources=None,
                round_no=60, opp_delivered=False, task_score=45,
                phase="NORMAL", opp_task_score=0, opp_moving=None):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["phase"] = phase
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                if my_moving:  # (nextNodeId, edgeId)
                    p.update(state="MOVING", currentNodeId=my_pos,
                             nextNodeId=my_moving[0], routeEdgeId=my_moving[1],
                             currentProcess=None, buffs=[],
                             resources=resources or {}, freshness=95.0,
                             goodFruit=90, badFruit=0, taskScore=task_score)
                else:
                    p.update(state="IDLE", currentNodeId=my_pos, nextNodeId=None,
                             routeEdgeId=None, currentProcess=None, buffs=[],
                             resources=resources or {}, freshness=95.0,
                             goodFruit=90, badFruit=0, taskScore=task_score)
            else:
                if opp_moving:
                    cur_o, next_o, edge_id, remain = opp_moving
                    p.update(state="MOVING", currentNodeId=cur_o,
                             nextNodeId=next_o, routeEdgeId=edge_id,
                             edgeTotalMs=remain * 1000, edgeProgressMs=0,
                             currentProcess=None, delivered=opp_delivered,
                             retired=False, taskScore=opp_task_score)
                else:
                    p.update(state="IDLE", currentNodeId=opp_pos, nextNodeId=None,
                             routeEdgeId=None, currentProcess=None,
                             delivered=opp_delivered, retired=False,
                             taskScore=opp_task_score)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    # 1) 进入条件：双方到武关 S10 的 ETA 差在竞争带内（S02 vs S03 差 35？
    #    不对——S03 恰好近 35 帧，取双方同在 S03 差 0）→ 竞速模式激活
    gs = gs_race(my_pos="S03", opp_pos="S03")
    pl = TaskPlanner()
    ok &= check("竞速: 同位起跑进入竞争带", pl.race_mode(gs), "")
    base_fv = FRESH_VALUE_PER_FRAME + TIME_SCORE_PER_FRAME
    fv = pl._frame_value(gs, 300)
    ok &= check("竞速: 带内帧价按倍率上调",
                abs(fv - base_fv * RACE_FRAME_MULT) < 1e-9, f"fv={fv:.3f}")
    ok &= check("竞速: 资源口径豁免竞速溢价",
                abs(pl._frame_value(gs, 300, race_adjust=False) - base_fv) < 1e-9,
                "")

    # 2) 退出条件：对手远远落后（S01 vs 我 S07，差 >25）→ 不在带内
    ok &= check("竞速: 差距拉开退出",
                not TaskPlanner().race_mode(gs_race(my_pos="S07", opp_pos="S01")),
                "")
    # 3) 过完咽喉（S11 之后无 KEY_PASS）→ 自然退出
    ok &= check("竞速: 过完咽喉退出",
                not TaskPlanner().race_mode(gs_race(my_pos="S11", opp_pos="S11")),
                "")
    # 4) 对手已交付 → 无漏斗威胁
    ok &= check("竞速: 对手已交付不竞速",
                not TaskPlanner().race_mode(
                    gs_race(my_pos="S03", opp_pos="S13", opp_delivered=True)),
                "")

    # 5) 马匹武器化：竞争带内唯一的马也骑（平时留给 T06——语料里速度
    #    手段全花在"输了以后"，这里花在决定输赢的窗口）
    gs = gs_race(my_pos="S02", my_moving=("S03", "E02"), opp_pos="S03",
                 resources={"SHORT_HORSE": 1})
    a = PlannerStrategy().decide(gs)
    ok &= check("竞速: 带内唯一的马也骑",
                any(x["action"] == "USE_RESOURCE"
                    and x["resourceType"] == "SHORT_HORSE" for x in a),
                json.dumps(a, ensure_ascii=False))
    # 5b) 带外行为不变：对手远落后时马仍留给 T06
    gs = gs_race(my_pos="S02", my_moving=("S03", "E02"), opp_pos="S01",
                 round_no=91, resources={"SHORT_HORSE": 1})
    a = PlannerStrategy().decide(gs)
    ok &= check("竞速: 带外马仍预留 T06",
                not any(x["action"] == "USE_RESOURCE" for x in a),
                json.dumps(a, ensure_ascii=False))

    # 6) 蹲刷禁用：S09 是任务候选点、对手同位（带内、非领先）——平时会蹲，
    #    竞争带内先抢走廊
    gs = gs_race(my_pos="S09", opp_pos="S09", round_no=200, task_score=90)
    a = PlannerStrategy().main_action(gs)
    ok &= check("竞速: 带内不蹲刷，推进走廊",
                a and a["action"] == "MOVE", str(a))

    # 7) 关隘热设卡：刚过武关（slack 40 低于常规闸门 65），对手 ETA ~56 帧
    #    在热窗口内 → 设卡兑现竞速胜利
    gs = gs_race(my_pos="S10", opp_pos="S09", round_no=200)
    a = PlannerStrategy()._guard_opportunity(gs, "S10", Plan("deliver", slack=40))
    ok &= check("竞速: 关隘热窗口 slack 40 开卡",
                a is not None and a["action"] == "SET_GUARD"
                and a["targetNodeId"] == "S10", str(a))
    # 7b) slack 低于热闸门 25 仍不设（交付优先的底线不动）
    a = PlannerStrategy()._guard_opportunity(gs, "S10", Plan("deliver", slack=20))
    ok &= check("竞速: slack 低于热闸门仍不设", a is None, str(a))
    # 7c0) vs2931：r286 S10 卡逼出对手 3 次削弱，r292 被打穿后我们仍
    #      站在 S10；这时应补第二张卡，而不是被 40 帧重试间隔拦住。
    st_reguard = PlannerStrategy()
    st_reguard._guard_sent["S10"] = 286
    gs = gs_race(my_pos="S10", opp_pos="S09", round_no=294,
                 task_score=120, opp_task_score=120)
    a = st_reguard._guard_opportunity(gs, "S10", Plan("deliver", slack=80))
    ok &= check("竞速: 无打穿证据时同点重试仍被拦",
                a is None, str(a))
    st_reguard._own_guard_broken["S10"] = 292
    a = st_reguard._guard_opportunity(gs, "S10", Plan("deliver", slack=80))
    ok &= check("竞速: 我方卡刚被削穿且仍占点时补卡",
                a is not None and a["action"] == "SET_GUARD"
                and a["targetNodeId"] == "S10", str(a))
    st_track = PlannerStrategy()
    gs_on = gs_race(my_pos="S10", opp_pos="S09", round_no=291)
    gs_on.nodes["S10"]["guard"] = {"ownerTeamId": gs_on.my_team,
                                   "defense": 6, "maxDefense": 7,
                                   "active": True}
    st_track._absorb_feedback(gs_on)
    gs_off = gs_race(my_pos="S10", opp_pos="S09", round_no=292)
    gs_off.nodes["S10"]["guard"] = {"ownerTeamId": gs_off.my_team,
                                    "defense": 0, "maxDefense": 7,
                                    "active": False}
    st_track._absorb_feedback(gs_off)
    ok &= check("竞速: 识别我方卡被打穿",
                st_track._own_guard_broken.get("S10") == 292,
                str(st_track._own_guard_broken))
    # 7c) 非关键关隘不吃热折扣：S11（PASS）slack 40 照旧被常规闸门拦住
    gs = gs_race(my_pos="S11", opp_pos="S10", round_no=200)
    a = PlannerStrategy()._guard_opportunity(gs, "S11", Plan("deliver", slack=40))
    ok &= check("竞速: 普通咽喉不吃热折扣", a is None, str(a))
    # 8) 普通汇入点反手卡：对手已展示普通点卡手后，S09 同路先到可回手
    st = PlannerStrategy()
    st._opp_ordinary_guard_seen = True
    gs = gs_race(my_pos="S09", opp_pos="S07", round_no=200)
    a = st._guard_opportunity(gs, "S09", Plan("deliver", slack=50))
    ok &= check("竞速: 已识别普通点卡手后可反手卡",
                a is not None and a["action"] == "SET_GUARD"
                and a["targetNodeId"] == "S09", str(a))
    # 8b) 同路先到时，设卡优先级高于脚下任务读条
    gs = gs_race(my_pos="S09", opp_pos="S07", round_no=200)
    gs.tasks = [{"taskId": "T_GUARD_FIRST", "taskTemplateId": "T01",
                 "nodeId": "S09", "processRound": 4, "score": 30,
                 "expireRound": 999, "active": True, "completed": False,
                 "failed": False, "ownerPlayerId": 0}]
    st = PlannerStrategy()
    st._opp_ordinary_guard_seen = True
    a = st.main_action(gs)
    ok &= check("竞速: 同路先到设卡优先于脚下任务",
                a is not None and a["action"] == "SET_GUARD"
                and a["targetNodeId"] == "S09", str(a))
    # 8c) 追分合流卡：复盘 2744vs2617，前段任务分 0:60 落后但我们
    #     先到 S09 10 帧；这 4 帧设卡是破对手 S10 先手的追分动作。
    gs = gs_race(my_pos="S09", round_no=314, task_score=0,
                 opp_task_score=60,
                 opp_moving=("S05", "S09", "E_S05_S09", 10))
    a = PlannerStrategy()._guard_opportunity(gs, "S09",
                                             Plan("deliver", slack=10))
    ok &= check("竞速: 追分态 S09 先到 10 帧设卡",
                a is not None and a["action"] == "SET_GUARD"
                and a["targetNodeId"] == "S09", str(a))
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=10))
    ok &= check("竞速: 追分合流卡优先于赶路",
                a is not None and a["action"] == "SET_GUARD"
                and a["targetNodeId"] == "S09", str(a))
    # 8d) 分差不够时仍不把普通驿站泛化成设卡点。
    gs = gs_race(my_pos="S09", round_no=314, task_score=0,
                 opp_task_score=30,
                 opp_moving=("S05", "S09", "E_S05_S09", 10))
    a = PlannerStrategy()._guard_opportunity(gs, "S09",
                                             Plan("deliver", slack=10))
    ok &= check("竞速: 普通驿站无追分分差不设卡", a is None, str(a))
    # 8e) vs2931：我方 r224 先到 S09，对手 r251 才到但任务分 90:60
    #     领先；这不是 0:60 的早段追分，却同样应把先到合流点兑现成拒止卡。
    gs = gs_race(my_pos="S09", round_no=225, task_score=60,
                 opp_task_score=90,
                 opp_moving=("S07", "S09", "E04", 26))
    a = PlannerStrategy()._guard_opportunity(gs, "S09",
                                             Plan("deliver", slack=35))
    ok &= check("竞速: 高分对手将至 S09 时先到合流点设卡",
                a is not None and a["action"] == "SET_GUARD"
                and a["targetNodeId"] == "S09", str(a))
    gs = gs_race(my_pos="S09", round_no=225, task_score=60,
                 opp_task_score=75,
                 opp_moving=("S07", "S09", "E04", 26))
    a = PlannerStrategy()._guard_opportunity(gs, "S09",
                                             Plan("deliver", slack=25))
    ok &= check("竞速: 小幅任务领先但必经时也设拒止卡",
                a is not None and a["action"] == "SET_GUARD"
                and a["targetNodeId"] == "S09", str(a))
    gs = gs_race(my_pos="S09", round_no=225, task_score=60,
                 opp_task_score=70,
                 opp_moving=("S07", "S09", "E04", 26))
    a = PlannerStrategy()._guard_opportunity(gs, "S09",
                                             Plan("deliver", slack=35))
    ok &= check("竞速: 高分合流卡分差不足不泛化",
                a is None, str(a))
    # 9) RUSH 起点二卡：规则允许 SET_GUARD，只保留交付余量底线
    gs = gs_race(my_pos="S13", opp_pos="S11", round_no=452, phase="RUSH")
    a = PlannerStrategy()._guard_opportunity(gs, "S13",
                                             Plan("deliver", slack=25))
    ok &= check("竞速: RUSH 起点允许二卡",
                a is not None and a["action"] == "SET_GUARD"
                and a["targetNodeId"] == "S13", str(a))
    # 10) 高任务分贴近宫门的边农边冲者：普通汇入点无需等它先暴露卡手习惯。
    gs = gs_race(my_pos="S09", opp_pos="S07", round_no=260,
                 opp_task_score=120,
                 opp_moving=("S07", "S09", "E_X", 8))
    a = PlannerStrategy()._guard_opportunity(gs, "S09",
                                             Plan("deliver", slack=50))
    ok &= check("竞速: 边农边冲同路先到先卡",
                a is not None and a["action"] == "SET_GUARD"
                and a["targetNodeId"] == "S09", str(a))
    # 11) 尾段直送时不再顺手领文书/情报，避免 S13 这种位置白烧 6 帧。
    gs = gs_race(my_pos="S13", opp_pos="S10", round_no=430,
                 task_score=120, opp_delivered=True)
    gs.nodes["S13"]["resourceStock"] = {"INTEL": 1}
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=120))
    ok &= check("竞速: 尾段直送跳过非硬件资源",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S14",
                str(a))
    return ok


def test_race_cliff():
    """V3.21 悬崖带：咽喉近 + 尚无敌卡 + 非安全领先 → 帧价切换为悬崖斜率。

    立项：随机化 camper 四死局中 10/15 同构——S07 顺路停留 ~14 帧把
    "同帧进边"（免疫）变成"落后 18~20 帧"（死等+满防税起步），漏斗竞速
    是悬崖函数而非线性。悬崖价 30/帧咬掉带内顺路任务与领取，两局翻盘
    且赢局逐位无损。与 race_mode 解耦：±25 对称带在尾侧有 ~55 帧的
    度量偏差（锚点漂移 + t_o 裸 ETA），落后侧延伸到 60。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]
    from lychee.planner import (TaskPlanner, FRESH_VALUE_PER_FRAME,
                                TIME_SCORE_PER_FRAME, RACE_CLIFF_FRAME_VALUE)

    def gs_cliff(my_pos="S07", opp_pos="S05", round_no=160, guard_s10=None,
                 ice_at=None, opp_task=0, opp_moving=None):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=my_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=95.0, goodFruit=90,
                         badFruit=0, taskScore=45)
            else:
                if opp_moving:
                    cur_o, next_o, edge_id, remain = opp_moving
                    p.update(state="MOVING", currentNodeId=cur_o,
                             nextNodeId=next_o, routeEdgeId=edge_id,
                             edgeTotalMs=remain * 1000, edgeProgressMs=0,
                             currentProcess=None, delivered=False,
                             retired=False, taskScore=opp_task)
                else:
                    p.update(state="IDLE", currentNodeId=opp_pos, nextNodeId=None,
                             routeEdgeId=None, currentProcess=None,
                             delivered=False, retired=False,
                             taskScore=opp_task)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = guard_s10 if n["nodeId"] == "S10" else None
            n["resourceStock"] = ({"FAST_HORSE": 1}
                                  if n["nodeId"] == ice_at else {})
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    # 1) 正例：我 S07（ETA 119 ≤ 125）、对手 S05（差 ~4）、S10 无卡 → 激活，
    #    帧价切换为悬崖斜率（资源目标口径豁免不变）
    gs = gs_cliff()
    pl = TaskPlanner()
    ok &= check("悬崖: 关前竞争带内激活", pl.race_cliff(gs), "")
    fv = pl._frame_value(gs, 100)
    ok &= check("悬崖: 带内帧价为悬崖斜率",
                abs(fv - RACE_CLIFF_FRAME_VALUE) < 1e-9, f"fv={fv}")
    base_fv = FRESH_VALUE_PER_FRAME + TIME_SCORE_PER_FRAME
    ok &= check("悬崖: 显式非竞速口径仍可豁免",
                abs(pl._frame_value(gs, 100, race_adjust=False)
                    - base_fv) < 1e-9, "")
    # 1b) 漏斗税恒用基础帧价：悬崖帧价已经代表输掉漏斗竞速的胜负尾部，
    #     _funnel_delta 的实际等待/税帧不能再乘 30 形成双计。
    def eval_with_funnel(delta):
        plx = TaskPlanner()
        pen = plx._penalty_fn(gs)
        ec = plx._edge_cost_fn(gs)
        to_gate, _ = gs.graph.shortest_path("S07", gs.gate_node,
                                            P.BASE_SPEED, pen, ec)
        plx._funnel_delta = lambda *_args, **_kwargs: delta
        task = {"taskId": "T_CLIFF_FUNNEL", "taskTemplateId": "T01",
                "nodeId": "S07", "processRound": 4, "score": 90,
                "expireRound": 999, "active": True, "completed": False,
                "failed": False, "ownerPlayerId": 0}
        return plx._evaluate(gs, task, "S07", 0, to_gate, to_gate,
                             999, P.BASE_SPEED, pen, ec)[0]
    net0 = eval_with_funnel(0)
    net10 = eval_with_funnel(10)
    ok &= check("悬崖: 漏斗税不乘悬崖帧价",
                abs((net0 - net10) - 10 * base_fv) < 1e-6,
                f"diff={net0 - net10:.3f} base={10 * base_fv:.3f}")

    # 2) 安全领先豁免：我 S09（56）、对手 S05（115）→ 领先 59 > 10，不悬崖
    ok &= check("悬崖: 安全领先不悬崖",
                not TaskPlanner().race_cliff(gs_cliff(my_pos="S09",
                                                      opp_pos="S05")), "")
    # 3) 深落后出带：对手已到咽喉（差 -119 < -60）→ 转入攻坚/漏斗经济
    ok &= check("悬崖: 深落后出带",
                not TaskPlanner().race_cliff(gs_cliff(my_pos="S07",
                                                      opp_pos="S10")), "")
    # 3b) 落后但在尾侧延伸内（我 S07 vs 对手 S09，差 ~-63+... 实取 S05→
    #     对手 S09: t_o=+56, 我 119 → 差 -63 恰在界外；用对手 S07 同位=0）
    ok &= check("悬崖: 同位平手在带内",
                TaskPlanner().race_cliff(gs_cliff(my_pos="S07",
                                                  opp_pos="S07")), "")
    # 4) 咽喉已有敌卡 → 悬崖已定，退出（转常规漏斗定价）
    ok &= check("悬崖: 敌卡落地即退出",
                not TaskPlanner().race_cliff(gs_cliff(
                    guard_s10={"ownerTeamId": "BLUE", "defense": 6,
                               "active": True})), "")
    # 5) 咽喉尚远不悬崖：S03（ETA ~194 > 125）竞速带内但未来方差主导
    ok &= check("悬崖: 关远处不悬崖（竞速带内）",
                not TaskPlanner().race_cliff(gs_cliff(my_pos="S03",
                                                      opp_pos="S03")), "")
    # 6) 开关
    pl6 = TaskPlanner()
    pl6.RACE_CLIFF_ENABLED = False
    ok &= check("悬崖: 开关关闭回落", not pl6.race_cliff(gs_cliff()), "")
    # 6b) 对手在途农任务（taskScore ≥ 30）且未形成宫门压力 → 纯 farmer，
    #     不按抢关悬崖处理。
    #     （A/B 实锤：无此门 farmer 局 48/48→42/48、镜像均分 -53）
    ok &= check("悬崖: 对手在途农任务不悬崖",
                not TaskPlanner().race_cliff(gs_cliff(opp_task=60)), "")
    # 6c) V3.31：边农边冲者不再被裸 taskScore 泛化成 farmer；它属于
    #     farm-rusher，走自己的前推/局部设卡/短等响应，不吃完整悬崖价
    #     （全局悬崖化被 toller seed3 证伪）。
    gs = gs_cliff(opp_task=90, opp_moving=("S07", "S09", "E_X", 20))
    pl = TaskPlanner()
    ok &= check("悬崖: 边农边冲进入 farm-rusher 档",
                pl._opp_tempo_mode(gs) == "farm-rusher"
                and pl.farm_rusher_pressure(gs), pl._opp_tempo_mode(gs))
    ok &= check("悬崖: farm-rusher 不吃完整悬崖价",
                not pl.race_cliff(gs), "")
    # 6d) 但高任务分去追任务/绕离宫门的 farmer 仍被农任务门保护。
    ok &= check("悬崖: 高分但未向宫门推进仍豁免",
                not TaskPlanner().race_cliff(gs_cliff(
                    opp_task=90, opp_moving=("S07", "S05", "E_X", 20))), "")

    gs_ice = gs_cliff(my_pos="S07", opp_pos="S05")
    gs_ice.nodes["S07"]["resourceStock"] = {"ICE_BOX": 1}
    a = PlannerStrategy().main_action(gs_ice, Plan("deliver", slack=180))
    ok &= check("悬崖: 带内仍允许顺路冰鉴",
                a and a["action"] == "CLAIM_RESOURCE"
                and a["resourceType"] == "ICE_BOX", str(a))

    # 文书/情报仍不在悬崖带顺手领；冰鉴作为确定鲜度收益单独保留。
    return ok


def test_parting_guard():
    """临别卡应对（2839 复盘根因 A 的回归钉子，场景=真实 r309~311）。

    第一名在武关农 8 帧任务、落防 6 卡即走。正确应对三连：卡主在场
    （新卡）→ 宽限等待；卡主踏边离场 → 2好1坏果拆卡（省 45 帧税）；
    真坐地户（闲置 ≥CAMPER_ESTABLISHED）→ 免宽限直接强通。V3.22 修正：
    坐地户驻留口径排除做任务帧——农 20+ 帧再落卡的过客不再被误判。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_pg(opp_on_edge=False, opp_proc=None, round_no=310,
              with_guard=True, bad=1, phase="NORMAL", defense=6,
              complete=None):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["phase"] = phase
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId="S08", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=95.0, goodFruit=97,
                         badFruit=bad, taskScore=150, squadAvailable=6)
            elif opp_on_edge:
                p.update(state="MOVING", currentNodeId=None, nextNodeId="S11",
                         routeEdgeId="E06", currentProcess=None,
                         delivered=False, retired=False, taskScore=120,
                         goodFruit=96)
            else:
                p.update(state="IDLE", currentNodeId="S10", nextNodeId=None,
                         routeEdgeId=None, currentProcess=opp_proc,
                         delivered=False, retired=False, taskScore=120,
                         goodFruit=96)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
            n["guard"] = ({"ownerTeamId": "BLUE", "defense": defense,
                           "initialDefense": 6, "active": True,
                           "completeRound": complete
                           if complete is not None else round_no - 1}
                          if (n["nodeId"] == "S10" and with_guard) else None)
        gs.on_inquire(d)
        return gs

    PROC = {"remainRound": 3, "targetNodeId": "S10"}

    def warmed(since, doing, until=310):
        """预热驻留/首见追踪：对手 since 起停在 S10，卡 r309 起可见。"""
        st = PlannerStrategy()
        for r in range(since, until):
            st._absorb_feedback(gs_pg(opp_proc=PROC if doing else None,
                                      round_no=r, with_guard=(r >= 309)))
        return st

    # 1) 卡主已踏边离开 → 拆卡（真实 r310+：不拆就是 45 帧税，四局 0:3 主因）
    st = warmed(301, True)
    gs = gs_pg(opp_on_edge=True)
    st._absorb_feedback(gs)
    a = st.main_action(gs)
    ok &= check("临别卡: 卡主离场即拆卡",
                a and a["action"] == "BREAK_GUARD"
                and a["targetNodeId"] == "S10", str(a))
    # 2) 卡主在场、边农边停 9 帧（过客）→ 宽限等待，不强通
    st = warmed(301, True)
    gs = gs_pg(opp_proc=PROC)
    st._absorb_feedback(gs)
    a = st.main_action(gs)
    ok &= check("临别卡: 农任务过客给宽限",
                a is None or a["action"] not in ("FORCED_PASS",
                                                 "BREAK_GUARD"), str(a))
    # 3) 真坐地户（闲置 25 帧）→ 免宽限直接强通
    st = warmed(285, False)
    gs = gs_pg()
    st._absorb_feedback(gs)
    a = st.main_action(gs)
    ok &= check("临别卡: 闲置坐地户免宽限强通",
                a and a["action"] == "FORCED_PASS", str(a))
    # 4) 驻留口径刻意含做任务帧：农 25 帧再落卡的按坐地户免宽限强通。
    #    曾试"闲置驻留"口径把这类判成过客——给 CamperBot delay 变体多送
    #    5 帧宽限与其动身帧共振，camper seed0/5 走廊时序拖死；语料里也
    #    没有"长农过客"形态（2839 只驻留 8 帧）。反过拟合纪律：回退
    st = warmed(285, True)
    gs = gs_pg(opp_proc=PROC)
    st._absorb_feedback(gs)
    a = st.main_action(gs)
    ok &= check("临别卡: 长农驻留按坐地户处理(刻意)",
                a and a["action"] == "FORCED_PASS", str(a))
    # 5) 根因 D（2839 掐 r450 RUSH 起点落二卡，我们坏果已尽，攻坚上限
    #    4 < 防 5；RUSH 期小分队违规 SQUAD_NOT_ALLOWED 削弱不可用）：
    #    风化时刻表公开可算——防守降到攻坚上限的等待若 < 强通税就等。
    #    卡 r400 落成防 6，r460 已风化到 5，下一次风化 r505（等 45>43?
    #    KEY_PASS 税 40…用防5: 15+25=40，等待 45 不划算→改用近例：
    #    complete=r399，首风化 45 → r444 掉到 5，二次 r474 掉到 4=上限，
    #    r460 决策时等 14 帧 < 税 40-2 → 等到可拆
    st = PlannerStrategy()
    gs = gs_pg(opp_on_edge=True, bad=0, phase="RUSH", defense=5,
               round_no=460, complete=399)
    st._absorb_feedback(gs)
    a = st.main_action(gs)
    ok &= check("临别卡: RUSH期弹尽等风化到可拆",
                a and a["action"] not in ("FORCED_PASS", "BREAK_GUARD",
                                          "MOVE"), str(a))
    # 5b) 风化太远（新卡防 6，降到 4 要 ~104 帧 > 税 45）→ 强通兜底
    st = PlannerStrategy()
    gs = gs_pg(opp_on_edge=True, bad=0, phase="RUSH", defense=6,
               round_no=460, complete=459)
    st._absorb_feedback(gs)
    a = st.main_action(gs)
    ok &= check("临别卡: 风化太远仍强通兜底",
                a and a["action"] == "FORCED_PASS", str(a))
    # 5c) 对照：坏果在手（攻坚值 7 ≥ 5）→ 直接拆，不等不税
    st = PlannerStrategy()
    gs = gs_pg(opp_on_edge=True, bad=1, phase="RUSH", defense=5,
               round_no=460)
    st._absorb_feedback(gs)
    a = st.main_action(gs)
    ok &= check("临别卡: RUSH期有弹药仍直接拆",
                a and a["action"] == "BREAK_GUARD", str(a))
    return ok


def test_contest_phase():
    """分段争夺折扣（V3.23）：反事实实测外部性只存在于关前。

    关前 0.5 不动（放宽被 840 局扫描证伪——贴身绕路外部性）；
    关后 0.9（硬抢真实胜率 97~100%）；对手已交付 1.0（规则上不可能
    被抢，曾漏检查白砍尾段任务）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]
    from lychee.planner import (TaskPlanner, CONTEST_RISK_DISCOUNT,
                                POST_CHOKE_CONTEST_DISCOUNT)

    def gs_cp(my_pos, opp_pos, opp_delivered=False, round_no=200,
              task_at=None):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        # 任务放在对手更近的节点（触发争夺折扣需要 opp_eta < f_to）
        d["tasks"] = [{"taskId": "T_X", "taskTemplateId": "T02",
                       "name": "测试", "nodeId": task_at or opp_pos,
                       "active": True,
                       "completed": False, "failed": False,
                       "ownerPlayerId": 0, "protectionPlayerId": 0,
                       "processRound": 4, "score": 30,
                       "refreshRound": round_no - 10,
                       "expireRound": round_no + 200}]
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=my_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=95.0, goodFruit=90,
                         badFruit=0, taskScore=45)
            else:
                p.update(state="IDLE", currentNodeId=opp_pos,
                         nextNodeId=None, routeEdgeId=None,
                         currentProcess=None, delivered=opp_delivered,
                         retired=False, taskScore=0)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    def disc(pl, gs):
        """任务净值反推折扣：对手在任务点同点（gap 恒正）时的 value 缩放。"""
        me = gs.me
        cur = me["currentNodeId"]
        speed = gs.my_speed()
        pen = pl._penalty_fn(gs)
        task = gs.tasks[0]
        r = pl._evaluate(gs, task, cur, 45, 100, 100, 300, speed, pen)
        return r

    # 1) 关前（S07，前方有武关）对手更近 → 0.5 折扣照旧
    gs = gs_cp("S07", "S09")
    pl = TaskPlanner()
    ok &= check("分段折扣: 关前照旧激进折扣",
                pl._choke_ahead(gs), "")
    # 2) 关后（S12，前方无咽喉）→ 不再是 0.5 段
    gs = gs_cp("S12", "S13")
    pl2 = TaskPlanner()
    ok &= check("分段折扣: 关后判定", not pl2._choke_ahead(gs), "")
    # 3) 行为级：关后对手更近的同一任务，净值高于关前口径（0.9 > 0.5）
    gs_post = gs_cp("S12", "S13")
    pl_on = TaskPlanner()
    r_on = disc(pl_on, gs_post)
    pl_off = TaskPlanner()
    pl_off.CONTEST_PHASE_ENABLED = False
    r_off = disc(pl_off, gs_post)
    ok &= check("分段折扣: 关后净值高于平折扣口径",
                r_on is not None and r_off is not None
                and r_on[0] > r_off[0],
                f"on={r_on} off={r_off}")
    # 4) 对手已交付 → 完全不折（净值又高于 0.9 口径；任务同在 S13）
    gs_done = gs_cp("S12", "S15", opp_delivered=True, task_at="S13")
    pl3 = TaskPlanner()
    r_done = disc(pl3, gs_done)
    ok &= check("分段折扣: 对手已交付不折扣",
                r_done is not None and r_done[0] > r_on[0],
                f"done={r_done} post={r_on}")

    # ---- 前推偏置 + 对手手册（V3.24）----
    import lychee.strategy as SMOD
    # 5) 因子语义：开局节点吃地板、宫门方向趋近 1、默认关=恒 1
    gs = gs_cp("S07", "S01")
    pl_fb = TaskPlanner()
    ok &= check("前推: 默认关恒等于 1",
                pl_fb._forward_factor(gs, "S02") == 1.0, "")
    pl_fb2 = TaskPlanner()
    pl_fb2.FORWARD_BIAS_FLOOR = 0.6
    f_early = pl_fb2._forward_factor(gs, "S02")
    f_late = pl_fb2._forward_factor(gs, "S13")
    ok &= check("前推: 开局节点降权且后段趋近 1",
                f_early < 0.75 < 0.9 < f_late, f"S02={f_early:.2f} S13={f_late:.2f}")
    # 6) 冲锋型在线识别（V3.25，撤 ID 手册后的触发方式）：
    #    在途任务分 ≥30 + 从不回头 → 激活；蹲点型画像后到 → 撤销
    del SMOD  # （V3.24 的 ID 手册已撤，import 保留位不再使用）

    def absorb_seq(st, positions, task_score):
        for pos, ts in zip(positions, task_score):
            gs = gs_cp("S07", pos)
            for p in gs.players.values():
                if p["playerId"] != 1001:
                    p["taskScore"] = ts
            st._absorb_feedback(gs)
        return st

    # 正例：对手 S03→S07→S09 单调推进且任务分涨到 60 → 识别为冲锋型
    st = absorb_seq(PlannerStrategy(), ["S03", "S07", "S09"], [0, 30, 60])
    ok &= check("冲锋识别: 边冲边农触发",
                st.planner.forward_rush_opp, "")
    # 反例 1：任务分恒 0 的直线推进（蹲点/竞速型）→ 不触发
    st = absorb_seq(PlannerStrategy(), ["S03", "S07", "S09"], [0, 0, 0])
    ok &= check("冲锋识别: 零任务分不触发",
                not st.planner.forward_rush_opp, "")
    # 反例 2：回头游走的农任务型 → 不触发（S09 后回撤 S05）
    st = absorb_seq(PlannerStrategy(), ["S03", "S07", "S09", "S05"],
                    [0, 0, 0, 30])
    ok &= check("冲锋识别: 回头游走不触发",
                not st.planner.forward_rush_opp, "")
    # 撤销：先触发，画像后到蹲点型 → 撤销
    st = absorb_seq(PlannerStrategy(), ["S03", "S07", "S09"], [0, 30, 60])
    st._opp_profile = "camper"
    st._fwd_rush_tick(gs_cp("S07", "S10"))
    ok &= check("冲锋识别: 蹲点画像后到即撤销",
                not st.planner.forward_rush_opp, "")
    # 因子联动：识别激活时 _forward_factor 吃 AUTO 地板
    st = absorb_seq(PlannerStrategy(), ["S03", "S07", "S09"], [0, 30, 60])
    gs = gs_cp("S07", "S09")
    f = st.planner._forward_factor(gs, "S02")
    ok &= check("冲锋识别: 激活后前期节点降权",
                f < 0.75, f"factor={f:.2f}")
    return ok


def test_target_stickiness():
    """V3.18 目标粘性：换目标要求 15% 净值优势，消除同级目标间的震荡。"""
    ok = True
    from lychee.planner import TaskPlanner
    pl = TaskPlanner()

    # 1) 首帧选 argmax 并承诺
    key = pl._sticky_choice({"A": (100, None), "B": (90, None)})
    ok &= check("粘性: 首帧承诺 argmax", key == "A", str(key))
    # 2) B 小幅反超（105 < 100×1.15）→ 不换
    key = pl._sticky_choice({"A": (100, None), "B": (105, None)})
    ok &= check("粘性: 15% 以内的反超不换", key == "A", str(key))
    # 3) B 显著反超（120 > 115）→ 换并更新承诺
    key = pl._sticky_choice({"A": (100, None), "B": (120, None)})
    ok &= check("粘性: 15% 以上的反超换目标", key == "B", str(key))
    # 4) 承诺目标失效（被抢/过期）→ 无粘性直接 argmax
    key = pl._sticky_choice({"A": (50, None)})
    ok &= check("粘性: 承诺失效直接换", key == "A", str(key))
    # 5) 无候选 → 清空承诺
    key = pl._sticky_choice({})
    ok &= check("粘性: 无候选清空承诺",
                key is None and pl._committed is None, str(key))
    return ok


def test_trap_ransom():
    """V3.18 陷阱等待租买止损：等够绕路差价换走廊；短边豁免；真漏斗口照旧等。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_camp(my_pos, camp_pos, round_no=200, edge_patch=None,
                task_score=90, opp_task_score=0):
        """对手停靠在我们去宫门的下一跳上（无卡，纯占位威胁）。"""
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        if edge_patch:
            for e in d.get("edges") or []:
                if e["edgeId"] in edge_patch:
                    e["distance"] = edge_patch[e["edgeId"]]
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=my_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=95.0, goodFruit=90,
                         badFruit=2, taskScore=task_score, squadAvailable=8)
            else:
                p.update(state="IDLE", currentNodeId=camp_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None,
                         delivered=False, retired=False,
                         taskScore=opp_task_score)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    def gs_converge(my_pos, opp_cur, opp_next, round_no=200):
        """对手正在赶往我们的普通下一跳，用于普通收敛掐边测试。"""
        gs = gs_camp(my_pos, opp_cur, round_no=round_no, task_score=130)
        for p in gs.players.values():
            if p["playerId"] != 1001:
                p.update(state="MOVING", currentNodeId=opp_cur,
                         nextNodeId=opp_next, routeEdgeId="E_X",
                         edgeTotalMs=10000, edgeProgressMs=0,
                         currentProcess=None)
        return gs

    # 1) 有第二条走廊时租买止损：S06 去宫门直路经 S08（山口，对手蹲点），
    #    替代走廊 S06→S03→官道存在。先等（廉价期权），等够绕路差价改道
    st = PlannerStrategy()
    moved, waited = None, 0
    for i in range(140):
        a = st.main_action(gs_camp("S06", "S08", round_no=200 + i))
        if a and a["action"] == "MOVE":
            moved = a
            break
        waited += 1
    ok &= check("租买: 蹲点者占山口，等够差价后改道官道",
                moved is not None and moved["targetNodeId"] == "S03",
                f"waited={waited} -> {moved}")
    ok &= check("租买: 改道前先付等待期权（不秒切）",
                30 <= waited <= 130, f"waited={waited}")
    ok &= check("租买: 改道承诺已记账",
                st._trap_avoid[0] == "S08", str(st._trap_avoid))

    # 1b) 承诺期间寻路持续绕开被避节点（不会下一帧又拐回去）
    a = st.main_action(gs_camp("S06", "S08", round_no=200 + waited + 1))
    ok &= check("租买: 承诺期间不回头", a and a["action"] == "MOVE"
                and a["targetNodeId"] == "S03", str(a))

    # 1c) 对手离开被避节点 → 承诺提前解除
    gs = gs_camp("S06", "S07", round_no=200 + waited + 2)  # 对手已去 S07
    st.decide(gs)
    ok &= check("租买: 对手离开即解除承诺",
                st._trap_avoid[0] is None, str(st._trap_avoid))

    # 2) 真漏斗口（S10，绕不开）→ 永远等待，V3.15 结论不回退
    st2 = PlannerStrategy()
    last = None
    for i in range(60):
        last = st2.main_action(gs_camp("S09", "S10", round_no=200 + i))
    ok &= check("租买: 真漏斗口绕不开照旧等待",
                last and last["action"] == "WAIT", str(last))
    # 2a) 山路口袋遇高分贴宫门且从未露卡的边农边冲者，仍不把
    #     "尚无卡证据"当安全证据：真实平台 lose 批次显示首卡常在
    #     我方已上长边后才落下，抢边会被中段冻结。
    st_probe = PlannerStrategy()
    moved = None
    for i in range(14):
        a = st_probe.main_action(gs_camp(
            "S08", "S10", round_no=300 + i, task_score=130,
            opp_task_score=120))
        if a and a["action"] == "MOVE":
            moved = a
            break
    ok &= check("租买: 山口边农边冲无卡证据不抢长边",
                moved is None, str(moved))

    # 2b) 配额语义修正（V3.28，规则 921）：曾把"对手两张卡配额已满"当
    #     无弹药豁免直接过——但规则原文是第 3 张卡合法且顶掉最早的
    #     （已扣成本不返还，无额外代价）。对手挂两张废卡就能骗过豁免
    #     再掐踏边。正确行为：占位威胁仍在，继续等待
    gs = gs_camp("S09", "S10", task_score=130)
    for nid in ("S05", "S07"):   # 对手的两张激活卡在别处
        gs.nodes[nid]["guard"] = {"ownerTeamId": "BLUE", "defense": 3,
                                  "maxDefense": 7, "active": True}
    a = PlannerStrategy().main_action(gs)
    ok &= check("租买: 对手卡配额满仍是威胁（921 第三张顶掉旧卡）",
                a and a["action"] == "WAIT", str(a))

    # 2c) 无弹药豁免之二：KEY_PASS 底价 1 好果掏不出 → 同样直接过
    gs = gs_camp("S09", "S10", task_score=130)
    for p in gs.players.values():
        if p["playerId"] != 1001:
            p["goodFruit"] = 0
    a = PlannerStrategy().main_action(gs)
    ok &= check("租买: 对手掏不出关隘底价好果直接过",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S10",
                str(a))

    # 3) 短边豁免：S13→S14 (E09) 改成 3 帧短边（设卡读条 4 帧），对手蹲在
    #    宫门也来不及成卡 → 直接过，规则数学可证（task_score 130 关掉 S13
    #    候选点的蹲刷分支，隔离被测逻辑）
    a = PlannerStrategy().main_action(
        gs_camp("S13", "S14", edge_patch={"E09": 2}, task_score=130))
    ok &= check("租买: 短边豁免直接过（读条 4 帧追不上）",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S14",
                str(a))
    # 3b) 宫门 RUSH 将开：对手提前在 S14 等 RUSH 时，若我们预计到门口
    #     正好赶上开门且能处理预期门卡，不再在门外白等到对手先验核。
    a = PlannerStrategy().main_action(
        gs_camp("S13", "S14", round_no=RUSH_EARLIEST - 50, task_score=130))
    ok &= check("租买: RUSH 还早时宫门占位仍等待",
                a and a["action"] == "WAIT", str(a))
    a = PlannerStrategy().main_action(
        gs_camp("S13", "S14", round_no=RUSH_EARLIEST - 18, task_score=130))
    ok &= check("租买: RUSH 将开且可破门卡时不在宫门外白等",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S14",
                str(a))
    # 4) 普通节点收敛掐边：收敛分支仍不防普通节点，避免 farmer 误伤；
    #    普通节点威胁通过驻扎等待和我方先到反手卡处理。
    gs = gs_converge("S04", "S02", "S05")
    a = PlannerStrategy().main_action(gs)
    ok &= check("租买: 普通节点收敛不误等",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S05",
                str(a))
    return ok


def test_card_profile():
    """V3.18 出牌画像：对手出牌频率加权替代均匀假设；种子 RNG 回放可复现。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_contest():
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = 100
        d["contests"], d["tasks"] = [], []
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId="S02", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={"PASS_TOKEN": 1}, freshness=95.0,
                         goodFruit=90, badFruit=0, guardActionPoint=1)
            else:  # 对手可负担全部 5 张牌
                p.update(state="IDLE", currentNodeId="S02", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={"FAST_HORSE": 1, "PASS_TOKEN": 1},
                         freshness=95.0, goodFruit=5, guardActionPoint=2,
                         delivered=False, retired=False)
        gs.on_inquire(d)
        return gs

    contest = {"contestId": "C_P", "contestType": "DOCK",
               "redPlayerId": 1001, "bluePlayerId": 2002}

    # 1) 无观测（均匀先验）：兵争克验牒/强行且近零成本，期望最优
    st = PlannerStrategy()
    st.CARD_MIX_RATE = 0  # 关掉混合，测纯 best-response
    pick_uniform = st.pick_card(gs_contest(), contest)
    ok &= check("画像: 无观测按均匀先验出兵争",
                pick_uniform == P.CARD_BING_ZHENG, str(pick_uniform))

    # 2) 观测到对手嗜好献贡（10 次）→ 兵争会被献贡克死，改打克献贡的强行
    #    （我方有马增益免费）
    st = PlannerStrategy()
    st.CARD_MIX_RATE = 0
    st._opp_card_hist = {P.CARD_XIAN_GONG: 10}
    gs = gs_contest()
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["buffs"] = [{"type": "SHORT_HORSE", "remainRound": 10}]
    pick_biased = st.pick_card(gs, contest)
    ok &= check("画像: 对手嗜好献贡则改打强行克制",
                pick_biased == P.CARD_QIANG_XING, str(pick_biased))

    def gs_s02_deadlock():
        gs = gs_contest()
        for p in gs.players.values():
            p.update(currentNodeId="S02", nextNodeId=None, routeEdgeId=None,
                     currentProcess=None, buffs=[], resources={},
                     freshness=96.0, goodFruit=90, guardActionPoint=4)
        gs.round = 72
        gs.events = []
        return gs

    st = PlannerStrategy()
    st.CARD_MIX_RATE = 0
    s02 = gs_s02_deadlock()
    deadlock_contest = {"contestId": "C_S02", "contestType": P.CONTEST_DOCK,
                        "targetNodeId": "S02",
                        "redPlayerId": 1001, "bluePlayerId": 2002}
    ok &= check("画像: S02 无压力仍按献贡最优",
                st.pick_card(s02, deadlock_contest) == P.CARD_XIAN_GONG, "")
    s02.events = [
        {"type": "WINDOW_CONTEST_DRAW",
         "payload": {"contestType": P.CONTEST_DOCK, "targetNodeId": "S02"}},
        {"type": "WINDOW_CONTEST_REPEAT_SUPPRESSED",
         "payload": {"contestType": P.CONTEST_DOCK, "targetNodeId": "S02"}},
    ]
    st._absorb_feedback(s02)
    ok &= check("画像: S02 连续平局后止损弃权",
                st.pick_card(s02, deadlock_contest) == P.CARD_ABSTAIN, "")

    # 3) 种子 RNG：同 matchId+playerId 的两个实例出牌序列完全一致
    #    （回放回归可复现；对手不知道种子派生方式，博弈价值不受影响）
    s1, s2 = PlannerStrategy(), PlannerStrategy()
    gs = gs_contest()
    seq1 = [s1.pick_card(gs, contest) for _ in range(30)]
    seq2 = [s2.pick_card(gs, contest) for _ in range(30)]
    ok &= check("画像: 同局种子出牌序列可复现", seq1 == seq2,
                f"{seq1[:5]} vs {seq2[:5]}")
    return ok


def test_opp_profile():
    """V3.20 对手画像：关隘闲置驻扎累计达阈值 → 蹲点型 → 漏斗先验免首卡升 1.0。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def prof_state(round_no, opp_node="S10", opp_proc=None, my_guard=None):
        """我在 S02 早期位置；对手 IDLE 停靠 opp_node；全图无卡无障碍。"""
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId="S02", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         goodFruit=8, badFruit=2, freshness=98.0,
                         resources={}, taskScore=0)
            else:
                p.update(state="IDLE", currentNodeId=opp_node, nextNodeId=None,
                         routeEdgeId=None, currentProcess=opp_proc,
                         delivered=False, retired=False)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            if my_guard and n["nodeId"] == my_guard:
                n["guard"] = {"ownerTeamId": "RED", "defense": 4,
                              "maxDefense": 7, "active": True}
        gs.on_inquire(d)
        return gs

    def feed(st, frames=None, **kw):
        n = frames if frames is not None else st.PROFILE_CAMP_IDLE + 3
        for i in range(n):
            st.decide(prof_state(40 + i, **kw))
        return st

    # 1) 正例：对手在武关（KEY_PASS）无事闲站 ≥阈值帧 → 蹲点型
    st = feed(PlannerStrategy())
    ok &= check("画像: 关隘闲置驻扎达阈值分类蹲点型",
                st._opp_profile == "camper", st._opp_profile)

    # 1b) 效果：分类后漏斗先验免首卡升 1.0（_guard_seen 仍为 False）
    ctx = st.planner._funnel_ctx(prof_state(60), "S02")
    ok &= check("画像: 蹲点型漏斗先验免首卡升 1.0",
                not st.planner._guard_seen and ctx and ctx[2] == 1.0, str(ctx))

    # 1c) 蹲潼关（PASS 型关隘）同样识别——replay36 死局的通行费来自双关卡
    st = feed(PlannerStrategy(), opp_node="S11")
    ok &= check("画像: 蹲 PASS 型关隘同样识别",
                st._opp_profile == "camper", st._opp_profile)

    # 2) 反例：对手在关隘做任务（currentProcess 非空）不算闲置——镜像不误伤
    st = feed(PlannerStrategy(), frames=30,
              opp_proc={"action": "CLAIM_TASK", "taskId": "T_X",
                        "remainRound": 10})
    ok &= check("画像: 关隘做任务不算蹲点",
                st._opp_profile == "unknown", st._opp_profile)

    # 3) 反例：对手停在我方卡的邻节点（S10 邻 S11 有我卡）是在等风化，
    #    不是蹲点——镜像局对峙不误伤
    st = feed(PlannerStrategy(), frames=30, my_guard="S11")
    ok &= check("画像: 我方卡前等待不算蹲点",
                st._opp_profile == "unknown", st._opp_profile)

    # 4) 反例：分类窗口外（>PROFILE_WINDOW）不再分类——晚期关隘等待多为
    #    战术性，且先验早被首卡定死
    st = PlannerStrategy()
    for i in range(30):
        st.decide(prof_state(st.PROFILE_WINDOW + 10 + i))
    ok &= check("画像: 窗口外不分类",
                st._opp_profile == "unknown", st._opp_profile)

    # 5) 开关（A/B 用）：关掉后 planner 只见 unknown，先验回落默认
    st = PlannerStrategy()
    st.PROFILE_ENABLED = False
    feed(st)
    ctx = st.planner._funnel_ctx(prof_state(60), "S02")
    ok &= check("画像: 开关关闭时先验回落默认",
                st.planner.opp_profile == "unknown"
                and ctx and ctx[2] == st.planner.FUNNEL_GUARD_PRIOR, str(ctx))
    return ok


def test_farm_meta():
    """V3.26 农夫 meta 三件套（reports 三败局 vs2986/2619/2738 驱动）。

    ① farmer 画像：对手在途任务分 ≥60 且全场未见其设卡 → 漏斗先验降
       0.35（落卡即被 _guard_seen 粘性覆盖，2839 防御不拆）；
    ② 悬崖共点对峙豁免：任务在脚下、对手也在场 → 悬崖前提（我停它不停）
       不成立，回落竞速帧价——vs2619 的 S07 三连 90 分不再白让；
    ③ RUSH 余量分档 60→25：尾段零绕路小任务不再被 deliver 模式整档熔断。
    """
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_farm(my_pos="S07", opp_pos="S07", round_no=167, opp_task=0,
                my_task=45, phase="NORMAL", task_at=None, task_score=30,
                proc=4, opp_guard_at=None, opp_proc=None):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"], d["phase"] = round_no, phase
        d["contests"] = []
        d["weather"] = {"active": [], "forecast": []}
        d["tasks"] = []
        if task_at:
            d["tasks"] = [{"taskId": "T_FM", "taskTemplateId": "T01",
                           "nodeId": task_at, "score": task_score,
                           "processRound": proc, "active": True,
                           "completed": False, "ownerId": 0,
                           "expireRound": 600, "protectTeam": 0}]
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=my_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=95.0, goodFruit=90,
                         badFruit=0, taskScore=my_task, verified=False)
            else:
                p.update(state="IDLE", currentNodeId=opp_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=opp_proc, buffs=[],
                         delivered=False, retired=False, taskScore=opp_task)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
            if opp_guard_at and n["nodeId"] == opp_guard_at:
                n["guard"] = {"ownerTeamId": "BLUE", "defense": 5,
                              "maxDefense": 7, "active": True,
                              "completeRound": round_no - 5,
                              "initialDefense": 6}
        gs.on_inquire(d)
        return gs

    # ---- ① farmer 画像 ----
    st = PlannerStrategy()
    st.decide(gs_farm(opp_pos="S09", opp_task=90))
    ok &= check("农夫: 任务分 ≥60 且未见卡分类 farmer",
                st._opp_profile == "farmer", st._opp_profile)
    ctx = st.planner._funnel_ctx(gs_farm(opp_pos="S09", opp_task=90), "S07")
    ok &= check("农夫: farmer 漏斗先验降档 0.35",
                ctx and ctx[2] == st.planner.FUNNEL_FARMER_PRIOR, str(ctx))
    # 对手落卡后 _guard_seen 粘性覆盖 farmer 档——2839 防御不拆
    # （round_no 换新帧：_funnel_cache 按 (round, cur) 键，同帧会吃缓存）
    gs_g = gs_farm(opp_pos="S09", opp_task=90, opp_guard_at="S10",
                   round_no=168)
    ctx = st.planner._funnel_ctx(gs_g, "S07")
    ok &= check("农夫: 落卡后先验回 1.0（粘性覆盖）",
                st.planner._guard_seen and ctx and ctx[2] == 1.0, str(ctx))
    # 分数不够（样例 30）不分类——顺手一个任务不算农夫
    st2 = PlannerStrategy()
    st2.decide(gs_farm(opp_pos="S09", opp_task=30))
    ok &= check("农夫: 任务分 <60 不分类",
                st2._opp_profile == "unknown", st2._opp_profile)
    # 关隘排除（V3.26.1，电池抓获）：站在武关上农到 90 的是延迟 camper
    # 候选不是农夫——随机化 camper 曾 12/48 误判、seed15 退回未交付
    st2b = PlannerStrategy()
    st2b.decide(gs_farm(opp_pos="S10", opp_task=90))
    ok &= check("农夫: 关隘上农任务不分类（延迟 camper 不误判）",
                st2b._opp_profile == "unknown", st2b._opp_profile)

    # ---- ② 悬崖共点对峙豁免 ----
    # 复刻 vs2619 r169：双方同在 S07，对手分 0（行为门看不见未来的农夫）
    # 且正在读任务条（"同桌"实锤，V3.26.2 口径），悬崖激活中，
    # 脚下 30 分任务应被接下而不是让给对手
    OPP_PROC = {"action": "CLAIM_TASK", "taskId": "T_OPP",
                "targetNodeId": "S07", "remainRound": 3}
    st3 = PlannerStrategy()
    gs_melee = gs_farm(task_at="S07", opp_proc=OPP_PROC)
    assert st3.planner.race_cliff(gs_melee), "前提: 悬崖带应激活"
    plan = st3.planner.plan(gs_melee)
    ok &= check("对峙: 悬崖带内共点任务被接下（vs2619 复刻）",
                plan.kind == "task" and plan.position == "S07", repr(plan))
    # 控制组 1：对手不在场（S05，非同桌）→ 悬崖照砍（seed10/15 不回退）
    st4 = PlannerStrategy()
    plan = st4.planner.plan(gs_farm(opp_pos="S05", task_at="S07"))
    ok &= check("对峙: 对手不在场时悬崖照砍",
                plan.kind != "task", repr(plan))
    # 控制组 1b：对手同点但只是停靠/领资源（无任务读条）→ 不豁免
    # ——camper 沿途 2 帧资源停靠曾触发豁免（seed15 -410，A/B 抓获）
    st4b = PlannerStrategy()
    plan = st4b.planner.plan(gs_farm(task_at="S07"))
    ok &= check("对峙: 对手仅停靠不豁免（seed15 钉子）",
                plan.kind != "task", repr(plan))
    # 控制组 2：见过对手设卡 → 不豁免（会设卡的对手同桌是钓鱼）
    st5 = PlannerStrategy()
    st5.planner._guard_seen = True
    plan = st5.planner.plan(gs_farm(task_at="S07"))
    ok &= check("对峙: 见过卡的对手不豁免",
                plan.kind != "task", repr(plan))

    # ---- ③ RUSH 余量分档 ----
    # 复刻 vs2986 r476@S12：T_021 型 15 分/5 帧任务，60 余量下 slack<0
    # 整档熔断，25 余量下应接下。对手已过关（S13），无悬崖干扰
    def rush_case(margin_normal):
        stx = PlannerStrategy()
        if margin_normal:
            stx.planner.RUSH_SAFETY_MARGIN = stx.planner.SAFETY_MARGIN
        gs = gs_farm(my_pos="S12", opp_pos="S13", round_no=476,
                     opp_task=150, my_task=120, phase="RUSH",
                     task_at="S12", task_score=15, proc=5)
        return stx.planner.plan(gs)
    plan = rush_case(margin_normal=False)
    ok &= check("余量: RUSH 分档后尾段零绕路任务放行（vs2986 复刻）",
                plan.kind == "task" and plan.position == "S12", repr(plan))
    plan = rush_case(margin_normal=True)
    ok &= check("余量: 旧 60 余量对照组确认熔断（回归基线）",
                plan.kind == "deliver", repr(plan))
    # 普通阶段余量不变：同局面 NORMAL 相同 round 仍按 60 算
    st6 = PlannerStrategy()
    gs_n = gs_farm(my_pos="S12", opp_pos="S13", round_no=476, opp_task=150,
                   my_task=120, phase="NORMAL", task_at="S12",
                   task_score=15, proc=5)
    plan = st6.planner.plan(gs_n)
    ok &= check("余量: 普通阶段仍按 60（不放松非 RUSH 路径）",
                plan.kind == "deliver", repr(plan))
    return ok


def test_road_tax():
    """V3.27 官道税修正：①阴影×漏斗去重 ②刷新流期望。

    reports 新败局（vs2986 -6 / vs2738 -5，任务分已平、纯走廊时差）：
    r99 岔路把官道冤枉成山线的楔子是武关被"阴影咽喉罚 35 + 漏斗税"
    双重计税；刷新流让热点波次（S07 型）在估值里可见。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]
    from lychee.planner import TaskPlanner

    def gs_road(round_no=100, opp_pos="S07", tasks=None):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"] = []
        d["tasks"] = tasks or []
        d["weather"] = {"active": [], "forecast": []}
        for p_ in d["players"]:
            if p_["playerId"] == 1001:
                p_.update(state="IDLE", currentNodeId="S03", nextNodeId=None,
                          routeEdgeId=None, currentProcess=None, buffs=[],
                          resources={}, freshness=95.0, goodFruit=95,
                          badFruit=0, taskScore=60)
            else:
                p_.update(state="IDLE", currentNodeId=opp_pos, nextNodeId=None,
                          routeEdgeId=None, currentProcess=None,
                          delivered=False, retired=False, taskScore=0)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    # ---- ① 阴影×漏斗去重 ----
    # 对手在 S07（官道，先于我们到 S10）→ S10 同时在阴影集和漏斗 ctx 里。
    # 去重开：S10 罚 = 时间罚（阴影 35 不叠）；去重关：罚 +35
    pl = TaskPlanner()
    gs = gs_road()
    pl._funnel_ctx(gs, "S03", pl._penalty_fn(gs), pl._edge_cost_fn(gs))
    assert pl._last_choke == "S10", f"前提: 漏斗咽喉应为 S10, got {pl._last_choke}"
    assert "S10" in pl._shadow_nodes(gs), "前提: S10 应在阴影集"
    pen_on = pl._penalty_fn(gs)("S10")
    pl2 = TaskPlanner()
    pl2.SHADOW_FUNNEL_DEDUP = False
    pl2._funnel_ctx(gs, "S03", pl2._penalty_fn(gs), pl2._edge_cost_fn(gs))
    pen_off = pl2._penalty_fn(gs)("S10")
    ok &= check("官道税: 漏斗咽喉不叠阴影罚（去重 35）",
                pen_off - pen_on == pl.SHADOW_CHOKE_PENALTY,
                f"on={pen_on} off={pen_off}")
    # 非漏斗咽喉的阴影罚保持原样（如对手路线上的其他咽喉）
    other = [n for n in pl._shadow_nodes(gs)
             if n != "S10" and gs.node(n).get("nodeType") in
             ("KEY_PASS", "PASS", "MOUNTAIN_PASS")]
    if other:
        ok &= check("官道税: 非漏斗咽喉阴影罚不变",
                    pl._penalty_fn(gs)(other[0])
                    == pl2._penalty_fn(gs)(other[0]), str(other[0]))

    # ---- ② 刷新流期望 ----
    def mk_task(tid, node, refresh):
        return {"taskId": tid, "taskTemplateId": "T01", "nodeId": node,
                "score": 30, "processRound": 4, "active": True,
                "completed": False, "ownerId": 0, "expireRound": 600,
                "protectTeam": 0, "refreshRound": refresh}
    pl3 = TaskPlanner()
    # 喂 4 帧观测：S07 三波、S06 一波
    for rnd, tasks in ((30, [mk_task("T_a", "S07", 30)]),
                       (60, [mk_task("T_a", "S07", 30), mk_task("T_b", "S06", 60)]),
                       (100, [mk_task("T_c", "S07", 100)]),
                       (120, [mk_task("T_d", "S07", 120)])):
        pl3._observe_spawns(gs_road(round_no=rnd, tasks=tasks))
    ok &= check("刷新流: 观测计数按节点累计",
                pl3._spawn_count.get("S07") == 3
                and pl3._spawn_count.get("S06") == 1,
                str(pl3._spawn_count))
    g_late = gs_road(round_no=150)
    r_hot = pl3._refresh_rate(g_late, "S07")
    r_cold = pl3._refresh_rate(g_late, "S09")
    ok &= check("刷新流: 热点率高于冷点", r_hot > r_cold >= 0.0,
                f"S07={r_hot:.4f} S09={r_cold:.4f}")
    b = pl3._refresh_bonus(g_late, "S07", ["S09", "S10"], 60)
    ok &= check("刷新流: 热点目标有正加成且不超上限",
                0 < b <= pl3.REFRESH_VALUE_CAP, f"bonus={b:.1f}")
    return ok


def test_rule_fixes():
    """V3.28 规则实锤修复：①RUSH 设卡解禁（6.5 只禁小分队）+ 宫前驿
    入列（2839 二卡镜像）②session 保命网（合法 JSON 坏结构不许杀进程）。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    # ---- ① RUSH 后卡：r455 我们领跑停靠 S13（宫前驿），对手 S11 在追
    #      （ETA ~35 在 8~150 窗口内）→ 应回手一张卡收尾段税
    gs = GameState(1001)
    gs.on_start(start)
    d = json.loads(json.dumps(inquire))
    d["round"], d["phase"] = 455, "RUSH"
    d["contests"], d["tasks"] = [], []
    d["weather"] = {"active": [], "forecast": []}
    for p_ in d["players"]:
        if p_["playerId"] == 1001:
            p_.update(state="IDLE", currentNodeId="S13", nextNodeId=None,
                      routeEdgeId=None, currentProcess=None, buffs=[],
                      resources={}, freshness=90.0, goodFruit=90, badFruit=0,
                      taskScore=150, verified=False)
        else:
            p_.update(state="IDLE", currentNodeId="S11", nextNodeId=None,
                      routeEdgeId=None, currentProcess=None, buffs=[],
                      delivered=False, retired=False, taskScore=150)
    for n in d["nodes"]:
        n["hasObstacle"] = False
        n["guard"] = None
        n["resourceStock"] = {}
        # 保留 S13 的宫前交接处理会挡在设卡之前，本用例只测设卡时机，
        # 统一清掉处理需求
        n.pop("processType", None)
        n["processRound"] = 0
    gs.on_inquire(d)
    st = PlannerStrategy()
    st._processed_here = True
    a = st.main_action(gs)
    ok &= check("规则: RUSH 领跑过宫前驿回手卡（2839 二卡镜像）",
                a and a["action"] == "SET_GUARD"
                and a.get("targetNodeId") == "S13", str(a))

    # ---- ② session 保命网：合法 JSON 但缺关键字段的 inquire 不杀进程
    from lychee.session import StrategySession
    from lychee_basic_client.config import Config

    class FakeSock:
        def __init__(self):
            self.sent = []
        def sendall(self, b):
            self.sent.append(b)

    cfg = Config(host="x", port=1, player_id=1001, player_name="t",
                 version="test")
    sess = StrategySession.__new__(StrategySession)
    sess._sock = FakeSock()
    sess._config = cfg
    sess._match_id = "M"
    from lychee.log import get_logger
    sess.log = get_logger(1001)
    sess.state = GameState(1001)
    sess.state.on_start(start)
    sess.strategy = PlannerStrategy()
    # players 缺 playerId、nodes 缺 nodeId——曾是穿透 run() 的 KeyError 型毒帧
    poison = {"round": 7, "players": [{"noPlayerId": True}],
              "nodes": [{"broken": 1}], "edges": []}
    try:
        sess._handle_inquire(poison)
        survived = True
    except Exception:
        survived = False
    ok &= check("保命网: 坏结构 inquire 不杀进程且已回包",
                survived and len(sess._sock.sent) == 1, f"sent={len(sess._sock.sent)}")
    # run() 级别：_handle_message 抛异常 → 跳帧 + 心跳（不退出）
    sent0 = len(sess._sock.sent)
    def boom(_msg):
        raise KeyError("round")
    sess._handle_message = boom
    import types
    # 手工模拟 run() 的保命网分支（不真起 socket 读循环）
    try:
        try:
            sess._handle_message({"msg_name": "inquire", "msg_data": {"round": 9}})
        except Exception:
            sess._safe_heartbeat(9)
        net_ok = True
    except Exception:
        net_ok = False
    ok &= check("保命网: 处理层异常兜底心跳可发出",
                net_ok and len(sess._sock.sent) == sent0 + 1,
                f"sent={len(sess._sock.sent)}")
    return ok


def test_farmer_walkin():
    """V3.29 farmer 咽喉有界等待（replay93：S09 对零设卡农夫死等 109 帧，
    r598 交付差 2 帧收盘）。三重门（farmer 画像+未见卡+读任务条中）
    全中 → 等待封顶 TRAP_FARMER_WAIT 后走边；任一门不中照旧无上限等。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_choke(round_no, opp_proc=None, opp_task=90, opp_moving=False,
                 task_at_choke=False, task_at_cur=False, my_task=150):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        if task_at_choke:
            d["tasks"] = [{"taskId": "T_CHOKE", "taskTemplateId": "T01",
                           "nodeId": "S10", "score": 30,
                           "processRound": 5, "active": True,
                           "completed": False, "failed": False,
                           "ownerPlayerId": 0, "protectionPlayerId": 0,
                           "expireRound": 600}]
        if task_at_cur:
            d["tasks"].append({"taskId": "T_IDLE_S09", "taskTemplateId": "T01",
                               "nodeId": "S09", "score": 15,
                               "processRound": 5, "active": True,
                               "completed": False, "failed": False,
                               "ownerPlayerId": 0, "protectionPlayerId": 0,
                               "expireRound": 600})
        d["weather"] = {"active": [], "forecast": []}
        for p_ in d["players"]:
            if p_["playerId"] == 1001:
                p_.update(state="IDLE", currentNodeId="S09", nextNodeId=None,
                          routeEdgeId=None, currentProcess=None, buffs=[],
                          resources={}, freshness=90.0, goodFruit=90,
                          badFruit=0, taskScore=my_task, verified=False)
            else:
                if opp_moving:
                    p_.update(state="MOVING", currentNodeId="S08",
                              nextNodeId="S10", routeEdgeId="E17",
                              edgeTotalMs=24000, edgeProgressMs=0,
                              currentProcess=None, buffs=[],
                              delivered=False, retired=False,
                              taskScore=opp_task, goodFruit=90)
                else:
                    p_.update(state="IDLE", currentNodeId="S10", nextNodeId=None,
                              routeEdgeId=None, currentProcess=opp_proc, buffs=[],
                              delivered=False, retired=False, taskScore=opp_task,
                              goodFruit=90)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    FARM_PROC = {"action": "CLAIM_TASK", "taskId": "T_X",
                 "targetNodeId": "S10", "remainRound": 3}

    def feed(opp_proc, profile, frames):
        st = PlannerStrategy()
        st.PROFILE_ENABLED = False          # 手动钉画像，隔离被测逻辑
        st._opp_profile = profile
        last = None
        for i in range(frames):
            g = gs_choke(260 + i, opp_proc=opp_proc)
            st.planner.opp_profile = profile
            last = st.main_action(g)
        return st, last

    st, a = feed(FARM_PROC, "farmer", 30)
    ok &= check("农夫走边: 三重门全中等待封顶后走边（replay93 钉子）",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S10",
                str(a))
    st, a = feed(FARM_PROC, "farmer", 20)
    ok &= check("农夫走边: 预算内仍等待（不秒过）",
                a and a["action"] == "WAIT", str(a))
    st, a = feed(None, "farmer", 30)
    ok &= check("农夫走边: 它没在读任务条则照旧无上限等",
                a and a["action"] == "WAIT", str(a))
    st, a = feed(FARM_PROC, "camper", 30)
    ok &= check("农夫走边: camper 画像不豁免（V3.15 教义不动）",
                a and a["action"] == "WAIT", str(a))
    st = PlannerStrategy()
    st.PROFILE_ENABLED = False
    st._opp_profile = "farmer"
    st.planner._guard_seen = True
    last = None
    for i in range(30):
        g = gs_choke(260 + i, opp_proc=FARM_PROC)
        st.planner.opp_profile = "farmer"
        last = st.main_action(g)
    ok &= check("农夫走边: 见过卡的对手不豁免",
                last and last["action"] == "WAIT", str(last))

    def feed_converge(profile, frames, opp_task=90, guard_seen=False,
                      task_at_choke=False):
        st = PlannerStrategy()
        st.PROFILE_ENABLED = False
        st._opp_profile = profile
        st.planner._guard_seen = guard_seen
        last = None
        for i in range(frames):
            g = gs_choke(300 + i, opp_task=opp_task, opp_moving=True,
                         task_at_choke=task_at_choke)
            last = st.main_action(g)
        return last

    a = feed_converge("farmer", 1, task_at_choke=True)
    ok &= check("农夫走边: 可见任务且无重叠时收敛零等待",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S10",
                str(a))
    st_idle = PlannerStrategy()
    st_idle.PROFILE_ENABLED = False
    st_idle._opp_profile = "farmer"
    g_idle = gs_choke(320, opp_task=150, opp_moving=True,
                      task_at_cur=True, my_task=120)
    a = st_idle.main_action(g_idle, Plan("deliver", slack=120))
    ok &= check("农夫走边: 防陷阱等待帧顺手吃脚下短任务",
                a and a["action"] == "CLAIM_TASK"
                and a["taskId"] == "T_IDLE_S09", str(a))
    a = feed_converge("farmer", 13)
    ok &= check("农夫走边: 高分 farmer 收敛咽喉短等后走边（replay95 钉子）",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S10",
                str(a))
    a = feed_converge("farmer", 12)
    ok &= check("农夫走边: farmer 收敛预算内仍等待",
                a and a["action"] == "WAIT", str(a))
    a = feed_converge("farmer", 20, opp_task=60)
    ok &= check("农夫走边: 收敛任务分不足不豁免",
                a and a["action"] == "WAIT", str(a))
    a = feed_converge("camper", 20)
    ok &= check("农夫走边: camper 收敛不豁免",
                a and a["action"] == "WAIT", str(a))
    a = feed_converge("farmer", 20, guard_seen=True)
    ok &= check("农夫走边: 见过卡的 farmer 收敛不豁免",
                a and a["action"] == "WAIT", str(a))

    def gs_farm_rusher(round_no, camped=False):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"], d["phase"] = round_no, "NORMAL"
        d["contests"], d["tasks"] = [], []
        d["weather"] = {"active": [], "forecast": []}
        edge_total = gs.graph.edge_total_move(gs.graph.edges["E05"])
        for p_ in d["players"]:
            if p_["playerId"] == 1001:
                p_.update(state="IDLE", currentNodeId="S08", nextNodeId=None,
                          routeEdgeId=None, currentProcess=None, buffs=[],
                          resources={}, freshness=90.0, goodFruit=90,
                          badFruit=2, taskScore=130, verified=False)
            else:
                if camped:
                    p_.update(state="IDLE", currentNodeId="S10", nextNodeId=None,
                              routeEdgeId=None, currentProcess=None, buffs=[],
                              delivered=False, retired=False, taskScore=90,
                              goodFruit=90)
                else:
                    p_.update(state="MOVING", currentNodeId="S09",
                              nextNodeId="S10", routeEdgeId="E05",
                              edgeTotalMs=edge_total,
                              edgeProgressMs=edge_total - 5000,
                              currentProcess=None, buffs=[],
                              delivered=False, retired=False, taskScore=90,
                              goodFruit=90)
        for n in d["nodes"]:
            n["hasObstacle"] = False
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    st = PlannerStrategy()
    moved = None
    for i in range(20):
        a = st.main_action(gs_farm_rusher(287 + i, camped=False))
        if a and a["action"] == "MOVE":
            moved = a
            break
    ok &= check("边农边冲: 汇聚中不走12帧放行（2839死形钉子）",
                moved is None, str(moved))
    st = PlannerStrategy()
    moved = None
    for i in range(20):
        a = st.main_action(gs_farm_rusher(287 + i, camped=True))
        if a and a["action"] == "MOVE":
            moved = a
            break
    ok &= check("边农边冲: 已停靠口袋点仍不抢可设卡长边",
                moved is None, str(moved))
    return ok


def test_front_tempo_tail_follow():
    """V3.30/V3.35：全量 FRONT_TEMPO 仍关；山路线保速轻门默认开。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_front(bad=0, stock=False, tasks=True, opp_cur="S03",
                 opp_next="S07", opp_edge="E03", opp_progress=6000,
                 obstacle=True):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"], d["phase"] = 90, "NORMAL"
        d["contests"] = []
        d["weather"] = {"active": [], "forecast": []}
        d["tasks"] = []
        if tasks is True:
            d["tasks"] = [
                {"taskId": "T_S03", "taskTemplateId": "T01",
                 "nodeId": "S03", "processRound": 3, "score": 30,
                 "expireRound": 300, "active": True, "completed": False,
                 "failed": False, "ownerPlayerId": 0, "routeBucket": P.ROAD,
                 "protectionPlayerId": 0},
                {"taskId": "T_S06", "taskTemplateId": "T04",
                 "nodeId": "S06", "processRound": 6, "score": 30,
                 "expireRound": 300, "active": True, "completed": False,
                 "failed": False, "ownerPlayerId": 0,
                 "routeBucket": P.MOUNTAIN,
                 "protectionPlayerId": 0},
            ]
        elif tasks:
            d["tasks"] = list(tasks)
        total = gs.graph.edge_total_move(gs.graph.edges[opp_edge])
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId="S03", nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=98.0, goodFruit=90,
                         badFruit=bad, taskScore=0, verified=False)
            else:
                p.update(state="MOVING", currentNodeId=opp_cur,
                         nextNodeId=opp_next, routeEdgeId=opp_edge,
                         edgeTotalMs=total, edgeProgressMs=opp_progress,
                         currentProcess=None, buffs=[],
                         delivered=False, retired=False, taskScore=0)
        for n in d["nodes"]:
            n["hasObstacle"] = n["nodeId"] == "S06" and obstacle
            n["guard"] = None
            n["resourceStock"] = {}
            if stock and n["nodeId"] == "S03":
                n["resourceStock"] = {P.ICE_BOX: 1, P.PASS_TOKEN: 1,
                                      P.INTEL: 1}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    pl = TaskPlanner()
    ok &= check("前段尾随: RoadFarmer 语料复证后默认启用（V3.91）",
                pl.FRONT_TEMPO_ENABLED, "")
    ok &= check("前段保速: 山路线轻门默认开启",
                pl.FRONT_TEMPO_MOUNTAIN_RECOVERY, "")
    pl._map_progress = lambda state, cur: 0.1
    pl._opp_on_my_forward_path = lambda state, cur: False
    pl._opp_gate_lead = lambda state, cur: -10
    pl._opp_dwell_idle = 45          # 富点干等已实锤
    dummy = type("DummyState", (), {
        "phase": P.PHASE_NORMAL,
        "graph": None,     # farm_rusher_pressure 的早退门
        "opp": {"currentNodeId": "S04", "delivered": False, "retired": False,
                "taskScore": 45},
    })()
    ok &= check("前段尾随: 我方领先不足30帧仍算竞争（符号钉子）",
                pl._front_tempo_contested(dummy, "S03"), "")
    pl._opp_gate_lead = lambda state, cur: -31
    ok &= check("前段尾随: 我方领先超过30帧退出竞争",
                not pl._front_tempo_contested(dummy, "S03"), "")
    # V3.91 形态门控四轮收紧：只对"零设卡 + 富点干等实锤"的纯农尾随。
    # camper 的领先是诱饵（camper17 +75→-71）；rusher 不农任务
    # （rusher3 -89）；farm-rusher/toller 落卡前与纯农同貌但尾随=喂它
    # 掐踏边（toller0 +26→-36）；干等帧是 2986 与 2839 唯一行为分水岭
    pl._opp_gate_lead = lambda state, cur: -10
    pl.opp_profile = "camper"
    ok &= check("前段尾随: camper 画像不尾随（形态门控）",
                not pl._front_tempo_contested(dummy, "S03"), "")
    pl.opp_profile = "unknown"
    pl.forward_rush_opp = True
    dummy.opp["taskScore"] = 0
    ok &= check("前段尾随: 纯 rusher 不尾随（形态门控）",
                not pl._front_tempo_contested(dummy, "S03"), "")
    dummy.opp["taskScore"] = 45
    ok &= check("前段尾随: farm-rusher 不尾随（toller0 证据）",
                not pl._front_tempo_contested(dummy, "S03"), "")
    pl.forward_rush_opp = False
    ok &= check("前段尾随: 零设卡纯农+干等实锤 → 尾随",
                pl._front_tempo_contested(dummy, "S03"), "")
    pl._opp_dwell_idle = 0
    ok &= check("前段尾随: 无干等实锤不尾随（2839 落卡前不可区分）",
                not pl._front_tempo_contested(dummy, "S03"), "")
    pl._opp_dwell_idle = 45
    pl._guard_seen = True
    ok &= check("前段尾随: 见过卡即永久关闭尾随",
                not pl._front_tempo_contested(dummy, "S03"), "")
    pl._guard_seen = False

    # 尾随冻结预算（V3.93）：领先的活对手共享前路=暴露位；纯农豁免
    pl2 = TaskPlanner()
    pl2._opp_on_my_forward_path = lambda state, cur: True
    pl2._opp_gate_lead = lambda state, cur: 12
    dummy2 = type("DummyState", (), {
        "phase": P.PHASE_NORMAL, "graph": None,
        "opp": {"currentNodeId": "S07", "delivered": False,
                "retired": False, "taskScore": 45},
    })()
    ok &= check("冻结预算: 对手领先且共享前路 → 暴露",
                pl2._rear_freeze_exposed(dummy2, "S03"), "")
    pl2.opp_profile = "farmer"
    ok &= check("冻结预算: 纯农画像+未见卡 → 豁免",
                not pl2._rear_freeze_exposed(dummy2, "S03"), "")
    pl2._guard_seen = True
    ok &= check("冻结预算: 见过卡后 farmer 也不豁免",
                pl2._rear_freeze_exposed(dummy2, "S03"), "")
    pl2._guard_seen = False
    pl2.opp_profile = "unknown"
    pl2._opp_gate_lead = lambda state, cur: -8
    ok &= check("冻结预算: 我方领先无暴露",
                not pl2._rear_freeze_exposed(dummy2, "S03"), "")
    dummy2.opp["delivered"] = True
    pl2._opp_gate_lead = lambda state, cur: 12
    ok &= check("冻结预算: 对手已交付无暴露",
                not pl2._rear_freeze_exposed(dummy2, "S03"), "")

    st = PlannerStrategy()
    st.planner.FRONT_TEMPO_ENABLED = True
    st.planner._opp_dwell_idle = 45      # 富点干等实锤（V3.91 门控前置）
    gs = gs_front(tasks=True)
    gs.players[2002]["taskScore"] = 45   # farmer 模式前置
    plan = st.planner.plan(gs)
    ok &= check("前段尾随: S03/S06 低进度任务不再截停",
                plan.kind != "task", repr(plan))
    a = st.main_action(gs, plan)
    ok &= check("前段尾随: 无关键资源时直接追 S07",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S07",
                f"{plan} -> {a}")

    t_s06_travel = {"taskId": "T_S06_TRAVEL", "taskTemplateId": "T01",
                    "nodeId": "S06", "processRound": 6, "score": 30,
                    "expireRound": 300, "active": True, "completed": False,
                    "failed": False, "ownerPlayerId": 0,
                    "protectionPlayerId": 0, "routeBucket": P.MOUNTAIN}
    st_corr = PlannerStrategy()
    gs = gs_front(tasks=(t_s06_travel,), obstacle=False)
    plan = st_corr.planner.plan(gs)
    a = st_corr.main_action(gs, plan)
    ok &= check("前段走廊: 对手官道已动身时不离站切山路",
                plan.kind != "task" and a and a["action"] == "MOVE"
                and a["targetNodeId"] == "S07",
                f"{plan} -> {a}")

    t_s05_water = {"taskId": "T_S05_WATER", "taskTemplateId": "T02",
                   "nodeId": "S05", "processRound": 4, "score": 30,
                   "expireRound": 320, "active": True, "completed": False,
                   "failed": False, "ownerPlayerId": 0,
                   "protectionPlayerId": 0, "routeBucket": P.WATER}
    gs = gs_front(tasks=(t_s05_water,), obstacle=False)
    e04_total = gs.graph.edge_total_move(gs.graph.edges["E04"])
    gs.players[1001].update(currentNodeId="S07", taskScore=60,
                            resources={}, goodFruit=96, badFruit=1)
    gs.players[2002].update(state="MOVING", currentNodeId="S07",
                            nextNodeId="S09", routeEdgeId="E04",
                            edgeTotalMs=e04_total, edgeProgressMs=1000,
                            taskScore=60, currentProcess=None)
    st_water = PlannerStrategy()
    plan = st_water.planner.plan(gs)
    a = st_water.main_action(gs, plan)
    ok &= check("前段走廊: S07 后不为 S05 水路任务反切",
                plan.kind != "task" and a and a["action"] == "MOVE"
                and a["targetNodeId"] == "S09",
                f"{plan} -> {a}")

    t_s07_road = {"taskId": "T_S07_ROAD", "taskTemplateId": "T01",
                  "nodeId": "S07", "processRound": 4, "score": 30,
                  "expireRound": 320, "active": True, "completed": False,
                  "failed": False, "ownerPlayerId": 0,
                  "protectionPlayerId": 0, "routeBucket": P.ROAD}
    t_s06_mtn = {"taskId": "T_S06_MTN", "taskTemplateId": "T01",
                 "nodeId": "S06", "processRound": 6, "score": 30,
                 "expireRound": 300, "active": True, "completed": False,
                 "failed": False, "ownerPlayerId": 0,
                 "protectionPlayerId": 0, "routeBucket": P.MOUNTAIN}
    st_corr2 = PlannerStrategy()
    gs = gs_front(tasks=(t_s07_road, t_s06_mtn), opp_cur="S03",
                  opp_next="S06", opp_edge="E18", obstacle=False)
    plan = st_corr2.planner.plan(gs)
    ok &= check("前段走廊: 对手山路已动身时不反切官道任务",
                plan.kind == "task" and plan.position == "S06",
                repr(plan))

    st2 = PlannerStrategy()
    st2.planner.FRONT_TEMPO_ENABLED = True
    st2.planner._opp_dwell_idle = 45
    gs = gs_front(stock=True, tasks=False)
    gs.players[2002]["taskScore"] = 45
    plan = st2.planner.plan(gs)
    a = st2.main_action(gs, plan)
    ok &= check("前段尾随: 顺路领取收缩到硬件资源",
                a and a["action"] == "CLAIM_RESOURCE"
                and a["resourceType"] == P.ICE_BOX,
                f"{plan} -> {a}")

    pl_poor = PlannerStrategy().planner
    pen_poor = pl_poor._penalty_fn(gs_front(bad=0, tasks=False))
    pl_rich = PlannerStrategy().planner
    pen_rich = pl_rich._penalty_fn(gs_front(bad=2, tasks=False))
    ok &= check("前段尾随: 可秒破时咽喉阴影降档",
                pen_rich("S10") + 10 < pen_poor("S10"),
                f"poor={pen_poor('S10'):.1f} rich={pen_rich('S10'):.1f}")

    def gs_replay93(cur, base, tasks, opp_cur, opp_next, opp_edge,
                    opp_task_score=0):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"], d["phase"] = 100, "NORMAL"
        d["contests"] = []
        d["weather"] = {"active": [], "forecast": []}
        d["tasks"] = list(tasks)
        edge_total = gs.graph.edge_total_move(gs.graph.edges[opp_edge])
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=cur, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=94.0, goodFruit=95,
                         badFruit=0, taskScore=base, verified=False)
            else:
                p.update(state="MOVING", currentNodeId=opp_cur,
                         nextNodeId=opp_next, routeEdgeId=opp_edge,
                         edgeTotalMs=edge_total, edgeProgressMs=edge_total - 5000,
                         currentProcess=None, buffs=[],
                         delivered=False, retired=False,
                         taskScore=opp_task_score)
        for n in d["nodes"]:
            n["hasObstacle"] = n["nodeId"] in {"S06", "S08"}
            n["guard"] = None
            n["resourceStock"] = {}
            n.pop("processType", None)
            n["processRound"] = 0
        gs.on_inquire(d)
        return gs

    t_s06 = {"taskId": "T_S06_ONLY", "taskTemplateId": "T04",
             "nodeId": "S06", "processRound": 6, "score": 30,
             "expireRound": 300, "active": True, "completed": False,
             "failed": False, "ownerPlayerId": 0, "protectionPlayerId": 0,
             "routeBucket": P.MOUNTAIN}
    gs = gs_replay93("S03", 30, (t_s06,), "S02", "S03", "E02",
                     opp_task_score=30)
    st3 = PlannerStrategy()
    plan = st3.planner.plan(gs)
    ok &= check("前段保速: 默认不做 S06 山路清障",
                plan.kind != "task", repr(plan))

    gs = gs_replay93("S03", 30, (t_s06,), "S02", "S03", "E02",
                     opp_task_score=0)
    st_heavy = PlannerStrategy()
    plan = st_heavy.planner.plan(gs)
    a = st_heavy.main_action(gs, plan)
    ok &= check("前段重山线: 无对手画像也不被相邻 T04 拽进 S06",
                plan.kind != "task" and a and a["action"] == "MOVE"
                and a["targetNodeId"] == "S07",
                f"{plan} -> {a}")

    t_s10_same = {"taskId": "T_S10_SAME", "taskTemplateId": "T01",
                  "nodeId": "S10", "processRound": 4, "score": 30,
                  "expireRound": 420, "active": True, "completed": False,
                  "failed": False, "ownerPlayerId": 0,
                  "protectionPlayerId": 0, "routeBucket": P.WATER}
    gs = gs_replay93("S10", 90, (t_s10_same,), "S09", "S10", "E05",
                     opp_task_score=120)
    plan = PlannerStrategy().planner.plan(gs)
    ok &= check("前段保速: S10 同点 90->120 任务要补",
                plan.kind == "task" and plan.position == "S10",
                repr(plan))

    gs = gs_replay93("S10", 30, (t_s10_same,), "S09", "S10", "E05",
                     opp_task_score=150)
    gs.players[2002]["delivered"] = True
    gs.nodes["S10"]["resourceStock"] = {P.INTEL: 1}
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=130))
    ok &= check("S10低分直送: 先补脚下任务不先领情报",
                a and a["action"] == "CLAIM_TASK"
                and a["taskId"] == "T_S10_SAME",
                str(a))

    a = PlannerStrategy().main_action(
        gs, Plan("resource", position="S10", resource=P.INTEL, slack=130))
    ok &= check("S10低分补课: 非直送计划也先补脚下任务",
                a and a["action"] == "CLAIM_TASK"
                and a["taskId"] == "T_S10_SAME",
                str(a))

    t_s11_next = {"taskId": "T_S11_NEXT", "taskTemplateId": "T01",
                  "nodeId": "S11", "processRound": 4, "score": 30,
                  "expireRound": 420, "active": True, "completed": False,
                  "failed": False, "ownerPlayerId": 0,
                  "protectionPlayerId": 0, "routeBucket": P.ROAD}
    a = PlannerStrategy().main_action(
        gs, Plan("task", task=t_s11_next, position="S11", slack=130))
    ok &= check("S10低分补课: 去别处任务前先吃同点任务",
                a and a["action"] == "CLAIM_TASK"
                and a["taskId"] == "T_S10_SAME",
                str(a))

    gs = gs_replay93("S10", 120, (t_s10_same,), "S09", "S10", "E05",
                     opp_task_score=150)
    gs.players[2002]["delivered"] = True
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=90))
    ok &= check("S10低分直送: 120后继续补到150",
                a and a["action"] == "CLAIM_TASK"
                and a["taskId"] == "T_S10_SAME",
                str(a))

    # replay99 型水路终局：我方已 150 且站住 S10，脚下 30 分任务对自己
    # 是 0 边际，但能截掉对手最后一档任务组件；驻守模式应先截任务。
    t_s10_deny = {"taskId": "T_S10_DENY", "taskTemplateId": "T01",
                  "nodeId": "S10", "processRound": 4, "score": 30,
                  "expireRound": 540, "active": True, "completed": False,
                  "failed": False, "ownerPlayerId": 0,
                  "protectionPlayerId": 0, "routeBucket": P.WATER}
    gs = gs_replay93("S10", 150, (t_s10_deny,), "S09", "S10", "E05",
                     opp_task_score=120)
    gs.players[2002]["edgeProgressMs"] = 1000
    gs.nodes["S10"]["guard"] = {"ownerTeamId": gs.my_team, "defense": 6,
                                "maxDefense": 7, "active": True}
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=130))
    ok &= check("S10收租: 封顶后截胡对手任务波次",
                a and a["action"] == "CLAIM_TASK"
                and a["taskId"] == "T_S10_DENY", str(a))

    gs = gs_replay93("S10", 150, (), "S09", "S10", "E05",
                     opp_task_score=120)
    gs.players[2002]["edgeProgressMs"] = 1000
    gs.nodes["S10"]["guard"] = {"ownerTeamId": gs.my_team, "defense": 6,
                                "maxDefense": 7, "active": True}
    gs.nodes["S10"]["resourceStock"] = {P.INTEL: 1}
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=130))
    ok &= check("S10收租: 无任务时驻守不顺手领情报",
                a and a["action"] == "WAIT", str(a))

    gs = gs_replay93("S10", 120, (), "S09", "S10", "E05",
                     opp_task_score=120)
    gs.players[2002]["edgeProgressMs"] = 1000
    gs.nodes["S10"]["guard"] = {"ownerTeamId": gs.my_team, "defense": 6,
                                "maxDefense": 7, "active": True}
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=40))
    ok &= check("S10收租: 120分后也驻守武关所有权",
                a and a["action"] == "WAIT", str(a))

    gs = gs_replay93("S10", 120, (), "S09", "S10", "E05",
                     opp_task_score=120)
    gs.players[2002]["edgeProgressMs"] = 1000
    a = PlannerStrategy().main_action(gs, Plan("deliver", slack=-30))
    ok &= check("S10收租: 对手踏边时按硬截止临别设卡",
                a and a["action"] == "SET_GUARD" and a["targetNodeId"] == "S10",
                str(a))

    gs = gs_replay93("S10", 150, (), "S09", "S10", "E05",
                     opp_task_score=150)
    gs.players[2002]["edgeProgressMs"] = 1000
    gs.players[2002]["squadAvailable"] = 8
    gs.nodes["S10"]["guard"] = {"ownerTeamId": gs.my_team, "defense": 6,
                                "maxDefense": 7, "active": True}
    st_no_hold = PlannerStrategy()
    active = st_no_hold._s10_toll_hold_active(gs, Plan("deliver", slack=130),
                                              "S10")
    ok &= check("S10收租: 对手也封顶且人手充足则不驻守",
                not active, str(active))

    t_s09_same = {"taskId": "T_S09_SAME_LOW", "taskTemplateId": "T01",
                  "nodeId": "S09", "processRound": 5, "score": 15,
                  "expireRound": 540, "active": True, "completed": False,
                  "failed": False, "ownerPlayerId": 0,
                  "protectionPlayerId": 0, "routeBucket": P.WATER}
    gs = gs_replay93("S09", 60, (t_s09_same,), "S09", "S10", "E05",
                     opp_task_score=120)
    plan = PlannerStrategy().planner.plan(gs)
    ok &= check("前段保速: S09 低分同点任务不被悬崖价误杀",
                plan.kind == "task" and plan.position == "S09",
                repr(plan))

    gs_rescue = gs_replay93("S09", 90, (t_s09_same,), "S08", "S10", "E17",
                            opp_task_score=150)
    st_rescue = PlannerStrategy()
    a = st_rescue.main_action(gs_rescue, Plan("deliver", slack=120))
    ok &= check("前段保速: S09 直送兜底先吃脚下15分",
                a and a["action"] == "CLAIM_TASK"
                and a["taskId"] == "T_S09_SAME_LOW",
                f"{a}")

    t_s07 = {"taskId": "T_S07_OVER", "taskTemplateId": "T01",
             "nodeId": "S07", "processRound": 4, "score": 30,
             "expireRound": 320, "active": True, "completed": False,
             "failed": False, "ownerPlayerId": 0, "protectionPlayerId": 0,
             "routeBucket": P.ROAD}
    gs = gs_replay93("S07", 120, (t_s07,), "S07", "S09", "E04")
    gs.players[2002]["taskScore"] = 45   # farmer 模式前置（V3.91 门控）
    st4 = PlannerStrategy()
    st4.planner.FRONT_TEMPO_ENABLED = True
    st4.planner._opp_dwell_idle = 45     # 富点干等实锤前置
    plan = st4.planner.plan(gs)
    ok &= check("前段保速: S07 到120后先抢 S10 不贪到150",
                plan.kind != "task", repr(plan))
    return ok


def main():
    ok = test_codec()
    ok &= test_state_and_strategy()
    ok &= test_planner()
    ok &= test_contention()
    ok &= test_breakthrough()
    ok &= test_edge_block()
    ok &= test_p0_audit()
    ok &= test_horse_economy()
    ok &= test_watchdog()
    ok &= test_active_guard()
    ok &= test_corridor()
    ok &= test_ice_hunt()
    ok &= test_fresh_race()
    ok &= test_honest_eta()
    ok &= test_trap_proof()
    ok &= test_bundle()
    ok &= test_tempo_guard()
    ok &= test_replay25()
    ok &= test_tail_farm()
    ok &= test_reject_join()
    ok &= test_weaken_discipline()
    ok &= test_latent_mechanics()
    ok &= test_corridor_reserve()
    ok &= test_lenient_frame()
    ok &= test_race_tempo()
    ok &= test_race_cliff()
    ok &= test_parting_guard()
    ok &= test_contest_phase()
    ok &= test_target_stickiness()
    ok &= test_trap_ransom()
    ok &= test_card_profile()
    ok &= test_opp_profile()
    ok &= test_farm_meta()
    ok &= test_road_tax()
    ok &= test_rule_fixes()
    ok &= test_farmer_walkin()
    ok &= test_front_tempo_tail_follow()
    print()
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
