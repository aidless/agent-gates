#!/usr/bin/env python3
"""检索三探针：证明"搜到的结果真的相关"，而不是关键词巧合。

它拦的是哪一种失败
    grep 命中的东西看着相关，但其实只是**同一个词恰好出现**。三种典型：
      ① 同义失效：换个说法就搜不到同一件事 —— 检索器把"措辞"当成了"主题"。
      ② 否定失效：加了"不要/不能/别"，结果一模一样 —— 检索器看不见否定词，
         于是"台账怎么写"和"台账不要怎么写"返回同一批条目，而这两件事的
         正确答案是相反的。
      ③ 多义失效：同一个词的两个含义（flock 文件锁 / 重启占用锁）混成一批，
         排第一的是错的那个含义。

    MEMORY.md §14 第 ④ 条把这三条列为"检索判据"，但此前只有文字，没有闸。
    本脚本把它变成能红能绿的机器判据。

判据（写死在这里，改口径必须同步改这段）
    语料 = MEMORY.md 的每个 `## ` 段 + notes/*.md 的每个 `## ` 段 +
           .agent-memory/ledger/lessons.md 的每个 `## L-<series>-` 块。
    检索 = 纯 stdlib BM25（中文按 bigram，英文按词），无外部依赖。

    同义 probe:  top-k(a) ∩ top-k(b) 必须非空。
                 失败 = 两种说法被当成两个主题。
    否定 probe:  top-1(a) 必须与 top-1(b) 不同，且 top-1(a) 里真的含否定语境
                 （"不要"/"不许"/"绝不"/"不能"/"避免"/"禁止"之一）。
                 失败 = 否定词没改变排第一的答案（"不要怎么写"与"怎么写"
                 给出同一条），或改变了但改到了一条根本不在讲禁忌的条目上。
                 注意：这里**不用**集合 Jaccard —— 实测"只把排序换了、集合没变"
                 正是否定被正确理解时的典型形态（min语料上 Jaccard 恒 1.0），
                 用它会把好结果判成坏结果。判据必须看顺序，不能只看集合。
    多义 probe:  top-1(a) 与 top-1(b) 必须是**不同的块**。
                 失败 = 两个含义被检索到同一处（歧义没被上下文分开）。
                 为什么不用"来源文件不同"：台账是 3000+ 块的巨型文件，
                 flock 那条与占用门那条**都在 lessons.md 里**。用文件判会
                 把两条正确分开的结果判成失败 —— 那是量错了底物。

exit 码：0=三探针全过  1=有探针不过  2=语料缺失/探针集为空

反证（--selftest）：用一份**已知答案的微型语料**断言三探针各自能红能绿 ——
只证明"能绿"的闸是装饰（AGENTS 第 21 条）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter

import config

MEMORY = config.MEMORY
NOTES_DIR = config.NOTES_DIR
LEDGER = config.LEDGER

# 同义：top-k 交集必须非空
SYN_K = 5
# 否定：与原句结果的 Jaccard 必须低于这个值（=1.0 说明否定词完全没起作用）
NEG_MARKERS = ("不要", "不许", "绝不", "不能", "避免", "别用", "禁止")


# ── 语料 ────────────────────────────────────────────────────────────────
def chunk_text(path: str, pat: str, min_chars: int = 120):
    """按 `pat` 切块，返回 [(chunk_id, text)]。chunk_id 带 path:line 便于追。"""
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return out
    parts = re.split(pat, raw, flags=re.M)
    # re.split 带捕获组时会把分隔符夹在中间，这里只用整段切分，不带捕获组
    for p in parts:
        t = p.strip()
        if len(t) >= min_chars:
            out.append(t)
    return out


def load_corpus(root: str = ".", include_self: bool = False):
    """返回 [(chunk_id, text)]。chunk_id = 相对路径 + 段序号。

    include_self=False（默认）时剔除含 SELF_MARKERS 的块 —— 那些块在记录判据
    本身，会让探针自我实现。要复现污染结果时传 True。
    """
    docs = []
    mem = os.path.join(root, MEMORY)
    for i, t in enumerate(chunk_text(mem, r"^## ", 80)):
        docs.append((f"{MEMORY}#s{i}", t))
    nd = os.path.join(root, NOTES_DIR)
    if os.path.isdir(nd):
        for fn in sorted(os.listdir(nd)):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(nd, fn)
            for i, t in enumerate(chunk_text(p, r"^## ", 120)):
                docs.append((f"{NOTES_DIR}/{fn}#s{i}", t))
    led = os.path.join(root, LEDGER)
    for i, t in enumerate(chunk_text(led, r"^## L-[a-z]+-", 80)):
        docs.append((f"{LEDGER}#b{i}", t))
    if not include_self:
        docs = [d for d in docs
                if not any(m in d[1] for m in SELF_MARKERS)]
    return docs


# ── 检索（BM25，纯 stdlib）──────────────────────────────────────────────
def tokenize(text: str):
    """中文 bigram + 英文/数字词。不引第三方分词器：语料是中文，bigram 对这种
    "同一短语换个说法"的场景够用，且不会因为词典不同导致两次跑分不可比。"""
    low = text.lower()
    toks = re.findall(r"[a-z0-9_]+", low)
    for run in re.findall(r"[\u4e00-\u9fff]+", low):
        if len(run) == 1:
            toks.append(run)
        else:
            toks.extend(run[i:i + 2] for i in range(len(run) - 1))
    return toks


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.ids = [d[0] for d in docs]
        self.tfs = [Counter(tokenize(d[1])) for d in docs]
        self.lens = [sum(t.values()) for t in self.tfs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 0.0
        self.k1, self.b = k1, b
        df = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(self.tfs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, q: str, k: int = 5):
        qt = tokenize(q)
        scores = []
        for i, tf in enumerate(self.tfs):
            s = 0.0
            dl = self.lens[i] or 1
            for t in qt:
                if t not in tf:
                    continue
                f = tf[t]
                s += self.idf.get(t, 0.0) * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * dl / (self.avg or 1)))
            if s > 0:
                scores.append((s, self.ids[i]))
        scores.sort(key=lambda x: (-x[0], x[1]))
        return [i for _, i in scores[:k]]


# ── 三探针 ──────────────────────────────────────────────────────────────
def probe_synonym(engine, a: str, b: str, k: int = SYN_K):
    """同义：两种说法必须命中至少一块相同的条目。"""
    ia, ib = engine.search(a, k), engine.search(b, k)
    return bool(set(ia) & set(ib))


def probe_negation(engine, docs, a: str, b: str, k: int = SYN_K):
    """否定：带否定词那句必须与原句结果不同，且它的结果里真有否定语境。"""
    ia, ib = engine.search(a, k), engine.search(b, k)
    if not ia or not ib:
        return False
    if ia[0] == ib[0]:
        return False          # 否定词没改变排第一的答案 ⇒ 检索器看不见否定
    text = dict(docs)
    return any(m in text.get(ia[0], "") for m in NEG_MARKERS)


def probe_polysemy(engine, a: str, b: str):
    """多义：同一词的两个含义必须落到不同来源文件，否则歧义没被上下文分开。"""
    ia, ib = engine.search(a, 1), engine.search(b, 1)
    if not ia or not ib:
        return False
    return ia[0] != ib[0]


def src_of(chunk_id: str) -> str:
    return chunk_id.split("#")[0]


# ── 探针集（真实语料 + 已核对过的主题）──────────────────────────────────
# ── 探针集：从 probes.json 读 ──────────────────────────────────────────
# 探针集是**项目内容**，不是脚本内容：它必须是"你这个语料里真实存在的问题"，
# 换成别人的语料就没有意义。所以整段外置，脚本里只留加载与校验。
#
# probes.json 结构：
#   {"synonym": [[q1, q2], ...],        同义 —— top-k 交集必须非空
#    "negation": [[q1, q2], ...],       否定 —— top-1 必须换块
#    "polysemy": [[q1, q2], ...],       多义 —— top-1 必须换块
#    "known_fail": [[kind, q1, q2], ...], 已登记的能力上限，不阻断
#    "self_markers": ["...", ...]}      记录判据本身的块的标识词
SYN_PAIRS: list = []
NEG_PAIRS: list = []
POLY_PAIRS: list = []
KNOWN_FAIL: set = set()
SELF_MARKERS: tuple = ()


def load_probes(path=None) -> str:
    """读 probes.json 填模块级探针集。返回错误描述，空串表示成功。"""
    global SYN_PAIRS, NEG_PAIRS, POLY_PAIRS, KNOWN_FAIL, SELF_MARKERS
    path = path or config.PROBES
    if not os.path.exists(path):
        return f"探针集缺失: {path}（用 --probes 或 AGENT_GATES_PROBES 指定）"
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        return f"探针集读不了: {path}: {e}"
    SYN_PAIRS = [tuple(x) for x in d.get("synonym", [])]
    NEG_PAIRS = [tuple(x) for x in d.get("negation", [])]
    POLY_PAIRS = [tuple(x) for x in d.get("polysemy", [])]
    KNOWN_FAIL = {tuple(x) for x in d.get("known_fail", [])}
    SELF_MARKERS = tuple(d.get("self_markers", []))
    if not (SYN_PAIRS or NEG_PAIRS or POLY_PAIRS):
        return f"探针集为空: {path}（三类探针至少要有一对）"
    return ""


def selftest() -> int:
    fails = []

    def chk(name, cond, detail=""):
        if not cond:
            fails.append(f"{name}" + (f" — {detail}" if detail else ""))

    # 微型语料（12 块）：三组各自有明确正确答案。
    # 为什么必须 ≥12 块：top-5 在 6 块语料上等于"全部"，Jaccard 恒 1.0，
    # 否定探针会被"没有区分度"而不是"否定失效"判红 —— 那是夹具的错，不是判据的错。
    docs = [
        ("mini/a.md#s1", "git 配置环境变量 GIT_CONFIG_GLOBAL 传不进 pnpm 子进程，"
                         "原因是 pnpm 用 shim 启动 node，环境变量在中途被清洗。"),
        ("mini/a.md#s2", "pnpm 子进程拿不到 git 配置环境变量，set-env 被 exec 覆盖。"),
        ("mini/b.md#s1", "台账怎么写：当场 append 一行，写前重取最大号。"),
        ("mini/b.md#s2", "台账**不要**怎么写：绝不要 open(p,'w') 整份写回，"
                         "那会截断别人的条目。禁止 read 之后改再写。"),
        ("mini/c.md#s1", "flock 排他锁保护追加路径，O_APPEND 单次写。"),
        ("mini/d.md#s1", "重启前检测悬着的回合：有 turn/start 无配对 turn/end 就算占用。"),
        ("mini/e.md#s1", "KVCache 把 KV 对缓存下来，避免每步重算注意力。"),
        ("mini/e.md#s2", "向量数据库存的是向量，不是语义本身。"),
        ("mini/f.md#s1", "ARC-AGI 的题目是网格变换，靠预测再行动。"),
        ("mini/f.md#s2", "Kaggle 提交额度每周有限，先算清楚再点火。"),
        ("mini/g.md#s1", "抖音取数走 CDP 加 faster-whisper，长音频按 RTF 0.7 排期。"),
        ("mini/g.md#s2", "论文图表要过六键溯源闸，caption 必须 follows 数据。"),
    ]
    eng = BM25(docs)

    # 同义：绿（两种说法命中同一块）
    chk("同义/绿", probe_synonym(eng, "GIT_CONFIG_GLOBAL 传不进 pnpm",
                                 "pnpm 拿不到 git 环境变量"))
    # 同义：红（完全无关的两件事不许判过）
    chk("同义/红", not probe_synonym(eng, "flock 排他锁", "论文 caption 溯源"))

    # 否定：绿 —— 带"不要"的一句结果与原句不同，且命中含否定语境的块
    chk("否定/绿", probe_negation(eng, docs, "台账不要怎么写", "台账怎么写"))
    # 否定：红 —— 同一句对同一句，Jaccard 恒 1.0，必须判不过
    chk("否定/红（同一句两次必须判不过）",
        not probe_negation(eng, docs, "台账怎么写", "台账怎么写"))

    # 多义：绿 —— 同一词两个含义落到不同来源文件
    chk("多义/绿", probe_polysemy(eng, "flock 排他锁 锁保护",
                                  "占用 悬着的回合 重启前检测"))
    # 多义：红 —— 同一含义两次必须判不过（防止"永不判红"的假门）
    chk("多义/红", not probe_polysemy(eng, "flock 排他锁", "flock 排他锁 追加"))

    print()
    if fails:
        print(f"✗ selftest {len(fails)} 条不通过：{fails}")
        return 1
    print("✓ selftest 全通过（三探针各自红/绿两段）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="检索三探针：同义 / 否定 / 多义（BM25，纯 stdlib）")
    ap.add_argument("--root", default=".", help="工作区根")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--include-self", action="store_true",
                    help="把'记录判据本身'的块算进语料（复现 7/7 自我污染用）")
    ap.add_argument("--probes", default=config.PROBES,
                    help=f"探针集 json（默认 {config.PROBES}）")
    ap.add_argument("--min-chunks", type=int, default=50,
                    help="语料少于这么多块时判不可信并退出（默认 50；示例数据可调小）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        return selftest()

    err = load_probes(a.probes)
    if err:
        print(f"[probes] {err}", file=sys.stderr)
        return 2

    docs = load_corpus(a.root, include_self=a.include_self)
    if len(docs) < a.min_chunks:
        print(f"[probes] 语料过少（{len(docs)} 块 < {a.min_chunks}），探针结果不可信",
              file=sys.stderr)
        return 2
    eng = BM25(docs)
    print(f"[probes] 语料 {len(docs)} 块")

    results = []
    for x, y in SYN_PAIRS:
        ok = probe_synonym(eng, x, y)
        results.append(("同义", x, y, ok))
    for x, y in NEG_PAIRS:
        ok = probe_negation(eng, docs, x, y)
        results.append(("否定", x, y, ok))
    for x, y in POLY_PAIRS:
        ok = probe_polysemy(eng, x, y)
        results.append(("多义", x, y, ok))

    npass = sum(1 for r in results if r[3])
    new_fail, known = [], []
    for kind, x, y, ok in results:
        if ok:
            print(f"  ✓ [{kind}] {x}  ↔  {y}")
            continue
        tag = "已知上限" if (kind, x, y) in KNOWN_FAIL else "新失败"
        print(f"  ✗ [{kind}] {x}  ↔  {y}   ← {tag}")
        (known if (kind, x, y) in KNOWN_FAIL else new_fail).append((kind, x, y))
    print(f"\n[probes] {npass}/{len(results)} 通过"
          + (f"；已知上限 {len(known)} 条，新失败 {len(new_fail)} 条"
             if known or new_fail else ""))
    if known:
        print("  已知上限=本机检索能力实测天花板，不阻断。换成语义检索后应自己变绿，"
              "届时删掉 KNOWN_FAIL 对应行让它重新成为阻断项。")
    return 1 if new_fail else 0


if __name__ == "__main__":
    sys.exit(main())
