# dsh-recall

在本地会话记录里「回忆」：上次那事是怎么弄的？

DeepSeek Harness 的会话存在 `~/.dsh/sessions/` 里，是一堆 **zstd 压缩的 jsonl**：
结构清楚、但 grep 不了、翻不动。于是每次想不起「上次那个脚本怎么写的」，只能让 agent 从头再讲一遍。

本工具把它们压成**一页一个会话的事实卡**，之后关键词秒查。零 LLM、零联网。

```
$ python recall.py recall 部署脚本

2026-10-28 21:15 | D:\work\api | 修 CI 部署脚本
  命中 文件/用户句 (score=4) · 用户句 7
  首句 部署脚本又挂了，看一下
  末回 根因是 curl 少了 -f，成功退出码掩盖了 404……
  文件 D:\work\api\deploy.sh | D:\work\api\.github\workflows\ci.yml
```

## 事实卡里有什么

每个会话一条记录：

| 字段 | 内容 |
|---|---|
| `title` | 会话标题 |
| `user[]` | 用户原话（前 200 字，最多若干条，已过滤 system-reminder） |
| `last_reply` | 末条助手回复（前 400 字） |
| `files[]` | 工具调用里出现过的**绝对路径**（最多 80 条） |
| `tools{}` | 工具使用次数 Top 12 |
| `start` / `end` | 起止时间 |
| `ws` / `cwd` | 工作区 |
| `path` | 原始会话文件路径（要翻全文时用） |

只存「一页摘要」，不存全文 —— 所以它检索快，但也**搜不到中间对话里的词**（见边界）。

## 用法

```bash
pip install zstandard

python recall.py scan                 # 增量扫描 → 事实卡
python recall.py recall 关键词         # 查
python recall.py index                # 生成 Markdown 索引（全部）
python recall.py index 7              # 只索引最近 7 天
python recall.py daily                # 今天的事实素材
python recall.py daily 2026-11-02     # 指定日期
```

`scan` 是**增量**的：按文件大小 + mtime 判断，没变就跳过。350 个会话首次全扫约十几秒，之后基本零成本。
`recall` 会先自动 `scan`，所以平时只用 `recall` 就够了。

### 打分配置

| 命中位置 | 分 |
|---|---|
| 标题 | 3 |
| 用户原话 | 2 |
| 文件路径 | 2 |
| 末条回复 | 1 |

≥2 分算高置信；只有末回命中（1 分）时降级为「低置信」并明确标注。多个关键词分数累加。

## 数据放哪

| 变量 | 默认 |
|---|---|
| `DSH_SESSIONS` | `~/.dsh/sessions`（**只读**） |
| `DSH_RECALL_HOME` | `~/.dsh-recall`（只写这里） |

产物：`sessions.jsonl`（事实卡）、`.state.json`（增量状态）、`00-index.md`、`daily/*.md`。

**不联网，不上传，不读会话目录以外的任何东西。** 删掉 `DSH_RECALL_HOME` 就等于什么都没发生。

## 怎么让 agent 自己用

把 `SKILL.md` 放进你的技能目录。之后问「上次那个是怎么弄的」，agent 会先 `recall` 一下再回答 ——
而不是自信地编一个。

## 边界（诚实版）

- **它不是全文检索。** 事实卡只留标题/用户原话/末条回复/文件路径。中间对话里的细节搜不到 ——
  那里请顺着 `path` 字段去读原文。
- **它靠启发式打分，不靠语义。** 「差不多的意思」不会命中，得用真的出现过的词。
  中文按子串匹配，不需要分词。
- **它只认 DSH 的会话格式**（`<workspace>/<session>/session*.jsonl.zstd`）。换了目录结构就得改 `walk()`/`parse()`。
- 会话文件被删除或轮转，旧事实卡仍留着 —— 需要时可整目录删掉重扫。

## License

MIT
