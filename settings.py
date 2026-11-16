# -*- coding: utf-8 -*-
"""配置加载与校验（新增模块）

两份配置，职责分离：
    config/filter_intent.yaml  筛选意图（自然语言意图 + Tag + 冲突策略 + 阈值 + 粗召回参数）
    config/app.yaml            运行时参数（爬虫、LLM、去重、存储、推送、可观测性）

设计约束：
    * 代码里不出现任何意图描述、Tag 字面量、阈值常量；全部从 YAML 读取。
    * 加载时做一次集中校验，一次性把问题报全，避免带病运行。
    * 兼容向后兼容场景：只配 Tag 不写意图时进入 legacy_tag 模式（不调用 LLM）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

try:  # 提前给出可读的缺失依赖提示，而不是裸 ModuleNotFoundError
    _ = yaml.safe_load
except AttributeError:  # pragma: no cover
    raise ModuleNotFoundError(
        "PyYAML 版本不可用：请执行 python -m pip install -r requirements.txt"
    ) from None

CONFIG_DIR = Path(__file__).resolve().parent / "config"
DEFAULT_APP_CONFIG = CONFIG_DIR / "app.yaml"
DEFAULT_INTENT_CONFIG = CONFIG_DIR / "filter_intent.yaml"
DEFAULT_PROMPT_TEMPLATE = CONFIG_DIR / "prompts" / "filter_prompt.md"
DEFAULT_SOURCE_PROMPT_TEMPLATE = CONFIG_DIR / "prompts" / "source_adapter_prompt.md"
DEFAULT_ITEM_PROMPT_TEMPLATE = CONFIG_DIR / "prompts" / "item_extraction_prompt.md"
DEFAULT_SOURCES_CONFIG = CONFIG_DIR / "sources.yaml"

#: 冲突策略可选值：
#:   natural_language    自然语言意图为准（默认）
#:   tags                Tag 为准
#:   drop_on_conflict    结论不一致时直接丢弃
CONFLICT_POLICIES = ("natural_language", "tags", "drop_on_conflict")

#: 粗召回用的本地 embedding 实现名（不依赖外部服务）
LOCAL_EMBEDDING_ALIASES = ("local-hashing", "local-tfidf", "local", "hashing-tfidf")


class ConfigError(ValueError):
    """配置缺失或非法。"""


# ---------------------------------------------------------------------------
# 校验小工具：把错误累积起来一次性抛出
# ---------------------------------------------------------------------------


class _Errors:
    def __init__(self) -> None:
        self.items: list[str] = []
        #: 不阻断流程的提示（空列表项、未知配置项等），由 runner 打进日志
        self.warnings: list[str] = []

    def add(self, path: str, message: str) -> None:
        self.items.append(f"{path}: {message}")

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(f"{path}: {message}")

    def raise_if_any(self, where: str) -> None:
        if self.items:
            detail = "\n  - ".join(self.items)
            raise ConfigError(f"{where} 校验失败（{len(self.items)} 处）：\n  - {detail}")


def _warn_unknown_keys(
    data: Mapping[str, Any], allowed: frozenset[str] | set[str], path: str, errors: _Errors
) -> None:
    """拼错键名是最常见的「我明明配置了却没生效」原因，这里显式提示。"""
    for key in data:
        if key not in allowed:
            errors.warn(f"{path}.{key}", "未知配置项，已忽略（检查拼写或缩进层级）")


def _as_mapping(value: Any, path: str, errors: _Errors) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        errors.add(path, f"应为字典，实际 {type(value).__name__}")
        return {}
    return {str(k): v for k, v in value.items()}


def _as_str(
    data: Mapping[str, Any],
    key: str,
    path: str,
    errors: _Errors,
    *,
    default: str = "",
    allow_blank: bool = True,
) -> str:
    value = data.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        errors.add(f"{path}.{key}", f"应为字符串，实际 {type(value).__name__}")
        return default
    value = value.strip()
    if not allow_blank and not value:
        errors.add(f"{path}.{key}", "不能为空")
    return value


def _as_bool(data: Mapping[str, Any], key: str, path: str, errors: _Errors, *, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no", "1", "0"):
        return value.strip().lower() in ("true", "yes", "1")
    if value is None:
        return default
    errors.add(f"{path}.{key}", f"应为布尔值，实际 {value!r}")
    return default


def _as_int(
    data: Mapping[str, Any],
    key: str,
    path: str,
    errors: _Errors,
    *,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = data.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(str(value).strip())
        except (TypeError, ValueError):
            errors.add(f"{path}.{key}", f"应为整数，实际 {value!r}")
            return default
    if minimum is not None and value < minimum:
        errors.add(f"{path}.{key}", f"应 >= {minimum}，实际 {value}")
    if maximum is not None and value > maximum:
        errors.add(f"{path}.{key}", f"应 <= {maximum}，实际 {value}")
    return int(value)


def _as_float(
    data: Mapping[str, Any],
    key: str,
    path: str,
    errors: _Errors,
    *,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = data.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool):
        errors.add(f"{path}.{key}", f"应为数字，实际 {value!r}")
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        errors.add(f"{path}.{key}", f"应为数字，实际 {value!r}")
        return default
    if minimum is not None and value < minimum:
        errors.add(f"{path}.{key}", f"应 >= {minimum}，实际 {value}")
    if maximum is not None and value > maximum:
        errors.add(f"{path}.{key}", f"应 <= {maximum}，实际 {value}")
    return value


def _as_str_list(
    data: Mapping[str, Any],
    key: str,
    path: str,
    errors: _Errors,
    *,
    default: list[str] | None = None,
) -> list[str]:
    """只接受列表；元素必须是字符串。去重保序。"""
    value = data.get(key, default if default is not None else [])
    if value is None:
        return list(default or [])
    if isinstance(value, str):
        errors.add(f"{path}.{key}", "应为列表（YAML 里写成 [\"a\", \"b\"]），而不是单个字符串")
        return list(default or [])
    if not isinstance(value, (list, tuple, set)):
        errors.add(f"{path}.{key}", f"应为列表，实际 {type(value).__name__}")
        return list(default or [])
    result: list[str] = []
    for index, item in enumerate(value):
        if item is None:
            errors.warn(
                f"{path}.{key}[{index}]",
                "该列表项为空（YAML 里写成了单独的 `-` 或 `- `），已忽略；请补上内容或删掉这一行",
            )
            continue
        if not isinstance(item, str):
            errors.add(f"{path}.{key}[{index}]", f"应为字符串，实际 {type(item).__name__}")
            continue
        item = item.strip()
        if not item:
            errors.warn(f"{path}.{key}[{index}]", "空字符串，已忽略")
            continue
        if item not in result:
            result.append(item)
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败 {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"配置根节点应为字典: {path}")
    return {str(k): v for k, v in data.items()}


# ---------------------------------------------------------------------------
# 筛选意图配置
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IntentConfig:
    """自然语言意图（主判据）。"""

    description: str = ""
    examples_include: list[str] = field(default_factory=list)
    examples_exclude: list[str] = field(default_factory=list)
    preferences: str = ""

    @property
    def is_blank(self) -> bool:
        return not self.description and not self.examples_include and not self.examples_exclude


@dataclass(slots=True)
class TagsConfig:
    """Tag 三段（辅助信号 + 粗召回抓手）。"""

    must_include: list[str] = field(default_factory=list)
    boost: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)

    @property
    def is_blank(self) -> bool:
        return not (self.must_include or self.boost or self.exclude)

    def all_tags(self) -> list[str]:
        seen: list[str] = []
        for tag in [*self.must_include, *self.boost, *self.exclude]:
            if tag not in seen:
                seen.append(tag)
        return seen


@dataclass(slots=True)
class PrefilterConfig:
    """粗召回配置。"""

    embedding_model: str = "local-hashing"
    similarity_threshold: float = 0.35
    keyword_from_intent: bool = True
    keyword_top_k: int = 40
    min_keyword_hits: int = 1
    must_include_min_hits: int = 1

    @property
    def is_local_embedding(self) -> bool:
        return self.embedding_model.strip().lower() in LOCAL_EMBEDDING_ALIASES


@dataclass(slots=True)
class FilterIntentConfig:
    """config/filter_intent.yaml 的内存表示。"""

    intent: IntentConfig = field(default_factory=IntentConfig)
    tags: TagsConfig = field(default_factory=TagsConfig)
    conflict_policy: str = "natural_language"
    score_threshold: float = 0.6
    prefilter: PrefilterConfig = field(default_factory=PrefilterConfig)
    #: 只配 Tag、不写意图时是否允许走 legacy_tag 模式（向后兼容旧行为）
    allow_tag_only_legacy: bool = True
    source_path: Path | None = None
    #: 加载时产生的不阻断流程的提示（空列表项、未知配置项等）
    warnings: list[str] = field(default_factory=list)

    @property
    def has_intent(self) -> bool:
        return bool(self.intent.description.strip())

    @property
    def is_legacy_tag_mode(self) -> bool:
        """没有自然语言意图、只有 Tag -> 纯 Tag 判定，不调用 LLM。"""
        return not self.has_intent and not self.tags.is_blank

    def describe(self) -> str:
        if self.is_legacy_tag_mode:
            return "legacy_tag（仅 Tag，不调用 LLM）"
        return f"intent+tags（conflict_policy={self.conflict_policy}, threshold={self.score_threshold}）"


def _parse_filter_intent(data: Mapping[str, Any], source: Path | None) -> FilterIntentConfig:
    errors = _Errors()
    _warn_unknown_keys(
        data,
        frozenset(
            {"intent", "tags", "conflict_policy", "score_threshold", "prefilter", "allow_tag_only_legacy"}
        ),
        "<root>",
        errors,
    )

    intent_raw = _as_mapping(data.get("intent"), "intent", errors)
    _warn_unknown_keys(intent_raw, frozenset({"description", "examples", "preferences"}), "intent", errors)
    intent = IntentConfig(
        description=str(intent_raw.get("description") or "").strip(),
        preferences=str(intent_raw.get("preferences") or "").strip(),
    )
    if intent.description and not isinstance(intent_raw.get("description"), str):
        errors.add("intent.description", f"应为字符串，实际 {type(intent_raw.get('description')).__name__}")
    examples_raw = _as_mapping(intent_raw.get("examples"), "intent.examples", errors)
    _warn_unknown_keys(
        examples_raw, frozenset({"include", "exclude"}), "intent.examples", errors
    )
    intent.examples_include = _as_str_list(examples_raw, "include", "intent.examples", errors)
    intent.examples_exclude = _as_str_list(examples_raw, "exclude", "intent.examples", errors)
    if "preferences" in intent_raw and not isinstance(intent_raw["preferences"], str):
        errors.add("intent.preferences", f"应为字符串，实际 {type(intent_raw['preferences']).__name__}")

    tags_raw = _as_mapping(data.get("tags"), "tags", errors)
    _warn_unknown_keys(
        tags_raw, frozenset({"must_include", "boost", "exclude"}), "tags", errors
    )
    tags = TagsConfig(
        must_include=_as_str_list(tags_raw, "must_include", "tags", errors),
        boost=_as_str_list(tags_raw, "boost", "tags", errors),
        exclude=_as_str_list(tags_raw, "exclude", "tags", errors),
    )
    overlap = set(tags.must_include) & set(tags.exclude)
    if overlap:
        errors.add("tags", f"同一个 Tag 同时出现在 must_include 与 exclude: {sorted(overlap)}")
    overlap = set(tags.boost) & set(tags.exclude)
    if overlap:
        errors.add("tags", f"同一个 Tag 同时出现在 boost 与 exclude: {sorted(overlap)}")

    conflict_policy = str(data.get("conflict_policy") or "natural_language").strip()
    if conflict_policy not in CONFLICT_POLICIES:
        errors.add("conflict_policy", f"必须是 {list(CONFLICT_POLICIES)} 之一，实际 {conflict_policy!r}")

    score_threshold = data.get("score_threshold", 0.6)
    if isinstance(score_threshold, bool) or not isinstance(score_threshold, (int, float)):
        errors.add("score_threshold", f"应为 0~1 的数字，实际 {score_threshold!r}")
        score_threshold = 0.6
    else:
        score_threshold = float(score_threshold)
        if not 0.0 <= score_threshold <= 1.0:
            errors.add("score_threshold", f"必须在 [0, 1] 内，实际 {score_threshold}")

    prefilter_raw = _as_mapping(data.get("prefilter"), "prefilter", errors)
    _warn_unknown_keys(
        prefilter_raw,
        frozenset(
            {
                "embedding_model",
                "similarity_threshold",
                "keyword_from_intent",
                "keyword_top_k",
                "min_keyword_hits",
                "must_include_min_hits",
            }
        ),
        "prefilter",
        errors,
    )
    prefilter = PrefilterConfig(
        embedding_model=_as_str(
            prefilter_raw, "embedding_model", "prefilter", errors, default="local-hashing"
        ),
        similarity_threshold=_as_float(
            prefilter_raw,
            "similarity_threshold",
            "prefilter",
            errors,
            default=0.35,
            minimum=0.0,
            maximum=1.0,
        ),
        keyword_from_intent=_as_bool(
            prefilter_raw, "keyword_from_intent", "prefilter", errors, default=True
        ),
        keyword_top_k=_as_int(
            prefilter_raw, "keyword_top_k", "prefilter", errors, default=40, minimum=1, maximum=500
        ),
        min_keyword_hits=_as_int(
            prefilter_raw, "min_keyword_hits", "prefilter", errors, default=1, minimum=1
        ),
        must_include_min_hits=_as_int(
            prefilter_raw, "must_include_min_hits", "prefilter", errors, default=1, minimum=1
        ),
    )

    allow_tag_only_legacy = _as_bool(
        data, "allow_tag_only_legacy", "<root>", errors, default=True
    )

    config = FilterIntentConfig(
        intent=intent,
        tags=tags,
        conflict_policy=conflict_policy,
        score_threshold=score_threshold,
        prefilter=prefilter,
        allow_tag_only_legacy=allow_tag_only_legacy,
        source_path=source,
    )

    # 交叉校验：两份判据不能同时为空；只配 Tag 时必须显式允许 legacy 模式
    if intent.is_blank and tags.is_blank:
        errors.add(
            "<root>",
            "intent.description 与 tags 同时为空：至少配置一段自然语言意图，或配置 Tag 走 legacy 模式",
        )
    elif not config.has_intent and config.allow_tag_only_legacy is False:
        errors.add(
            "intent.description",
            "intent.description 为空，且 allow_tag_only_legacy=false；请补写意图描述",
        )
    elif not config.has_intent and tags.is_blank:
        errors.add("intent.description", "不能为空（且没有可用的 Tag）")

    config.warnings = list(errors.warnings)
    errors.raise_if_any(f"筛选意图配置 {source or '<memory>'}")
    return config


def load_filter_intent(path: str | Path | None = None) -> FilterIntentConfig:
    """加载并校验筛选意图配置。"""
    target = Path(path) if path else DEFAULT_INTENT_CONFIG
    return _parse_filter_intent(_read_yaml(target), target)


def parse_filter_intent(data: Mapping[str, Any]) -> FilterIntentConfig:
    """测试/嵌入式场景：直接从字典构造。"""
    return _parse_filter_intent(data, None)


# ---------------------------------------------------------------------------
# 运行时配置
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SeleniumConfig:
    enabled: bool = True
    wait_seconds: float = 3.0
    headless: bool = True
    page_load_timeout: float = 20.0
    driver_path: str = ""


@dataclass(slots=True)
class AutoSourceConfig:
    """只给 URL 时的自动适配（AI 生成解析规则 + 确定性校验 + 缓存 + 兜底）。"""

    enabled: bool = True
    prompt_template: str = str(DEFAULT_SOURCE_PROMPT_TEMPLATE)
    max_attempts: int = 3
    html_max_chars: int = 12000
    min_items: int = 1
    min_detail_chars: int = 200
    heuristic_fallback: bool = True
    detail_adapt: bool = True
    profile_cache: str = "data/source_profiles.json"
    #: 第二层兜底：选择器抽不到时，让 AI 直接读页面输出条目列表
    item_fallback: bool = True
    item_prompt_template: str = str(DEFAULT_ITEM_PROMPT_TEMPLATE)
    item_text_max_chars: int = 8000
    item_max_items: int = 20
    #: 第三层兜底：连 AI 抽取都失败时，把整页当成一个候选条目（推送列表页 URL）
    page_fallback: bool = True
    #: 规则自愈：缓存规则连续 N 次抽到 0 条时自动删除并重新生成（0 = 关闭）
    self_heal_after: int = 2


@dataclass(slots=True)
class CrawlConfig:
    timeout_seconds: float = 20.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.5
    delay_seconds: float = 1.0
    user_agent_pool: list[str] = field(default_factory=list)
    prefer_fake_useragent: bool = True
    proxies: str = ""
    verify_tls: bool = True
    trust_env: bool = True
    max_items_per_source: int = 10
    fetch_detail: bool = True
    content_min_chars: int = 200
    #: 列表页先粗筛：只给通过 URL 去重/黑名单/粗召回的条目抓详情页
    list_prefilter: bool = True
    #: 同一次运行内复用已抓过的页面（列表页/详情页）
    page_cache: bool = True
    #: 单源连续失败熔断：连续失败 N 次后冷却一段时间不再抓（0 = 关闭）
    source_failure_threshold: int = 3
    source_cooldown_minutes: int = 360
    health_store: str = "data/source_health.json"
    selenium_fallback: SeleniumConfig = field(default_factory=SeleniumConfig)
    auto: AutoSourceConfig = field(default_factory=AutoSourceConfig)


@dataclass(slots=True)
class LLMConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    chat_path: str = "/chat/completions"
    model: str = "deepseek-chat"
    api_key: str = ""
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.1
    max_tokens: int = 4096
    timeout_seconds: float = 90.0
    max_retries: int = 2
    retry_backoff_seconds: float = 2.0
    json_response_format: bool = True
    batch_size: int = 5
    content_max_chars: int = 1300
    #: 单次运行 token 预算（prompt+completion 累计，0 = 不限）
    max_tokens_per_run: int = 60000
    prompt_template: str = str(DEFAULT_PROMPT_TEMPLATE)
    extra_headers: dict[str, str] = field(default_factory=dict)

    def resolve_api_key(self) -> str:
        """配置里优先；留空则回退环境变量（密钥不必入库）。"""
        if self.api_key.strip():
            return self.api_key.strip()
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "").strip()
        return ""

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/" + self.chat_path.lstrip("/")


@dataclass(slots=True)
class PreprocessConfig:
    min_content_chars: int = 80
    max_content_chars: int = 20000
    strip_query_params: list[str] = field(default_factory=list)
    blacklist_keywords: list[str] = field(default_factory=list)
    drop_short_title: bool = True


@dataclass(slots=True)
class DedupConfig:
    url_window_days: int = 30
    content_window_days: int = 7
    hamming_threshold: int = 3
    fingerprint_max_scan: int = 5000


@dataclass(slots=True)
class StorageConfig:
    db_path: str = "data/state.db"
    dropped_sample_retention_days: int = 7
    dropped_sample_max_chars: int = 200


@dataclass(slots=True)
class WeComPushConfig:
    """推送渠道（send-post.php）配置：请求地址与 sendkey 都外置到 YAML。"""

    api_url: str = ""
    sendkey: str = ""
    sendkey_env: str = "WECOM_SENDKEY"
    timeout_seconds: float = 15.0

    def resolve_sendkey(self) -> str:
        if self.sendkey.strip():
            return self.sendkey.strip()
        if self.sendkey_env:
            return os.environ.get(self.sendkey_env, "").strip()
        return ""

    @property
    def configured(self) -> bool:
        return bool(self.api_url.strip())


@dataclass(slots=True)
class PushConfig:
    enabled: bool = True
    max_per_run: int = 10
    channels: list[str] = field(default_factory=lambda: ["wecom_news"])
    description_max_chars: int = 500
    reason_max_chars: int = 160
    dry_run: bool = False
    wecom: WeComPushConfig = field(default_factory=WeComPushConfig)


#: 允许触发文字告警的事件名（写错会在配置校验阶段直接报错）
NOTIFY_EVENTS = (
    "run_aborted",
    "config_error",
    "crawl_failure",
    "source_adapt_failure",
    "llm_failure",
    "parse_failure",
    "push_failure",
    "summary",
)


@dataclass(slots=True)
class NotifyConfig:
    """重要报错用 send_text 通知。"""

    enabled: bool = False
    channel: str = "wecom_text"
    events: list[str] = field(default_factory=lambda: list(NOTIFY_EVENTS))
    max_per_run: int = 5
    min_interval_seconds: float = 300.0
    text_max_chars: int = 1000
    dry_run_silent: bool = True
    include_summary: bool = True


@dataclass(slots=True)
class ObservabilityConfig:
    log_dropped_samples: bool = True
    log_stage_counts: bool = True
    log_summary: bool = True
    prompt_preview_chars: int = 0
    event_prefix: str = "[pipeline]"


@dataclass(slots=True)
class AppConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    push: PushConfig = field(default_factory=PushConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    sources_path: str = str(DEFAULT_SOURCES_CONFIG)
    source_path: Path | None = None
    #: 加载时产生的不阻断流程的提示（未知配置项、空列表项等）
    warnings: list[str] = field(default_factory=list)


def _parse_app(data: Mapping[str, Any], source: Path | None) -> AppConfig:
    errors = _Errors()
    _warn_unknown_keys(
        data,
        frozenset(
            {
                "llm",
                "crawl",
                "preprocess",
                "dedup",
                "storage",
                "push",
                "notify",
                "observability",
                "sources_path",
            }
        ),
        "<root>",
        errors,
    )

    llm_raw = _as_mapping(data.get("llm"), "llm", errors)
    _warn_unknown_keys(
        llm_raw,
        frozenset(
            {
                "provider",
                "base_url",
                "chat_path",
                "model",
                "api_key",
                "api_key_env",
                "temperature",
                "max_tokens",
                "timeout_seconds",
                "max_retries",
                "retry_backoff_seconds",
                "json_response_format",
                "batch_size",
                "content_max_chars",
                "max_tokens_per_run",
                "prompt_template",
                "extra_headers",
            }
        ),
        "llm",
        errors,
    )
    llm = LLMConfig(
        provider=_as_str(llm_raw, "provider", "llm", errors, default="deepseek"),
        base_url=_as_str(llm_raw, "base_url", "llm", errors, default="https://api.deepseek.com"),
        chat_path=_as_str(llm_raw, "chat_path", "llm", errors, default="/chat/completions"),
        model=_as_str(llm_raw, "model", "llm", errors, default="deepseek-chat", allow_blank=False),
        api_key=_as_str(llm_raw, "api_key", "llm", errors, default=""),
        api_key_env=_as_str(llm_raw, "api_key_env", "llm", errors, default="DEEPSEEK_API_KEY"),
        temperature=_as_float(llm_raw, "temperature", "llm", errors, default=0.1, minimum=0.0, maximum=2.0),
        max_tokens=_as_int(llm_raw, "max_tokens", "llm", errors, default=4096, minimum=1),
        timeout_seconds=_as_float(llm_raw, "timeout_seconds", "llm", errors, default=90.0, minimum=1.0),
        max_retries=_as_int(llm_raw, "max_retries", "llm", errors, default=2, minimum=0, maximum=10),
        retry_backoff_seconds=_as_float(
            llm_raw, "retry_backoff_seconds", "llm", errors, default=2.0, minimum=0.0
        ),
        json_response_format=_as_bool(
            llm_raw, "json_response_format", "llm", errors, default=True
        ),
        batch_size=_as_int(llm_raw, "batch_size", "llm", errors, default=5, minimum=1, maximum=50),
        content_max_chars=_as_int(
            llm_raw, "content_max_chars", "llm", errors, default=1300, minimum=100
        ),
        max_tokens_per_run=_as_int(
            llm_raw, "max_tokens_per_run", "llm", errors, default=60000, minimum=0
        ),
        prompt_template=_as_str(
            llm_raw, "prompt_template", "llm", errors, default=str(DEFAULT_PROMPT_TEMPLATE)
        ),
    )
    extra_headers = _as_mapping(llm_raw.get("extra_headers"), "llm.extra_headers", errors)
    llm.extra_headers = {str(k): str(v) for k, v in extra_headers.items()}
    if not llm.base_url.startswith(("http://", "https://")):
        errors.add("llm.base_url", f"应为 http(s) 地址，实际 {llm.base_url!r}")

    crawl_raw = _as_mapping(data.get("crawl"), "crawl", errors)
    _warn_unknown_keys(
        crawl_raw,
        frozenset(
            {
                "timeout_seconds",
                "max_retries",
                "retry_backoff_seconds",
                "delay_seconds",
                "user_agent_pool",
                "prefer_fake_useragent",
                "proxies",
                "verify_tls",
                "trust_env",
                "max_items_per_source",
                "fetch_detail",
                "content_min_chars",
                "list_prefilter",
                "page_cache",
                "source_failure_threshold",
                "source_cooldown_minutes",
                "health_store",
                "selenium_fallback",
                "auto",
            }
        ),
        "crawl",
        errors,
    )
    selenium_raw = _as_mapping(crawl_raw.get("selenium_fallback"), "crawl.selenium_fallback", errors)
    _warn_unknown_keys(
        selenium_raw,
        frozenset({"enabled", "wait_seconds", "headless", "page_load_timeout", "driver_path"}),
        "crawl.selenium_fallback",
        errors,
    )
    auto_raw = _as_mapping(crawl_raw.get("auto"), "crawl.auto", errors)
    _warn_unknown_keys(
        auto_raw,
        frozenset(
            {
                "enabled",
                "prompt_template",
                "max_attempts",
                "html_max_chars",
                "min_items",
                "min_detail_chars",
                "heuristic_fallback",
                "detail_adapt",
                "profile_cache",
                "item_fallback",
                "item_prompt_template",
                "item_text_max_chars",
                "item_max_items",
                "page_fallback",
                "self_heal_after",
            }
        ),
        "crawl.auto",
        errors,
    )
    crawl = CrawlConfig(
        timeout_seconds=_as_float(
            crawl_raw, "timeout_seconds", "crawl", errors, default=20.0, minimum=1.0
        ),
        max_retries=_as_int(crawl_raw, "max_retries", "crawl", errors, default=3, minimum=0, maximum=10),
        retry_backoff_seconds=_as_float(
            crawl_raw, "retry_backoff_seconds", "crawl", errors, default=1.5, minimum=0.0
        ),
        delay_seconds=_as_float(crawl_raw, "delay_seconds", "crawl", errors, default=1.0, minimum=0.0),
        user_agent_pool=_as_str_list(crawl_raw, "user_agent_pool", "crawl", errors),
        prefer_fake_useragent=_as_bool(
            crawl_raw, "prefer_fake_useragent", "crawl", errors, default=True
        ),
        proxies=_as_str(crawl_raw, "proxies", "crawl", errors, default=""),
        verify_tls=_as_bool(crawl_raw, "verify_tls", "crawl", errors, default=True),
        trust_env=_as_bool(crawl_raw, "trust_env", "crawl", errors, default=True),
        max_items_per_source=_as_int(
            crawl_raw, "max_items_per_source", "crawl", errors, default=10, minimum=1, maximum=200
        ),
        fetch_detail=_as_bool(crawl_raw, "fetch_detail", "crawl", errors, default=True),
        content_min_chars=_as_int(
            crawl_raw, "content_min_chars", "crawl", errors, default=200, minimum=0
        ),
        list_prefilter=_as_bool(
            crawl_raw, "list_prefilter", "crawl", errors, default=True
        ),
        page_cache=_as_bool(crawl_raw, "page_cache", "crawl", errors, default=True),
        source_failure_threshold=_as_int(
            crawl_raw, "source_failure_threshold", "crawl", errors, default=3, minimum=0, maximum=20
        ),
        source_cooldown_minutes=_as_int(
            crawl_raw, "source_cooldown_minutes", "crawl", errors, default=360, minimum=0
        ),
        health_store=_as_str(
            crawl_raw, "health_store", "crawl", errors, default="data/source_health.json",
            allow_blank=False,
        ),
        selenium_fallback=SeleniumConfig(
            enabled=_as_bool(selenium_raw, "enabled", "crawl.selenium_fallback", errors, default=True),
            wait_seconds=_as_float(
                selenium_raw, "wait_seconds", "crawl.selenium_fallback", errors, default=3.0, minimum=0.0
            ),
            headless=_as_bool(selenium_raw, "headless", "crawl.selenium_fallback", errors, default=True),
            page_load_timeout=_as_float(
                selenium_raw,
                "page_load_timeout",
                "crawl.selenium_fallback",
                errors,
                default=20.0,
                minimum=1.0,
            ),
            driver_path=_as_str(selenium_raw, "driver_path", "crawl.selenium_fallback", errors, default=""),
        ),
        auto=AutoSourceConfig(
            enabled=_as_bool(auto_raw, "enabled", "crawl.auto", errors, default=True),
            prompt_template=_as_str(
                auto_raw,
                "prompt_template",
                "crawl.auto",
                errors,
                default=str(DEFAULT_SOURCE_PROMPT_TEMPLATE),
            ),
            max_attempts=_as_int(
                auto_raw, "max_attempts", "crawl.auto", errors, default=3, minimum=1, maximum=10
            ),
            html_max_chars=_as_int(
                auto_raw, "html_max_chars", "crawl.auto", errors, default=12000, minimum=500
            ),
            min_items=_as_int(auto_raw, "min_items", "crawl.auto", errors, default=1, minimum=1),
            min_detail_chars=_as_int(
                auto_raw, "min_detail_chars", "crawl.auto", errors, default=200, minimum=20
            ),
            heuristic_fallback=_as_bool(
                auto_raw, "heuristic_fallback", "crawl.auto", errors, default=True
            ),
            detail_adapt=_as_bool(auto_raw, "detail_adapt", "crawl.auto", errors, default=True),
            profile_cache=_as_str(
                auto_raw,
                "profile_cache",
                "crawl.auto",
                errors,
                default="data/source_profiles.json",
                allow_blank=False,
            ),
            item_fallback=_as_bool(
                auto_raw, "item_fallback", "crawl.auto", errors, default=True
            ),
            item_prompt_template=_as_str(
                auto_raw,
                "item_prompt_template",
                "crawl.auto",
                errors,
                default=str(DEFAULT_ITEM_PROMPT_TEMPLATE),
            ),
            item_text_max_chars=_as_int(
                auto_raw,
                "item_text_max_chars",
                "crawl.auto",
                errors,
                default=8000,
                minimum=500,
            ),
            item_max_items=_as_int(
                auto_raw, "item_max_items", "crawl.auto", errors, default=20, minimum=1, maximum=100
            ),
            page_fallback=_as_bool(
                auto_raw, "page_fallback", "crawl.auto", errors, default=True
            ),
            self_heal_after=_as_int(
                auto_raw, "self_heal_after", "crawl.auto", errors, default=2, minimum=0, maximum=20
            ),
        ),
    )
    if crawl.proxies and not crawl.proxies.startswith(("http://", "https://", "socks5://")):
        errors.add("crawl.proxies", f"代理地址格式可疑: {crawl.proxies!r}")

    pre_raw = _as_mapping(data.get("preprocess"), "preprocess", errors)
    _warn_unknown_keys(
        pre_raw,
        frozenset(
            {"min_content_chars", "max_content_chars", "strip_query_params", "blacklist_keywords", "drop_short_title"}
        ),
        "preprocess",
        errors,
    )
    preprocess = PreprocessConfig(
        min_content_chars=_as_int(
            pre_raw, "min_content_chars", "preprocess", errors, default=80, minimum=0
        ),
        max_content_chars=_as_int(
            pre_raw, "max_content_chars", "preprocess", errors, default=20000, minimum=100
        ),
        strip_query_params=_as_str_list(pre_raw, "strip_query_params", "preprocess", errors),
        blacklist_keywords=_as_str_list(pre_raw, "blacklist_keywords", "preprocess", errors),
        drop_short_title=_as_bool(pre_raw, "drop_short_title", "preprocess", errors, default=True),
    )
    if preprocess.min_content_chars > preprocess.max_content_chars:
        errors.add("preprocess.min_content_chars", "不能大于 max_content_chars")

    dedup_raw = _as_mapping(data.get("dedup"), "dedup", errors)
    _warn_unknown_keys(
        dedup_raw,
        frozenset({"url_window_days", "content_window_days", "hamming_threshold", "fingerprint_max_scan"}),
        "dedup",
        errors,
    )
    dedup = DedupConfig(
        url_window_days=_as_int(dedup_raw, "url_window_days", "dedup", errors, default=30, minimum=0),
        content_window_days=_as_int(
            dedup_raw, "content_window_days", "dedup", errors, default=7, minimum=0
        ),
        hamming_threshold=_as_int(
            dedup_raw, "hamming_threshold", "dedup", errors, default=3, minimum=0, maximum=64
        ),
        fingerprint_max_scan=_as_int(
            dedup_raw, "fingerprint_max_scan", "dedup", errors, default=5000, minimum=1
        ),
    )

    storage_raw = _as_mapping(data.get("storage"), "storage", errors)
    _warn_unknown_keys(
        storage_raw,
        frozenset({"db_path", "dropped_sample_retention_days", "dropped_sample_max_chars"}),
        "storage",
        errors,
    )
    storage = StorageConfig(
        db_path=_as_str(storage_raw, "db_path", "storage", errors, default="data/state.db", allow_blank=False),
        dropped_sample_retention_days=_as_int(
            storage_raw, "dropped_sample_retention_days", "storage", errors, default=7, minimum=0
        ),
        dropped_sample_max_chars=_as_int(
            storage_raw, "dropped_sample_max_chars", "storage", errors, default=200, minimum=20
        ),
    )

    push_raw = _as_mapping(data.get("push"), "push", errors)
    _warn_unknown_keys(
        push_raw,
        frozenset(
            {
                "enabled",
                "max_per_run",
                "channels",
                "description_max_chars",
                "reason_max_chars",
                "dry_run",
                "wecom",
            }
        ),
        "push",
        errors,
    )
    wecom_raw = _as_mapping(push_raw.get("wecom"), "push.wecom", errors)
    _warn_unknown_keys(
        wecom_raw,
        frozenset({"api_url", "sendkey", "sendkey_env", "timeout_seconds"}),
        "push.wecom",
        errors,
    )
    wecom = WeComPushConfig(
        api_url=_as_str(wecom_raw, "api_url", "push.wecom", errors, default=""),
        sendkey=_as_str(wecom_raw, "sendkey", "push.wecom", errors, default=""),
        sendkey_env=_as_str(wecom_raw, "sendkey_env", "push.wecom", errors, default="WECOM_SENDKEY"),
        timeout_seconds=_as_float(
            wecom_raw, "timeout_seconds", "push.wecom", errors, default=15.0, minimum=1.0
        ),
    )
    if wecom.api_url and not wecom.api_url.startswith(("http://", "https://")):
        errors.add(
            "push.wecom.api_url",
            f"应为 http(s) 地址（例如 https://your-host/send-post.php），实际 {wecom.api_url!r}",
        )
    push = PushConfig(
        enabled=_as_bool(push_raw, "enabled", "push", errors, default=True),
        max_per_run=_as_int(push_raw, "max_per_run", "push", errors, default=10, minimum=0),
        channels=_as_str_list(push_raw, "channels", "push", errors, default=["wecom_news"]),
        description_max_chars=_as_int(
            push_raw, "description_max_chars", "push", errors, default=500, minimum=50
        ),
        reason_max_chars=_as_int(
            push_raw, "reason_max_chars", "push", errors, default=160, minimum=20
        ),
        dry_run=_as_bool(push_raw, "dry_run", "push", errors, default=False),
        wecom=wecom,
    )

    notify_raw = _as_mapping(data.get("notify"), "notify", errors)
    _warn_unknown_keys(
        notify_raw,
        frozenset(
            {
                "enabled",
                "channel",
                "events",
                "max_per_run",
                "min_interval_seconds",
                "text_max_chars",
                "dry_run_silent",
                "include_summary",
            }
        ),
        "notify",
        errors,
    )
    notify_events = _as_str_list(
        notify_raw, "events", "notify", errors, default=list(NOTIFY_EVENTS)
    )
    unknown_events = [event for event in notify_events if event not in NOTIFY_EVENTS]
    if unknown_events:
        errors.add(
            "notify.events",
            f"未知事件 {unknown_events}；可用事件：{list(NOTIFY_EVENTS)}",
        )
    notify = NotifyConfig(
        enabled=_as_bool(notify_raw, "enabled", "notify", errors, default=False),
        channel=_as_str(notify_raw, "channel", "notify", errors, default="wecom_text"),
        events=notify_events,
        max_per_run=_as_int(notify_raw, "max_per_run", "notify", errors, default=5, minimum=0),
        min_interval_seconds=_as_float(
            notify_raw, "min_interval_seconds", "notify", errors, default=300.0, minimum=0.0
        ),
        text_max_chars=_as_int(
            notify_raw, "text_max_chars", "notify", errors, default=1000, minimum=50
        ),
        dry_run_silent=_as_bool(notify_raw, "dry_run_silent", "notify", errors, default=True),
        include_summary=_as_bool(notify_raw, "include_summary", "notify", errors, default=True),
    )

    obs_raw = _as_mapping(data.get("observability"), "observability", errors)
    _warn_unknown_keys(
        obs_raw,
        frozenset(
            {
                "log_dropped_samples",
                "log_stage_counts",
                "log_summary",
                "prompt_preview_chars",
                "event_prefix",
            }
        ),
        "observability",
        errors,
    )
    observability = ObservabilityConfig(
        log_dropped_samples=_as_bool(
            obs_raw, "log_dropped_samples", "observability", errors, default=True
        ),
        log_stage_counts=_as_bool(obs_raw, "log_stage_counts", "observability", errors, default=True),
        log_summary=_as_bool(obs_raw, "log_summary", "observability", errors, default=True),
        prompt_preview_chars=_as_int(
            obs_raw, "prompt_preview_chars", "observability", errors, default=0, minimum=0
        ),
        event_prefix=_as_str(obs_raw, "event_prefix", "observability", errors, default="[pipeline]"),
    )

    app = AppConfig(
        llm=llm,
        crawl=crawl,
        preprocess=preprocess,
        dedup=dedup,
        storage=storage,
        push=push,
        notify=notify,
        observability=observability,
        sources_path=_as_str(data, "sources_path", "<root>", errors, default=str(DEFAULT_SOURCES_CONFIG)),
        source_path=source,
    )
    app.warnings = list(errors.warnings)
    errors.raise_if_any(f"运行时配置 {source or '<memory>'}")
    return app


def load_app_config(path: str | Path | None = None) -> AppConfig:
    """加载并校验运行时配置。"""
    target = Path(path) if path else DEFAULT_APP_CONFIG
    return _parse_app(_read_yaml(target), target)


def parse_app_config(data: Mapping[str, Any]) -> AppConfig:
    """测试/嵌入式场景：直接从字典构造。"""
    return _parse_app(data, None)


__all__ = [
    "CONFIG_DIR",
    "CONFLICT_POLICIES",
    "NOTIFY_EVENTS",
    "ConfigError",
    "AppConfig",
    "AutoSourceConfig",
    "CrawlConfig",
    "DedupConfig",
    "FilterIntentConfig",
    "IntentConfig",
    "LLMConfig",
    "NotifyConfig",
    "ObservabilityConfig",
    "PrefilterConfig",
    "PreprocessConfig",
    "PushConfig",
    "SeleniumConfig",
    "StorageConfig",
    "TagsConfig",
    "WeComPushConfig",
    "load_app_config",
    "load_filter_intent",
    "parse_app_config",
    "parse_filter_intent",
]
