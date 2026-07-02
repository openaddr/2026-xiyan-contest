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
                         taskScore=task_score, buffs=[])
        gs.on_inquire(d)
        return gs

    # ---- 场景1: 站在可领任务点（T_003@S07，15分，保护期归我）应领任务 ----
    gs = make_state()
    a = PlannerStrategy().decide(gs)
    kinds = {x["action"]: x for x in a}
    ok &= check("规划: 站在任务点发 CLAIM_TASK",
                kinds.get("CLAIM_TASK", {}).get("taskId") == "T_003",
                json.dumps(a, ensure_ascii=False))
    # 同帧应派小分队去探路（任务点已在脚下，探宫门 S14）
    ok &= check("规划: 同帧派小分队探宫门",
                kinds.get("SQUAD_SCOUT", {}).get("targetNodeId") == "S14",
                json.dumps(kinds.get("SQUAD_SCOUT"), ensure_ascii=False))

    # ---- 场景2: 截止临近（r560）应放弃任务直奔交付线 ----
    gs = make_state(round_no=560, node="S09")
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
    ok &= check("规划: 高基础分下有明确决策", plan.kind in ("task", "deliver"), repr(plan))

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
                     taskScore=0)
        for n in d["nodes"]:
            if n["nodeId"] == "S02":
                n.update(processType="TRANSFER", processRound=4)
        gs.on_inquire(d)
        return gs

    # ---- 错峰：同一帧，两边必然一个 PROCESS 一个 WAIT ----
    for rnd in (44, 45):
        acts = {}
        for pid in (1001, 2002):
            a = PlannerStrategy().main_action(mirror_state(pid, rnd))
            acts[pid] = a["action"]
        ok &= check(f"错峰: r{rnd} 双方不同帧启动处理",
                    sorted(acts.values()) == ["PROCESS", "WAIT"],
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
    gs = GameState(1001)
    gs.on_start(start)
    d = json.loads(json.dumps(inquire))
    d["contests"], d["tasks"] = [], []
    for p in d["players"]:
        if p["playerId"] == 1001:
            p.update(state="MOVING", currentNodeId="S01", nextNodeId="S02",
                     routeEdgeId="E01", currentProcess=None, buffs=[],
                     resources={})
    gs.on_inquire(d)
    a = PlannerStrategy().decide(gs)
    kinds = [x["action"] for x in a]
    has_squad = "SQUAD_SCOUT" in kinds
    has_main = any(k in P.MAIN_ACTION_TYPES for k in kinds)
    ok &= check("移动中: 小分队动作伴随显式 MOVE 保持推进",
                (not has_squad) or has_main, f"{kinds}")
    if has_main:
        mv = [x for x in a if x["action"] == "MOVE"]
        ok &= check("移动中: MOVE 目标为当前目标节点",
                    not mv or mv[0]["targetNodeId"] == "S02",
                    json.dumps(a, ensure_ascii=False))
    return ok


def main():
    ok = test_codec()
    ok &= test_state_and_strategy()
    ok &= test_planner()
    ok &= test_contention()
    print()
    print("ALL PASS" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
