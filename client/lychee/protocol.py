"""协议常量与 action 构造器。

字段口径见《一骑红尘：荔枝争运战 通信协议》第 8 章「actions[] 动作字段矩阵」。
规则数值（耗时系数、速度、鲜度损耗）见《参赛任务书》2.3.2 / 3.2.2，属于规则固定项。
"""

# ---------- 消息名 ----------
MSG_REGISTRATION = "registration"
MSG_START = "start"
MSG_READY = "ready"
MSG_INQUIRE = "inquire"
MSG_ACTION = "action"
MSG_OVER = "over"
MSG_ERROR = "error"

# ---------- 阶段 ----------
PHASE_NORMAL = "NORMAL"
PHASE_RUSH = "RUSH"
PHASE_ENDED = "ENDED"

# ---------- 主车队状态 ----------
ST_IDLE = "IDLE"
ST_MOVING = "MOVING"
ST_WAITING = "WAITING"
ST_PROCESSING = "PROCESSING"
ST_CONTESTING = "CONTESTING"
ST_RESTING = "RESTING"
ST_FORCED_PASSING = "FORCED_PASSING"
ST_VERIFYING = "VERIFYING"
ST_COST_BANKRUPT = "COST_BANKRUPT"
ST_DELIVERED = "DELIVERED"
ST_RETIRED = "RETIRED"

# 这些状态下主车队动作不会生效，等待即可
BUSY_STATES = {ST_PROCESSING, ST_RESTING, ST_FORCED_PASSING, ST_VERIFYING, ST_CONTESTING}

# ---------- 路线类型 ----------
ROAD, WATER, MOUNTAIN, BRANCH = "ROAD", "WATER", "MOUNTAIN", "BRANCH"

# 每 1 点路线距离所需移动量（任务书 2.3.2，规则固定项）
ROUTE_COEFF = {ROAD: 1380, WATER: 1250, MOUNTAIN: 1780, BRANCH: 1550}

# 每结算帧移动中的鲜度损耗（任务书 3.2.2）
ROUTE_FRESH_DECAY = {ROAD: 0.055, WATER: 0.045, MOUNTAIN: 0.07, BRANCH: 0.065}
IDLE_FRESH_DECAY = 0.05

# 基础每帧移动量（任务书 2.3.2）
BASE_SPEED = 1000
SPEED_FAST_HORSE = 1200
SPEED_SHORT_HORSE = 1150
SPEED_RUSH = 1300

# 天气通行倍率（任务书 2.3.2）
WEATHER_MOVE_TAX = {("HEAVY_RAIN", WATER): 1350, ("MOUNTAIN_FOG", MOUNTAIN): 1100}

# 鲜度好果转坏阈值（任务书 3.2.1）：鲜度首次低于这些值，各转 1 篓好果为坏果
FRESH_THRESHOLDS = (90, 80, 70, 60, 50, 40, 30, 20, 10)

# ---------- 资源 ----------
ICE_BOX = "ICE_BOX"
FAST_HORSE = "FAST_HORSE"
SHORT_HORSE = "SHORT_HORSE"
BOAT_RIGHT = "BOAT_RIGHT"
PASS_TOKEN = "PASS_TOKEN"
OFFICIAL_PERMIT = "OFFICIAL_PERMIT"
INTEL = "INTEL"

# ---------- 天气 ----------
HOT = "HOT"
HEAVY_RAIN = "HEAVY_RAIN"
MOUNTAIN_FOG = "MOUNTAIN_FOG"

# ---------- 窗口牌 ----------
CARD_YAN_DIE = "YAN_DIE"        # 消耗 1 文书（过所/官凭）
CARD_QIANG_XING = "QIANG_XING"  # 有马类/疾行令增益免费，否则消耗 1 马
CARD_XIAN_GONG = "XIAN_GONG"    # 鲜度>=80 且消耗 1 好果
CARD_BING_ZHENG = "BING_ZHENG"  # 消耗 1 护卫行动点
CARD_ABSTAIN = "ABSTAIN"

# 克制关系（任务书 5.4.4）：CARD_BEATS[x] = x 能赢的牌
CARD_BEATS = {
    CARD_YAN_DIE: {CARD_QIANG_XING},
    CARD_QIANG_XING: {CARD_XIAN_GONG},
    CARD_XIAN_GONG: {CARD_YAN_DIE, CARD_BING_ZHENG},
    CARD_BING_ZHENG: {CARD_YAN_DIE, CARD_QIANG_XING},
    CARD_ABSTAIN: set(),
}

# ---------- 窗口类型 ----------
CONTEST_RESOURCE = "RESOURCE"
CONTEST_TASK = "TASK"
CONTEST_GATE = "GATE"
CONTEST_DOCK = "DOCK"
CONTEST_PASS = "PASS"
CONTEST_OBSTACLE = "OBSTACLE"

# ---------- 终局急策 ----------
RUSH_SPEED = "RUSH_SPEED"
RUSH_PROTECT = "RUSH_PROTECT"
BREAK_ORDER = "BREAK_ORDER"

# ---------- 主车队动作集合 ----------
MAIN_ACTION_TYPES = {
    "WAIT", "MOVE", "PROCESS", "DOCK", "CLAIM_RESOURCE", "USE_RESOURCE",
    "CLAIM_TASK", "CLEAR", "SET_GUARD", "BREAK_GUARD", "FORCED_PASS",
    "VERIFY_GATE", "DELIVER",
}

# ---------- 常见错误码（业务拒绝 / 非法动作） ----------
E_PROCESS_REQUIRED = "PROCESS_REQUIRED"
E_PROCESS_NOT_AVAILABLE = "PROCESS_NOT_AVAILABLE"
E_MOVE_BLOCKED_BY_GUARD = "MOVE_BLOCKED_BY_GUARD"
E_OBJECT_BUSY = "OBJECT_BUSY"
E_DELIVER_NOT_VERIFIED = "DELIVER_NOT_VERIFIED"


# ================= action 构造器 =================
# 每帧额度：主车队 1 + 小分队 1 + 窗口出牌 1 + 终局急策 1（任务书 4.1）

def a_wait():
    return {"action": "WAIT"}


def a_move(target_node_id):
    return {"action": "MOVE", "targetNodeId": target_node_id}


def a_process(target_node_id=None):
    a = {"action": "PROCESS"}
    if target_node_id:
        a["targetNodeId"] = target_node_id
    return a


def a_claim_resource(node_id, resource_type):
    return {"action": "CLAIM_RESOURCE", "targetNodeId": node_id, "resourceType": resource_type}


def a_use_resource(resource_type, target_node_id=None):
    a = {"action": "USE_RESOURCE", "resourceType": resource_type}
    if target_node_id:
        a["targetNodeId"] = target_node_id
    return a


def a_claim_task(task_id):
    return {"action": "CLAIM_TASK", "taskId": task_id}


def a_clear(node_id):
    return {"action": "CLEAR", "targetNodeId": node_id}


def a_set_guard(node_id, extra_good_fruit=0):
    return {"action": "SET_GUARD", "targetNodeId": node_id, "extraGoodFruit": extra_good_fruit}


def a_break_guard(node_id, good_fruit=0, bad_fruit=0, break_order=False):
    a = {"action": "BREAK_GUARD", "targetNodeId": node_id,
         "goodFruit": good_fruit, "badFruit": bad_fruit}
    if break_order:
        a["rushTactic"] = BREAK_ORDER
    return a


def a_forced_pass(node_id):
    return {"action": "FORCED_PASS", "targetNodeId": node_id}


def a_verify_gate(break_order=False):
    a = {"action": "VERIFY_GATE"}
    if break_order:
        a["rushTactic"] = BREAK_ORDER
    return a


def a_deliver():
    return {"action": "DELIVER"}


def a_window_card(contest_id, card):
    return {"action": "WINDOW_CARD", "contestId": contest_id, "card": card}


def a_squad_scout(node_id):
    return {"action": "SQUAD_SCOUT", "targetNodeId": node_id}


def a_squad_clear(node_id):
    return {"action": "SQUAD_CLEAR", "targetNodeId": node_id}


def a_squad_reinforce(node_id):
    return {"action": "SQUAD_REINFORCE", "targetNodeId": node_id}


def a_squad_weaken(node_id):
    return {"action": "SQUAD_WEAKEN", "targetNodeId": node_id}


def a_rush_speed():
    return {"action": "RUSH_SPEED"}


def a_rush_protect():
    return {"action": "RUSH_PROTECT"}
