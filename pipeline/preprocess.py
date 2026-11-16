# -*- coding: utf-8 -*-
"""预处理：正文清洗、URL 归一化、64 位 simhash 指纹、分词与关键词抽取。

全部为纯本地实现，不产生外部调用：
    * 正文抽取优先用 trafilatura（若已安装），否则回退 lxml 自研清洗；
    * 分词优先用 jieba（若已安装），否则回退「ASCII 词 + 中文二元组」；
    * 指纹用 64 位 simhash，汉明距离阈值由 config/app.yaml 的 dedup.hamming_threshold 决定。
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:  # 可选依赖：装了就用，没装自动降级
    import trafilatura  # type: ignore
except Exception:  # pragma: no cover - 取决于部署环境
    trafilatura = None

try:  # 可选依赖：中文分词
    import jieba  # type: ignore

    # jieba 首次加载会把「Building prefix dict…」打到 stderr，统一压到 ERROR
    try:
        import logging

        jieba.setLogLevel(logging.ERROR)
    except Exception:  # pragma: no cover
        pass
    _JIEBA_READY = True
except Exception:  # pragma: no cover - 取决于部署环境
    jieba = None  # type: ignore
    _JIEBA_READY = False

from lxml import etree

# ---------------------------------------------------------------------------
# 通用词法资源（与筛选主题无关，属语言层面的基础数据）
# ---------------------------------------------------------------------------

_ASCII_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]{1,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_WS_RE = re.compile(r"[ \t\u3000]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")

_STOPWORDS = frozenset(
    """
    a an the and or but if then than so as at by for from in into of on onto over to up with
    is are was were be been being do does did doing have has had having not no nor too very
    can could may might must shall should will would you your yours we us our ours they them
    their theirs he she it its this that these those there here what which who whom whose when
    where why how all any both each few more most other some such only own same just also
    about above after again against below between during before beneath beside besides beyond
    i me my mine himself herself itself themselves ourselves yourself
    """.split()
)

_CJK_STOPWORDS = frozenset(
    """
    的 了 和 是 在 与 及 或 而 但 也 都 就 又 很 更 最 太 还 再 才 只 被 把 给 让 使 对 从 向
    于 为 以 及 其 之 上 下 中 内 外 前 后 时 我 你 他 她 它 我们 你们 他们 她们 它们 这 那
    这种 那种 这些 那些 一个 一些 一种 各种 每个 所有 任何 什么 怎么 为什么 因为 所以 如果
    虽然 但是 而且 并且 然后 因此 于是 以及 还有 没有 不是 可以 可能 需要 应该 已经 正在
    通过 关于 按照 根据 由于 对于 方面 情况 问题 内容 文章 相关 介绍 分享 目前 现在 今天
    非常 比较 更加 特别 主要 一般 通常 例如 比如 包括 等 等等 啊 吗 吧 呢 着 过 地 得
    """.split()
)

#: 清洗时直接丢弃的标签（不承载正文）
_DROP_TAGS = frozenset(
    "script style noscript iframe svg canvas form input button select textarea template "
    "object embed video audio link meta".split()
)

#: 语义上就不属于正文的容器
_BOILERPLATE_TAGS = frozenset("nav header footer aside menu".split())

#: class/id 命中即视为样板区块
_BOILERPLATE_ATTR_RE = re.compile(
    r"(?:^|[-_ ])("
    r"nav|navbar|navigation|menu|footer|header|sidebar|side|advert|ads|ad|advertisement|"
    r"banner|promo|sponsor|subscribe|newsletter|comment|comments|share|social|related|"
    r"recommend|breadcrumb|pagination|pager|login|signup|register|cookie|popup|modal|"
    r"toolbar|widget|copyright|disclaimer|qrcode|weixin|weibo|download|app-?down"
    r")(?:$|[-_ ])",
    re.IGNORECASE,
)

#: 出现在正文行首/行内的样板句子（整行丢弃）
_BOILERPLATE_LINE_RE = re.compile(
    r"^(?:分享到|分享至|微信扫一扫|扫码关注|扫描二维码|点击阅读原文|阅读原文|上一篇|下一篇|"
    r"相关推荐|相关阅读|热门推荐|责任编辑|本文来源|来源[:：]|版权声明|免责声明|"
    r"登录|注册|立即下载|下载客户端|打开APP|打开App|返回顶部|收藏|点赞|在看|"
    r"share on|follow us|subscribe|cookie policy)\b\s*[:：]?\s*.*$",
    re.IGNORECASE,
)

#: 逐段收集正文的块级元素（保持段落边界，避免整段粘连）
_LINE_BLOCK_XPATH = (
    ".//p | .//h1 | .//h2 | .//h3 | .//h4 | .//h5 | .//h6 | .//li | "
    ".//blockquote | .//pre | .//figcaption | .//td | .//dd"
)

_BLOCK_CONTAINER_XPATH = (
    "//article | //main | //*[@role='main'] | //*[@itemprop='articleBody'] | "
    "//div[contains(@class,'article') or contains(@class,'content') or contains(@class,'post')]"
)


# ---------------------------------------------------------------------------
# 正文清洗
# ---------------------------------------------------------------------------


def visible_text_length(html: str) -> int:
    """粗略统计可见文本长度，用于判断页面是不是被 JS 渲染的空壳。"""
    if not html:
        return 0
    try:
        tree = etree.HTML(html)
    except Exception:
        return len(html)
    if tree is None:
        return len(html)
    for el in list(tree.iter()):
        if not isinstance(el.tag, str):
            continue
        if el.tag.lower() in _DROP_TAGS:
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
    text = "".join(tree.itertext())
    return len(_WS_RE.sub(" ", text).strip())


def _strip_unwanted(tree: etree._Element) -> None:
    """移除脚本/样式/导航/广告等区块（一次性收集再删除，避免边遍历边改）。"""
    doomed: list[etree._Element] = []
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower()
        if tag in _DROP_TAGS or tag in _BOILERPLATE_TAGS:
            doomed.append(el)
            continue
        if (el.get("hidden") is not None) or (el.get("aria-hidden") or "").lower() == "true":
            doomed.append(el)
            continue
        style = (el.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            doomed.append(el)
            continue
        marker = " ".join(filter(None, [el.get("class") or "", el.get("id") or ""]))
        if marker and _BOILERPLATE_ATTR_RE.search(marker):
            doomed.append(el)
    for el in doomed:
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


def _block_score(el: etree._Element) -> tuple[int, int]:
    """正文候选块打分：文本量越大越好，链接文字越多越像导航。"""
    text = _WS_RE.sub(" ", "".join(el.itertext())).strip()
    link_text_len = sum(
        len(_WS_RE.sub(" ", "".join(a.itertext())).strip()) for a in el.iter("a")
    )
    return (len(text) - 2 * link_text_len, len(text))


def _pick_content_block(tree: etree._Element) -> etree._Element:
    best: etree._Element | None = None
    best_score = (0, 0)
    for el in tree.xpath(_BLOCK_CONTAINER_XPATH):
        score = _block_score(el)
        if score > best_score:
            best, best_score = el, score
    if best is None:
        body = tree.find("body")
        best = body if body is not None else tree
    return best


def _collect_lines(block: etree._Element) -> list[str]:
    """按块级元素逐段收集文本，保留段落边界。"""
    lines: list[str] = []
    for el in block.xpath(_LINE_BLOCK_XPATH):
        text = _WS_RE.sub(" ", "".join(el.itertext())).strip()
        if text:
            lines.append(text)
    if not lines:
        raw = _WS_RE.sub(" ", "".join(block.itertext())).strip()
        lines = [seg.strip() for seg in re.split(r"(?<=[。！？!?；;])", raw) if seg.strip()]
    return lines


def _normalize_lines(lines: Iterable[str]) -> str:
    kept: list[str] = []
    seen: set[str] = set()
    for line in lines:
        line = _WS_RE.sub(" ", line).strip()
        line = line.replace("\u200b", "").replace("\ufeff", "")
        if not line:
            continue
        if _BOILERPLATE_LINE_RE.match(line):
            continue
        if len(line) < 2:
            continue
        key = line
        if key in seen:
            continue
        seen.add(key)
        kept.append(line)
    return _MULTI_NEWLINE_RE.sub("\n\n", "\n".join(kept)).strip()


def _extract_with_lxml(html: str) -> str:
    try:
        tree = etree.HTML(html)
    except Exception:
        return ""
    if tree is None:
        return ""
    _strip_unwanted(tree)
    block = _pick_content_block(tree)
    return _normalize_lines(_collect_lines(block))


def _looks_like_html(value: str) -> bool:
    return bool(re.search(r"<[a-zA-Z][^>]*>", value or ""))


def clean_content(raw: str, *, max_chars: int = 20000) -> str:
    """把 HTML 或半成品文本清洗成可入库、可喂给 LLM 的正文。

    顺序：trafilatura（可选） -> lxml 自研清洗 -> 原文兜底。
    """
    if not raw:
        return ""
    text = raw.strip()
    if not _looks_like_html(text):
        cleaned = _normalize_lines(re.split(r"\n+", text))
        return cleaned[:max_chars] if max_chars > 0 else cleaned

    cleaned = ""
    if trafilatura is not None:
        try:
            extracted = trafilatura.extract(
                text,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
            if extracted:
                cleaned = _normalize_lines(extracted.splitlines())
        except Exception:
            cleaned = ""

    if len(cleaned) < 50:
        cleaned = _extract_with_lxml(text)

    if not cleaned:  # 极端兜底：至少剥掉标签
        try:
            tree = etree.HTML(text)
            cleaned = _normalize_lines(("".join(tree.itertext())).splitlines()) if tree is not None else ""
        except Exception:
            cleaned = ""

    return cleaned[:max_chars] if max_chars > 0 else cleaned


# ---------------------------------------------------------------------------
# URL 归一化
# ---------------------------------------------------------------------------

_DEFAULT_STRIP_PARAMS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "yclid",
    "msclkid",
    "spm",
)


def normalize_url(url: str, strip_params: Sequence[str] | None = None) -> str:
    """URL 归一化：去锚点、去跟踪参数、排序 query、收敛大小写与末尾斜杠。

    归一化后的 URL 是去重与幂等写入的主键（url_norm）。
    """
    if not url:
        return ""
    strip_set = {p.strip().lower() for p in (strip_params or _DEFAULT_STRIP_PARAMS) if p and p.strip()}
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "http").lower()
    netloc = parts.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if not path:
        path = "/"

    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        if low in strip_set or low.startswith("utm_"):
            continue
        kept.append((key, value))
    kept.sort(key=lambda kv: (kv[0], kv[1]))
    query = urlencode(kept)

    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).netloc or "").lower()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 分词与关键词
# ---------------------------------------------------------------------------

_STOPWORDS_ALL = _STOPWORDS | _CJK_STOPWORDS

#: 意图描述里的「负面段落」起点标记（粗召回只从正面描述里抽关键词）
_NEGATIVE_MARKER_RE = re.compile(
    r"^\s*(?:不要|不关注|不包括|不含|排除|不接受|不看|避免|拒绝|无需|exclude|do\s+not|don't|not\s+interested)",
    re.IGNORECASE,
)


def split_intent_text(text: str) -> tuple[str, str]:
    """把意图描述拆成（正面文本, 负面文本）。

    "不要：融资新闻、招聘信息" 这类整行会被归入负面文本，只用于过滤关键词，
    不会作为正向召回信号（否则「融资」会变成召回关键词）。
    """
    if not text:
        return "", ""
    positive: list[str] = []
    negative: list[str] = []
    for line in text.splitlines():
        if _NEGATIVE_MARKER_RE.match(line):
            negative.append(line)
        else:
            positive.append(line)
    positive_text = "\n".join(positive).strip()
    negative_text = "\n".join(negative).strip()
    return (positive_text or text.strip(), negative_text)


def _ascii_tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _ASCII_WORD_RE.finditer(text)]


def _cjk_bigrams(text: str, limit: int = 4000) -> list[str]:
    tokens: list[str] = []
    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            continue
        for i in range(len(run) - 1):
            tokens.append(run[i : i + 2])
        if len(tokens) >= limit:
            break
    return tokens


def tokenize(text: str) -> list[str]:
    """确定性分词：ASCII 词 + 中文二元组（不依赖第三方库，保证向量可比）。"""
    if not text:
        return []
    low = text.lower()
    tokens = [t for t in _ascii_tokens(low) if t not in _STOPWORDS_ALL]
    tokens.extend(_cjk_bigrams(low))
    return tokens


def _jieba_tokens(text: str) -> list[str]:
    if not _JIEBA_READY:
        return []
    try:
        return [w.strip().lower() for w in jieba.lcut(text, cut_all=False) if w.strip()]  # type: ignore[union-attr]
    except Exception:  # pragma: no cover - jieba 偶发字典异常
        return []


def extract_keywords(
    text: str,
    top_k: int = 40,
    *,
    avoid_text: str = "",
    split_negative: bool = True,
) -> list[str]:
    """从一段自然语言意图里抽关键词，作为粗召回的抓手。

    有 jieba 时用词级候选（更贴近语义），否则回退二元组 + ASCII 词。
    split_negative=True 时自动把「不要：…」这类负面段落排除在正向关键词之外。
    """
    if not text:
        return []
    avoid = (avoid_text or "").lower()
    if split_negative:
        positive, negative = split_intent_text(text)
        if negative:
            avoid = (avoid + "\n" + negative).strip()
        text = positive
    candidates: list[str] = []
    for token in _jieba_tokens(text):
        if token in _STOPWORDS_ALL or len(token) < 2:
            continue
        if re.fullmatch(r"[\W_]+", token):
            continue
        if avoid and token in avoid:
            continue
        candidates.append(token)
    for token in _ascii_tokens(text):
        if token in _STOPWORDS_ALL or len(token) < 2:
            continue
        if avoid and token in avoid:
            continue
        candidates.append(token)

    if not candidates:
        candidates = tokenize(text)

    counts = Counter(candidates)
    # 先按出现次数，再按长度（长词信息量更大），保证结果稳定
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    result: list[str] = []
    for word, _count in ranked:
        if word not in result:
            result.append(word)
        if len(result) >= top_k:
            break
    return result


# ---------------------------------------------------------------------------
# simhash 指纹
# ---------------------------------------------------------------------------


def _token_hash(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8", "ignore"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash_text(text: str, *, bits: int = 64) -> int:
    """64 位 simhash：TF 加权的 token 哈希按位投票。"""
    if not text:
        return 0
    counts = Counter(tokenize(text))
    if not counts:
        return 0
    weights = [0] * bits
    for token, tf in counts.items():
        weight = 1.0 + (tf.bit_length() - 1)  # 亚线性 TF，抑制超高频词
        hashed = _token_hash(token)
        for pos in range(bits):
            if hashed >> pos & 1:
                weights[pos] += weight
            else:
                weights[pos] -= weight
    value = 0
    for pos in range(bits):
        if weights[pos] > 0:
            value |= 1 << pos
    return value


def hamming_distance(a: int, b: int) -> int:
    return bin((a ^ b) & ((1 << 64) - 1)).count("1")


def simhash_similarity(a: int, b: int) -> float:
    return 1.0 - hamming_distance(a, b) / 64.0


# ---------------------------------------------------------------------------
# 送 LLM 的正文择段
# ---------------------------------------------------------------------------


def select_excerpt(
    content: str,
    keywords: Sequence[str] = (),
    limit: int = 1300,
    *,
    head_ratio: float = 0.3,
) -> str:
    """按关键词择段：保留开头导语 + 命中关键词的段落，控制送入 LLM 的正文长度。

    * 没有关键词 / 没有命中 → 退化为截取前 limit 字；
    * 有关键词命中 → 先给开头 head_ratio 比例的导语，再按段落关键词命中密度补足到 limit。
    """
    text = (content or "").strip()
    if not text:
        return ""
    limit = max(50, int(limit))
    if len(text) <= limit:
        return text

    terms = [term.strip().lower() for term in keywords if term and term.strip()]
    if not terms:
        return text[:limit]

    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    if len(paragraphs) <= 1:
        # 没有段落边界时按句切
        paragraphs = [p.strip() for p in re.split(r"(?<=[。！？!?；;])", text) if p.strip()]
    if not paragraphs:
        return text[:limit]

    head_budget = int(limit * max(0.0, min(1.0, head_ratio)))
    picked: list[str] = []
    used = 0
    for paragraph in paragraphs:
        if used + len(paragraph) > head_budget and picked:
            break
        picked.append(paragraph)
        used += len(paragraph) + 1

    scored: list[tuple[float, int, str]] = []
    for index, paragraph in enumerate(paragraphs):
        if paragraph in picked:
            continue
        lowered = paragraph.lower()
        hits = sum(lowered.count(term) for term in terms)
        if not hits:
            continue
        scored.append((hits / (len(paragraph) + 20), index, paragraph))
    scored.sort(key=lambda item: (-item[0], item[1]))

    for _density, _index, paragraph in scored:
        if used + len(paragraph) + 1 > limit:
            continue
        picked.append(paragraph)
        used += len(paragraph) + 1
        if used >= limit * 0.95:
            break

    excerpt = "\n".join(picked)
    if len(excerpt) < limit * 0.6:
        # 择段没挑够（关键词太稀疏）→ 用原文补齐剩余预算
        remainder = limit - len(excerpt)
        excerpt = (excerpt + "\n" + text[:remainder]).strip()
    return excerpt[:limit]
