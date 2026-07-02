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
from lychee.planner import marginal_task_value, task_component_score
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

    # ---- 场景1c: 冰鉴鲜度 ≤90 就用（防跌破转坏阈值） ----
    gs = make_state()
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["freshness"] = 88.0
            p["resources"] = {"ICE_BOX": 1}
    gs.tasks = []  # 排除任务干扰
    a = PlannerStrategy().main_action(gs)
    ok &= check("保鲜: 鲜度88即用冰鉴",
                a and a["action"] == "USE_RESOURCE" and a["resourceType"] == "ICE_BOX",
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

    # ---- 场景1e: RUSH 在宫门先打护果令再验核 ----
    gs = make_state(node="S14")
    gs.phase = "RUSH"
    for p in gs.players.values():
        if p["playerId"] == 1001:
            p["rushTacticUsedCount"] = 0  # 样例里已用过急策，重置以测该分支
    st = PlannerStrategy()
    a1 = st.main_action(gs)
    a2 = st.main_action(gs)
    ok &= check("急策: 验核前打护果令",
                a1 and a1["action"] == "RUSH_PROTECT"
                and a2 and a2["action"] == "VERIFY_GATE",
                f"{a1} -> {a2}")

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
                              P.CARD_YAN_DIE, P.CARD_ABSTAIN} and len(cards) >= 2,
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

    # ---- 对手读条中：排队等待，不重复提交 ----
    gs = mirror_state(1001, 44)
    opp = gs.players[2002]
    opp.update(state="PROCESSING",
               currentProcess={"action": "PROCESS", "type": "PROCESS",
                               "targetNodeId": "S02", "remainRound": 3})
    a = PlannerStrategy().main_action(gs)
    ok &= check("错峰: 对手读条中我方排队", a["action"] == "WAIT", str(a))

    # ---- 混合出牌：镜像局面下出牌有随机性，不再恒定同一张 ----
    random.seed(42)
    gs = mirror_state(1001, 44)
    contest = {"contestId": "C_X", "contestType": "DOCK",
               "redPlayerId": 1001, "bluePlayerId": 2002}
    st = PlannerStrategy()
    picks = [st.pick_card(gs, contest) for _ in range(60)]
    distinct = set(picks)
    ok &= check("出牌: 混合策略出现多种牌", len(distinct) >= 2, f"{sorted(distinct)}")
    ok &= check("出牌: 弃权不占主导", picks.count(P.CARD_ABSTAIN) < 20,
                f"abstain={picks.count(P.CARD_ABSTAIN)}/60")
    ok &= check("出牌: 全部为合法牌",
                distinct <= {P.CARD_YAN_DIE, P.CARD_QIANG_XING, P.CARD_XIAN_GONG,
                             P.CARD_BING_ZHENG, P.CARD_ABSTAIN})

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

    # 6) 规则限制：上次强通到达节点不能重复强通
    st = PlannerStrategy()
    st._last_forced_node = "S10"
    a = st.decide(blocked_state(defense=6, bad=0, squad=0))
    ok &= check("突破: 重复强通被规避",
                not any(x["action"] == "FORCED_PASS" for x in a),
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

    # 3) 停在节点、对手正读条设卡下一跳 -> 等卡成型后攻坚，不上边挨冻
    a = PlannerStrategy().decide(edge_state(guard_def=0, opp_setting=True,
                                            on_edge=False))
    main_acts = [x for x in a if x["action"] in P.MAIN_ACTION_TYPES]
    ok &= check("防冻结: 对手设卡读条中不上边",
                len(main_acts) == 1 and main_acts[0]["action"] == "WAIT",
                json.dumps(a, ensure_ascii=False))

    # 4) 卡成型后（停在节点）恢复正常攻坚
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

    # 5) 帧价值修正：绕路成本恒含用时分斜率
    from lychee.planner import TaskPlanner, FRESH_VALUE_PER_FRAME, TIME_SCORE_PER_FRAME
    gs = moving_state({"SHORT_HORSE": 1})
    fv = TaskPlanner._frame_value(gs, eta_direct=200)
    ok &= check("帧价值: 恒含用时斜率",
                abs(fv - (FRESH_VALUE_PER_FRAME + TIME_SCORE_PER_FRAME)) < 1e-9,
                f"fv={fv:.3f}")
    return ok


def test_watchdog():
    """看门狗 + 人手账本兜底：字段缺失/看不到卡也不能冻死。"""
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

    # 2) 看不到敌卡的冻结：8 帧停滞后看门狗改道
    gs2 = GameState(1001)
    gs2.on_start(start)
    st2 = PlannerStrategy()
    moved = None
    for i in range(12):
        gs2.on_inquire(frozen_inquire(320 + i, guard_visible=False, squad_field=6))
        a = st2.decide(gs2)
        mv = [x for x in a if x["action"] == "MOVE"]
        # 排除自动补的续走 MOVE(目标 S10)——看门狗改道目标一定不是 S10
        mv = [x for x in mv if x["targetNodeId"] != "S10"]
        if mv:
            moved = (i, mv[0])
            break
    ok &= check("看门狗: 停滞后改道离开冻结边",
                moved is not None and moved[1]["targetNodeId"] != "S10",
                str(moved))

    # 3) 正常推进时看门狗不动作
    gs3 = GameState(1001)
    gs3.on_start(start)
    st3 = PlannerStrategy()
    fired = False
    for i in range(12):
        d = frozen_inquire(320 + i, guard_visible=False, squad_field=6)
        for p in d["players"]:
            if p["playerId"] == 1001:
                p["edgeProgressMs"] = 6000 + i * 1000   # 每帧在走
        gs3.on_inquire(d)
        a = st3.decide(gs3)
        if any(x["action"] == "MOVE" and x["targetNodeId"] != "S10" for x in a):
            fired = True
    ok &= check("看门狗: 正常推进不误触发", not fired)
    return ok


def test_active_guard():
    """V3 主动设卡：领先过咽喉时回手设卡挡对手。"""
    ok = True
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        start = json.load(f)["msg_data"]
    with open(os.path.join(DOC_DIR, "inquire消息.json"), encoding="utf-8") as f:
        inquire = json.load(f)["msg_data"]

    def gs_at(cur="S10", opp_pos="S07", round_no=200, phase="NORMAL",
              good=90, my_guards=(), node_guarded=False):
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
                         badFruit=0, taskScore=90)
            else:
                p.update(state="MOVING" if opp_pos else "IDLE",
                         currentNodeId=opp_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None,
                         delivered=False, retired=False)
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

    # 2) 对手已过（在 S11，路线不再经过 S10）-> 不设
    a = PlannerStrategy().main_action(gs_at(opp_pos="S11"))
    ok &= check("设卡: 对手已过不白设",
                not (a and a["action"] == "SET_GUARD"), str(a))

    # 3) 已有 2 张有效卡 -> 不设（防顶掉旧卡）
    a = PlannerStrategy().main_action(gs_at(my_guards=("S03", "S08")))
    ok &= check("设卡: 已有2卡不再设",
                not (a and a["action"] == "SET_GUARD"), str(a))

    # 4) RUSH 阶段 -> 专心交付不设卡
    a = PlannerStrategy().main_action(gs_at(round_no=460, phase="RUSH"))
    ok &= check("设卡: RUSH 不设卡",
                not (a and a["action"] == "SET_GUARD"), str(a))

    # 5) 该节点已有卡（谁的都算）-> 不设
    a = PlannerStrategy().main_action(gs_at(node_guarded=True))
    ok &= check("设卡: 节点已有卡不重复",
                not (a and a["action"] == "SET_GUARD"), str(a))

    # 6) 好果紧张 -> 不做对抗投资
    a = PlannerStrategy().main_action(gs_at(good=8))
    ok &= check("设卡: 好果紧张不投资",
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
                strip_process=True):
        gs = GameState(1001)
        gs.on_start(start)
        d = json.loads(json.dumps(inquire))
        d["round"] = round_no
        d["contests"], d["tasks"] = [], []
        d["weather"] = weather or {"active": [], "forecast": []}
        for p in d["players"]:
            if p["playerId"] == 1001:
                p.update(state="IDLE", currentNodeId=my_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None, buffs=[],
                         resources={}, freshness=95.0, goodFruit=95, badFruit=0)
            else:
                p.update(state="MOVING", currentNodeId=opp_pos, nextNodeId=None,
                         routeEdgeId=None, currentProcess=None,
                         delivered=False, retired=False)
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

    # 1) 对手正站在我们的下一跳 S11 → 不上边，等待
    st = PlannerStrategy()
    a = st.main_action(gs_tail())
    ok &= check("防陷阱: 对手占下一跳时等待",
                a and a["action"] == "WAIT", str(a))

    # 2) 对手在赶往 S11 且明显先到 → 同样等待
    a = PlannerStrategy().main_action(
        gs_tail(opp_cur="S12", opp_next="S11", opp_edge="E07"))
    ok &= check("防陷阱: 对手先到下一跳时等待",
                a and a["action"] == "WAIT", str(a))

    # 3) 对手已离开（在 S12 且驶向 S13）→ 正常上边
    a = PlannerStrategy().main_action(
        gs_tail(opp_cur="S12", opp_next="S13", opp_edge="E08"))
    ok &= check("防陷阱: 对手离开后正常推进",
                a and a["action"] == "MOVE" and a["targetNodeId"] == "S11", str(a))

    # 4) 它留了卡 → 站在节点上攻坚拆（好果2×2+坏果2×3=10 ≥ 6）
    a = PlannerStrategy().main_action(
        gs_tail(opp_cur="S12", opp_next="S13", opp_edge="E08", guard_def=6))
    ok &= check("防陷阱: 留卡则节点攻坚瞬拆",
                a and a["action"] == "BREAK_GUARD" and a["targetNodeId"] == "S11",
                str(a))

    # 5) 对峙上限：连续等待 30 帧后硬闯，不陪它耗
    st = PlannerStrategy()
    last = None
    for i in range(35):
        last = st.main_action(gs_tail(round_no=380 + i))
    ok &= check("防陷阱: 对峙超限后硬闯",
                last and last["action"] == "MOVE", str(last))

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
                         badFruit=1, taskScore=120)
            else:
                total = 55200
                p.update(state="MOVING", currentNodeId="S09", nextNodeId="S10",
                         routeEdgeId="E05", edgeTotalMs=total,
                         edgeProgressMs=int(total * opp_progress),
                         currentProcess=None, delivered=False, retired=False)
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

    # 5) 人手不足 → 强通兜底不变
    gs = base_gs("S09", "S12", round_no=520, squad=2, bad=0)
    for n in gs.nodes.values():
        if n["nodeId"] == "S10":
            n["guard"] = {"ownerTeamId": "BLUE", "defense": 6,
                          "maxDefense": 7, "active": True}
    a = PlannerStrategy().main_action(gs)
    ok &= check("突破: 人手不足仍强通兜底",
                a and a["action"] == "FORCED_PASS", str(a))
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
    print()
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
