# 信息抓取推送

> 确定性爬虫 + LLM 语义筛选 + 推送

```
多源采集 → 结构化 → 预处理 → 多级过滤 → LLM 按意图精筛 → 幂等推送
```

## 特性

- **配置驱动**：意图、Tag、阈值、源地址、截断长度、重试次数全部在 YAML，代码只读配置。
- **三级源适配**：HTML 列表（XPath/CSS 手写或 AI 生成）、JSON 接口、RSS/Atom。
- **AI 生成解析规则**：只填 URL 也能跑。规则带页面指纹缓存、形态校验、真实页面试抽、连续失效自愈。
- **六道筛选闸门**：列表先筛 → URL 去重 → 内容近重复 → 硬规则 → 粗召回 → LLM 精筛，成本递增、先召回后精度。
- **服务端不采信 LLM**：模型返回的 `is_relevant` 不被读取，服务端按冲突策略重新裁决。
- **失败不记账**：LLM 调用失败、解析失败的内容不写去重记录，下一轮自动重试。
- **每层可降级**：AI 不可用有启发式，详情抓不到用摘要，通知失败只记日志。
- **三层推送幂等**：进程内集合 + `push_log` 表 + 接口返回码。
- **结论可追溯**：`filter_results.raw_response` 存原始返回，`dropped_samples` 存丢弃阶段与原因。

## 快速开始

**环境**：Python 3.10+（开发使用 3.11），Windows / Linux / macOS 均可。

```bash
git clone <你的仓库地址> && cd InformationFiltering

# 1. 建虚拟环境并安装依赖
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt

# 2. 配置密钥（二选一，也可以直接填 config/app.yaml）
export DEEPSEEK_API_KEY="sk-..."     # Windows: set DEEPSEEK_API_KEY=sk-...
export WECOM_SENDKEY="..."           # Windows: set WECOM_SENDKEY=...

# 3. 打开一个源
#    编辑 config/sources.yaml，把 auto-news 的 enabled 改成 true，url 填目标列表页

# 4. 先校验配置，再干跑
python main.py --check-config
python main.py --dry-run --limit 5

# 5. 正式运行
python main.py
```

可选增强（不装也能跑，代码自动降级）：

| 包 | 作用 | 未安装时 |
|---|---|---|
| `trafilatura` | 正文抽取首选 | 回退 lxml 自清洗 |
| `jieba` | 中文分词 | 回退「ASCII 词 + 中文二元组」 |
| `selenium` | JS 站兜底渲染 | 关闭 Selenium 兜底，仅用 HTTP |

## 常用命令

| 命令 | 作用 |
|---|---|
| `python main.py` | 正式运行：抓取 → 筛选 → 推送 |
| `python main.py --dry-run` | 只跑到粗筛，不调用 LLM、不推送 |
| `python main.py --check-config` | 只校验配置，不联网 |
| `python main.py --limit 5` | 本次最多处理 5 条原始条目 |
| `python main.py --source auto-news` | 只跑指定源（可重复） |
| `python main.py --no-push` | 精筛后不推送 |
| `python main.py --setup-source <URL>` | 只让 AI 生成并缓存某站的解析规则 |
| `python main.py --verbose` | 日志同时打印到终端 |
| `python main.py --config a.yaml --intent b.yaml --sources c.yaml` | 覆盖默认配置路径 |

`run_pipeline.py` 与 `main.py` 完全等价，保留旧入口的习惯用法。

## 配置三件套

| 文件 | 管什么 | 换主题 | 换站点 |
|---|---|---|---|
| `config/filter_intent.yaml` | 筛选意图、Tag、冲突策略、召回阈值 | ✅ 只改这里 | — |
| `config/app.yaml` | LLM、爬虫、去重、存储、推送、通知 | 偶尔调阈值 | 偶尔调超时/代理 |
| `config/sources.yaml` | 源地址与解析规则 | — | ✅ 只改这里 |

仓库里的 `config/app.yaml` 密钥字段默认为空串，通过 `api_key_env` / `sendkey_env` 读环境变量，**不要把密钥提交进仓库**。

想换一个筛选主题，最小改动就是：

```yaml
# config/filter_intent.yaml
intent:
  description: |
    我关注与「AI Agent 工程化落地」相关的内容，特别关注：
    - 多智能体协作、工具调用、提示词工程、评测与可观测性；
    - 有实操细节、代码示例或生产环境经验的分享。
    不要：融资新闻、产品发布会通稿、标题党、纯概念科普、课程种草、招聘信息。
```

## 目录结构

```text
InformationFiltering/
├── config/
│   ├── app.yaml                 # 运行时配置：LLM / 爬虫 / 去重 / 存储 / 推送 / 通知
│   ├── filter_intent.yaml       # 筛选意图与 Tag
│   ├── sources.yaml             # 源地址与解析规则
│   └── prompts/                 # Prompt 模板与冲突策略措辞
├── crawler/                     # 采集层：HTTP、抽取、Feed、AI 适配、健康检查
├── pipeline/                    # 处理层：预处理、去重、召回、精筛、存储、推送、编排
├── models.py                    # 全链路数据结构（RawItem / ArticleItem / FilterResult ...）
├── settings.py                  # YAML → 强类型配置，含默认值与校验
├── main.py / run_pipeline.py    # 入口
├── send_wcom_msg.py             # 企业微信 send-post 封装
├── log_recording.py             # 按月轮转的日志
├── doc/doc.md                   # 设计文档（架构、参数、调优、排障）
└── data/ logs/ .tmp/            # 运行时产物，已被 .gitignore 排除
```

## 运行产物

| 路径 | 内容 |
|---|---|
| `data/state.db` | sqlite：`seen_urls`、`content_fingerprints`、`filter_results`、`dropped_samples`、`push_log` |
| `data/source_profiles.json` | AI 生成的解析规则缓存（可随时删除以触发重建） |
| `logs/YYYY-MM.log` | 按月日志 |
| `.tmp/` | 原子写入的暂存目录 |

这四个路径都不会进版本库。

## 文档

| 文档 | 内容 |
|---|---|
| [doc.md](doc/doc.md) | 完整设计文档：架构、六道闸门、参数含义、调优手册、排障 |

## 其他

- 只内置了企业微信 `send-post` 渠道，接口封装在 `send_wcom_msg.py`，接别的渠道需要自己加一层。
- 粗召回用的是本地 hashing-TF 向量，属词法级相似度，不理解同义词；语义判断交给 LLM。
- simhash 在短文本上不稳定，纯 RSS 摘要源容易误判近重复，文档里给了降阈值/关窗口的做法。
- AI 生成解析规则依赖页面结构，整站改版时会失效，需要等自愈或手动重生成。
- 单进程同步执行，没有并发抓取；靠计划任务定时跑，量级在「每天几百条」这个范围。

## 免责声明

本项目仅用于个人订阅与信息聚合。请遵守目标站点的 `robots.txt`、服务条款与当地法律法规，控制请求频率，不要用于商业转载或高频抓取。因使用本项目产生的一切后果由使用者自行承担。

## License

[MIT](LICENSE)
