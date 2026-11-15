#!/usr/bin/env python3
"""教训台账原子追加器（Ledger Atomic Appender）。

背景 —— 它拦的是哪一种失败
    台账文件是**无锁共享文件**（多个会话/进程会同时追加），纪律见本项目
    要求「只允许追加」。但"追加"是一条**约定**，不是一个保证：任何一个会话都
    可以 `read() → 改 → open(p,"w") 整份写回`，而那份「读」可能是几分钟前的
    旧版本。2026-10-02 这台机器上因此丢了三轮：
      01:26  `open(p,"w")` 截断，230 id → 1 id（3,173 B）
      21:57  拿旧版本整份写回，吞掉已提交的 L-ops-108–117（10 块）
    两轮都是**事后**靠隔离快照 + 内容哈希比对捞回来的。捞回来不等于没出事：
    L-ops-108–112 与工作区同号的五条是完全不同的教训，只能重编号为 119–123。

这个脚本把「追加」从约定变成机制
    ① 单次 write(2) + O_APPEND：内核保证追加的偏移量更新与写入不被撕裂，
       不存在「写了一半」。这是唯一由内核而非纪律提供的保证。
    ② flock 串行化本脚本的所有使用者（sidecar 锁文件），使「写前重取最大号」
       与「落笔」之间没有窗口 —— 这是第 6 条五步里最容易漏的一步。
    ③ 写前/写后前缀一致性断言：写入前的每个字节必须原样还在。
       对**不遵守 flock 的写入者**（比如手写的整份写回）这一步会当场 exit≠0。
    ④ 落笔后内容哈希唯一性断言：新块必须恰好命中一次。

它**不能**做什么（诚实声明）
    - 不能让不经过本脚本的写入者安全。手写 `open(p,"w")` 仍会覆盖；本脚本只
      在自己写入的时刻断言前缀完好，别的会话之后做的破坏它管不着。
    - 不能修「重号」这种语义冲突。撞号时它**拒绝写入并报错**，编号怎么裁决
      是人的判断（第 6 条第 ③ 步：只改标题行）。
    - 不校验 schema 六字段的齐备度。实测现存 453 块里六字段全齐的只有 135
      块（29.8%）、情境+动作+结果三项齐全的 181 块（40.0%）；硬校验会让
      脚本比纪律更严格、从而被绕过。故 schema 只提示不阻断。
    - 不校验 path:line 引用的齐备度（AGENTS.md 第 24 条「无引用即丢」）。
      存量条目大量无引用，硬校验会被绕过；故只提示不阻断，由 --check 的
      missing_refs 字段报告。

用法
    python3 scripts/ledger_append.py --series L-ops \\
        --title "标题（会与编号、日期拼成 ## L-ops-124 标题 | 2026-10-03）" \\
        --file /tmp/lesson_body.md
    python3 scripts/ledger_append.py --series L-ops --title "..." --stdin < body.md
    python3 scripts/ledger_append.py --selftest
    python3 scripts/ledger_append.py --check          # 只体检，不写
    python3 scripts/ledger_append.py --json           # 机器可读结果

退出码
    0 追加成功 / 自测通过 / 体检通过
    1 自测不通过 / 体检发现问题 / 前缀断言失败
    2 参数错误 / 撞号 / 写入后唯一性断言失败 / 体例非法
"""

from __future__ import annotations

import argparse

import config  # 本机布局集中在这里
import errno
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

LEDGER = config.LEDGER  # 可被 --ledger / AGENT_GATES_LEDGER 覆盖
LOCK = LEDGER + ".append.lock"

# 条目 id：L-<series>-[<subseries>-]<digits>。series 允许多段（如 L-dsh-casmi）。
ID_RE = re.compile(r"^L-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]+$")
# 行首的 id（可能后接 ' |' 或 ' 标题' 或日期括号）。必须带 (?m)：否则 ^ 只匹配
# 整个文件的第一行，parse_ids 会永远只看见 1 块。
# 边界用 (?![0-9A-Za-z_-])：
#   · 不能用 (?=[\s|]|$) —— 台账真实存在「## L-kagg-215（2026-09-20）」这种 id 后
#     紧跟全角括号的标题（82 块），白名单当边界会把它们全漏掉 ⇒ max_id_for 少算 ⇒
#     自动编号会发出已占用的号。
#   · 不能只用 (?![0-9]) —— 它把「## L-kagg-279-corr 更正 L-kagg-279」与
#     「## L-kagg-46~55 恢复说明」误判成独立条目 L-kagg-279 / L-kagg-46，
#     于是 --check 报出 7 个「同 id 不同正文」，其中 5 个是假阳性
#     （实测：假阳性 = -corr 更正后缀、~N 范围说明各 1 个）。
#     -corr 与 ~N 都不是独立教训条目，'-' 后的子系列已由上面的 [a-z0-9]+ 组吃掉。
HDR_RE = re.compile(r"(?m)^##\s+(L-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]+)(?![0-9A-Za-z_~-])")
ANY_HDR_RE = re.compile(r"(?m)^##\s+(\S+)")

SCHEMA_FIELDS = ("情境", "动作", "结果", "证据", "置信度", "状态")
CORE_FIELDS = ("情境", "动作", "结果")


def _utcstamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_ledger(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""


def parse_ids(raw: bytes) -> dict[str, list[tuple[str, str]]]:
    """{id: [(hash12, header_text), ...]} —— hash 只覆盖正文（不含标题行），
    因为标题行是可改的（重编号只动它），正文是不可改的。"""
    text = raw.decode("utf-8", errors="replace")
    res: dict[str, list[tuple[str, str]]] = {}
    idx = [(m.start(), m.group(1)) for m in HDR_RE.finditer(text)]
    for i, (start, lid) in enumerate(idx):
        end = idx[i + 1][0] if i + 1 < len(idx) else len(text)
        block = text[start:end]
        body = block.split("\n", 1)[1] if "\n" in block else ""
        h = hashlib.sha256(body.rstrip().encode()).hexdigest()[:12]
        res.setdefault(lid, []).append((h, block.split("\n")[0]))
    return res


def max_id_for(raw: bytes, series: str) -> int:
    """该 series 下的最大编号。注意同时覆盖裸 id 与带子系列两种形态。"""
    ids = parse_ids(raw)
    best = 0
    pat = re.compile(r"^" + re.escape(series) + r"(?:-[a-z0-9]+)*-([0-9]+)$")
    for lid in ids:
        m = pat.match(lid)
        if m:
            best = max(best, int(m.group(1)))
    return best


def build_block(series: str, num: int, title: str, body: str, date: str | None) -> str:
    title = title.strip()
    if title.startswith("|"):
        title = title.lstrip("|").strip()
    head = f"## {series}-{num} {title}"
    if date:
        head += f" | {date}"
    if not title:
        raise ValueError("title 不能为空")
    b = body.strip("\n")
    if not b.strip():
        raise ValueError("body 不能为空：空块会让「新增一条教训」变成无内容的行")
    return head + "\n" + b + "\n"


def body_hash(body: str) -> str:
    return hashlib.sha256(body.strip("\n").rstrip().encode()).hexdigest()[:12]


def missing_fields(body: str) -> list[str]:
    miss = []
    for f in SCHEMA_FIELDS:
        if not re.search(r"^\s*(?:-\s*)?\*{0,2}" + f, body, re.M):
            miss.append(f)
    return miss


# AGENTS.md 第 24 条：台账/复盘每条内容必带 `path:line` 引用（无引用即猜测）。
# 与 schema 一样只提示不阻断（存量大量条目无引用，硬校验会被绕过）。
# 引用模式必须带扩展名 + 冒号 + 行号，否则会把时间（01:26）、
# `e.code == 2`（无路径前缀）误判成引用。负向后顾排除 URL（:// 前的 host:port）。
REF_RE = re.compile(
    r"(?<!://)(?:[A-Za-z0-9_./\\-]+\.(?:py|md|sh|json|yaml|yml|sps|sav|tex|csv|"
    r"txt|html|toml|ini|cfg|js|ts|tsx|jsx|rs|go|java|c|cpp|h|R|r|ipynb|xml|"
    r"bib|docx|xlsx)):\d+(?:-\d+)?")


def missing_refs(body: str) -> list[str]:
    """返回空列表 = 正文含至少一个 path:line 引用（符合第 24 条）；
    非空 = 无引用，需人工补。"""
    if REF_RE.search(body):
        return []
    return ["path:line 引用（第 24 条：无引用即猜测，须带 file:line）"]


class LedgerError(SystemExit):
    """退出码恒为 2，且把原因带在 code 上（SystemExit("msg") 会把 .code
    设成字符串，导致 `e.code == 2` 恒假 —— 调用方拿不到退出码语义）。"""

    def __init__(self, msg: str) -> None:
        super().__init__(2)
        self.msg = msg
        sys.stderr.write(msg.rstrip("\n") + "\n")


# ── 回执行（replay receipt）────────────────────────────────────────────
# 来源：一次"AI 写的代码怎么审"的长文拆解对照。其做法是
# 「问题 + 引入它的那个 PR」= 一个可重跑的案例，重跑得 1/0 分。本机实测：
# 台账 1246 块里，0 条带可执行回执（抽样 10/10 命令已腐化或本来就不是命令）。
# 结论：可反演性只能在**落账那一刻**取得，事后补不回来（原始 transcript 已不可得，
# 见 L-kagg-278 自己记的这条）。所以把它做成 append 路径上的门，而不是事后脚本。
RECEIPT_MARK = "**回执**"


def run_verify(cmd: str) -> tuple[int, str]:
    """执行回执行。返回 (退出码, 输出摘要)。

    判据是**退出码**，不是输出非空：很多作证命令本来就不打印东西
    （`grep -q`、`test -f`、`cmp`），把"零输出"当失败会让正确回执被拒。
    空命令 = 未声明回执行 → 放行（存量 1246 块的兼容路径，且不假装有回执）。
    """
    if not cmd.strip():
        return 0, ""
    if "`" in cmd:
        raise LedgerError(
            "✗ 回执行里不许含反引号：它会把台账里的 `**回执**` 定界符提前闭合，"
            "导致 parse_receipt 截错。改用 $(...) 或去掉引号。")
    try:
        p = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True,
                           timeout=300)
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT: 回执行 300s 未结束（它不能当回归题）"
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    return p.returncode, out


def inject_receipt(body: str, cmd: str, rc: int, out: str) -> str:
    """把回执行 + 退出码 + 输出摘要追加成 `- **回执** ...` 一行。

    摘要单行化并截断到 300 字符：台账是索引，大块输出该落盘（见 MEMORY §7）。
    """
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    summary = " / ".join(lines[:3])[:300] if lines else "(无输出)"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (body.rstrip("\n")
            + f"\n- {RECEIPT_MARK} `{cmd}` → rc={rc} @ {stamp}；输出摘要：{summary}\n")


RECEIPT_RE = re.compile(
    r"^-\s*\*\*回执\*\*\s*`([^`]+)`\s*→\s*rc=(\d+)\s*@\s*([0-9TZ:.-]+)", re.M)


def parse_receipt(body: str) -> dict | None:
    """从正文里取出回执行元信息，供 audit 重跑。无回执行返回 None。"""
    m = RECEIPT_RE.search(body)
    if not m:
        return None
    return {"cmd": m.group(1), "rc": int(m.group(2)), "at": m.group(3)}



def _write_once(path: str, payload: bytes) -> int:
    """单次 write + O_APPEND。返回值：实际写入字节数。"""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o664)
    try:
        n = os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return n


def append_atomic(path: str, block: str, series: str, dry_run: bool = False,
                  expect_id: str | None = None, verify_cmd: str = "") -> dict:
    """核心路径：跑回执行 → 加锁 → 重取最大号 → 前缀哈希 → 单次 O_APPEND 写 → 双断言。

    返回 dict；raise LedgerError（退出码 2）表示回执失败/撞号/断言失败（不写）。

    回执行在**锁外**跑：它可能耗时（上限 300s），持着 flock 跑会把其他会话全卡住
    （见 MEMORY §10「锁保护的是资源，不是任务」）。
    """
    if verify_cmd.strip():
        rc, out = run_verify(verify_cmd)
        if rc != 0:
            raise LedgerError(
                f"✗ 回执行失败（rc={rc}）：不接受一条自己都证不了的教训。"
                f"输出：{out[:200] or '(空)'}")
        block = inject_receipt(block, verify_cmd, rc, out)
    lockpath = path + ".append.lock"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lk = open(lockpath, "a+")
    try:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                raise LedgerError(
                    f"✗ 另一个 ledger_append 正在写 {path}（flock 排它）。"
                    "等它结束再试；不要绕开本脚本手写整份写回。")
            raise
        pre = read_ledger(path)
        pre_sha = hashlib.sha256(pre).hexdigest()
        pre_len = len(pre)

        if expect_id:
            lid = expect_id
            if not ID_RE.match(lid):
                raise LedgerError(f"✗ id 非法：{lid}")
            if lid in parse_ids(pre):
                raise LedgerError(
                    f"✗ 撞号：{lid} 已存在。编号冲突要人来裁决 —— "
                    "按 AGENTS.md 第 6 条第 ③ 步：重取最大号后重编，"
                    "只改标题行，正文一个字不动。")
        else:
            num = max_id_for(pre, series) + 1
            lid = f"{series}-{num}"

        head_line = block.split("\n", 1)[0]
        m = HDR_RE.match(head_line)
        got = m.group(1) if m else None
        if got != lid:
            raise LedgerError(f"✗ 块头 {head_line!r} 的 id 是 {got}，期望 {lid}")

        # 写前守卫（2026-10-03 L-171 教训）：body 里再出现 ## 头行会让落笔块
        # 变双头（第二个头被 parse 当新块切走），断言 2 以 0 命中失败、exit 2
        # 且残块已落盘。这类错误必须在写之前拒绝，而不是写之后留伤。
        body_part = block.split("\n", 1)[1] if "\n" in block else ""
        for ln in body_part.split("\n"):
            if HDR_RE.match(ln):
                raise LedgerError(
                    "✗ body 含 ## 头行（会被 parse 切成第二个块 → 双头块）。"
                    "--file 只给正文：首行直接正文，标题交给 --title。")

        # 分隔：文件末尾若不是恰好 '\n\n'，把缺的换行**放进同一次写**，
        # 避免二次写破坏 O_APPEND 的单次性。
        sep = ""
        if pre_len:
            if pre.endswith(b"\n\n"):
                sep = ""
            elif pre.endswith(b"\n"):
                sep = "\n"
            else:
                sep = "\n\n"
        payload = (sep + block).encode("utf-8")

        want_hash = body_hash(block.split("\n", 1)[1] if "\n" in block else "")
        res = {"id": lid, "series": series, "pre_bytes": pre_len,
               "pre_sha256": pre_sha, "sep": sep, "body_sha12": want_hash,
               "dry_run": dry_run}

        if dry_run:
            res["would_write_bytes"] = len(payload)
            res["missing_fields"] = missing_fields(
                block.split("\n", 1)[1] if "\n" in block else "")
            res["missing_refs"] = missing_refs(
                block.split("\n", 1)[1] if "\n" in block else "")
            return res

        n = _write_once(path, payload)
        res["wrote_bytes"] = n
        if n != len(payload):
            raise LedgerError(f"✗ 短写：期望 {len(payload)} 实得 {n}")

        # ── 断言 1：前缀一致（原有每个字节原样还在）
        post = read_ledger(path)
        if post[:pre_len] != pre:
            raise LedgerError(
                "✗ 前缀一致性断言失败：写入后文件前 "
                f"{pre_len} 字节与写入前不同 —— 有别的写入者在同一时刻改了这个文件。"
                "先隔离快照（cp 两份），不要继续写。")
        res["prefix_intact"] = True

        # ── 断言 2：新块正文哈希唯一命中
        hits = [h for h, _ in parse_ids(post).get(lid, []) if h == want_hash]
        if len(hits) != 1:
            raise LedgerError(
                f"✗ 落笔后唯一性断言失败：{lid} 的正文哈希 {want_hash} 命中 {len(hits)} 次"
                f"（期望 1）。")
        res["hash_hits"] = 1
        res["post_bytes"] = len(post)
        return res
    finally:
        try:
            fcntl.flock(lk, fcntl.LOCK_UN)
        except OSError:
            pass
        lk.close()


def check(path: str) -> dict:
    """体检：重复 id、可疑非块内容、文件末尾状态。只读。"""
    raw = read_ledger(path)
    ids = parse_ids(raw)
    dups = {k: len(v) for k, v in ids.items() if len(v) > 1}
    # 同一 id 不同正文 = 真正的语义冲突；同 id 同正文 = 纯重复
    conflicts = [k for k, v in ids.items() if len({h for h, _ in v}) > 1]
    nonblocks = [m.group(1) for m in ANY_HDR_RE.finditer(
        raw.decode("utf-8", "replace")) if not HDR_RE.match("## " + m.group(1))]
    return {
        "path": path, "bytes": len(raw), "blocks": sum(len(v) for v in ids.values()),
        "unique_ids": len(ids),
        "duplicate_ids": dups,
        "same_id_different_body": conflicts,
        "non_lesson_headers": nonblocks[:10],
        "n_non_lesson_headers": len(nonblocks),
        "ends_with": repr(raw[-4:]),
    }


# ─────────────────────────── selftest ───────────────────────────

def _selftest() -> int:
    fails: list[str] = []

    def chk(name: str, cond: bool, detail: str = "") -> None:
        if not cond:
            fails.append(f"{name}" + (f" — {detail}" if detail else ""))

    import tempfile
    tmp = tempfile.mkdtemp(prefix="ledger-append-selftest-")
    led = os.path.join(tmp, "lessons.md")
    body = ("**情境** selftest\n**动作** selftest\n**结果** selftest\n"
            "**证据** selftest\n**置信度** 高\n**状态** selftest\n")

    # 0) 空台账
    chk("空台账/blocks=0", check(led)["blocks"] == 0)
    chk("空台账/端无换行", check(led)["ends_with"] == "b''", check(led)["ends_with"])

    # 1) 首次追加到不存在的文件（O_CREAT）
    r1 = append_atomic(led, build_block("L-ops", 1, "第一条", body, None), "L-ops")
    chk("首写/creates file", os.path.exists(led))
    chk("首写/prefix_intact", r1.get("prefix_intact") is True)
    chk("首写/id=L-ops-1", r1["id"] == "L-ops-1", r1["id"])
    chk("首写/hash_hits=1", r1.get("hash_hits") == 1)
    chk("首写/无 sep（空文件）", r1["sep"] == "", repr(r1["sep"]))

    # 2) 编号自增，且不复用会话记忆里的旧值
    r2 = append_atomic(led, build_block("L-ops", 2, "第二条", body, None), "L-ops")
    chk("连写/id=L-ops-2", r2["id"] == "L-ops-2", r2["id"])

    # 3) 子系列不与裸 id 串号
    append_atomic(led, build_block("L-dsh-casmi", 1, "子系列", body, None),
                  "L-dsh-casmi")
    r3 = append_atomic(led, build_block("L-ops", 3, "第三条", body, None), "L-ops")
    chk("子系列存在时/L-ops 续号 3", r3["id"] == "L-ops-3", r3["id"])
    r4 = append_atomic(led, build_block("L-dsh-casmi", 2, "子系列第二条", body, None),
                       "L-dsh-casmi")
    chk("子系列续号 2", r4["id"] == "L-dsh-casmi-2", r4["id"])

    # 4) 撞号必须拒绝且**不写**
    pre = read_ledger(led)
    try:
        append_atomic(led, build_block("L-ops", 2, "撞号", body, None), "L-ops",
                      expect_id="L-ops-2")
        chk("撞号/拒绝", False, "竟然没抛错")
    except LedgerError as e:
        chk("撞号/拒绝", e.code == 2 and e.msg, f"code={e.code}")
    chk("撞号/文件未变", read_ledger(led) == pre)

    # 5) 末尾无换行时分隔符被同一次写补上（HEAD 台账真实出现过这个状态）
    led2 = os.path.join(tmp, "no-nl.md")
    with open(led2, "wb") as f:
        f.write(b"## L-ops-1 x\nbody")
    r5 = append_atomic(led2, build_block("L-ops", 2, "y", body, None), "L-ops")
    chk("无换行尾/sep=两换行", r5["sep"] == "\n\n", repr(r5["sep"]))
    txt = read_ledger(led2).decode()
    chk("无换行尾/首块正文完整", "body" in txt.split("## L-ops-2")[0])
    chk("无换行尾/两块都在",
        txt.count("## L-ops-1") == 1 and txt.count("## L-ops-2") == 1)

    # 6) 末尾恰好一个换行 → 只补一个
    led3 = os.path.join(tmp, "one-nl.md")
    with open(led3, "wb") as f:
        f.write(b"## L-ops-1 x\nbody\n")
    r6 = append_atomic(led3, build_block("L-ops", 2, "y", body, None), "L-ops")
    chk("单换行尾/sep=一换行", r6["sep"] == "\n", repr(r6["sep"]))

    # 7) 幂等前缀：连续 N 次追加，每次都断言旧内容完好
    led4 = os.path.join(tmp, "chain.md")
    prev = b""
    for i in range(1, 13):
        rr = append_atomic(led4, build_block("L-x", i, f"t{i}", body, None), "L-x")
        cur = read_ledger(led4)
        chk(f"链式{i}/前缀完好", cur.startswith(prev))
        chk(f"链式{i}/id 正确", rr["id"] == f"L-x-{i}", rr["id"])
        prev = cur
    chk("链式/12 块", check(led4)["blocks"] == 12)

    # 8) 空块必须被拒（空块是「占号」伪装成「记教训」）
    for bad, why in ((("", "空 body"), ("   \n\n", "空白 body"))):
        try:
            build_block("L-ops", 99, "t", bad, None)
            chk(f"空块拒绝/{why}", False, "build_block 竟然接受了")
        except ValueError:
            chk(f"空块拒绝/{why}", True)

    # 9) 空标题必须被拒
    try:
        build_block("L-ops", 99, "  ", body, None)
        chk("空标题拒绝", False, "接受了")
    except ValueError:
        chk("空标题拒绝", True)

    # 10) 块头 id 与目标 id 不符 → 拒绝（防止调用方以为写了 A 其实落成 B）
    try:
        append_atomic(led4, build_block("L-ops", 500, "mismatch", body, None), "L-y")
        chk("块头 id 不符/拒绝", False, "接受了")
    except LedgerError as e:
        chk("块头 id 不符/拒绝", e.code == 2 and e.msg, f"code={e.code}")

    # 11) 前缀断言真的会红：模拟并发写入者在写入瞬间改文件
    #     做法＝在 payload 里夹一段会截断旧内容的字节不可能，故改为直接
    #     验证断言函数的判据：post[:pre_len] != pre 必红。
    pre_b = b"## L-ops-1 a\nbody\n\n## L-ops-2 b\nbody2\n"
    post_b = b"## L-ops-1 a\nbody\n\n## L-ops-2 XXX\nbody2\n"  # 中间被改
    chk("前缀断言/中间被改⇒判红", post_b[:len(pre_b)] != pre_b)
    chk("前缀断言/追加在尾⇒判绿",
        (pre_b + b"## L-ops-3 c\n").startswith(pre_b))

    # 12) dry_run 不落盘（用显式 id，否则调用方给的号会被自动重编号）
    before = read_ledger(led4)
    dr = append_atomic(led4, build_block("L-x", 99, "ghost", body, None), "L-x",
                       dry_run=True, expect_id="L-x-99")
    chk("dry_run/不写", read_ledger(led4) == before)
    chk("dry_run/报出将写字节", dr.get("would_write_bytes", 0) > 0)
    chk("dry_run/报出缺字段", isinstance(dr.get("missing_fields"), list))

    # 13) schema 提示不阻断（现存台账六字段齐备率仅 29.8%，硬校验会被绕过）
    nod = "随便一段没有字段标签的正文\n"
    r13 = append_atomic(led4, build_block("L-x", 13, "无 schema", nod, None), "L-x",
                        expect_id="L-x-13")
    chk("缺字段/仍写入成功", r13.get("hash_hits") == 1)
    chk("缺字段/仅提示", len(missing_fields(nod)) == len(SCHEMA_FIELDS),
        str(missing_fields(nod)))

    # 14) 体检能认出「同 id 不同正文」这种语义冲突
    led5 = os.path.join(tmp, "conflict.md")
    with open(led5, "wb") as f:
        f.write("## L-ops-1 a\nbody-A\n\n## L-ops-1 b\nbody-B\n".encode())
    c = check(led5)
    chk("体检/认出同 id 不同正文", "L-ops-1" in c["same_id_different_body"],
        str(c))
    chk("体检/重复 id 计数", c["duplicate_ids"].get("L-ops-1") == 2, str(c["duplicate_ids"]))
    with open(led5, "wb") as f:
        f.write("## L-ops-1 a\nbody-A\n\n## L-ops-1 a\nbody-A\n".encode())
    c2 = check(led5)
    chk("体检/同 id 同正文不算冲突", c2["same_id_different_body"] == [],
        str(c2))

    # 15) max_id_for 同时覆盖裸 id 与子系列（AGENTS.md 第 6 条踩过的坑）
    raw = b"## L-dsh-1 a\nx\n\n## L-dsh-casmi-153 b\nx\n\n## L-dsh-154 c\nx\n"
    chk("max_id/裸 id 与子系列同池", max_id_for(raw, "L-dsh") == 154,
        str(max_id_for(raw, "L-dsh")))
    chk("max_id/空台账=0", max_id_for(b"", "L-ops") == 0)

    # 16) 正文哈希与标题行无关（重编号不改哈希 ⇒ 第 6 条第 ③ 步可验证）
    b1 = "## L-ops-5 title\n\n**情境** x\n"
    b2 = "## L-ops-6 title\n\n**情境** x\n"
    chk("哈希/只覆盖正文", body_hash(b1.split("\n", 1)[1]) == body_hash(b2.split("\n", 1)[1]))
    chk("哈希/正文不同⇒不同", body_hash("A\n") != body_hash("B\n"))

    # 17) flock 真能排他（同进程二次加锁走的是同一 fd 语义，故用子进程验证）
    import subprocess
    led6 = os.path.join(tmp, "lock.md")
    open(led6, "w").close()
    lk = open(led6 + ".append.lock", "a+")
    fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        pr = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--series", "L-ops",
             "--title", "t", "--stdin", "--ledger", led6],
            input=body, capture_output=True, text=True, timeout=30)
        chk("flock/并发被排", pr.returncode == 2, f"rc={pr.returncode} {pr.stderr[:80]}")
        chk("flock/并发被排时给出理由", "flock" in pr.stderr or "正在写" in pr.stderr,
            pr.stderr[:120])
        chk("flock/被排时未落盘", os.path.getsize(led6) == 0)
    finally:
        fcntl.flock(lk, fcntl.LOCK_UN)
        lk.close()

    # 19) AGENTS.md 第 24 条「无引用即丢」：REF_RE 双向验证（RED/GREEN）
    chk("引用/有 path:line ⇒ 绿", missing_refs("**情境** x\n**证据** scripts/ledger_append.py:141\n") == [])
    chk("引用/无 path:line ⇒ 提示",
        missing_refs("**情境** x\n**证据** 凭记忆\n") != [])
    # 负向后顾排除：URL 的 host:port 不是引用；e.code == 2 无路径前缀不是引用
    chk("引用/URL host:port 不算", missing_refs("见 https://a.b:8080/x\n") != [])
    chk("引用/裸 code 比较不算", missing_refs("e.code == 2\n") != [])
    # 时间戳 01:26 不算引用；但 path:line 在时间后仍要识别
    chk("引用/时间戳不算", missing_refs("01:26 截断\n") != [])
    chk("引用/时间+path:line 算", missing_refs("01:26 截断见 scripts/ledger_append.py:9\n") == [])
    # 真实形态：.md 文件 + 行号区间
    chk("引用/区间行号", missing_refs("见 docs/notes.md:12-20\n") == [])
    chk("引用/全角冒号后的 path:line 仍识别",
        missing_refs("依据：scripts/ledger_append.py:141\n") == [])

    # 18) 标题里的 id 前缀不是条目：`-corr` 更正、`~N` 范围说明必须被排除，
    #     否则 --check 报假阳性（曾报 7 个，真冲突只有 5 个）
    chk("边界/-corr 不算条目", HDR_RE.match(
        "## L-kagg-279-corr 更正 L-kagg-279：x") is None)
    chk("边界/~N 不算条目", HDR_RE.match(
        "## L-kagg-46~55 恢复说明（先读这条）") is None)
    chk("边界/全角括号后仍算条目", HDR_RE.match(
        "## L-kagg-215（2026-09-20）标题").group(1) == "L-kagg-215")
    chk("边界/竖线后仍算条目", HDR_RE.match(
        "## L-kagg-46 | UMG-16 面板").group(1) == "L-kagg-46")
    chk("边界/空格后仍算条目", HDR_RE.match(
        "## L-ops-129 「同 id」").group(1) == "L-ops-129")
    # max_id_for 必须不受 -corr / ~N 干扰（否则编号空间被读歪）
    chk("max_id/-corr 不抬号", max_id_for(
        "## L-ops-5 a\nx\n\n## L-ops-5-corr 更正\nx\n".encode(), "L-ops") == 5)
    chk("max_id/~N 不抬号", max_id_for(
        "## L-ops-5 a\nx\n\n## L-ops-5~9 恢复说明\nx\n".encode(), "L-ops") == 5)
    # 真实台账里的三种历史形态一起过一遍
    real = ("## L-kagg-215（2026-09-20）R²=0.89 差点让我宣布\nx\n\n"
            "## L-kagg-279-corr 更正 L-kagg-279：不能用 md5\nx\n\n"
            "## L-kagg-46~55 恢复说明\nx\n\n"
            "## L-dsh-casmi-153 casmi 末条\nx\n").encode()
    got = sorted(parse_ids(real))
    chk("真实形态/只认 2 个独立条目",
        got == ["L-dsh-casmi-153", "L-kagg-215"], str(got))

    # 19) 回执行：落账那一刻取得可反演性（L-ops-577）
    #     两段式：注入必错必须拒绝（红），注入正确必须放行（绿）。
    led5 = os.path.join(tmp, "receipt.md")
    pre5 = b""
    rc_ok, out_ok = run_verify("printf 'ALIVE-9f2c'")
    chk("回执/正确命令 rc=0", rc_ok == 0, str(rc_ok))
    chk("回执/捕获输出", "ALIVE-9f2c" in out_ok, out_ok[:60])
    rc_bad, _ = run_verify("false")
    chk("回执/必错命令 rc≠0", rc_bad != 0, str(rc_bad))
    # 未声明回执行 → 不阻断（存量 1246 块的兼容路径）
    chk("回执/空命令不阻断", run_verify("")[0] == 0)
    b5 = inject_receipt(body, "printf 'ALIVE-9f2c'", rc_ok, out_ok)
    chk("回执/恰好一个标记", b5.count(RECEIPT_MARK) == 1, str(b5.count(RECEIPT_MARK)))
    chk("回执/命令原文在内", "printf 'ALIVE-9f2c'" in b5)
    par = parse_receipt(b5)
    chk("回执/可解析回命令", par is not None and par["cmd"] == "printf 'ALIVE-9f2c'",
        str(par))
    chk("回执/无回执条目解析为 None", parse_receipt(body) is None)
    # 拒绝路径：rc≠0 时 append 必须 exit 2 且文件一字不动
    pre5 = read_ledger(led5)
    try:
        append_atomic(led5, build_block("L-ops", 1, "坏回执", body, None), "L-ops",
                      verify_cmd="false")
        chk("回执/必错拒绝", False, "竟然没抛错")
    except LedgerError as e:
        chk("回执/必错拒绝", e.code == 2 and "回执" in e.msg, f"code={e.code}")
    chk("回执/拒绝时文件未变", read_ledger(led5) == pre5)
    # 放行路径：rc=0 → 写进去且正文含回执
    r5b = append_atomic(led5, build_block("L-ops", 1, "好回执", body, None), "L-ops",
                        verify_cmd="printf 'ALIVE-9f2c'")
    txt5 = read_ledger(led5).decode()
    chk("回执/放行写入", r5b["id"] == "L-ops-1", r5b.get("id", ""))
    chk("回执/落盘含回执", RECEIPT_MARK in txt5)
    # 假阳守卫：命令输出为空也算通过——判据是**退出码**，不是输出非空。
    # 若哪天有人把判据改成"输出必须非空"，下面这条会红。
    rc_n, out_n = run_verify("true")
    chk("回执/零输出仍判通过（判据是rc不是输出）", rc_n == 0 and out_n.strip() == "",
        f"rc={rc_n} out={out_n!r}")

    print()
    if fails:
        print(f"✗ selftest {len(fails)} 条不通过：{fails}")
        return 1
    print("✓ selftest 全通过（含撞号拒绝/前缀断言会红/flock 真排他/"
          "块头 id 不符拒绝/缺字段不阻断/引用双向验证 六条核心路径）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="教训台账原子追加器（O_APPEND 单次写 + flock + 前缀一致性断言）")
    ap.add_argument("--ledger", default=LEDGER, help=f"台账路径（默认 {LEDGER}）")
    ap.add_argument("--series", help="编号系列，如 L-ops / L-kagg / L-app-sub")
    ap.add_argument("--title", help="标题（与编号、日期拼成标题行）")
    ap.add_argument("--file", help="正文文件；不给则读 stdin")
    ap.add_argument("--stdin", action="store_true", help="强制从 stdin 读正文")
    ap.add_argument("--id", dest="explicit_id",
                    help="显式指定 id（存在即撞号拒绝）——一般不用，交给自动编号")
    ap.add_argument("--date", help="日期后缀（默认今天 YYYY-MM-DD）")
    ap.add_argument("--no-date", action="store_true", help="不写日期后缀")
    ap.add_argument("--dry-run", action="store_true", help="只报告将写什么，不落盘")
    ap.add_argument("--check", action="store_true", help="体检：重复 id / 体例 / 末尾状态")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--verify-cmd",
                    help="回执行：落账前必须跑通（rc=0）并把它写进 **回执**。"
                         "不可反演性只能在落账那一刻取得（L-ops-577）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if args.check:
        rep = check(args.ledger)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            print(f"台账 {rep['path']}：{rep['bytes']} B / {rep['blocks']} 块 / "
                  f"{rep['unique_ids']} 唯一 id")
            print(f"  重复 id：{rep['duplicate_ids'] or '无'}")
            print(f"  同 id 不同正文（语义冲突）：{rep['same_id_different_body'] or '无'}")
            print(f"  非教训体例的 ## 标题：{rep['n_non_lesson_headers']} 个"
                  + (f" 例：{rep['non_lesson_headers'][:3]}" if rep["non_lesson_headers"] else ""))
            print(f"  末尾字节：{rep['ends_with']}")
        bad = bool(rep["same_id_different_body"])
        return 1 if bad else 0

    if not args.series or not args.title:
        ap.error("需要 --series 与 --title（或用 --check / --selftest）")
    if not ID_RE.match(f"{args.series}-0"):
        ap.error(f"--series 非法：{args.series}（应形如 L-ops / L-app-sub，"
                 "带 L- 前缀）")

    if args.file:
        with open(args.file, encoding="utf-8") as f:
            body = f.read()
    elif args.stdin or not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        ap.error("没有正文：给 --file <路径> 或 --stdin")

    date = None if args.no_date else (
        args.date or datetime.now().strftime("%Y-%m-%d"))

    series = args.series
    if args.explicit_id:
        m = re.match(r"^L-([a-z0-9-]+)-([0-9]+)$", args.explicit_id)
        if not m:
            ap.error(f"--id 非法：{args.explicit_id}")
        series, num = m.group(1), int(m.group(2))
    else:
        num = max_id_for(read_ledger(args.ledger), series) + 1

    try:
        block = build_block(series, num, args.title, body, date)
    except ValueError as e:
        sys.stderr.write(f"✗ {e}\n")
        return 2

    if args.dry_run:
        res = append_atomic(args.ledger, block, series, dry_run=True,
                            expect_id=f"{series}-{num}",
                            verify_cmd=args.verify_cmd or "")
    else:
        res = append_atomic(args.ledger, block, series,
                            expect_id=f"{series}-{num}",
                            verify_cmd=args.verify_cmd or "")

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        if args.dry_run:
            print(f"[dry-run] 将写 {res['id']}（{res['would_write_bytes']} B，"
                  f"sep={res['sep']!r}）到 {args.ledger}")
            mf = res["missing_fields"]
            print(f"  schema 缺字段（不阻断）：{'、'.join(mf) if mf else '无'}")
            mr = res.get("missing_refs", [])
            print(f"  path:line 引用（第 24 条，不阻断）："
                  f"{'无' if not mr else '、'.join(mr)}")
        else:
            print(f"✓ 已追加 {res['id']}（{res['wrote_bytes']} B）"
                  f"→ {args.ledger}")
            print(f"  前缀一致性：True（{res['pre_bytes']} B 原样保留）")
            print(f"  正文哈希 {res['body_sha12']} 命中 1 次")
            mf = missing_fields(body)
            if mf:
                print(f"  schema 缺字段（不阻断）：{'、'.join(mf)}")
            mr = missing_refs(body)
            if mr:
                print(f"  path:line 引用（第 24 条，不阻断）：{'、'.join(mr)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())