#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh-recall —— 在本地会话记录里「回忆」：上次那事是怎么弄的？

【为什么需要】
  agent 的会话记录是可搜索性最差的格式：一堆 zstd 压缩的 jsonl，翻不动、grep 不了。
  于是每次问「上次那个脚本是怎么写的」，只能从头再讲一遍。
  本工具把它们压成**一页一个会话的事实卡**（标题 / 用户原话 / 末条回复 / 动过的文件 / 常用工具），
  之后用关键词秒查。零 LLM、零联网、不读内容之外的任何东西。

【命令】
  python recall.py scan                 # 增量扫描会话目录 → 事实卡
  python recall.py recall 关键词 [更多]  # 查（中文/英文都行）
  python recall.py index [天数]          # 生成 Markdown 索引
  python recall.py daily [YYYY-MM-DD]    # 当天素材（供写日记/周报）

【数据放哪】
  只读会话目录；写只写自己的 out 目录：
    DSH_SESSIONS  （默认 ~/.dsh/sessions）
    DSH_RECALL_HOME（默认 ~/.dsh-recall）

【依赖】
  pip install zstandard
"""
import datetime
import json
import os
import re
import sys

try:
    import zstandard
except ImportError:
    sys.exit("缺少依赖：pip install zstandard")

VERSION = "0.1.0"

SESS_DIR = os.environ.get("DSH_SESSIONS") or os.path.join(os.path.expanduser("~"), ".dsh", "sessions")
HOME = os.environ.get("DSH_RECALL_HOME") or os.path.join(os.path.expanduser("~"), ".dsh-recall")
OUT_DIR = HOME
STATE = os.path.join(OUT_DIR, ".state.json")
OUT = os.path.join(OUT_DIR, "sessions.jsonl")
INDEX = os.path.join(OUT_DIR, "00-index.md")

BT = chr(96)
PATH_RE = re.compile(r"[A-Za-z]:\\[^\"'<>|?*\r\n\t,;)\]}]*")
SKIP_USER_PREFIX = ("<system-reminder>", "Current runtime context")


def ws_decode(name):
    """会话目录名 → 工作区路径。DSH 把 `D:\\a\\b` 编码成 `D--a-b`；非 Windows 形态则原样返回。"""
    s = name.strip("-")
    s = re.sub(r"~([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)
    i = s.find("-")
    if i < 0 or not re.match(r"^[A-Za-z]$", s[:i]):
        return s
    return s[:i] + ":\\" + s[i + 1:].replace("-", "\\")


def pretty_path(p):
    """显示时把家目录折叠成 ~，别把用户名泄到索引里。"""
    try:
        home = os.path.expanduser("~")
        if p.lower().startswith(home.lower()):
            return "~" + p[len(home):]
    except Exception:
        pass
    return p


def clean_path(m):
    m = m.rstrip("\\").strip()
    if len(m) < 9 or len(m) > 220:
        return None
    if "\\\\" in m or BT in m or "\\n" in m:
        return None
    if m.count("\\") < 2:
        return None
    return m


def _strs(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strs(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strs(v)


def walk(sess_dir=None):
    root = sess_dir or SESS_DIR
    if not os.path.isdir(root):
        return
    for ws in sorted(os.listdir(root)):
        wsd = os.path.join(root, ws)
        if not os.path.isdir(wsd):
            continue
        for sd in sorted(os.listdir(wsd)):
            sdp = os.path.join(wsd, sd)
            if not os.path.isdir(sdp):
                continue
            for f in os.listdir(sdp):
                if f.startswith("session") and f.endswith(".zstd"):
                    yield ws, os.path.join(sdp, f)


def parse(path, ws):
    with open(path, "rb") as f:
        raw = zstandard.ZstdDecompressor().stream_reader(f).read()
    lines = [l for l in raw.decode("utf-8", "replace").splitlines() if l.strip()]
    rec = {"id": None, "ws": ws_decode(ws), "title": None, "start": None, "end": None,
           "user": [], "files": [], "tools": {}, "events": len(lines), "bytes": len(raw)}
    files, tools, seen = [], {}, set()
    for l in lines:
        try:
            o = json.loads(l)
        except ValueError:
            continue
        t = o.get("type")
        tm = o.get("time")
        if tm:
            if rec["start"] is None or tm < rec["start"]:
                rec["start"] = tm
            if rec["end"] is None or tm > rec["end"]:
                rec["end"] = tm
        if t == "session":
            rec["id"] = o.get("id")
            rec["cwd"] = o.get("cwd")
        elif t == "session/title" and not rec["title"]:
            rec["title"] = (o.get("data") or {}).get("title")
        elif t == "user/message":
            d = o.get("data") or {}
            if ((d.get("source") or {}).get("kind")) != "user":
                continue
            for c in d.get("content") or []:
                if c.get("type") != "text":
                    continue
                txt = (c.get("text") or "").strip()
                if not txt or txt.startswith(SKIP_USER_PREFIX):
                    continue
                rec["user"].append(txt[:200])
        elif t == "assistant/message":
            d = o.get("data") or {}
            msg = d.get("message") or {}
            parts = [c.get("text") or "" for c in (msg.get("content") or []) if c.get("type") == "text"]
            txt = "\n".join(x for x in parts if x).strip()
            if txt:
                rec["last_reply"] = txt[:400]
        elif t == "tool/call":
            d = o.get("data") or {}
            n = d.get("name")
            if n:
                tools[n] = tools.get(n, 0) + 1
            a = d.get("arguments") or ""
            try:
                payload = json.loads(a)
            except ValueError:
                payload = [a]
            for s in _strs(payload):
                for m in PATH_RE.findall(s):
                    cp = clean_path(m)
                    if cp and cp.lower() not in seen:
                        seen.add(cp.lower())
                        files.append(cp)
    rec["tools"] = dict(sorted(tools.items(), key=lambda kv: -kv[1])[:12])
    rec["files"] = files[:80]
    return rec


def cmd_scan(quiet=False):
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    allf = list(walk())
    todo = []
    for ws, p in allf:
        try:
            st = os.stat(p)
        except OSError:
            continue
        prev = state.get(p)
        if prev and prev.get("size") == st.st_size and prev.get("mtime") == int(st.st_mtime):
            continue
        todo.append((ws, p, st))
    existing = {}
    if os.path.exists(OUT):
        with open(OUT, "r", encoding="utf-8") as f:
            for l in f:
                try:
                    o = json.loads(l)
                    if o.get("id"):
                        existing[o["id"]] = o
                except ValueError:
                    pass
    fails = 0
    for ws, p, st in todo:
        try:
            r = parse(p, ws)
        except Exception as e:              # noqa: BLE001 —— 单个坏文件不该拖垮整轮
            fails += 1
            if fails <= 3:
                print("FAIL %s: %s" % (p, e))
            continue
        r["mtime"] = int(st.st_mtime)
        r["size"] = st.st_size
        r["path"] = p
        if r.get("id"):
            existing[r["id"]] = r
        state[p] = {"size": st.st_size, "mtime": int(st.st_mtime)}
    with open(OUT, "w", encoding="utf-8") as f:
        for o in existing.values():
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    if not quiet:
        print("sessions=%d to_parse=%d fails=%d total=%d"
              % (len(allf), len(todo), fails, len(existing)))


def _load():
    recs = []
    if not os.path.exists(OUT):
        return recs
    with open(OUT, "r", encoding="utf-8") as f:
        for l in f:
            try:
                recs.append(json.loads(l))
            except ValueError:
                pass
    return recs


def cmd_index(days=0):
    recs = [r for r in _load() if r.get("end") and r.get("user")]
    recs.sort(key=lambda r: -r["end"])
    cut = 0
    if days:
        cut = datetime.datetime.now().timestamp() * 1000 - days * 86400000
    rows = []
    for r in recs:
        if cut and r["end"] < cut:
            continue
        d = datetime.datetime.fromtimestamp(r["end"] / 1000).strftime("%m-%d %H:%M")
        title = (r.get("title") or "（无标题）").replace("|", "/")[:40]
        first = (r["user"][0] if r["user"] else "").replace("|", "/").replace("\n", " ")[:60]
        ws = pretty_path(r["ws"]).replace("|", "/")
        rows.append("| %s | %s | %s | %d | %s | %s |" % (
            d, ws, title, len(r["user"]), ",".join(list(r["tools"])[:3]), first))
    head = ["# 会话索引（机器生成 · 勿手改）", "",
            "> 由 " + BT + "recall.py index" + BT + " 生成；按最后活动倒序。",
            "> 事实卡本体：" + BT + "sessions.jsonl" + BT + "（含文件清单与工具统计）。", "",
            "| 最后活动 | 工作区 | 标题 | 用户句 | 主用工具 | 首句 |", "|---|---|---|---|---|---|"]
    with open(INDEX, "w", encoding="utf-8") as f:
        f.write("\n".join(head + rows) + "\n")
    print("index rows=%d -> %s" % (len(rows), INDEX))


def cmd_daily(day=""):
    r"""当天事实素材 → <out>/daily/<日期>.md。只是原料，不是日记。"""
    cmd_scan(quiet=True)
    day = day or datetime.date.today().isoformat()
    recs = []
    for r in _load():
        def d(k):
            return datetime.datetime.fromtimestamp((r.get(k) or 0) / 1000).strftime("%Y-%m-%d")
        if day == d("start") or day == d("end"):
            recs.append(r)
    recs.sort(key=lambda r: r.get("start") or 0)
    ddir = os.path.join(OUT_DIR, "daily")
    os.makedirs(ddir, exist_ok=True)
    path = os.path.join(ddir, day + ".md")
    L = ["# %s 事实素材（机器生成）" % day, "", "会话数：%d" % len(recs), ""]
    for r in recs:
        t1, t2 = sorted([r.get("start") or 0, r.get("end") or 0])
        d1 = datetime.datetime.fromtimestamp(t1 / 1000)
        d2 = datetime.datetime.fromtimestamp(t2 / 1000)
        cross = d1.date() != d2.date()
        fmt = "%m-%d %H:%M" if cross else "%H:%M"
        L += ["## %s-%s · %s" % (d1.strftime(fmt), d2.strftime(fmt), r.get("title") or "（无标题）"), ""]
        L.append("- 工作区：%s" % pretty_path(r.get("ws") or ""))
        L.append("- 用户句 %d 条" % len(r.get("user") or []))
        for u in (r.get("user") or [])[:6]:
            L.append("  - " + u.replace("\n", " ")[:160])
        fl = r.get("files") or []
        if fl:
            L.append("- 动过 %d 个路径，摘 8：" % len(fl))
            for x in fl[:8]:
                L.append("  - " + x)
        tl = list(r.get("tools") or {})
        if tl:
            L.append("- 主用工具：" + ", ".join(tl[:6]))
        L.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("daily %s sessions=%d -> %s" % (day, len(recs), path))


def cmd_recall(kw, n=6):
    kws = [k.lower() for k in re.split(r"[\s,，;；/]+", kw) if k]
    if not kws:
        print("用法: recall 关键词 [关键词2]")
        return
    cmd_scan(quiet=True)
    strict, loose = [], []
    for r in _load():
        if not r.get("user"):
            continue
        score, why = 0, []
        t = (r.get("title") or "").lower()
        lr = (r.get("last_reply") or "").lower()
        us = " ".join(r["user"]).lower()
        fs = " ".join(r.get("files") or []).lower()
        for k in kws:
            if k in t:
                score += 3
                why.append("标题")
            if k in us:
                score += 2
                why.append("用户句")
            if k in lr:
                score += 1
                why.append("末回")
            if k in fs:
                score += 2
                why.append("文件")
        if score >= 2:
            strict.append((score, r, sorted(set(why))))
        elif score:
            loose.append((score, r, sorted(set(why))))
    hits = strict or loose
    if not strict and loose:
        print("（无高置信命中，以下为低置信结果）")
    hits.sort(key=lambda x: (-x[0], -(x[1].get("end") or 0)))
    for score, r, why in hits[:n]:
        d = datetime.datetime.fromtimestamp((r.get("end") or 0) / 1000).strftime("%Y-%m-%d %H:%M")
        print("-" * 62)
        print("%s | %s | %s" % (d, pretty_path(r.get("ws") or ""), r.get("title") or "（无标题）"))
        print("  命中 %s (score=%d) · 用户句 %d" % ("/".join(why), score, len(r["user"])))
        if r["user"]:
            print("  首句 " + r["user"][0].replace("\n", " ")[:110])
        if r.get("last_reply"):
            print("  末回 " + r["last_reply"].replace("\n", " ")[:240])
        fl = [x for x in (r.get("files") or []) if any(k in x.lower() for k in kws)]
        if fl:
            print("  文件 " + " | ".join(fl[:3]))
        if r.get("path"):
            print("  记录 " + pretty_path(r["path"]))
    print("-" * 62)
    print("命中 %d 个会话，显示前 %d" % (len(hits), min(n, len(hits))))


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    arg = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if arg in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if arg == "--version":
        print("dsh-recall " + VERSION)
        return 0
    if not os.path.isdir(SESS_DIR):
        print("会话目录不存在：%s\n→ 用环境变量 DSH_SESSIONS 指定" % SESS_DIR)
        return 2
    if arg == "recall":
        cmd_recall(" ".join(sys.argv[2:]))
    elif arg == "daily":
        cmd_daily(sys.argv[2] if len(sys.argv) > 2 else "")
    elif arg == "index":
        cmd_index(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    elif arg == "scan":
        cmd_scan()
    else:
        print("未知命令：%s（可用：scan / recall / index / daily）" % arg)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
