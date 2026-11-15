#!/usr/bin/env bash
# 一条命令验证三个闸都是活的：三个 selftest + 三个示例判决。
# 每个都要求退出码符合预期，任何一步不符即整体失败。
set -uo pipefail
cd "$(dirname "$0")"

fail=0
say() { printf '\n=== %s ===\n' "$1"; }
want() {  # want <期望rc> <说明> <命令...>
  local exp="$1" desc="$2"; shift 2
  "$@" >/tmp/_ag.out 2>&1; local rc=$?
  if [ "$rc" = "$exp" ]; then echo "  ✓ $desc (rc=$rc)"
  else echo "  ✗ $desc：期望 rc=$exp 实得 rc=$rc"; tail -5 /tmp/_ag.out; fail=1; fi
}

say "闸一 ledger_append --selftest"
want 0 "六条核心路径" python3 ledger_append.py --selftest

say "闸三 retrieval_probes --selftest"
want 0 "三探针各红/绿两段" python3 retrieval_probes.py --selftest

say "闸二 eval_gate --selftest"
want 0 "七条反证" python3 eval_gate.py --selftest \
  --baseline examples/runs/baseline.jsonl --itemset examples/itemset.jsonl

say "示例：散弹作答涨的题必须被扣回（GREEN，有效涨分 0）"
want 0 "扣掉 1 题可疑通过" python3 eval_gate.py \
  --baseline examples/runs/baseline.jsonl --current examples/runs/current-spray.jsonl \
  --itemset examples/itemset.jsonl

say "示例：退化 3 题必须 RED"
want 1 "Δ-3" python3 eval_gate.py \
  --baseline examples/runs/baseline.jsonl --current examples/runs/current-regressed.jsonl \
  --itemset examples/itemset.jsonl

say "示例：自己比自己必须 GREEN"
want 0 "Δ0" python3 eval_gate.py \
  --baseline examples/runs/baseline.jsonl --current examples/runs/baseline.jsonl \
  --itemset examples/itemset.jsonl

say "示例：检索探针（一条已知上限不阻断）"
want 0 "新失败 0" python3 retrieval_probes.py \
  --root examples --probes examples/probes.json --min-chunks 6

printf '\n'
[ "$fail" = 0 ] && echo "ALL OK" || echo "FAILED"
exit "$fail"
