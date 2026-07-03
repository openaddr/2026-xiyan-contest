#!/usr/bin/env python3
"""replay_report.py — 把 replay.txt 清洗成人类可读文本战报（全黑名单、RLE 压缩、零丢失）。

【黑名单原则】只对「已知噪音/已知可压缩」做处理，其余原样保留；碰到没见过的字段/事件
一律保留不丢，事后发现新噪音再补进黑名单。绝不白名单丢弃任何消息。

  - NOISE_EVENT_TYPES：明确要丢的事件类型（与玩家/节点状态冗余，可推回）
  - CONTINUOUS_FIELDS：玩家每帧都在变、且可由「帧推进」理解的字段，合并为区间（rX-Y: a->b）
  - USELESS_PAYLOAD_KEYS：事件 payload 里纯元数据 key（eventId/orderId 等），删 key 不删事件
  - 其它一切（玩家任意字段、节点任意字段、contests/bounties/tasks/weather/phase）原样保留；
    连续相同的帧用 RLE 合并；一旦变化就记一条「rX <对象> 变更：<完整新状态>」。

输入：replay.txt（行分隔 JSON：第 1 行=start；其后每行=一帧）
输出：默认写「源文件名.report.txt」；--stdout 打到屏幕。

用法：
  python3 replay_report.py "replay (37).txt"           # 写 replay (37).report.txt
  python3 replay_report.py "replay (37).txt" --stdout   # 打到 stdout
"""
import json
import os
import re
import sys

# ============ 黑名单（只有这里列出的才动，其它一律保留） ============

# 事件类型：明确丢弃（与玩家/节点状态字段冗余，删掉零信息丢失）
NOISE_EVENT_TYPES = {
    "MOVE_PROGRESS",        # = 玩家 edgeProgressMs 字段
    "FRESHNESS_DROP",       # = 玩家 freshness 字段
    "PROCESS_PROGRESS",     # = 玩家 currentProcess.remainRound
    "WINDOW_CARD_REVEAL",   # 每拍都触发，胜负由 WINDOW_CONTEST_RESOLVE 体现
    "TICK", "SCORE_UPDATE",
}

# 事件 payload 里纯元数据 key（删 key 不删事件本身）
USELESS_PAYLOAD_KEYS = {"eventId", "orderId"}

# 玩家「连续变化」字段：合并成区间 (start->end)，不逐帧记
CONTINUOUS_FIELDS = {
    "edgeProgressMs", "edgeProgressPermille", "moveProgress",
    "moveProgressRound", "freshness",
}

# 计时/进度字段（每帧递减或递增）：变更检测时剥掉（不触发"变更行"），但输出时保留原值。
# 这些都不是语义事件（设卡/破卡/任务完成 才是），逐帧记只会制造噪音。
TIMER_KEYS = {
    "edgeProgressMs", "edgeProgressPermille", "moveProgress", "moveProgressRound", "freshness",
    "ageRound",             # 设卡风化年龄每帧增
    "remainRound", "remainingRound",  # 探路标记/天气/处理 剩余帧每帧减
    "expireRound",          # 任务过期帧每帧减
    "deadlineRound",        # 窗口截止帧每帧减
    "suppressUntilRound", "cooldownUntilRound",
    "progress",             # 处理进度
    "tick",
    # 路线类型帧计数器（每帧在该路线类型上递增）
    "roadRounds", "waterRounds", "mountainRounds", "branchRounds",
    # 高频翻转字段（随双方移动每帧变，非语义事件；任务 REFRESH/COMPLETE/CLAIM 已在 Events 段）
    "protectionPlayerId",
}


def strip_timers(obj):
    """递归剥掉计时字段（仅用于变更签名比较；输出仍用原对象）。"""
    if isinstance(obj, dict):
        return {k: strip_timers(v) for k, v in obj.items() if k not in TIMER_KEYS}
    if isinstance(obj, list):
        return [strip_timers(x) for x in obj]
    return obj

SQUAD_COST = {"SQUAD_SCOUT": 1, "SQUAD_CLEAR": 2, "SQUAD_REINFORCE": 2, "SQUAD_WEAKEN": 2}


# ============ 工具 ============

def parse(path):
    with open(path, encoding="utf-8") as f:
        objs = []
        for line in f:
            if not line.strip():
                continue
            # 服务端回放里 sourceActionTypes 等字段用了裸数字 key（如 {2744:"CLEAR"}，非标准 JSON），补回双引号
            line = re.sub(r'([{,]\s*)(-?\d+)(\s*:)', r'\1"\2"\3', line)
            objs.append(json.loads(line))
        return objs


# ============ 固定枚举中文翻译表（仅协议里固定的码，key 保留英文方便检索） ============
# 全局（无歧义码）；有歧义的（PASS 等）放 ZH_BY_KEY 按字段翻
ZH = {
    # phase
    "NORMAL": "普通", "RUSH": "宫宴冲刺", "ENDED": "已结束",
    # 玩家状态
    "IDLE": "空闲", "MOVING": "移动中", "WAITING": "主动等待", "PROCESSING": "处理中",
    "CONTESTING": "窗口争夺中", "RESTING": "休整中", "FORCED_PASSING": "强制通行中",
    "VERIFYING": "验核中", "COST_BANKRUPT": "冻结清算", "DELIVERED": "已交付", "RETIRED": "已退赛",
    # 路线类型 / routeBucket
    "ROAD": "官道", "WATER": "水路", "MOUNTAIN": "山路", "BRANCH": "支路",
    # 资源
    "ICE_BOX": "冰鉴", "FAST_HORSE": "快马", "SHORT_HORSE": "短程马",
    "BOAT_RIGHT": "船权", "PASS_TOKEN": "过所", "OFFICIAL_PERMIT": "官凭", "INTEL": "情报",
    # 天气
    "HOT": "酷暑", "HEAVY_RAIN": "暴雨", "MOUNTAIN_FOG": "山雾",
    # 障碍
    "ROCKFALL": "落石", "FLOOD": "水涨", "MUD": "泥泞", "DOCK_BLOCK": "码头阻塞",
    "PASS_CROWD": "关口拥堵", "BROKEN_BRIDGE": "断桥", "LANDSLIDE": "山体滑坡",
    # 任务模板（任务书 5.2 固定）
    "T01": "限时过关", "T02": "抵驿催运", "T04": "清障任务", "T06": "争马换乘",
    "T08": "码头争船", "T11": "栈道复核", "T12": "官道关验", "T13": "水陆联运", "T14": "山口急递",
    # 主车队动作
    "WAIT": "等待", "MOVE": "移动", "PROCESS": "处理", "DOCK": "登船",
    "CLAIM_RESOURCE": "领取资源", "USE_RESOURCE": "使用资源", "CLAIM_TASK": "领取任务",
    "CLEAR": "清障", "SET_GUARD": "设卡", "BREAK_GUARD": "攻坚破卡",
    "FORCED_PASS": "强制通行", "VERIFY_GATE": "宫门验核", "DELIVER": "交付", "WINDOW_CARD": "窗口出牌",
    # 小分队动作
    "SQUAD_SCOUT": "小队探路", "SQUAD_CLEAR": "小队清障", "SQUAD_REINFORCE": "小队增援",
    "SQUAD_WEAKEN": "小队削弱",
    # 终局急策
    "RUSH_SPEED": "疾行令", "RUSH_PROTECT": "护果令", "BREAK_ORDER": "破关令",
    # 窗口牌
    "YAN_DIE": "验牒", "QIANG_XING": "强行", "XIAN_GONG": "献贡", "BING_ZHENG": "兵争", "ABSTAIN": "弃权",
    # 队伍
    "RED": "红方", "BLUE": "蓝方",
    # 移动方向
    "FORWARD": "前进", "PAUSED": "暂停", "BACKWARD": "后退", "NONE": "无",
    # 处理类型（processType）
    "TRANSFER": "前段交接", "BOARD": "登船", "WATER_TRANSFER": "水路换运",
    "PASS_TRANSFER": "入关交接", "PALACE_TRANSFER": "宫前交接", "VERIFY": "宫门验核",
    "STATION_PROCESS": "驿站处理", "PASS_NODE": "过关", "CLEAR_OBSTACLE": "清障处理",
    # 结果/错误码
    "SUCCESS": "成功",
    # 阻挡类型 (blockType) / 通用
    "GUARD": "设卡", "OBSTACLE": "障碍", "RESOURCE": "资源", "TASK": "任务",
    # 悬赏类型
    "NORMAL_BOUNTY": "普通悬赏", "KEY_BOUNTY": "关键关隘悬赏", "GUARD_BREAK": "破关悬赏",
    # 事件类型（NOISE 的不会出现；其余翻译）
    "SQUAD_DISPATCH": "小队派遣", "NODE_ENTER": "进站", "TASK_REFRESH": "任务刷新",
    "TASK_COMPLETE": "任务完成", "TASK_EXPIRE": "任务过期", "RESOURCE_CLAIM": "资源领取",
    "RESOURCE_USE": "资源使用", "PROCESS_COMPLETE": "处理完成", "VERIFY_GATE_COMPLETE": "验核完成",
    "DELIVER_SUCCESS": "交付成功", "FORCED_PASS_END": "强制通行结束", "GUARD_SET": "设卡完成",
    "GUARD_WEATHERING": "设卡风化", "OBSTACLE_CLEAR": "障碍清除", "GOOD_TO_BAD": "好果转坏",
    "RUSH_START": "宫宴冲刺开始", "BOUNTY_CREATE": "悬赏生成", "BOUNTY_EXPIRE": "悬赏过期",
    "BOUNTY_AWARD": "悬赏发放", "BREAK_ORDER_BIND": "破关令绑定",
    "SCOUT_MARKER_ADD": "探路标记添加", "SCOUT_MARKER_APPLY": "探路标记消耗",
    "SCOUT_MARKER_CONSUME": "探路标记消耗", "SCOUT_MARKER_EXPIRE": "探路标记过期",
    "WINDOW_CONTEST_START": "窗口开始", "WINDOW_CONTEST_END": "窗口结束",
    "WINDOW_CONTEST_RESOLVE": "窗口结算", "DOCK_CONTEST_WIN": "码头窗口胜出",
    "PASS_CONTEST_WIN": "通行窗口胜出", "INVALID_ACTION": "非法动作", "ACTION_REJECTED": "动作被拒",
}

# 按字段翻译（处理同码不同义）
ZH_BY_KEY = {
    # contestType 里 PASS=通行窗口、DOCK=码头窗口、GATE=宫门窗口...
    "contestType": {"RESOURCE": "资源窗口", "TASK": "任务窗口", "GATE": "宫门窗口",
                    "DOCK": "码头窗口", "PASS": "通行窗口", "OBSTACLE": "障碍窗口"},
    # nodeType
    "nodeType": {"START": "起点", "CHECKPOINT": "驿站", "STATION": "驿站", "PASS": "关隘",
                 "KEY_PASS": "关键关隘", "MOUNTAIN_PASS": "山口", "MOUNTAIN_NODE": "山区节点",
                 "WATER_STATION": "水驿", "DOCK": "码头", "JUNCTION": "交汇",
                 "PALACE_STATION": "宫前驿", "GATE": "宫门", "FINISH": "终点"},
}


def zh(k, v):
    """翻译固定枚举值（key k 决定歧义字段的语义）；不在表里则原样保留。"""
    if not isinstance(v, str):
        return v
    byk = ZH_BY_KEY.get(k)
    if byk and v in byk:
        return byk[v]
    return ZH.get(v, v)


# ============ 字段名(key)中文翻译表（黑名单：能翻的翻，没见过的原样保留） ============
ZH_KEY = {
    # 玩家
    "playerId": "玩家ID", "camp": "阵营", "teamId": "队伍", "playerName": "名称",
    "online": "在线", "state": "状态", "currentNodeId": "当前节点", "nextNodeId": "下一节点",
    "routeEdgeId": "所在边", "routeType": "路线类型", "moveDirection": "移动方向",
    "moveProgress": "移动进度", "moveProgressRound": "移动进度帧", "currentEdgeCost": "当前边成本",
    "edgeProgressPermille": "边进度千分比", "edgeProgressMs": "边进度ms", "edgeTotalMs": "边总ms",
    "freshness": "鲜度", "goodFruit": "好果", "badFruit": "坏果", "verified": "已验核",
    "delivered": "已交付", "retired": "已退赛", "retiredRound": "退赛帧",
    "missingActionRounds": "缺动作帧数", "illegalActionCount": "非法动作数",
    "penaltyScore": "惩罚分", "breakOrderReady": "破关令就绪", "rushTacticUsedCount": "急策已用次数",
    "currentProcess": "当前处理", "frozenGoodFruit": "冻结好果", "squadAvailable": "可用小队",
    "squadInFlight": "在途小分队", "guardActionPoint": "护卫行动点", "resources": "资源",
    "buffs": "增益", "totalScore": "总分", "taskScore": "任务分", "bountyScore": "悬赏分",
    "scoreDetail": "分项", "mainRoute": "主路线", "branchRounds": "支路帧数",
    "mountainRounds": "山路帧数", "roadRounds": "官道帧数", "waterRounds": "水路帧数",
    "routeSwitchCount": "换线次数", "routeTaskScore": "分路任务分", "routeResourceCount": "分路资源数",
    "totalKilled": "击杀数", "totalGold": "金币", "morale": "士气", "progress": "进度",
    # 节点
    "nodeId": "节点ID", "name": "名称", "x": "X", "y": "Y", "nodeType": "节点类型",
    "processRound": "处理帧数", "start": "起点", "terminal": "终点", "visible": "可见",
    "resourceVisible": "资源可见", "resourceStock": "资源库存", "effectiveCombatCount": "有效攻坚次数",
    "guardBlockCount": "设卡阻挡次数", "keyPassCombatCount": "关键关隘攻坚次数",
    "hasObstacle": "有障碍", "obstacleType": "障碍类型", "canWindow": "可触发窗口",
    "scouted": "探路标记", "processType": "处理类型", "guard": "设卡",
    # 设卡子字段
    "ownerTeamId": "归属队伍", "defense": "防守值", "initialDefense": "初始防守值",
    "maxDefense": "防守上限", "completeRound": "完成帧", "ageRound": "年龄帧", "active": "有效",
    # 窗口 contest
    "contestId": "窗口ID", "contestType": "窗口类型", "targetNodeId": "目标节点",
    "resourceId": "资源ID", "taskId": "任务ID", "initiatorPlayerId": "发起方",
    "redPlayerId": "红方", "bluePlayerId": "蓝方", "initialTimeTaxRound": "初始时间税帧",
    "initialBlockType": "初始阻挡类型", "initialGuardOwnerTeamId": "初始设卡归属",
    "initialGuardCompleteRound": "初始设卡完成帧", "initialGuardTaxRound": "初始设卡税帧",
    "initialObstacle": "初始障碍", "initialObstacleType": "初始障碍类型",
    "initialObstacleTaxRound": "初始障碍税帧", "breakOrderCostTypes": "破关令成本类型",
    "sourceActionTypes": "来源动作类型", "sourceTaskIds": "来源任务ID",
    "roundIndex": "拍序", "totalRounds": "总拍数", "redPoint": "红方胜点", "bluePoint": "蓝方胜点",
    "redCostCount": "红方耗牌数", "blueCostCount": "蓝方耗牌数", "deadlineRound": "截止帧",
    "resolved": "已结算", "status": "状态", "cards": "出牌", "objectKey": "对象键",
    "suppressUntilRound": "抑制至帧", "remainRound": "剩余帧", "blockType": "阻挡类型",
    "redCard": "红方牌", "blueCard": "蓝方牌", "timeTax": "时间税", "taxRound": "税帧",
    # 悬赏
    "bountyId": "悬赏ID", "bountyType": "悬赏类型", "bounty": "悬赏", "bounties": "悬赏列表",
    "triggerReason": "触发原因", "triggerRound": "触发帧", "cooldownUntilRound": "冷却至帧",
    "rewardScore": "悬赏分", "rewardResourceType": "奖励资源类型", "completed": "已完成",
    "winnerPlayerId": "获取方", "winnerTeamId": "获取队伍", "winner": "胜方",
    # 任务
    "taskTemplateId": "任务模板", "routeBucket": "路线桶", "score": "分值",
    "refreshRound": "刷新帧", "expireRound": "过期帧", "failed": "已失败",
    "ownerPlayerId": "归属", "protectionPlayerId": "保护方", "supplement": "补充",
    "claimRound": "领取帧", "requiredResourceTypes": "需要资源", "requiredFreshness": "需要鲜度",
    "tasks": "任务列表", "taskTemplates": "任务模板列表", "taskCandidates": "任务候选点",
    # 天气
    "weatherId": "天气ID", "type": "类型", "region": "区域", "startRound": "开始帧",
    "durationRound": "持续帧数", "forecast": "预告", "weather": "天气",
    # 消息/事件 payload
    "eventId": "事件ID", "payload": "载荷", "round": "帧", "action": "动作",
    "orderId": "订单ID", "fromNodeId": "起点", "toNodeId": "终点", "fromNode": "起点", "toNode": "终点",
    "before": "前", "after": "后", "loss": "损失", "result": "结果", "success": "成功",
    "defenseAfter": "攻坚后防守值", "resourceType": "资源类型", "remainingTriggers": "剩余触发数",
    "source": "来源", "code": "代码", "errorCode": "错误码", "reason": "原因",
    "failureReason": "失败原因", "resultType": "结果类型", "bindAction": "绑定动作",
    "clearedByPlayerId": "清除方", "clearedByTeamId": "清除队伍", "clearRound": "清除帧",
    "afterRound": "后帧", "beforeRound": "前帧", "weatherType": "天气类型",
    "freshnessMultiplier": "鲜度系数", "untilRound": "至帧", "endRound": "结束帧",
    "consumedFrozenCost": "消耗冻结成本", "costType": "成本类型", "candidateNodeIds": "候选节点",
    "obstacleResidue": "清障残留", "overReason": "结束原因", "overRound": "结束帧",
    "ownerProcessType": "归属处理类型", "points": "点数", "count": "数量", "total": "总数",
    "time": "时间", "delivery": "送达", "penalty": "惩罚", "threshold": "阈值",
    # 图/边/角色（多在 init，少量进战报）
    "edgeId": "边ID", "distance": "距离", "bidirectional": "双向", "pathId": "路径ID",
    "roles": "角色", "startNodeId": "起点ID", "gateNodeId": "宫门ID",
    "terminalNodeIds": "终点ID", "safeZoneNodeIds": "安全区", "reverifyNodeId": "复验节点",
    "rushExcludedNodeIds": "冲刺排除节点", "processNodes": "处理站", "routeTaskBuckets": "路线任务桶",
    "routeCostMultiplier": "路线成本系数", "routePaths": "路线", "mapId": "地图ID",
    "matchId": "对局ID", "seed": "种子", "tick": "tick", "phase": "阶段",
    "messages": "消息", "players": "玩家", "nodes": "节点", "edges": "边",
    "contests": "窗口", "events": "事件",
}


def zh_key(k):
    return ZH_KEY.get(k, k)


def fnum(x):
    if isinstance(x, float):
        return f"{x:.2f}"
    return x


def compact_val(k, v):
    """递归紧凑展示，叶子字符串值走翻译 zh(k,v)；key 也走翻译 zh_key(k)。"""
    if isinstance(v, dict):
        return "{" + ",".join(f"{zh_key(kk)}={compact_val(kk, vv)}" for kk, vv in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ",".join(compact_val(k, x) for x in v) + "]"
    return str(zh(k, v))


def compact(d):
    """把 dict 紧凑展示成 k=v（递归，全字段保留，枚举值/字段名都翻中文）。"""
    return " ".join(f"{zh_key(k)}={compact_val(k, v)}" for k, v in d.items())


def player_sig(p):
    """玩家签名 = 剥掉计时字段后的全部字段（任何语义字段变化都开新段）。"""
    return strip_timers(p)


def getp(rec, pid):
    for p in rec.get("players", []):
        if str(p.get("playerId")) == pid:
            return p
    return None


# ============ 玩家时间线（RLE，全字段） ============

def player_runs(rounds, pid):
    """连续相同签名（全离散字段）的帧合并；记录连续字段的区间。"""
    runs = []
    cur = None
    for rec in rounds:
        p = getp(rec, pid)
        if not p:
            continue
        sig = player_sig(p)
        # 签名可 JSON 化才能比较
        sig_key = json.dumps(sig, ensure_ascii=False, sort_keys=True)
        cont = {k: p.get(k) for k in CONTINUOUS_FIELDS}
        if cur is None or sig_key != cur["sig_key"]:
            if cur:
                runs.append(cur)
            cur = {"sig": sig, "sig_key": sig_key, "r0": rec["round"], "r1": rec["round"],
                   "cont0": dict(cont), "cont1": dict(cont), "end": p}
        else:
            cur["r1"] = rec["round"]
            cur["cont1"] = dict(cont)
            cur["end"] = p
    if cur:
        runs.append(cur)
    return runs


def fmt_run(run, prev_node):
    r0, r1 = run["r0"], run["r1"]
    nf = r1 - r0 + 1
    rng = f"r{r0}" if nf == 1 else f"r{r0}-{r1}"
    sig = run["sig"]
    node = sig.get("currentNodeId")
    nxt = sig.get("nextNodeId")
    edge = sig.get("routeEdgeId")
    st = sig.get("state")
    parts = [f"{rng:<11}"]

    # 人类可读摘要（位置/动作，状态码翻中文）—— 仅用于一眼看懂，不替代下面的全字段
    zst = zh("state", st)  # 状态中文化
    if st == "MOVING" and edge:
        ms0, ms1 = run["cont0"].get("edgeProgressMs"), run["cont1"].get("edgeProgressMs")
        frozen = (ms0 is not None and ms1 is not None and ms1 <= ms0 and nf > 1)
        tag = "  *** 冻结 ***" if frozen else ""
        parts.append(f"{zst} {edge} {node}->{nxt} edgeMs {ms0}->{ms1}{tag}")
    elif st in ("IDLE", "WAITING"):
        parts.append(f"@{node} {zst}" + (" [进站]" if node != prev_node else ""))
    else:
        parts.append(f"@{node} {zst}")
    parts.append(f"({nf}帧)")

    # 连续字段区间
    f0, f1 = run["cont0"].get("freshness"), run["cont1"].get("freshness")
    if f0 is not None and f1 is not None and abs((f1 or 0) - (f0 or 0)) >= 0.01:
        parts.append(f"fresh {fnum(f0)}->{fnum(f1)}")
    elif f1 is not None:
        parts.append(f"fresh {fnum(f1)}")

    # 全字段黑名单保留 —— 仅跳过纯静态身份 / 冗余派生预览（这些不丢信息：身份每次相同，
    # scoreDetail 是 freshness/gf/... 的服务端重算，与已展示字段重复）
    SKIP = {"playerId", "teamId", "camp", "playerName", "online",
            "id", "name", "version", "scoreDetail"}
    shown = {"state", "currentNodeId", "nextNodeId", "routeEdgeId"}  # 已在摘要里
    rest = {k: v for k, v in sig.items()
            if k not in SKIP and k not in shown and v not in (None, "", [], {}, 0, False)}
    if rest:
        parts.append("| " + compact(rest))
    return "  ".join(parts)


def player_timeline(rounds, pid):
    lines = []
    prev_node = None
    for run in player_runs(rounds, pid):
        lines.append(fmt_run(run, prev_node))
        prev_node = run["sig"].get("currentNodeId")
    return lines


# ============ 通用「对象变更」追踪器（黑名单：任何字段变都记） ============

def object_changes(rounds, key, sig_fn, label_fn):
    """对每帧的某集合（nodes/contests/bounties/tasks）做变更追踪。

    sig_fn(item) -> 可比较签名（默认整个 dict）；label_fn(item) -> 一行标题。
    返回变更行列表。任何字段变化都触发一条；保留完整新状态。
    """
    lines = []
    prev = {}  # id -> sig_key
    prev_item = {}
    for rec in rounds:
        r = rec["round"]
        items = rec.get(key) or []
        if isinstance(items, dict):
            items = [{"k": k, **v} if isinstance(v, dict) else {"k": k, "v": v}
                     for k, v in items.items()]
        seen = set()
        for it in items:
            # 唯一 id：taskId/contestId/bountyId 优先（任务同时有 nodeId，不能让 nodeId 抢先，
            # 否则同节点多任务会碰撞、每帧重发）
            iid = it.get("taskId") or it.get("contestId") or it.get("bountyId") \
                or it.get("nodeId") or it.get("k") or id(it)
            seen.add(iid)
            # 签名剥掉计时字段（ageRound/expireRound/remainRound 等），避免每帧一条；
            # 输出仍用完整 it（计时字段的值在变更点照常展示）
            sig_key = json.dumps(strip_timers(it), ensure_ascii=False, sort_keys=True)
            if prev.get(iid) != sig_key:
                title = label_fn(it) if label_fn else str(iid)
                lines.append(f"  r{r:<5} {title}: {compact(it)}")
            prev[iid] = sig_key
            prev_item[iid] = it
        # 消失的项
        for iid in list(prev):
            if iid not in seen and iid in prev_item:
                title = (label_fn(prev_item[iid]) if label_fn else str(iid))
                lines.append(f"  r{r:<5} {title}: REMOVED")
                del prev[iid]
                del prev_item[iid]
    return lines


# ============ 事件（黑名单：只丢 NOISE 类型，payload 全保留） ============

def filtered_events(rounds):
    out = []
    for rec in rounds:
        r = rec["round"]
        for m in rec.get("messages", []):
            mtype = m.get("type") or ""
            if mtype in NOISE_EVENT_TYPES:
                continue  # 黑名单：明确丢弃
            pl = {k: v for k, v in (m.get("payload") or {}).items()
                  if k not in USELESS_PAYLOAD_KEYS}  # 仅删元数据 key，事件本身保留
            pid = pl.get("playerId")
            by = f"P{pid}" if pid is not None else ""
            out.append(f"  r{r:<5} {zh(None,mtype):<16} {by:<6} {compact(pl)}")
    return out


# ============ 派生里程碑（只是汇总，不替代上面的原始记录） ============

def milestones(rounds, players_meta):
    m = {"rushOnset": None, "delivery": {}, "verify": {}, "retire": {},
         "freeze": {pid: [] for pid in players_meta},
         "squad": {pid: {k: 0 for k in SQUAD_COST} for pid in players_meta}}
    prev_phase, prev_sig, cur_freeze = None, {pid: None for pid in players_meta}, \
        {pid: None for pid in players_meta}
    for rec in rounds:
        r = rec["round"]
        if rec.get("phase") == "RUSH" and prev_phase != "RUSH" and m["rushOnset"] is None:
            m["rushOnset"] = r
        prev_phase = rec.get("phase")
        for p in rec.get("players", []):
            pid = str(p.get("playerId"))
            if pid not in players_meta:
                continue
            if p.get("verified") and pid not in m["verify"]:
                m["verify"][pid] = r
            if p.get("delivered") and pid not in m["delivery"]:
                m["delivery"][pid] = r
            if p.get("retired") and pid not in m["retire"]:
                m["retire"][pid] = r
            if p.get("state") == "MOVING" and p.get("routeEdgeId"):
                sig = (p.get("routeEdgeId"), p.get("edgeProgressMs"))
                prev = prev_sig[pid]
                if prev and prev[0] == sig[0] and prev[1] == sig[1]:
                    if cur_freeze[pid] is None:
                        cur_freeze[pid] = [r - 1, r, p.get("routeEdgeId"), p.get("nextNodeId")]
                    else:
                        cur_freeze[pid][1] = r
                else:
                    if cur_freeze[pid] and cur_freeze[pid][1] - cur_freeze[pid][0] >= 3:
                        m["freeze"][pid].append(cur_freeze[pid])
                    cur_freeze[pid] = None
                prev_sig[pid] = sig
            else:
                if cur_freeze[pid] and cur_freeze[pid][1] - cur_freeze[pid][0] >= 3:
                    m["freeze"][pid].append(cur_freeze[pid])
                cur_freeze[pid] = None
                prev_sig[pid] = None
        for msg in rec.get("messages", []):
            if msg.get("type") == "SQUAD_DISPATCH":
                pl = msg.get("payload") or {}
                pid = str(pl.get("playerId"))
                act = pl.get("action")
                if pid in players_meta and act in SQUAD_COST:
                    m["squad"][pid][act] += 1
    for pid in players_meta:
        if cur_freeze[pid] and cur_freeze[pid][1] - cur_freeze[pid][0] >= 3:
            m["freeze"][pid].append(cur_freeze[pid])
    return m


# ============ 主流程 ============

def build_report(path):
    lines = parse(path)
    init = lines[0]
    rounds = [r for r in lines[1:] if r.get("round") is not None]
    over = next((r for r in lines[1:] if r.get("round") is None), None)

    players_meta = {}
    for p in init.get("players", []):
        pid = str(p.get("playerId"))
        players_meta[pid] = {"team": p.get("teamId"),
                             "name": p.get("name") or p.get("playerName")}

    ms = milestones(rounds, players_meta)
    final_src = over if over else (rounds[-1] if rounds else {})

    out = []
    out.append("=" * 70)
    out.append(f"对局  {init.get('matchId')}  地图={init.get('mapId')}  "
               f"总帧数={init.get('durationRound')}")
    out.append("=" * 70)
    out.append("玩家:")
    for pid, info in players_meta.items():
        out.append(f"  P{pid}  {info['team']:<5} {info['name']}")
    out.append("")
    out.append("最终结果:")
    for p in final_src.get("players", []):
        pid = str(p.get("playerId"))
        if pid not in players_meta:
            continue
        dlv = f"已交付 r{ms['delivery'].get(pid)}" if p.get("delivered") \
            else ("已退赛" if p.get("retired") else "未交付")
        out.append(f"  P{pid}  {dlv:<14} 总分={p.get('totalScore')} 任务分={p.get('taskScore')} "
                   f"鲜度={fnum(p.get('freshness'))} 好果={p.get('goodFruit')} 坏果={p.get('badFruit')} "
                   f"悬赏={p.get('bountyScore')} 惩罚={p.get('penaltyScore')}")
    out.append(f"宫宴冲刺触发: r{ms['rushOnset']}")
    out.append("")

    out.append("小分队消耗:")
    for pid in players_meta:
        sp = ms["squad"][pid]
        spent = sum(sp[k] * c for k, c in SQUAD_COST.items())
        detail = " ".join(f"{zh(None,k)}={sp[k]}" for k in SQUAD_COST if sp[k])
        out.append(f"  P{pid}: 共耗 {spent}/8" + (f"  ({detail})" if detail else ""))
    out.append("")
    freezers = {pid: v for pid, v in ms["freeze"].items() if v}
    if freezers:
        out.append("冻结区间 (移动中 + edgeMs 不增, >=3帧):")
        for pid, ivs in freezers.items():
            for a, b, edge, nxt in ivs:
                out.append(f"  P{pid}: r{a}-{b} 在 {edge} ->{nxt} ({b-a+1}帧)")
        out.append("")

    for pid, info in players_meta.items():
        out.append("-" * 70)
        out.append(f"P{pid} ({info['team']}, {info['name']}) 时间线  [全字段保留，| 后为离散字段]")
        out.append("-" * 70)
        out.extend(player_timeline(rounds, pid))
        out.append("")

    # 节点（全字段变更追踪）
    nlines = object_changes(rounds, "nodes", None, lambda it: f"node {it.get('nodeId')}")
    if nlines:
        out.append("-" * 70)
        out.append("节点状态变化 (任意字段变即记一行，完整新状态)")
        out.append("-" * 70)
        out.extend(nlines)
        out.append("")

    # 窗口 / 悬赏 / 任务（全字段变更追踪 —— 不再丢弃）
    for key, name in [("contests", "窗口"), ("bounties", "悬赏"), ("tasks", "任务")]:
        clines = object_changes(rounds, key, None,
                                lambda it, n=name: f"{n} {it.get('contestId') or it.get('bountyId') or it.get('taskId')}")
        if clines:
            out.append("-" * 70)
            out.append(f"{name}状态变化")
            out.append("-" * 70)
            out.extend(clines)
            out.append("")

    # 天气（全字段变更追踪）
    wlines = []
    prev_w = None
    for rec in rounds:
        w = rec.get("weather")
        sig = json.dumps(strip_timers(w), ensure_ascii=False, sort_keys=True)  # 剥 remainRound
        if sig != prev_w:
            wlines.append(f"  r{rec['round']:<5} 天气: {compact(w or {})}")
            prev_w = sig
    if wlines:
        out.append("-" * 70)
        out.append("天气变化")
        out.append("-" * 70)
        out.extend(wlines)
        out.append("")

    # 阶段变化
    plines = []
    prev_ph = None
    for rec in rounds:
        ph = rec.get("phase")
        if ph != prev_ph:
            plines.append(f"  r{rec['round']:<5} 阶段={zh('phase',ph)}")
            prev_ph = ph
    if plines:
        out.append("-" * 70)
        out.append("阶段变化")
        out.append("-" * 70)
        out.extend(plines)
        out.append("")

    # 事件（黑名单：仅丢 NOISE_EVENT_TYPES，payload 全保留）
    elines = filtered_events(rounds)
    if elines:
        out.append("-" * 70)
        out.append(f"事件 (黑名单过滤: 仅丢 {sorted(NOISE_EVENT_TYPES)}; 其余全保留)")
        out.append("-" * 70)
        out.extend(elines)

    return "\n".join(out), rounds


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    flags = [a for a in sys.argv[1:] if a.startswith("-")]
    if not args:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    path = args[0]
    text, rounds = build_report(path)
    if "--stdout" in flags:
        print(text)
    else:
        out_path = os.path.splitext(path)[0] + ".report.txt"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {out_path} ({len(text)} bytes, {len(text.splitlines())} lines; "
              f"source {len(rounds)} rounds)", file=sys.stderr)
        for line in text.splitlines():
            if line.startswith("MATCH") or line.startswith("  P") or \
                    line.startswith("RUSH") or line.startswith("Squad") or \
                    line.startswith("Freeze"):
                print(line, file=sys.stderr)


if __name__ == "__main__":
    main()
