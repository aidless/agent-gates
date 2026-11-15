#!/usr/bin/env python3
"""eval 闭环闸：把"这一版是否比基线退步"变成有退出码的判定。

两种用法：
  纯对比（秒级，不跑模型）:  python3 eval_gate.py --baseline runs/baseline.jsonl --current runs/x.jsonl
  跑完再比:                  python3 eval_gate.py --run --tag v2 --jobs 2 --timeout 600

判定口径（只有一条，写死在 compare() 里，改口径必须同步改这条注释）：
  POS 判据 = 通过的题数（PASS）。超时单独计数，永不折算成 PASS，也不把"超时变少"当成涨分。
  假阳判据（2026-11-15 加，见 fp_suspect）= PASS 但 expect 关键词在答案里首次出现
    位置相距 > FP_WINDOW 字符 ⇒ 判"散弹蒙对"，不计入有效通过。
    有效涨分 effective_delta = delta − |新增可疑题|，判决用 effective_delta。
    基线里本来就有的可疑题不算这一版的锅（只在 common 内比较）。
  GREEN : effective_delta >= 0
  YELLOW: -2 <= effective_delta < 0            （退化 1~2 题，提示但不阻断）
  RED   : effective_delta < -2                 （退化 3 题以上，exit 1）
  exit 码：0=GREEN 2=YELLOW 1=RED 3=输入不全/口径不合法

反证：--selftest 会构造一份人为退化的 current，断言必须判 RED，否则闸是装饰。
"""
import argparse, json, re, subprocess, sys, os

import config

YELLOW_SLACK = 2
RED_SLACK = 3
# 假阳守卫窗口：同一题的关键词若首次出现位置相距超过这么多字符，
# 判为该题"靠散弹蒙对"。见 fp_suspect()。
FP_WINDOW = 120
ITEMSET = config.ITEMSET  # 可被 --itemset / AGENT_GATES_ITEMSET 覆盖


def ans_norm(s):
    """与 eval_run.judge 同口径的归一化：去空白 + 小写。口径必须一模一样，
    否则闸看到的'答案'和判分器看到的不是同一个东西。"""
    return re.sub(r"\s+", "", (s or "").lower())


def fp_suspect(item, answer, window=FP_WINDOW):
    """关键词判据的假阳守卫：答案是不是只把候选词散落念了一遍。

    判据（可证伪、不猜语义）：取每个 expect 关键词在答案中的**首次**出现位置，
    排序后看**是否存在至少一对相邻关键词的间距 <= window**。
    真结论至少会把两个相关词放进同一段论述；散弹式答案里每个词都孤零零地
    散落全篇，**所有**相邻间距都超窗。
    单关键词题恒不可疑（没有可共现的对象，不能因为"只有一个词"就报可疑）。
    缺词返回 []：那本来就不 PASS，不归假阳管。

    为什么不是 max-min（v1 的写法，已在真实数据上翻车）：B3 的答案引了
    `ops/upgrade.sh:55-80` 逐项列 6 个备份对象，3692 字符，
    4 个关键词里最后一个在 @2642 —— max-min=2378 判可疑，但它是**长而详细的
    正确答案**。max-min 量的是"答案有多长"，不是"答案有没有蒙"。
    那是 AGENTS 第 37 条第三型：让"大"盖住"对"。
    """
    keys = [k for k in (item.get("expect") or []) if k]
    if len(keys) < 2 or not answer:
        return []
    low = ans_norm(answer)
    pos = []
    for k in keys:
        i = low.find(ans_norm(k))
        if i < 0:
            return []
        pos.append(i)
    pos.sort()
    # 至少一对相邻关键词共处一段 ⇒ 是在同一处论述里作答，不是散弹。
    if any(b - a <= window for a, b in zip(pos, pos[1:])):
        return []
    return list(keys)


def expect_map(path=None):
    """id → expect。落盘的 runs 里不含 expect（它是题集属性，不是运行产物），
    所以假阳判据要靠 id 把题集 join 回来。存量 runs 因此也能直接过这道闸。

    path 默认取模块级 ITEMSET（运行时读，所以 --itemset 能生效）。"""
    path = path or ITEMSET
    m = {}
    if not path or not os.path.exists(path):
        return m
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                if r.get("expect"):
                    m[r["id"]] = r["expect"]
    return m


def load(path, with_expect=True):
    rows = {}
    if not path or not os.path.exists(path):
        return rows
    em = expect_map() if with_expect else {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if "expect" not in r and r.get("id") in em:
                r = dict(r, expect=em[r["id"]])
            rows[r["id"]] = r
    return rows


def stats(rows):
    ids = set(rows)
    pos = {i for i, r in rows.items() if r.get("pass")}
    to = {i for i, r in rows.items()
          if not r.get("pass") and ("TIMEOUT" in (r.get("stderr") or "") or (r.get("seconds") or 0) >= 299)}
    wrong = ids - pos - to
    # 假阳：判 PASS 但关键词散落（散弹蒙对）。这是闸原本报不出的那一半。
    suspect = {i for i, r in rows.items()
               if r.get("pass") and fp_suspect(r, r.get("answer") or "")}
    return {"ids": ids, "pos": pos, "timeout": to, "wrong": wrong,
            "suspect": suspect}


def compare(base, cur, strict_same_ids=True):
    """RETURN (verdict, exit_code, detail_dict)"""
    b, c = stats(base), stats(cur)
    common = b["ids"] & c["ids"]
    only_base = b["ids"] - c["ids"]

    if not common:
        return "RED", 3, {"reason": "两批结果没有可比题目"}
    if strict_same_ids and only_base:
        # 基线里有题而这次没跑，不能拿"少跑几题"当涨分
        return "RED", 3, {"reason": f"当前批次缺 {len(only_base)} 道基线题: {sorted(only_base)}"}

    base_pos = len(b["pos"] & common)
    cur_pos = len(c["pos"] & common)
    delta = cur_pos - base_pos
    newly_suspect = (c["suspect"] & common) - (b["suspect"] & common)
    eff = delta - len(newly_suspect)
    detail = {
        "common": len(common),
        "base_pos": base_pos, "cur_pos": cur_pos, "delta": delta,
        "base_timeout": len(b["timeout"] & common), "cur_timeout": len(c["timeout"] & common),
        "regressed": sorted((b["pos"] & common) - c["pos"]),
        "newly_passed": sorted((c["pos"] & common) - b["pos"]),
        "cur_wrong": sorted(c["wrong"] & common),
        # 假阳维度：这一版新增的"可疑通过"。只报新增那些，基线的锅不背。
        "cur_suspect": sorted(newly_suspect),
        "effective_delta": eff,
    }
    if eff >= 0:
        return "GREEN", 0, detail
    if eff >= -YELLOW_SLACK:
        return "YELLOW", 2, detail
    return "RED", 1, detail


def run(tag, jobs, timeout, only):
    """真跑一遍题集（需要 eval_run.py）。纯对比用法不需要走到这里。"""
    cmd = [sys.executable, config.EVAL_RUN, "--set", ITEMSET,
           "--tag", tag, "--jobs", str(jobs), "--timeout", str(timeout)]
    if only:
        cmd += ["--only", only]
    print("[gate] 跑分:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=False)
    return os.path.join(config.RUNS_DIR, f"{tag}.jsonl")


def selftest(base_path):
    base = load(base_path)
    if not base:
        print(f"[selftest] 基线为空: {base_path}", file=sys.stderr)
        return 1
    # 反证 1：人为退化 3 题 → 必须 RED
    cur = dict(base)
    victims = [i for i, r in base.items() if r.get("pass")][:3]
    for i in victims:
        cur[i] = dict(base[i], pass_=False, **{"pass": False}, seconds=600.0, stderr="TIMEOUT after 600s")
    v, code, _ = compare(base, cur)
    assert v == "RED" and code == 1, f"反证失败：退化 3 题应判 RED，实得 {v}/{code}"
    # 反证 2：只退化 1 题 → 必须 YELLOW（不能一有波动就红）
    cur2 = dict(base)
    cur2[victims[0]] = dict(base[victims[0]], **{"pass": False}, seconds=600.0, stderr="TIMEOUT")
    v2, code2, _ = compare(base, cur2)
    assert v2 == "YELLOW" and code2 == 2, f"反证失败：退化 1 题应判 YELLOW，实得 {v2}/{code2}"
    # 反证 3：完全一致 → 必须 GREEN
    v3, code3, _ = compare(base, dict(base))
    assert v3 == "GREEN" and code3 == 0, f"反证失败：完全一致应判 GREEN，实得 {v3}/{code3}"
    # 反证 4：当前批次少跑题 → 不许当涨分
    cur4 = {k: v for k, v in base.items()}
    drop = victims[0]
    del cur4[drop]
    v4, code4, _ = compare(base, cur4)
    assert code4 == 3, f"反证失败：缺题应 exit 3，实得 {v4}/{code4}"
    # 反证 5（假阳守卫）：关键词判据能被"把候选词整篇念一遍"骗过 —— 这不是退步，
    # 是乱给分。注入这种答案必须判为可疑，否则闸只会报"少拿了几题"，
    # 报不出"多给了几题"。
    # 形状要点：两个关键词必须**分开散落**在全篇不同段落 —— 都挤在一句话里的
    # 答案恰好是正常答案的形状，那不该可疑。
    spray = ("这道题我不确定，可能涉及 schedule_update。" + ("无关内容。" * 60)
             + "另外别处又提到 schedule_delete。")
    assert fp_suspect({"expect": ["schedule_update", "schedule_delete"]}, spray) == [
        "schedule_update", "schedule_delete"], "假阳守卫失败：散弹答案必须判可疑"
    # 反证 6（不许乱判）：结论正确的紧邻答案不能被判可疑，否则闸误伤真通过。
    good = "被移除的两个工具是 `schedule_update` 与 `schedule_delete`，其余保留。"
    assert fp_suspect({"expect": ["schedule_update", "schedule_delete"]}, good) == [], \
        f"假阳守卫误判：正常答案被判可疑 {fp_suspect({'expect': ['schedule_update', 'schedule_delete']}, good)}"
    # 单关键词题恒不可疑（无可共现对象）——不能因为"只有一个词"就报可疑
    assert fp_suspect({"expect": ["executeToolBatch"]}, "x" * 500 + "executeToolBatch") == []
    # 反证 7（防我自己的复发：B3 形态）：长而详细、最后才出现第 4 个关键词的
    # 正确答案必须不可疑。v1 用 max-min 在这里把真实的好答案判成了蒙对。
    b3_like = ("## 必须备份的 6 项\n回滚取决于 do_rollback() 用到什么"
               "（见 `ops/upgrade.sh:55-80`）：\n"
               "| 1 | `/srv/app/runtime/` |\n"
               "| 2 | `/srv/app/deps/node_modules/` |\n"
               "| 3 | `/srv/app/package.json` |\n"
               "| 4 | `/srv/app/lockfile` |\n" + ("详述。" * 200)
               + "第六项是 `app-global` 软链接与 `overrides.yml`。")
    got7 = fp_suspect({"expect": ["app-global", "node_modules",
                                  "package.json", "overrides.yml"]}, b3_like)
    assert got7 == [], f"长而详细的正确答案被判可疑（B3 复发）：{got7}";
    # 维度必须进 compare：散弹式涨分不许当涨分。
    # 挑一道原本 FAIL 且 expect 有 ≥2 个词的题（单关键词题没有可共现对象，
    # 结构上就不可能被散弹骗过，拿它做夹具等于测不到东西）。
    cand = [k for k, r in base.items()
            if not r.get("pass") and len(r.get("expect") or []) >= 2]
    assert cand, "基线里找不到'原本FAIL且多关键词'的题，夹具无法构造"
    tid = cand[0]
    tk = base[tid]["expect"]
    spray2 = ("不确定。" + tk[0] + "。" + ("无关内容。" * 60)
              + "另外别处提到 " + tk[1] + "。")
    spray_rows = {k: dict(v) for k, v in base.items()}
    spray_rows[tid] = dict(base[tid], **{"pass": True}, answer=spray2)
    b_pos = sum(1 for r in base.values() if r.get("pass"))
    sp_pos = sum(1 for r in spray_rows.values() if r.get("pass"))
    assert sp_pos > b_pos, "构造失败：散弹版本应表面涨分"
    v6, code6, d6 = compare(base, spray_rows)
    assert d6.get("cur_suspect") == [tid], \
        f"假阳维度没进 compare：cur_suspect={d6.get('cur_suspect')} 期望 [{tid}]"
    assert d6.get("effective_delta") == 0, \
        f"散弹涨的那 1 题必须被扣回（effective_delta 应为 0，实得 {d6.get('effective_delta')}）"

    print("selftest OK: 退化3→RED / 退化1→YELLOW / 一致→GREEN / 缺题→exit3 / "
          "散弹→可疑且不涨分 / 正常答案不误伤 / 长答案有一对共现不误伤")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default=config.BASELINE,
                    help=f"基线 runs（默认 {config.BASELINE}）")
    ap.add_argument("--current", default="")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--tag", default="")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--only", default="")
    ap.add_argument("--itemset", default=ITEMSET,
                    help=f"题集 jsonl（默认 {ITEMSET}）")  # 假阳判据要 join expect
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    ITEMSET_GLOBAL = a.itemset   # 假阳判据要 join 的题集
    globals()["ITEMSET"] = ITEMSET_GLOBAL

    if a.selftest:
        sys.exit(selftest(a.baseline))

    base = load(a.baseline)
    if not base:
        print(f"[gate] 基线缺失: {a.baseline}", file=sys.stderr)
        sys.exit(3)

    cur_path = a.current
    if a.run:
        cur_path = run(a.tag or "gate-run", a.jobs, a.timeout, a.only)
    if not cur_path:
        print("[gate] 需要 --current 或 --run", file=sys.stderr)
        sys.exit(3)

    cur = load(cur_path)
    if not cur:
        print(f"[gate] 当前结果缺失或为空: {cur_path}", file=sys.stderr)
        sys.exit(3)

    v, code, d = compare(base, cur)
    print(f"\n[gate] {v}  (baseline={a.baseline}, current={cur_path})")
    if "reason" in d:
        print(f"  原因: {d['reason']}")
    else:
        print(f"  可比题数 {d['common']} | 基线 PASS {d['base_pos']} → 当前 {d['cur_pos']} (Δ{d['delta']:+d})")
        eff_n = len(d["cur_suspect"])
        print(f"  有效涨分 {d['effective_delta']:+d}"
              + (f"（已扣掉 {eff_n} 题可疑通过）" if eff_n else ""))
        if d["cur_suspect"]:
            print(f"  可疑通过（关键词散落全篇 = 散弹蒙对，不算涨分）: "
                  f"{', '.join(d['cur_suspect'])}")
        print(f"  超时 {d['base_timeout']} → {d['cur_timeout']}")
        if d["regressed"]:
            print(f"  退步: {', '.join(d['regressed'])}")
        if d["newly_passed"]:
            print(f"  新通过: {', '.join(d['newly_passed'])}")
        if d["cur_wrong"]:
            print(f"  当前答错: {', '.join(d['cur_wrong'])}")
    sys.exit(code)


if __name__ == "__main__":
    main()
