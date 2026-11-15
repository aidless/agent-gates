#!/usr/bin/env python3
"""集中所有"本机布局"相关的路径。

设计原则：**判据不进 config，路径才进**。阈值（YELLOW_SLACK / FP_WINDOW /
SYN_K …）留在各自的脚本里，因为那是可辩的判据，藏进配置就没人读了。

默认值 = 当前目录下的常见布局，因此 clone 下来 `python3 xxx.py --selftest`
不需要任何配置。换项目时用环境变量覆盖，或直接用各脚本的 CLI 参数。
"""
import os

ROOT = os.environ.get("AGENT_GATES_ROOT", ".")


def _p(*parts):
    return os.path.join(ROOT, *parts)


# 检索语料
MEMORY = os.environ.get("AGENT_GATES_MEMORY", _p("MEMORY.md"))
NOTES_DIR = os.environ.get("AGENT_GATES_NOTES", _p("notes"))
LEDGER = os.environ.get("AGENT_GATES_LEDGER", _p(".agent-memory/ledger/lessons.md"))

# 评测
ITEMSET = os.environ.get("AGENT_GATES_ITEMSET", _p("itemset.jsonl"))
RUNS_DIR = os.environ.get("AGENT_GATES_RUNS", _p("runs"))
BASELINE = os.environ.get("AGENT_GATES_BASELINE", os.path.join(RUNS_DIR, "baseline.jsonl"))
EVAL_RUN = os.environ.get("AGENT_GATES_EVAL_RUN", _p("eval_run.py"))

# 探针集（每个项目自己的主题，必须外置）
PROBES = os.environ.get("AGENT_GATES_PROBES", _p("probes.json"))
