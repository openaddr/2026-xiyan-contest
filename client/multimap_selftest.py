#!/usr/bin/env python3
"""多地图变体自测：验证策略不依赖固定地图拓扑。

与 selftest.py 的区别：selftest 全部基于同一张固定地图（start/inquire 消息）
构造【状态变体】；本脚手架程序化生成【地图拓扑变体】（改 edges/nodes/
nodeType/坐标/gate 入度），断言策略在每个变体上：
  1. 不崩溃、寻路连通（基础不变量）；
  2. 关键行为地图无关：RUSH+gate前驱+gate无卡→推进验核（demo 式干走回归）、
     gate 遇敌卡→破卡/强通（用 guard.maxDefense 读上限，不写死）。

设计纪律（任务书 2.2 地图可变项）：
  - 变体生成只改 map 数据，不动策略代码；
  - 断言用 state.gate_node / graph / nodeType / guard.maxDefense，禁止写死
    "S13"/"S14"/"防守4" 这类常量。
"""
import copy
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")   # Windows 控制台中文
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lychee import protocol as P
from lychee.state import GameState
from lychee.strategy import PlannerStrategy

DOC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))
    return bool(cond)


def xfail(name, cond, detail=""):
    """已知隐患回归：cond=True 表示隐患已修复（XPASS），False 表示仍存在（XFAIL，不计入失败）。"""
    mark = "XPASS" if cond else "XFAIL"
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))
    return True   # 不影响总体 PASS


# ================= 地图变体生成器（只改 map 数据）=================

def _load_base_start():
    with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
        return json.load(f)["msg_data"]


def _nodes(start):
    """nodes 列表的可写引用（顶层或 map 下，state.on_start 两者都读）。"""
    return start.get("map", {}).get("nodes") or start["nodes"]


def _edges(start):
    return start.get("map", {}).get("edges") or start["edges"]


def _sync_edges(start):
    """改完 map.edges 后同步到顶层（on_start 先读顶层）。"""
    if "map" in start and "edges" in start["map"]:
        start["edges"] = start["map"]["edges"]


def add_edge(start, eid, frm, to, rtype, dist):
    e = {"edgeId": eid, "fromNodeId": frm, "toNodeId": to,
         "routeType": rtype, "distance": dist, "bidirectional": True}
    start["map"]["edges"].append(e)
    _sync_edges(start)


def set_nodetype(start, node_id, ntype):
    for n in _nodes(start):
        if n.get("nodeId") == node_id:
            n["nodeType"] = ntype


def variant_multi_entry_gate(base):
    """给 gate 增加一条备用入边（多入度），测遇墙可绕而非死破。"""
    s = copy.deepcopy(base)
    s["_variant"] = "multi_entry_gate"
    add_edge(s, "E_V1", "S08", s["map"]["gameplay"]["roles"]["gateNodeId"],
             "BRANCH", 50)
    return s


def variant_moved_keypass(base):
    """把 KEY_PASS 标记从原关隘挪到另一个节点，测咽喉识别不写死站点。"""
    s = copy.deepcopy(base)
    s["_variant"] = "moved_keypass"
    # 原图 KEY_PASS 是 S10；取消它，把 S08（山路）标成新关隘
    orig_kp = next((n["nodeId"] for n in _nodes(base)
                    if n.get("nodeType") == "KEY_PASS"), "S10")
    set_nodetype(s, orig_kp, "STATION")
    set_nodetype(s, "S08", "KEY_PASS")
    return s


def variant_pruned_branches(base):
    """删掉所有 BRANCH 支路，只留主官道+水路+山路，测寻路仍连通、策略不崩。"""
    s = copy.deepcopy(base)
    s["_variant"] = "pruned_branches"
    s["map"]["edges"] = [e for e in _edges(s) if e.get("routeType") != "BRANCH"]
    _sync_edges(s)
    return s


def variant_rerouted(base):
    """把首段官道改成山路（routeType 换），测鲜度/耗时定价自适应。"""
    s = copy.deepcopy(base)
    s["_variant"] = "rerouted"
    for e in s["map"]["edges"]:
        if e.get("fromNodeId") == "S01" and e.get("toNodeId") == "S02":
            e["routeType"] = "MOUNTAIN"
    _sync_edges(s)
    return s


def variant_shuffled_coords(base):
    """随机扰动中间节点坐标（保持 S01/S14/S15 固定），测坐标无关。"""
    s = copy.deepcopy(base)
    s["_variant"] = "shuffled_coords"
    fixed = {"S01", "S14", "S15"}
    for n in _nodes(s):
        if n["nodeId"] not in fixed:
            n["x"] = (n.get("x", 0) + 7) % 80
            n["y"] = (n.get("y", 0) + 11) % 60
    return s


VARIANTS = [variant_multi_entry_gate, variant_moved_keypass,
            variant_pruned_branches, variant_rerouted,
            variant_shuffled_coords]


# ================= 状态构造（地图无关）=================

def make_state(start, my_node, phase="NORMAL", round_no=200, verified=False,
               me=None, opp=None, gate_guard=0):
    """从变体 start 派生一个最小合法 inquire，完全控制字段。

    gate_guard>0 时在 gate 上放敌方设卡（防守值=gate_guard，上限读 maxDefense
    默认 4，不写死）。
    """
    gs = GameState(1001)
    gs.on_start(start)
    gate = gs.gate_node
    nodes_rt = []
    for n in _nodes(start):
        nid = n["nodeId"]
        # nodeType 必须带：feat 的 on_inquire 不从 static 继承 nodeType 到运行时
        # nodes，而 _mid_edge_trap_risk 的 ordinary 判定依赖它（gate 要判为咽喉）。
        # 故意不带 processType：让 gate 前驱在端到端断言里不被处理站分支拦截，
        # 能走到赶路段/_mid_edge_trap_risk（真实对局会先 PROCESS 完再进陷阱判断）。
        rec = {"nodeId": nid, "hasObstacle": False, "guard": None,
               "resourceStock": {}, "scouted": [],
               "nodeType": n.get("nodeType") or n.get("type")}
        if gate_guard and nid == gate:
            rec["guard"] = {"ownerTeamId": "BLUE", "defense": gate_guard,
                            "maxDefense": 4, "active": True}
        nodes_rt.append(rec)
    me_p = {"playerId": 1001, "teamId": "RED", "state": "IDLE",
            "currentNodeId": my_node, "nextNodeId": None, "routeEdgeId": None,
            "currentProcess": None, "buffs": [], "resources": {},
            "freshness": 90.0, "goodFruit": 95, "badFruit": 2,
            "taskScore": 90, "totalScore": 200, "squadAvailable": 6,
            "guardActionPoint": 4, "verified": verified, "delivered": False,
            "retired": False, "rushTacticUsedCount": 0}
    if me:
        me_p.update(me)
    opp_p = {"playerId": 2002, "teamId": "BLUE", "state": "IDLE",
             "currentNodeId": "S01", "nextNodeId": None, "routeEdgeId": None,
             "currentProcess": None, "delivered": False, "retired": False,
             "goodFruit": 95, "badFruit": 0, "taskScore": 0, "totalScore": 0,
             "squadAvailable": 6, "guardActionPoint": 4}
    if opp:
        opp_p.update(opp)
    d = {"round": round_no, "phase": phase,
         "players": [me_p, opp_p], "nodes": nodes_rt,
         "edges": _edges(start), "tasks": [], "contests": [], "events": [],
         "bounties": [], "weather": {"active": [], "forecast": []}}
    gs.on_inquire(d)
    return gs


def _gate_pred(gs):
    """gate 的一个图邻居（动态找前驱，不写死）。"""
    nb = gs.graph.neighbors(gs.gate_node)
    return nb[0][0] if nb else None


# ================= 不变量断言（每个变体都跑）=================

def invariant_runs(name, start):
    """基础：寻路连通 + 策略不崩溃 + 动作合法。"""
    ok = True
    gs = GameState(1001)
    gs.on_start(start)
    f, p = gs.graph.shortest_path(gs.start_node, gs.gate_node)
    ok &= check(f"{name}: start->gate 连通", p and 0 < f < 600, f"{f} 帧")
    f2, p2 = gs.graph.shortest_path(gs.gate_node, gs.terminal_node)
    ok &= check(f"{name}: gate->terminal 连通", p2 and 0 < f2 < 200, f"{f2} 帧")

    gs = make_state(start, my_node=gs.start_node)
    try:
        a = PlannerStrategy().decide(gs)
    except Exception as e:
        ok &= check(f"{name}: decide 不崩溃", False, repr(e))
        return ok
    ok &= check(f"{name}: decide 返回列表", isinstance(a, list), str(a))
    valid = {x.get("action") for x in a} <= (P.MAIN_ACTION_TYPES | {
        "WINDOW_CARD", "SQUAD_SCOUT", "SQUAD_CLEAR", "SQUAD_REINFORCE",
        "SQUAD_WEAKEN", "BREAK_ORDER"})
    ok &= check(f"{name}: 动作均为合法类型", valid, str({x.get("action") for x in a}))
    return ok


def invariant_gate_rush_open(name, start):
    """RUSH + gate前驱 + gate无卡 + 对手远处 → 推进验核，不干走（demo 失误回归）。

    地图无关：前驱用 graph.neighbors 动态找；对手放远处（不在 gate、不逼近），
    所以 _mid_edge_trap_risk 不触发，策略应正常推进。
    """
    gs_tmp = GameState(1001)
    gs_tmp.on_start(start)
    pred = _gate_pred(gs_tmp)
    if not pred:
        return check(f"{name}: gate 有前驱(跳过)", True, "无前驱节点")
    gs = make_state(start, my_node=pred, phase="RUSH", round_no=450,
                    me={"taskScore": 120, "totalScore": 300})
    a = PlannerStrategy().decide(gs)
    mains = [x for x in a if x["action"] in P.MAIN_ACTION_TYPES]
    # 期望：推进——MOVE(gate) 或 若 cur 已被当作 gate（拓扑变体可能 pred==gate）
    # 直接 VERIFY_GATE。关键是不停在原地 WAIT 白耗。
    advancing = (any(x["action"] == "MOVE" and x["targetNodeId"] == gs.gate_node
                     for x in mains)
                 or any(x["action"] == "VERIFY_GATE" for x in mains)
                 or any(x["action"] == "USE_RESOURCE" for x in mains))  # 上马赶路也算
    stuck_wait = any(x["action"] == "WAIT" for x in mains) and not advancing
    return check(f"{name}: RUSH+gate前驱+无卡 推进验核", advancing,
                 f"mains={[x['action'] for x in mains]}")


def invariant_break_gate_guard(name, start):
    """gate 有防4敌卡 + 我在 gate 邻居 → 破卡/强通，不愣着（地图无关）。

    防守上限从 guard.maxDefense 读，攻坚值用 2 好果=4≥4 一击破。
    """
    gs_tmp = GameState(1001)
    gs_tmp.on_start(start)
    pred = _gate_pred(gs_tmp)
    if not pred:
        return check(f"{name}: gate 有前驱(跳过)", True)
    gs = make_state(start, my_node=pred, phase="RUSH", round_no=450,
                    gate_guard=4, me={"goodFruit": 95, "badFruit": 2,
                                      "taskScore": 120, "totalScore": 300})
    a = PlannerStrategy().decide(gs)
    acts = {x["action"] for x in a}
    broke = any(x["action"] == "BREAK_GUARD" and x["targetNodeId"] == gs.gate_node
                for x in a)
    # 破卡、强通、或先削弱都算"有对抗动作"；纯 WAIT 才是 demo 式失能
    countered = (broke or "FORCED_PASS" in acts or "SQUAD_WEAKEN" in acts)
    return check(f"{name}: gate遇敌卡有对抗动作", countered,
                 f"acts={sorted(acts)}")


def xfail_gate_camped_wait(name, start):
    """RUSH+gate前驱+对手焙gate → _mid_edge_trap_risk 应有界等待后放行。

    直接单测 _mid_edge_trap_risk：端到端会被 gate 前驱(宫前驿)的处理站分支先
    接管（真实对局里会先 PROCESS 完再进陷阱判断），所以这里隔离测陷阱层本身。
    地图无关：pred 用 graph.neighbors(gate) 动态找。
    """
    from lychee.planner import Plan
    gs_tmp = GameState(1001)
    gs_tmp.on_start(start)
    pred = _gate_pred(gs_tmp)
    if not pred:
        return True
    gs = make_state(start, my_node=pred, phase="RUSH", round_no=450,
                    gate_guard=0,
                    me={"taskScore": 120, "totalScore": 300, "goodFruit": 95,
                        "badFruit": 2, "squadAvailable": 6},
                    opp={"currentNodeId": gs_tmp.gate_node, "state": "IDLE",
                         "routeEdgeId": None, "goodFruit": 95, "badFruit": 0,
                         "squadAvailable": 6, "guardActionPoint": 4,
                         "taskScore": 0})
    plan = Plan("deliver", slack=100)
    st = PlannerStrategy()
    risks = [st._mid_edge_trap_risk(gs, pred, gs.gate_node, plan)
             for _ in range(30)]
    # 修复前：咽喉无界等待，30 帧全 True（XFAIL）。
    # 修复后：等满 TRAP_RUSH_GATE_WAIT(25) 后放行 → 第 26 帧起出现 False（XPASS）。
    capped = any(not r for r in risks[24:])
    n_true = sum(risks)
    return xfail(f"{name}: 对手焙gate 25帧后有界放行", capped,
                 f"{n_true} True / {30 - n_true} False")


# ================= main =================

def run_variant(variant_fn, base):
    start = variant_fn(base)
    name = start.get("_variant", variant_fn.__name__)
    print(f"\n=== 变体: {name} ===")
    ok = True
    ok &= invariant_runs(name, start)
    ok &= invariant_gate_rush_open(name, start)
    ok &= invariant_break_gate_guard(name, start)
    xfail_gate_camped_wait(name, start)   # 已知隐患，不计入 ok
    return ok


def main():
    base = _load_base_start()
    print("=" * 60)
    print("多地图变体自测（验证策略拓扑无关）")
    print("=" * 60)
    all_ok = True
    for vf in VARIANTS:
        all_ok &= run_variant(vf, base)
    print("\n" + "=" * 60)
    print("ALL PASS" if all_ok else "FAILURES")
    print("(XFAIL = 已知隐患未修，不计入失败；XPASS = 已修复)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
