#!/usr/bin/env python3
"""离线裁判（V3.18 基建）：进程内双策略对战，替代内网平台跑批。

用途：镜像自博弈、参数 A/B、敏感性扫描。规则按任务书实现，任务刷新
时刻表/寿命按回放语料（20/25/31）校准：波次帧固定，内容按种子生成。

保真度声明（与真实服务端的已知偏差，对 A/B 对称因此不影响比较结论）：
- 未实现：清障残留通行税(6帧)、COST_BANKRUPT 冻结好果清算、暴雨对
  登船/换运处理 +4 帧、悬赏的关键关隘 3 次计数线（30 帧线已实现）、
  资源二次打断保护的细节、任务保护期（用 30 帧简化）。
- 简化：强制通行全程按路线鲜度扣（真实规则税段按 0.05）；交付瞬时结算。
- 事件载荷刻意复刻平台缺陷：ACTION_REJECTED 不带 action 字段
 （replay20/36 实测，策略层的 reject-join 依赖这个行为）。

用法：
    python3 arena.py --seed 7                # 单局镜像，打印摘要
    python3 arena.py --seeds 1-20            # 批量镜像
    python3 arena.py --seed 7 --json         # 机器可读
库接口：
    run_match(seed, patches_a={...}, patches_b={...}) -> dict
    patches: {"planner.RACE_FRAME_MULT": 2.0, "strategy.GUARD_SLACK_HOT": 40}
"""
import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lychee import protocol as P
from lychee.state import GameState
from lychee.strategy import PlannerStrategy
from lychee.world import MapGraph

DOC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

PID_A, PID_B = 1001, 2002
TEAM = {PID_A: "RED", PID_B: "BLUE"}

TEMPLATE_SCORE = {"T01": 30, "T02": 30, "T04": 30, "T06": 30, "T08": 30,
                  "T11": 30, "T12": 15, "T13": 15, "T14": 15}
TEMPLATE_PROC = {"T01": 3, "T02": 4, "T04": 6, "T06": 3, "T08": 4,
                 "T11": 4, "T12": 5, "T13": 5, "T14": 5}
# 波次帧/候选池/寿命：回放 20/25/31 交叉校准。时刻表跨局固定；每波的
# (模板, 节点) 从三局观测值的并集里按种子取样——真实服务端的波次节点
# 高度结构化（第 1 波三局完全一致），桶内全图均匀采样会把早期任务刷到
# 到达即过期的下游，任务经济完全失真
TASK_WAVES = [
    (1, 220, [[("T01", "S03", "ROAD")], [("T08", "S04", "WATER")],
              [("T04", "S08", "MOUNTAIN"), ("T11", "S08", "MOUNTAIN")]]),
    (30, 180, [[("T02", "S07", "ROAD")]]),
    (60, 180, [[("T04", "S06", "MOUNTAIN"), ("T11", "S08", "MOUNTAIN")]]),
    (90, 180, [[("T06", "S09", "WATER"), ("T08", "S05", "WATER"),
                ("T08", "S04", "WATER")]]),
    (100, 220, [[("T02", "S07", "ROAD"), ("T13", "S09", "ROAD")],
                [("T02", "S10", "WATER"), ("T08", "S04", "WATER")],
                [("T11", "S08", "MOUNTAIN"), ("T04", "S08", "MOUNTAIN")]]),
    (120, 180, [[("T02", "S07", "ROAD")]]),
    (150, 180, [[("T02", "S10", "MOUNTAIN"), ("T08", "S05", "WATER"),
                 ("T04", "S08", "MOUNTAIN")]]),
    (180, 180, [[("T06", "S09", "ROAD")]]),
    (200, 220, [[("T11", "S11", "ROAD"), ("T06", "S09", "ROAD")],
                [("T06", "S04", "WATER"), ("T11", "S10", "WATER")],
                [("T11", "S10", "MOUNTAIN"), ("T11", "S11", "MOUNTAIN"),
                 ("T13", "S12", "MOUNTAIN")]]),
    (300, 220, [[("T01", "S03", "ROAD")], [("T08", "S04", "WATER")],
                [("T06", "S06", "MOUNTAIN"), ("T02", "S10", "MOUNTAIN")]]),
    (360, 180, [[("T02", "S07", "ROAD")], [("T08", "S05", "WATER")],
                [("T11", "S08", "MOUNTAIN"), ("T11", "S11", "MOUNTAIN")]]),
    (400, 200, [[("T02", "S07", "ROAD"), ("T06", "S09", "ROAD")],
                [("T06", "S09", "WATER"), ("T08", "S05", "WATER"),
                 ("T02", "S10", "WATER")],
                [("T11", "S08", "MOUNTAIN"), ("T11", "S10", "MOUNTAIN"),
                 ("T11", "S11", "MOUNTAIN")]]),
]
WEATHER_WINDOWS = [(80, 120), (200, 240), (320, 360), (440, 480)]
WEATHER_TYPES = ("HOT", "HEAVY_RAIN", "MOUNTAIN_FOG")
BUFF_LIFE = {P.FAST_HORSE: 20, P.SHORT_HORSE: 14, P.RUSH_SPEED: 15,
             "RUSH_PROTECT": 30}
BUFF_SPEED = {P.FAST_HORSE: 1200, P.SHORT_HORSE: 1150, P.RUSH_SPEED: 1300}


def milestone(base):
    return 50 if base >= 110 else 35 if base >= 90 else 15 if base >= 60 else 0


class Team:
    def __init__(self, pid, start_node):
        self.pid = pid
        self.node = start_node
        self.edge = None          # (edge dict, from, to, progress, total, paused)
        self.state = P.ST_IDLE
        self.good, self.bad, self.fresh = 100, 0, 100.0
        self.resources = {}
        self.squad = 8
        self.guard_pts = 4
        self.task_score = 0
        self.bounty_raw = 0
        self.verified = False
        self.delivered = False
        self.deliver_round = None
        self.deliver_good = 0
        self.deliver_fresh = 0.0
        self.buffs = {}           # type -> remain
        self.proc = None          # {kind, target, remain, meta}
        self.rest = 0
        self.forced = None        # {target, total, edge_frames, progress, route}
        self.rush_used = 0
        self.illegal = 0
        self.thresholds = set()   # 已触发的转坏阈值
        self.processed = set()    # 本次停靠已完成的固定处理节点（离站清空）
        self.last_forced = None
        self.station_done = False  # 当前停靠节点固定处理是否已完成

    # ---- 视图 ----
    def total_score_live(self):
        s = self.task_score + min(self.bounty_raw, 80)
        if self.delivered:
            s = final_score_parts(self)["total"]
        return s

    def player_dict(self, rnd):
        d = {
            "playerId": self.pid, "teamId": TEAM[self.pid],
            "state": self.state,
            "currentNodeId": None if self.edge else self.node,
            "nextNodeId": None, "routeEdgeId": None,
            "goodFruit": self.good, "badFruit": self.bad,
            "freshness": round(self.fresh, 2),
            "resources": dict(self.resources),
            "squadAvailable": self.squad,
            "guardActionPoint": self.guard_pts,
            "taskScore": self.task_score,
            "totalScore": self.total_score_live(),
            "verified": self.verified, "delivered": self.delivered,
            "retired": False,
            "buffs": [{"type": t, "remainRound": v} for t, v in self.buffs.items()],
            "rushTacticUsedCount": self.rush_used,
            "currentProcess": None,
        }
        if self.edge:
            e, frm, to, prog, total, paused = self.edge
            d.update(currentNodeId=frm, nextNodeId=to, routeEdgeId=e["edgeId"],
                     edgeProgressMs=int(prog), edgeTotalMs=int(total))
        if self.forced:
            d.update(state=P.ST_FORCED_PASSING,
                     currentProcess={"action": "FORCED_PASS", "type": "FORCED_PASS",
                                     "targetNodeId": self.forced["target"],
                                     "remainRound": self.forced["total"]
                                     - self.forced["progress"]})
        elif self.proc:
            d["currentProcess"] = {"action": self.proc["kind"],
                                   "type": self.proc["kind"],
                                   "targetNodeId": self.proc.get("target"),
                                   "taskId": (self.proc.get("meta") or {}).get("taskId"),
                                   "remainRound": self.proc["remain"]}
        return d


def final_score_parts(t):
    base = t.task_score
    if t.delivered:
        delivery = min(240, 120 + base * 4 // 3)
        good_s = math.floor(t.deliver_good / 100 * 180)
        fresh_s = math.floor(t.deliver_fresh / 100 * 180)
        raw_time = math.floor((600 - t.deliver_round) / 600 * 70)
        time_s = math.floor(raw_time * min(base, 90) / 90)
        task_s = min(180, base + milestone(base))
        bounty = (min(t.bounty_raw, 80) + 20) if t.bounty_raw > 0 else 0
    else:
        delivery = good_s = fresh_s = time_s = 0
        task_s = min(base, 80)
        bounty = min(t.bounty_raw, 25)
    penalty = min(20, max(0, t.illegal - 5))
    total = max(0, delivery + good_s + fresh_s + time_s + task_s + bounty - penalty)
    return {"delivery": delivery, "good": good_s, "fresh": fresh_s,
            "time": time_s, "task": task_s, "bounty": bounty,
            "penalty": penalty, "total": total}


class Arena:
    def __init__(self, seed, patches_a=None, patches_b=None, log=None,
                 strategy_cls=PlannerStrategy, cls_a=None, cls_b=None):
        import random as _random
        self.rng = _random.Random(f"arena:{seed}")
        self.seed = seed
        self.match_id = f"arena_{seed}"
        self.log = log
        with open(os.path.join(DOC_DIR, "start消息.json"), encoding="utf-8") as f:
            start = json.load(f)["msg_data"]
        self.start_msg = start
        gp = start["map"]["gameplay"]
        self.roles = gp["roles"]
        self.gate = self.roles["gateNodeId"]
        self.terminal = self.roles["terminalNodeIds"][0]
        self.nodes = {}            # 静态 + 运行时
        for n in start.get("nodes") or start["map"]["nodes"]:
            node = dict(n)
            node.setdefault("nodeType", node.get("type"))
            node["resourceStock"] = {}
            node["guard"] = None
            node["hasObstacle"] = node["nodeId"] in gp["obstacleCandidateNodeIds"]
            node["scout_marks"] = []       # [(team, expire)]
            self.nodes[node["nodeId"]] = node
        for pn in gp.get("processNodes") or []:
            n = self.nodes.get(pn["nodeId"])
            if n:
                n["processType"] = pn.get("processType")
                n["processRound"] = pn.get("processRound", 0)
        # 宫门验核站
        self.nodes[self.gate].setdefault("processType", "VERIFY")
        self.nodes[self.gate].setdefault("processRound", 6)
        for r in gp.get("resources") or start.get("resources") or []:
            self.nodes[r["nodeId"]]["resourceStock"][r["resourceType"]] = \
                self.nodes[r["nodeId"]]["resourceStock"].get(r["resourceType"], 0) \
                + r.get("count", 1)
        self.edges = start["edges"]
        self.graph = MapGraph(self.edges)
        self.candidates = gp["taskCandidates"]
        self.buckets = gp["routeTaskBuckets"]

        self.round = 0
        self.phase = P.PHASE_NORMAL
        self.tasks = []
        self.task_seq = 0
        self.bounties = []
        self.bounty_cd = {}        # node -> 冷却到期帧
        self.guard_meta = {}       # node -> {owner_pid, done_round, next_weather,
                                   #          active_from, fail_attacks, bounty_done}
        self.guard_order = []      # (node, done_round) 每队上限 2 用
        self.contests = []         # 活跃窗口
        self.contest_seq = 0
        self.draw_count = {}       # objkey -> 连续平局数
        self.cooldown = {}         # objkey -> 到期帧
        self.claim_interrupted = set()   # (node, rtype) 已用掉一次打断
        self.squads_inflight = []  # {pid, kind, target, land}
        self.events_next = []      # 下一帧下发的事件
        self.weather_plan = self._gen_weather()
        self.weather_active = None  # {type, start, end}
        self.residual = {}         # 未实现，占位

        self.teams = {PID_A: Team(PID_A, self.roles["startNodeId"]),
                      PID_B: Team(PID_B, self.roles["startNodeId"])}
        self.strategies = {}
        self.states = {}
        for pid, patches, cls in ((PID_A, patches_a, cls_a),
                                  (PID_B, patches_b, cls_b)):
            st = (cls or strategy_cls)()
            for key, val in (patches or {}).items():
                scope, attr = key.split(".", 1)
                obj = st.planner if scope == "planner" else st
                assert hasattr(obj, attr) or scope == "planner", key
                setattr(obj, attr, val)
            gs = GameState(pid)
            gs.on_start(self._start_for())
            self.strategies[pid] = st
            self.states[pid] = gs
        self.metrics = {pid: {"guards_set": 0, "breaks": 0, "forced": 0,
                              "frozen_frames": 0, "weakens": 0, "tasks_done": 0,
                              "contest_wins": 0}
                        for pid in self.teams}

    # ================= 初始化 =================

    def _start_for(self):
        d = json.loads(json.dumps(self.start_msg))
        d["matchId"] = self.match_id
        return d

    def _gen_weather(self):
        plan = []
        for lo, hi in WEATHER_WINDOWS:
            plan.append({"type": self.rng.choice(WEATHER_TYPES),
                         "start": self.rng.randint(lo, hi), "dur": 60})
        return plan

    # ================= 任务波次 =================

    def _spawn_wave(self, wave):
        rnd, life, pools = wave
        active = [t for t in self.tasks
                  if t["active"] and not t["completed"] and not t["failed"]]
        for pool in pools:
            if len(active) >= 10:
                break
            cands = [(tpl, nd, bk) for tpl, nd, bk in pool
                     if tpl != "T04" or self.nodes[nd]["hasObstacle"]]
            if not cands:
                continue
            tpl, nd, bucket = self.rng.choice(cands)
            self.task_seq += 1
            t = {"taskId": f"T_{self.task_seq:03d}", "taskTemplateId": tpl,
                 "name": tpl, "nodeId": nd, "routeBucket": bucket,
                 "processType": "CLAIM_TASK",
                 "processRound": TEMPLATE_PROC[tpl],
                 "score": TEMPLATE_SCORE[tpl],
                 "refreshRound": rnd, "expireRound": rnd + life,
                 "active": True, "completed": False, "failed": False,
                 "ownerPlayerId": 0, "protectionPlayerId": 0}
            self.tasks.append(t)
            active.append(t)

    # ================= 公开状态 =================

    def _weather_dict(self):
        active, forecast = [], []
        for w in self.weather_plan:
            if w["start"] <= self.round < w["start"] + w["dur"]:
                active.append({"type": w["type"], "startRound": w["start"],
                               "remainRound": w["start"] + w["dur"] - self.round})
            elif 0 <= w["start"] - self.round <= 30:
                forecast.append({"type": w["type"], "startRound": w["start"],
                                 "durationRound": w["dur"]})
        return {"active": active, "forecast": forecast}

    def _node_dicts(self):
        out = []
        for nid, n in self.nodes.items():
            g = None
            meta = self.guard_meta.get(nid)
            if n["guard"]:
                g = dict(n["guard"])
                g["active"] = (g.get("defense", 0) > 0
                               and meta and self.round >= meta["active_from"])
            out.append({"nodeId": nid, "nodeType": n.get("nodeType"),
                        "processType": n.get("processType"),
                        "processRound": n.get("processRound", 0),
                        "hasObstacle": n["hasObstacle"],
                        "resourceStock": dict(n["resourceStock"]),
                        "guard": g,
                        "x": n.get("x"), "y": n.get("y"),
                        "scouted": [{"teamId": tm, "remainingTriggers": 1}
                                    for tm, exp in n["scout_marks"]
                                    if exp >= self.round]})
        return out

    def _contest_dicts(self):
        out = []
        for c in self.contests:
            out.append({"contestId": c["id"], "contestType": c["type"],
                        "redPlayerId": PID_A, "bluePlayerId": PID_B
                        if c["parties"] == {PID_A, PID_B} else None,
                        "resolved": c.get("resolved", False),
                        "status": "OPEN" if not c.get("resolved") else "RESOLVED",
                        "taskId": (c.get("obj") or {}).get("taskId"),
                        "redPoint": c["points"].get(PID_A, 0),
                        "bluePoint": c["points"].get(PID_B, 0)})
        return out

    def _inquire(self):
        return {"matchId": self.match_id, "round": self.round,
                "phase": self.phase,
                "players": [self.teams[p].player_dict(self.round)
                            for p in (PID_A, PID_B)],
                "nodes": self._node_dicts(), "edges": self.edges,
                "weather": self._weather_dict(),
                "tasks": [dict(t) for t in self.tasks],
                "bounties": [dict(b) for b in self.bounties],
                "contests": self._contest_dicts(),
                "events": list(self.events_next),
                "actionResults": [], "scorePreview": {}}

    # ================= 事件与拒绝 =================

    def _emit(self, etype, **payload):
        self.events_next.append({"type": etype, "payload": payload})

    def _reject(self, pid, code):
        # 复刻平台缺陷：载荷不带 action 字段
        self._emit("ACTION_REJECTED", playerId=pid, errorCode=code)

    # ================= 天气/速度/鲜度 =================

    def _weather_mult(self, route_type):
        w = self._weather_dict()["active"]
        for x in w:
            if x["type"] == "HEAVY_RAIN" and route_type == P.WATER:
                return 1350
            if x["type"] == "MOUNTAIN_FOG" and route_type == P.MOUNTAIN:
                return 1100
        return 1000

    def _speed(self, t):
        s = P.BASE_SPEED
        for b, remain in t.buffs.items():
            s = max(s, BUFF_SPEED.get(b, 0))
        return s

    def _fresh_decay(self, t):
        if t.delivered:
            return
        base = P.IDLE_FRESH_DECAY
        route = None
        if t.edge and t.state == P.ST_MOVING:
            route = t.edge[0].get("routeType")
        elif t.forced:
            f = t.forced
            route = f["route"] if f["progress"] <= f["edge_frames"] else None
        if route:
            base = P.ROUTE_FRESH_DECAY.get(route, base)
        mult = 1.0
        for w in self._weather_dict()["active"]:
            if w["type"] == "HOT":
                mult *= 1.5
            elif w["type"] == "HEAVY_RAIN" and route == P.WATER:
                mult *= 1.3
        if P.RUSH_SPEED in t.buffs:
            mult *= 1.25
        if "RUSH_PROTECT" in t.buffs:
            mult *= 0.2
        t.fresh = max(0.0, t.fresh - base * mult)
        # 转坏阈值（首次低于）
        for th in P.FRESH_THRESHOLDS:
            if t.fresh < th and th not in t.thresholds:
                t.thresholds.add(th)
                if t.good > 0:
                    t.good -= 1
                    t.bad += 1
        if t.fresh <= 0 and t.good > 0 and not t.delivered:
            t.good = 0  # 鲜度归零报废

    # ================= 设卡与阻挡 =================

    def _enemy_guard(self, node_id, pid):
        n = self.nodes[node_id]
        g, meta = n["guard"], self.guard_meta.get(node_id)
        if g and meta and g["defense"] > 0 and self.round >= meta["active_from"] \
                and g["ownerTeamId"] != TEAM[pid]:
            return g
        return None

    def _guard_cap(self, node_id):
        n = self.nodes[node_id]
        if n.get("nodeType") == "KEY_PASS":
            return 7
        if node_id == self.gate:
            return 4
        if n["hasObstacle"]:
            return 5
        return 6

    def _remove_guard(self, node_id):
        self.nodes[node_id]["guard"] = None
        self.guard_meta.pop(node_id, None)
        self.guard_order = [(nd, r) for nd, r in self.guard_order if nd != node_id]
        for b in self.bounties:
            if b["nodeId"] == node_id and b["active"] and not b["completed"]:
                b["active"] = False
                self.bounty_cd[node_id] = self.round + 120

    # ================= 窗口 =================

    def _objkey(self, ctype, obj):
        if ctype == P.CONTEST_TASK:
            return ("task", obj["taskId"])
        if ctype == P.CONTEST_RESOURCE:
            return ("res", obj["nodeId"], obj["resourceType"])
        if ctype == P.CONTEST_DOCK:
            return ("dock", obj["nodeId"])
        if ctype == P.CONTEST_GATE:
            return ("gate",)
        if ctype == P.CONTEST_OBSTACLE:
            return ("obstacle", obj["nodeId"])
        return ("pass", obj["nodeId"])

    def _open_contest(self, ctype, obj, pending):
        """pending: pid -> 该方胜出后要执行的动作描述（None=守方被动）。"""
        self.contest_seq += 1
        c = {"id": f"C_{self.contest_seq:03d}", "type": ctype, "obj": obj,
             "created": self.round, "points": {PID_A: 0, PID_B: 0},
             "parties": set(pending), "pending": pending,
             "cards": {}, "resolved": False}
        self.contests.append(c)
        for pid, act in pending.items():
            t = self.teams[pid]
            if act is not None or ctype != P.CONTEST_PASS:
                t.state = P.ST_CONTESTING
        return c

    def _card_affordable(self, pid, card):
        t = self.teams[pid]
        if card == P.CARD_ABSTAIN:
            return True
        if card == P.CARD_YAN_DIE:
            return t.resources.get(P.PASS_TOKEN, 0) + \
                t.resources.get(P.OFFICIAL_PERMIT, 0) > 0
        if card == P.CARD_QIANG_XING:
            has_buff = any(b in t.buffs for b in
                           (P.FAST_HORSE, P.SHORT_HORSE, P.RUSH_SPEED))
            return has_buff or t.resources.get(P.FAST_HORSE, 0) > 0 \
                or t.resources.get(P.SHORT_HORSE, 0) > 0
        if card == P.CARD_XIAN_GONG:
            return t.fresh >= 80 and t.good >= 1
        if card == P.CARD_BING_ZHENG:
            return t.guard_pts >= 1
        return False

    def _pay_card(self, pid, card):
        t = self.teams[pid]
        if card == P.CARD_YAN_DIE:
            for r in (P.PASS_TOKEN, P.OFFICIAL_PERMIT):
                if t.resources.get(r, 0) > 0:
                    t.resources[r] -= 1
                    return
        elif card == P.CARD_QIANG_XING:
            if not any(b in t.buffs for b in
                       (P.FAST_HORSE, P.SHORT_HORSE, P.RUSH_SPEED)):
                for r in (P.FAST_HORSE, P.SHORT_HORSE):
                    if t.resources.get(r, 0) > 0:
                        t.resources[r] -= 1
                        return
        elif card == P.CARD_XIAN_GONG:
            t.good -= 1
        elif card == P.CARD_BING_ZHENG:
            t.guard_pts -= 1

    @staticmethod
    def _beat(a, b):
        if a == b:
            return 0
        if b == P.CARD_ABSTAIN:
            return 1
        if a == P.CARD_ABSTAIN:
            return -1
        if b in P.CARD_BEATS.get(a, ()):
            return 1
        if a in P.CARD_BEATS.get(b, ()):
            return -1
        return 0

    def _contest_beats(self, window_cards):
        """window_cards: pid -> (contestId, card) 本帧提交。"""
        for c in self.contests:
            if c["resolved"]:
                continue
            beat_no = self.round - c["created"]
            if beat_no < 1 or beat_no > 3:
                continue
            played = {}
            for pid in (PID_A, PID_B):
                card = P.CARD_ABSTAIN
                if pid in c["parties"] or c["type"] == P.CONTEST_PASS:
                    sub = window_cards.get(pid)
                    if sub and sub[0] == c["id"]:
                        card = sub[1]
                # PASS 守方忙碌 → 强制弃权
                if c["type"] == P.CONTEST_PASS and c["pending"].get(pid) is None:
                    t = self.teams[pid]
                    if t.proc or t.forced or t.rest > 0 or t.delivered:
                        card = P.CARD_ABSTAIN
                if card != P.CARD_ABSTAIN and not self._card_affordable(pid, card):
                    card = P.CARD_ABSTAIN
                played[pid] = card
            for pid, card in played.items():
                if card != P.CARD_ABSTAIN:
                    self._pay_card(pid, card)
                self._emit("WINDOW_CARD_REVEAL", playerId=pid, card=card,
                           contestId=c["id"])
            r = self._beat(played[PID_A], played[PID_B])
            if r > 0:
                c["points"][PID_A] += 1
            elif r < 0:
                c["points"][PID_B] += 1
            if beat_no == 3:
                self._resolve_contest(c)
        self.contests = [c for c in self.contests
                         if not c["resolved"] or self.round - c["created"] <= 4]

    def _resolve_contest(self, c):
        c["resolved"] = True
        pa, pb = c["points"][PID_A], c["points"][PID_B]
        key = self._objkey(c["type"], c["obj"])
        winner = PID_A if pa > pb else PID_B if pb > pa else None
        for pid in c["parties"]:
            t = self.teams[pid]
            if t.state == P.ST_CONTESTING:
                t.state = P.ST_IDLE
        if winner is None:
            self.draw_count[key] = self.draw_count.get(key, 0) + 1
            if self.draw_count[key] >= 2:
                cd = 6 if c["type"] == P.CONTEST_GATE else 18
                self.cooldown[key] = self.round + cd
                self.draw_count[key] = 0
            if c["type"] == P.CONTEST_PASS:
                atk = next(p for p, a in c["pending"].items() if a is not None)
                self.teams[atk].rest = 3
                self.teams[atk].state = P.ST_RESTING
            else:
                for pid in c["parties"]:
                    self.teams[pid].rest = 3
                    self.teams[pid].state = P.ST_RESTING
            self._emit("WINDOW_CONTEST_DRAW", contestId=c["id"])
            return
        self.draw_count.pop(key, None)
        self.metrics[winner]["contest_wins"] += 1
        self._emit("WINDOW_CONTEST_END", contestId=c["id"], winnerPlayerId=winner)
        act = c["pending"].get(winner)
        if c["type"] == P.CONTEST_PASS:
            atk = next(p for p, a in c["pending"].items() if a is not None)
            if winner == atk:
                self._start_forced(atk, c["obj"]["nodeId"], c["obj"]["tax"])
            else:
                tax = c["obj"]["tax"]
                t = self.teams[atk]
                t.rest = min(8, max(3, math.ceil(tax * 0.25)))
                t.state = P.ST_RESTING
            return
        if act:
            self._start_pending(winner, act)

    def _start_pending(self, pid, act):
        """窗口胜方从头开始其动作的读条。"""
        kind = act["kind"]
        t = self.teams[pid]
        if kind == "CLAIM_RESOURCE":
            t.proc = {"kind": "CLAIM_RESOURCE", "target": act["node"],
                      "remain": self._proc_frames(pid, act["node"], 2),
                      "meta": {"rtype": act["rtype"]}}
        elif kind == "CLAIM_TASK":
            task = act["task"]
            t.proc = {"kind": "CLAIM_TASK", "target": task["nodeId"],
                      "remain": self._proc_frames(pid, t.node,
                                                  task["processRound"]),
                      "meta": {"taskId": task["taskId"]}}
            task["ownerPlayerId"] = pid
            task["protectionPlayerId"] = pid
        elif kind == "PROCESS":
            n = self.nodes[act["node"]]
            t.proc = {"kind": "PROCESS", "target": act["node"],
                      "remain": self._proc_frames(pid, act["node"],
                                                  n.get("processRound", 0)),
                      "meta": {}}
        elif kind == "VERIFY_GATE":
            frames = self._proc_frames(pid, self.gate, 6)
            if act.get("break_order"):
                frames = max(3, frames - 3)
            t.proc = {"kind": "VERIFY_GATE", "target": self.gate,
                      "remain": frames, "meta": {}}
        elif kind == "CLEAR":
            t.proc = {"kind": "CLEAR", "target": act["node"],
                      "remain": self._proc_frames(pid, act["node"], 6), "meta": {}}
        t.state = P.ST_PROCESSING

    def _proc_frames(self, pid, node_id, base):
        """探路标记减时（-3，最低 2），消耗标记。"""
        n = self.nodes[node_id]
        for i, (tm, exp) in enumerate(n["scout_marks"]):
            if tm == TEAM[pid] and exp >= self.round and base >= 3:
                del n["scout_marks"][i]
                return max(2, base - 3)
        return base

    # ================= 强制通行 =================

    def _forced_tax(self, node_id):
        n = self.nodes[node_id]
        g = n["guard"]
        tax_g = 0
        if g and g["defense"] > 0:
            d = g["defense"]
            if n.get("nodeType") == "KEY_PASS":
                tax_g = min(50, 15 + d * 5)
            elif node_id == self.gate:
                tax_g = min(32, 12 + d * 5)
            elif n["hasObstacle"]:
                tax_g = min(28, 8 + d * 5)
            else:
                tax_g = min(40, 10 + d * 5)
        tax_o = 8 if n["hasObstacle"] else 0
        return max(tax_g, tax_o)

    def _start_forced(self, pid, target, tax):
        t = self.teams[pid]
        edge = self.graph.edge_between(t.node, target)
        ef = self.graph.edge_frames(edge, self._speed(t))
        t.forced = {"target": target, "edge_frames": ef, "total": ef + tax,
                    "progress": 0, "route": edge.get("routeType")}
        t.state = P.ST_FORCED_PASSING
        self.metrics[pid]["forced"] += 1
        self._emit("FORCED_PASS_START", playerId=pid, nodeId=target)

    # ================= 主动作解析 =================

    def _collect(self):
        """向两侧策略要动作；返回 pid -> {main, squad, card}。"""
        out = {}
        inq = self._inquire()
        blob = json.dumps(inq)
        for pid in (PID_A, PID_B):
            gs = self.states[pid]
            gs.on_inquire(json.loads(blob))
            try:
                acts = self.strategies[pid].decide(gs) or []
            except Exception as e:      # 策略崩溃按空动作，别拖死整局
                if self.log:
                    self.log(f"strategy {pid} crashed r{self.round}: {e!r}")
                acts = []
            slot = {"main": None, "squad": None, "card": None}
            for a in acts:
                k = a.get("action")
                if k == "WINDOW_CARD":
                    slot["card"] = (a.get("contestId"), a.get("card"))
                elif k and k.startswith("SQUAD_"):
                    slot["squad"] = a
                elif k in P.MAIN_ACTION_TYPES:
                    if slot["main"] is None:
                        slot["main"] = a
                    # 移动中补发的保持 MOVE：视为同一动作，不算超额
                    elif a.get("action") == "MOVE" or \
                            slot["main"].get("action") == "MOVE":
                        if slot["main"].get("action") != "MOVE":
                            pass
                    else:
                        slot["main"] = None
                        self.teams[pid].illegal += 1
                        break
            out[pid] = slot
        return out

    def _apply_mains(self, mains):
        """同帧冲突检测 → 窗口；否则各自执行。"""
        conflict_types = {}
        acts = {}
        for pid, a in mains.items():
            t = self.teams[pid]
            if a is None or t.delivered or t.rest > 0 or t.proc or t.forced \
                    or t.state == P.ST_CONTESTING:
                continue
            acts[pid] = a
        # 同帧同对象 → 窗口
        if len(acts) == 2:
            a1, a2 = acts[PID_A], acts[PID_B]
            k1, k2 = a1.get("action"), a2.get("action")
            obj = None
            if k1 == k2 == "CLAIM_TASK" and a1.get("taskId") == a2.get("taskId"):
                task = self._task(a1.get("taskId"))
                if task:
                    obj = (P.CONTEST_TASK, {"taskId": task["taskId"]},
                           {PID_A: {"kind": "CLAIM_TASK", "task": task},
                            PID_B: {"kind": "CLAIM_TASK", "task": task}})
            elif k1 == k2 == "CLAIM_RESOURCE" \
                    and a1.get("targetNodeId") == a2.get("targetNodeId") \
                    and a1.get("resourceType") == a2.get("resourceType"):
                obj = (P.CONTEST_RESOURCE,
                       {"nodeId": a1["targetNodeId"],
                        "resourceType": a1["resourceType"]},
                       {pid: {"kind": "CLAIM_RESOURCE",
                              "node": a1["targetNodeId"],
                              "rtype": a1["resourceType"]}
                        for pid in (PID_A, PID_B)})
            elif k1 == k2 == "PROCESS" \
                    and self.teams[PID_A].node == self.teams[PID_B].node:
                obj = (P.CONTEST_DOCK, {"nodeId": self.teams[PID_A].node},
                       {pid: {"kind": "PROCESS", "node": self.teams[pid].node}
                        for pid in (PID_A, PID_B)})
            elif k1 == k2 == "VERIFY_GATE":
                obj = (P.CONTEST_GATE, {"nodeId": self.gate},
                       {pid: {"kind": "VERIFY_GATE",
                              "break_order": bool(acts[pid].get("rushTactic"))}
                        for pid in (PID_A, PID_B)})
            if obj:
                ctype, o, pending = obj
                key = self._objkey(ctype, o)
                if self.cooldown.get(key, 0) > self.round:
                    for pid in acts:
                        self._reject(pid, "CONTEST_COOLDOWN")
                    return
                self._open_contest(ctype, o, pending)
                return
        for pid in (PID_A, PID_B):
            if pid in acts:
                self._apply_main(pid, acts[pid])

    def _task(self, tid):
        for t in self.tasks:
            if t["taskId"] == tid:
                return t
        return None

    def _apply_main(self, pid, a):
        t = self.teams[pid]
        kind = a.get("action")
        opp = self.teams[PID_B if pid == PID_A else PID_A]

        if kind == "WAIT":
            if t.edge:
                e = list(t.edge)
                e[5] = True
                t.edge = tuple(e)
                t.state = P.ST_WAITING
            return

        if kind == "MOVE":
            target = a.get("targetNodeId")
            if t.edge:
                e, frm, to, prog, total, paused = t.edge
                if target == to:      # 继续/恢复前进
                    t.edge = (e, frm, to, prog, total, False)
                    t.state = P.ST_MOVING
                elif target != frm and self.graph.edge_between(frm, target):
                    ne = self.graph.edge_between(frm, target)
                    t.edge = (ne, frm, target, 0,
                              self.graph.edge_total_move(ne), False)
                    t.state = P.ST_MOVING
                else:
                    t.illegal += 1
                return
            edge = self.graph.edge_between(t.node, target)
            if not edge:
                t.illegal += 1
                self._reject(pid, "MOVE_ILLEGAL")
                return
            n = self.nodes[t.node]
            needs = (n.get("processType") and n.get("processType") != "VERIFY"
                     and n.get("processRound", 0) > 0 and not t.station_done)
            if needs:
                self._reject(pid, P.E_PROCESS_REQUIRED)
                return
            if self.nodes[target]["hasObstacle"]:
                self._reject(pid, "MOVE_BLOCKED_BY_OBSTACLE")
                return
            if self._enemy_guard(target, pid):
                self._reject(pid, P.E_MOVE_BLOCKED_BY_GUARD)
                return
            t.edge = (edge, t.node, target, 0,
                      self.graph.edge_total_move(edge), False)
            t.state = P.ST_MOVING
            return

        if t.edge:
            # 边上只允许马类/疾行令
            if kind == "USE_RESOURCE" and a.get("resourceType") in \
                    (P.FAST_HORSE, P.SHORT_HORSE):
                self._use_resource(pid, a)
            elif kind == "RUSH_SPEED":
                self._rush_tactic(pid, "RUSH_SPEED")
            else:
                self._reject(pid, "NOT_ALLOWED_ON_EDGE")
            return

        if kind == "PROCESS":
            n = self.nodes[t.node]
            if not n.get("processType") or n.get("processType") == "VERIFY" \
                    or n.get("processRound", 0) <= 0:
                self._reject(pid, P.E_PROCESS_NOT_AVAILABLE)
                return
            if t.station_done:
                self._reject(pid, P.E_PROCESS_NOT_AVAILABLE)
                return
            if opp.proc and opp.proc["kind"] == "PROCESS" \
                    and opp.proc["target"] == t.node:
                self._reject(pid, P.E_OBJECT_BUSY)
                return
            key = ("dock", t.node)
            if self.cooldown.get(key, 0) > self.round:
                self._reject(pid, "CONTEST_COOLDOWN")
                return
            self._start_pending(pid, {"kind": "PROCESS", "node": t.node})
            return

        if kind == "CLAIM_RESOURCE":
            node_id, rtype = a.get("targetNodeId"), a.get("resourceType")
            if node_id != t.node or \
                    self.nodes[node_id]["resourceStock"].get(rtype, 0) <= 0:
                self._reject(pid, "RESOURCE_NOT_AVAILABLE")
                return
            key = ("res", node_id, rtype)
            if self.cooldown.get(key, 0) > self.round:
                self._reject(pid, "CONTEST_COOLDOWN")
                return
            # 后手打断：对方正读条领同一资源且首次 → 窗口
            if opp.proc and opp.proc["kind"] == "CLAIM_RESOURCE" \
                    and opp.proc["target"] == node_id \
                    and (opp.proc["meta"] or {}).get("rtype") == rtype:
                if (node_id, rtype) in self.claim_interrupted:
                    self._reject(pid, P.E_OBJECT_BUSY)
                    return
                self.claim_interrupted.add((node_id, rtype))
                opp.proc = None
                opp.state = P.ST_CONTESTING
                self._open_contest(
                    P.CONTEST_RESOURCE,
                    {"nodeId": node_id, "resourceType": rtype},
                    {p: {"kind": "CLAIM_RESOURCE", "node": node_id,
                         "rtype": rtype} for p in (PID_A, PID_B)})
                return
            self._start_pending(pid, {"kind": "CLAIM_RESOURCE",
                                      "node": node_id, "rtype": rtype})
            return

        if kind == "USE_RESOURCE":
            self._use_resource(pid, a)
            return

        if kind == "CLAIM_TASK":
            task = self._task(a.get("taskId"))
            if not task or not task["active"] or task["completed"] \
                    or task["failed"] or self.round > task["expireRound"]:
                self._reject(pid, "TASK_NOT_FOUND")
                return
            if task["protectionPlayerId"] not in (0, pid) \
                    or task["ownerPlayerId"] not in (0, pid):
                self._reject(pid, "TASK_PROTECTED")
                return
            tpl, nd = task["taskTemplateId"], task["nodeId"]
            if tpl == "T04":
                if not self.nodes[nd]["hasObstacle"]:
                    self._reject(pid, "TASK_REQUIREMENT_NOT_MET")
                    return
                if t.node != nd and not self.graph.edge_between(t.node, nd):
                    self._reject(pid, "TASK_REQUIREMENT_NOT_MET")
                    return
            elif t.node != nd:
                self._reject(pid, "TASK_REQUIREMENT_NOT_MET")
                return
            if tpl == "T06":
                horse = None
                for h in (P.FAST_HORSE, P.SHORT_HORSE):
                    if t.resources.get(h, 0) > 0:
                        horse = h
                        break
                if not horse:
                    self._reject(pid, "RESOURCE_NOT_ENOUGH")
                    return
                t.resources[horse] -= 1
            key = ("task", task["taskId"])
            if self.cooldown.get(key, 0) > self.round:
                self._reject(pid, "CONTEST_COOLDOWN")
                return
            self._start_pending(pid, {"kind": "CLAIM_TASK", "task": task})
            return

        if kind == "CLEAR":
            nd = a.get("targetNodeId")
            if not self.nodes[nd]["hasObstacle"] or t.good < 1 \
                    or (t.node != nd and not self.graph.edge_between(t.node, nd)):
                self._reject(pid, "CLEAR_NOT_AVAILABLE")
                return
            self._start_pending(pid, {"kind": "CLEAR", "node": nd})
            return

        if kind == "SET_GUARD":
            nd = a.get("targetNodeId")
            extra = a.get("extraGoodFruit", 0) or 0
            n = self.nodes[nd]
            if nd != t.node or nd == self.terminal or not (0 <= extra <= 2):
                self._reject(pid, "GUARD_NOT_ALLOWED")
                return
            meta = self.guard_meta.get(nd)
            if n["guard"] and n["guard"]["defense"] > 0:
                self._reject(pid, "GUARD_EXISTS")
                return
            base_cost = 1 if (n.get("nodeType") == "KEY_PASS"
                              or nd == self.gate) else 0
            if t.good < base_cost + extra:
                self._reject(pid, "RESOURCE_NOT_ENOUGH")
                return
            t.good -= base_cost + extra
            t.proc = {"kind": "SET_GUARD", "target": nd, "remain": 4,
                      "meta": {"extra": extra}}
            t.state = P.ST_PROCESSING
            return

        if kind == "BREAK_GUARD":
            self._break_guard(pid, a)
            return

        if kind == "FORCED_PASS":
            nd = a.get("targetNodeId")
            n = self.nodes[nd]
            guard = self._enemy_guard(nd, pid)
            if not self.graph.edge_between(t.node, nd) \
                    or (not guard and not n["hasObstacle"]):
                self._reject(pid, "FORCED_PASS_NOT_AVAILABLE")
                return
            if t.node == t.last_forced:
                self._reject(pid, "FORCED_PASS_REPEAT")
                return
            tax = self._forced_tax(nd)
            if guard:
                owner = PID_A if guard["ownerTeamId"] == TEAM[PID_A] else PID_B
                self._open_contest(P.CONTEST_PASS,
                                   {"nodeId": nd, "tax": tax},
                                   {pid: {"kind": "FORCED_PASS"}, owner: None})
            else:
                self._start_forced(pid, nd, tax)
            return

        if kind == "VERIFY_GATE":
            if self.phase != P.PHASE_RUSH or t.node != self.gate or t.verified:
                self._reject(pid, "VERIFY_NOT_AVAILABLE")
                return
            bo = bool(a.get("rushTactic") == P.BREAK_ORDER)
            if bo:
                bo = self._pay_break_order(pid)
            self._start_pending(pid, {"kind": "VERIFY_GATE", "break_order": bo})
            return

        if kind == "DELIVER":
            if t.node == self.terminal and t.verified and t.good > 0 \
                    and t.fresh > 0 and not t.delivered:
                t.delivered = True
                t.deliver_round = self.round
                t.deliver_good = t.good
                t.deliver_fresh = t.fresh
                t.state = P.ST_DELIVERED
                self._emit("DELIVER_SUCCESS", playerId=pid, round=self.round)
            return

        if kind == "RUSH_SPEED":
            self._rush_tactic(pid, "RUSH_SPEED")
            return
        if kind == "RUSH_PROTECT":
            self._rush_tactic(pid, "RUSH_PROTECT")
            return

    def _pay_break_order(self, pid):
        t = self.teams[pid]
        if self.phase != P.PHASE_RUSH or t.rush_used >= 1:
            return False
        if t.bad >= 2:
            t.bad -= 2
        elif t.good >= 1:
            t.good -= 1
        else:
            return False
        t.rush_used += 1
        return True

    def _rush_tactic(self, pid, which):
        t = self.teams[pid]
        if self.phase != P.PHASE_RUSH or t.rush_used >= 1:
            self._reject(pid, "RUSH_TACTIC_NOT_AVAILABLE")
            return
        if which == "RUSH_SPEED":
            if any(b in t.buffs for b in (P.FAST_HORSE, P.SHORT_HORSE)):
                self._reject(pid, "BUFF_CONFLICT")
                return
            if t.good < 2:
                self._reject(pid, "RESOURCE_NOT_ENOUGH")
                return
            t.good -= 2
            t.buffs[P.RUSH_SPEED] = BUFF_LIFE[P.RUSH_SPEED]
        else:
            if t.edge or t.proc or t.forced or t.rest > 0:
                self._reject(pid, "RUSH_TACTIC_NOT_AVAILABLE")
                return
            t.buffs["RUSH_PROTECT"] = BUFF_LIFE["RUSH_PROTECT"]
        t.rush_used += 1
        self._emit("RUSH_TACTIC_USE", playerId=pid, tactic=which)

    def _use_resource(self, pid, a):
        t = self.teams[pid]
        rtype = a.get("resourceType")
        if t.resources.get(rtype, 0) <= 0:
            self._reject(pid, "RESOURCE_NOT_ENOUGH")
            return
        if rtype == P.ICE_BOX:
            if t.fresh <= 0:
                self._reject(pid, "RESOURCE_NOT_AVAILABLE")
                return
            t.resources[rtype] -= 1
            t.fresh = min(100.0, t.fresh + 10)
        elif rtype in (P.FAST_HORSE, P.SHORT_HORSE):
            if P.RUSH_SPEED in t.buffs:
                self._reject(pid, "BUFF_CONFLICT")
                return
            t.resources[rtype] -= 1
            t.buffs.pop(P.FAST_HORSE, None)
            t.buffs.pop(P.SHORT_HORSE, None)
            t.buffs[rtype] = BUFF_LIFE[rtype]
        elif rtype == P.INTEL:
            target = a.get("targetNodeId")
            if t.edge or not target:
                self._reject(pid, "RESOURCE_NOT_AVAILABLE")
                return
            if self.graph.shortest_distance(t.node, target) > 15:
                self._reject(pid, "INTEL_TOO_FAR")
                return
            t.resources[rtype] -= 1
            self.nodes[target]["scout_marks"].append((TEAM[pid], self.round + 45))
        elif rtype in (P.PASS_TOKEN, P.OFFICIAL_PERMIT):
            t.resources[rtype] -= 1   # 主动使用白扣（规则 3.3.3）
        else:
            self._reject(pid, "RESOURCE_NOT_AVAILABLE")

    def _break_guard(self, pid, a):
        t = self.teams[pid]
        nd = a.get("targetNodeId")
        gf, bf = a.get("goodFruit", 0) or 0, a.get("badFruit", 0) or 0
        guard = self._enemy_guard(nd, pid)
        if not guard or nd == t.node or not self.graph.edge_between(t.node, nd) \
                or not (0 <= gf <= 2 and 0 <= bf <= 2) \
                or t.good < gf or t.bad < bf:
            self._reject(pid, "BREAK_NOT_AVAILABLE")
            return
        t.good -= gf
        t.bad -= bf
        attack = gf * 2 + bf * 3
        if a.get("rushTactic") == P.BREAK_ORDER and self._pay_break_order(pid):
            attack += 3
        meta = self.guard_meta[nd]
        self.metrics[pid]["breaks"] += 1
        if attack >= guard["defense"]:
            # 悬赏结算：攻破方总分更低才计分
            for b in self.bounties:
                if b["nodeId"] == nd and b["active"] and not b["completed"]:
                    opp = self.teams[PID_B if pid == PID_A else PID_A]
                    if t.total_score_live() < opp.total_score_live():
                        t.bounty_raw += b["rewardScore"]
                        b["completed"] = True
                        self._emit("BOUNTY_CLAIM", playerId=pid,
                                   nodeId=nd, score=b["rewardScore"])
            self._remove_guard(nd)
            self._emit("GUARD_BREAK", playerId=pid, nodeId=nd)
        else:
            guard["defense"] -= attack
            meta["fail_attacks"] += 1
            t.rest = 5
            t.state = P.ST_RESTING
            if meta["fail_attacks"] >= 2:
                self._maybe_bounty(nd)

    # ================= 小分队 =================

    def _apply_squad(self, pid, a):
        if a is None:
            return
        t = self.teams[pid]
        if self.phase == P.PHASE_RUSH or t.delivered:
            self._reject(pid, "SQUAD_NOT_ALLOWED")
            return
        kind = a["action"]
        cost = 1 if kind == "SQUAD_SCOUT" else 2
        if t.squad < cost:
            self._reject(pid, "SQUAD_NOT_ENOUGH")
            return
        target = a.get("targetNodeId")
        if target not in self.nodes:
            t.illegal += 1
            return
        t.squad -= cost
        src = self.nodes[t.edge[1] if t.edge else t.node]
        dst = self.nodes[target]
        D = max(abs((src.get("x") or 0) - (dst.get("x") or 0)),
                abs((src.get("y") or 0) - (dst.get("y") or 0)))
        delay = min(15, max(3, math.ceil(D / 3)))
        if kind == "SQUAD_SCOUT":
            for w in self._weather_dict()["active"]:
                if w["type"] == "MOUNTAIN_FOG":
                    delay = min(15, delay + 2)
        self.squads_inflight.append({"pid": pid, "kind": kind,
                                     "target": target,
                                     "land": self.round + delay})

    def _land_squads(self):
        order = {"SQUAD_REINFORCE": 0, "SQUAD_WEAKEN": 1,
                 "SQUAD_CLEAR": 2, "SQUAD_SCOUT": 3}
        due = [s for s in self.squads_inflight if s["land"] <= self.round]
        self.squads_inflight = [s for s in self.squads_inflight
                                if s["land"] > self.round]
        for s in sorted(due, key=lambda x: order[x["kind"]]):
            pid, nd = s["pid"], s["target"]
            n = self.nodes[nd]
            g = n["guard"]
            if s["kind"] == "SQUAD_SCOUT":
                n["scout_marks"].append((TEAM[pid], self.round + 45))
            elif s["kind"] == "SQUAD_CLEAR":
                if n["hasObstacle"]:
                    n["hasObstacle"] = False
                    self._emit("OBSTACLE_CLEAR", playerId=pid, nodeId=nd)
                    self._fail_dead_t04(nd)
            elif s["kind"] == "SQUAD_WEAKEN":
                if g and g["defense"] > 0 and g["ownerTeamId"] != TEAM[pid]:
                    g["defense"] = max(0, g["defense"] - 2)
                    self.metrics[pid]["weakens"] += 1
                    if g["defense"] <= 0:
                        self._remove_guard(nd)
            elif s["kind"] == "SQUAD_REINFORCE":
                if g and g["defense"] > 0 and g["ownerTeamId"] == TEAM[pid]:
                    g["defense"] = min(self._guard_cap(nd), g["defense"] + 2)

    def _fail_dead_t04(self, node_id):
        for task in self.tasks:
            if task["taskTemplateId"] == "T04" and task["nodeId"] == node_id \
                    and task["active"] and not task["completed"]:
                task["failed"] = True
                task["active"] = False

    # ================= 读条完成 =================

    def _advance_procs(self):
        for pid in (PID_A, PID_B):
            t = self.teams[pid]
            if not t.proc:
                continue
            t.proc["remain"] -= 1
            if t.proc["remain"] > 0:
                continue
            proc, t.proc = t.proc, None
            t.state = P.ST_IDLE
            kind, meta = proc["kind"], proc.get("meta") or {}
            if kind == "PROCESS":
                t.station_done = True
                self._emit("PROCESS_COMPLETE", playerId=pid,
                           nodeId=proc["target"])
            elif kind == "CLAIM_RESOURCE":
                n = self.nodes[proc["target"]]
                r = meta["rtype"]
                if n["resourceStock"].get(r, 0) > 0:
                    n["resourceStock"][r] -= 1
                    t.resources[r] = t.resources.get(r, 0) + 1
            elif kind == "CLAIM_TASK":
                task = self._task(meta["taskId"])
                if task and task["active"] and not task["completed"]:
                    task["completed"] = True
                    task["active"] = False
                    task["ownerPlayerId"] = pid
                    t.task_score += task["score"]
                    self.metrics[pid]["tasks_done"] += 1
                    if task["taskTemplateId"] == "T04":
                        self.nodes[task["nodeId"]]["hasObstacle"] = False
                        self._fail_dead_t04(task["nodeId"])
                    self._emit("TASK_COMPLETE", playerId=pid,
                               taskId=task["taskId"], score=task["score"])
            elif kind == "SET_GUARD":
                nd = proc["target"]
                n = self.nodes[nd]
                if not (n["guard"] and n["guard"]["defense"] > 0):
                    defense = min(self._guard_cap(nd), 2 + meta["extra"] * 2)
                    n["guard"] = {"ownerTeamId": TEAM[pid], "defense": defense,
                                  "maxDefense": self._guard_cap(nd)}
                    first = 45 if (n.get("nodeType") == "KEY_PASS"
                                   and defense >= 4) else 30
                    self.guard_meta[nd] = {
                        "owner_pid": pid, "done_round": self.round,
                        "active_from": self.round + 1,
                        "next_weather": self.round + first,
                        "fail_attacks": 0, "bounty_at": self.round + 30}
                    self.guard_order.append((nd, self.round))
                    self.metrics[pid]["guards_set"] += 1
                    self._emit("GUARD_SET", playerId=pid, nodeId=nd)
                    mine = [x for x in self.guard_order
                            if self.guard_meta.get(x[0], {}).get("owner_pid") == pid]
                    if len(mine) > 2:
                        self._remove_guard(mine[0][0])
            elif kind == "VERIFY_GATE":
                t.verified = True
                self._emit("VERIFY_GATE_COMPLETE", playerId=pid)
            elif kind == "CLEAR":
                nd = proc["target"]
                if self.nodes[nd]["hasObstacle"] and t.good >= 1:
                    t.good -= 1
                    self.nodes[nd]["hasObstacle"] = False
                    self._emit("OBSTACLE_CLEAR", playerId=pid, nodeId=nd)
                    self._fail_dead_t04(nd)

    # ================= 移动结算 =================

    def _advance_moves(self):
        for pid in (PID_A, PID_B):
            t = self.teams[pid]
            if t.forced:
                f = t.forced
                f["progress"] += 1
                if f["progress"] >= f["total"]:
                    t.node = f["target"]
                    t.last_forced = f["target"]
                    t.forced = None
                    t.state = P.ST_IDLE
                    t.station_done = False
                    self._emit("FORCED_PASS_END", playerId=pid,
                               nodeId=t.node)
                continue
            if not t.edge or t.state not in (P.ST_MOVING,):
                continue
            e, frm, to, prog, total, paused = t.edge
            if paused:
                continue
            if self._enemy_guard(to, pid):
                self.metrics[pid]["frozen_frames"] += 1
                continue   # 中边冻结
            speed = self._speed(t)
            mult = self._weather_mult(e.get("routeType"))
            prog += math.floor(speed * 1000 / mult)
            if prog >= total:
                t.node = to
                t.edge = None
                t.state = P.ST_IDLE
                t.station_done = False
            else:
                t.edge = (e, frm, to, prog, total, paused)

    # ================= 世界推进 =================

    def _advance_world(self):
        # 设卡风化 + 悬赏
        for nd in list(self.guard_meta):
            meta = self.guard_meta[nd]
            g = self.nodes[nd]["guard"]
            if not g:
                continue
            if self.round >= meta["next_weather"]:
                g["defense"] -= 1
                meta["next_weather"] = self.round + 30
                self._emit("GUARD_WEATHERING", nodeId=nd,
                           defense=g["defense"])
                if g["defense"] <= 0:
                    self._remove_guard(nd)
                    continue
            if self.round >= meta.get("bounty_at", 1 << 30) \
                    and g["defense"] > 0:
                meta["bounty_at"] = 1 << 30
                self._maybe_bounty(nd)
        # 任务过期
        for task in self.tasks:
            if task["active"] and self.round > task["expireRound"]:
                task["active"] = False
        # buffs
        for t in self.teams.values():
            for b in list(t.buffs):
                t.buffs[b] -= 1
                if t.buffs[b] <= 0:
                    del t.buffs[b]
            if t.rest > 0:
                t.rest -= 1
                if t.rest == 0 and t.state == P.ST_RESTING:
                    t.state = P.ST_IDLE
        # 过期探路标记
        for n in self.nodes.values():
            n["scout_marks"] = [(tm, exp) for tm, exp in n["scout_marks"]
                                if exp >= self.round]

    def _maybe_bounty(self, nd):
        if self.bounty_cd.get(nd, 0) > self.round:
            return
        for b in self.bounties:
            if b["nodeId"] == nd and b["active"] and not b["completed"]:
                return
        is_key = self.nodes[nd].get("nodeType") == "KEY_PASS"
        self.bounties.append({
            "bountyId": f"B_{nd}_{self.round}",
            "bountyType": "KEY_BOUNTY" if is_key else "NORMAL_BOUNTY",
            "nodeId": nd, "rewardScore": 18 if is_key else 10,
            "active": True, "completed": False, "winnerPlayerId": 0})
        self._emit("BOUNTY_CREATE", nodeId=nd)

    def _check_rush(self):
        if self.phase == P.PHASE_RUSH:
            return
        r = self.round
        if r >= 450:
            self.phase = P.PHASE_RUSH
            self._emit("RUSH_START")
            return
        if r < 390:
            return
        excluded = {"S11", "S12", "S13"}
        for t in self.teams.values():
            node = t.edge[1] if t.edge else t.node
            if node == self.gate:
                self.phase = P.PHASE_RUSH
                self._emit("RUSH_START")
                return
            if node not in excluded:
                if self.graph.shortest_distance(node, self.gate) <= 15:
                    self.phase = P.PHASE_RUSH
                    self._emit("RUSH_START")
                    return
                f, path = self.graph.shortest_path(node, self.terminal)
                if path and f <= 60:
                    self.phase = P.PHASE_RUSH
                    self._emit("RUSH_START")
                    return

    # ================= 主循环 =================

    def run(self, max_round=600):
        for _ in range(max_round):
            self.round += 1
            # 任务波次（先于本帧决策可见）
            for wave in TASK_WAVES:
                if wave[0] == self.round:
                    self._spawn_wave(wave)
            self._check_rush()
            actions = self._collect()
            self.events_next = []
            self._contest_beats({pid: actions[pid]["card"]
                                 for pid in actions})
            self._apply_mains({pid: actions[pid]["main"] for pid in actions})
            for pid in (PID_A, PID_B):
                self._apply_squad(pid, actions[pid]["squad"])
            self._advance_procs()
            self._advance_moves()
            self._land_squads()
            self._advance_world()
            for t in self.teams.values():
                self._fresh_decay(t)
            if all(t.delivered for t in self.teams.values()):
                break
        return self.result()

    def result(self):
        out = {"seed": self.seed, "rounds": self.round}
        for pid in (PID_A, PID_B):
            t = self.teams[pid]
            parts = final_score_parts(t)
            out[pid] = {"score": parts["total"], "parts": parts,
                        "delivered": t.delivered,
                        "deliverRound": t.deliver_round,
                        "taskBase": t.task_score,
                        "good": t.deliver_good if t.delivered else t.good,
                        "fresh": round(t.deliver_fresh if t.delivered
                                       else t.fresh, 1),
                        "metrics": self.metrics[pid]}
        sa, sb = out[PID_A]["score"], out[PID_B]["score"]
        out["winner"] = PID_A if sa > sb else PID_B if sb > sa else 0
        out["margin"] = sa - sb
        return out


def run_match(seed, patches_a=None, patches_b=None, max_round=600,
              cls_a=None, cls_b=None):
    return Arena(seed, patches_a, patches_b,
                 cls_a=cls_a, cls_b=cls_b).run(max_round)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int)
    ap.add_argument("--seeds", type=str, help="如 1-20")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--opp", choices=("camper", "rusher"),
                    help="B 座使用脚本陪练")
    args = ap.parse_args()
    seeds = [args.seed or 1]
    if args.seeds:
        lo, hi = args.seeds.split("-")
        seeds = list(range(int(lo), int(hi) + 1))
    cls_b = None
    if args.opp:
        from sparring import BOTS
        cls_b = BOTS[args.opp]
    rows = []
    for s in seeds:
        r = run_match(s, cls_b=cls_b)
        rows.append(r)
        a, b = r[PID_A], r[PID_B]
        if not args.json:
            print(f"seed={s:>3} rounds={r['rounds']:>3} "
                  f"A {a['score']:>3} (dlv={a['deliverRound']}, task={a['taskBase']}, "
                  f"g={a['good']}, f={a['fresh']}) | "
                  f"B {b['score']:>3} (dlv={b['deliverRound']}, task={b['taskBase']}, "
                  f"g={b['good']}, f={b['fresh']}) margin={r['margin']:+}")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        wins = sum(1 for r in rows if r["winner"] == PID_A)
        losses = sum(1 for r in rows if r["winner"] == PID_B)
        print(f"\nA wins {wins} / draws {len(rows)-wins-losses} / losses {losses}"
              f" | avg margin {sum(r['margin'] for r in rows)/len(rows):+.1f}")


if __name__ == "__main__":
    main()
