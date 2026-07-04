#!/usr/bin/env python3
"""哨兵电池（V3.25 起日常迭代闸门）：python3 smoke.py  （~20 秒）

全量 regress.py（216 局 ~3 分钟）只在版本收尾时跑；日常改动用本电池
快速把关。种子按历史信息量挑选：
- camper 0/4/5/10/15/19/23：相位敏感七种子（悬崖带/宽限带/前推偏置
  三轮实验的全部翻盘与回归都发生在这里）
- toller 0/1/5：冻结/回手卡代表局
- mirror 0/2/7、farmer 1/5、rusher 3：形态哨兵
基线（V3.25）：见文件尾 EXPECT——偏离即黄灯，去跑全量电池定位。
"""
import os
import sys
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from arena import run_match, PID_A, PID_B          # noqa: E402
from sparring import (CamperBot, FarmerBot, RusherBot,   # noqa: E402
                      TollerBot)

SENTINELS = (
    [("camper", s) for s in (0, 4, 5, 10, 15, 19, 23)]
    + [("toller", s) for s in (0, 1, 5)]
    + [("mirror", s) for s in (0, 2, 7)]
    + [("farmer", s) for s in (1, 5)]
    + [("rusher", 3)]
)
# V3.25 基线预期（数字来自全量电池，改动使其恶化才算红灯）
KNOWN_UNDLV = {("camper", 4), ("camper", 23)}   # 深链死局（观察清单）
KNOWN_LOSS = {("camper", 19)}                    # 交付负局（42/48 基线内）
BOTS = {"camper": CamperBot, "farmer": FarmerBot,
        "toller": TollerBot, "rusher": RusherBot}


def game(spec):
    kind, seed = spec
    if kind == "mirror":
        r = run_match(seed)
        return (kind, seed, r["winner"] == PID_A, r["margin"],
                r[PID_A]["delivered"] and r[PID_B]["delivered"])
    r = run_match(seed, cls_b=BOTS[kind])
    return (kind, seed, r["winner"] == PID_A, r["margin"],
            r[PID_A]["delivered"])


def main():
    with Pool(10) as p:
        rows = p.map(game, SENTINELS)
    bad = 0
    for kind, seed, win, margin, dlv in rows:
        flag = ""
        if kind != "mirror" and not dlv:
            flag = "  <-- 我方未交付"
            if (kind, seed) not in KNOWN_UNDLV:
                bad += 1
                flag += "（非已知深链，红灯！）"
        elif kind != "mirror" and not win \
                and (kind, seed) not in KNOWN_LOSS:
            bad += 1
            flag = "  <-- 输局（红灯）"
        print(f"{kind:7} seed={seed:>2} win={int(win)} "
              f"margin={margin:+5} dlv={int(dlv)}{flag}")
    print(f"\n{'全绿' if bad == 0 else f'{bad} 处红灯——跑全量 regress.py 定位'}"
          f"（已知深链 camper 4/23 未交付为常态）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
