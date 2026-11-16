from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


AssetSymbol = Literal["BTC", "ETH", "SOL"]
Stance = Literal["看多", "中性偏多", "中性", "中性偏空", "看空"]
FeatureHorizon = Literal["INTRADAY", "DAILY", "SWING", "STRUCTURAL"]
NewsCategory = Literal["CATALYST", "FLOW", "MACRO", "REGULATION", "FUNDAMENTAL", "SECURITY", "POSITIONING", "NOISE"]


class Candle(BaseModel):
    opened_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class MetricPoint(BaseModel):
    at: datetime
    value: float


class MarketSnapshot(BaseModel):
    candle_volume_unit: str = ""
    symbol: AssetSymbol
    as_of: datetime
    price: float
    change_24h_pct: float
    volume_24h_usd: float
    market_cap_usd: float | None = None
    fully_diluted_valuation_usd: float | None = None
    circulating_supply: float | None = None
    total_supply: float | None = None
    max_supply: float | None = None
    ath_change_pct: float | None = None
    cross_source_deviation_pct: float | None = None
    comparison_price_usd: float | None = None
    comparison_as_of: datetime | None = None
    funding_rate: float | None = None
    annualized_basis_pct: float | None = None
    open_interest_usd: float | None = None
    open_interest_change_1h_pct: float | None = None
    derivatives_as_of: datetime | None = None
    candles: list[Candle]
    candles_15m: list[Candle] = Field(default_factory=list)
    daily_candles: list[Candle] = Field(default_factory=list)
    sources: list[str]
    quality_flags: list[str] = Field(default_factory=list)


class NewsItem(BaseModel):
    id: str
    title: str
    url: str
    source: str
    published_at: datetime
    assets: list[AssetSymbol]
    sentiment: float = Field(ge=-1, le=1)
    source_score: int = Field(ge=0, le=100)
    summary: str = ""
    resolved_url: str | None = None
    category: NewsCategory = "NOISE"
    asset_relevance: float = Field(default=0, ge=0, le=1)
    price_relevance: float = Field(default=0, ge=0, le=1)
    causal_path: str = ""
    time_horizon: FeatureHorizon = "DAILY"
    novelty: float = Field(default=0, ge=0, le=1)


class FundamentalSnapshot(BaseModel):
    symbol: AssetSymbol
    as_of: datetime
    tvl_usd: float | None = None
    tvl_change_7d_pct: float | None = None
    fees_24h_usd: float | None = None
    fees_change_1d_pct: float | None = None
    stablecoin_supply_usd: float | None = None
    hashrate_eh_s: float | None = None
    hashrate_change_30d_pct: float | None = None
    difficulty: float | None = None
    difficulty_change_30d_pct: float | None = None
    transaction_count_24h: float | None = None
    active_addresses_24h: float | None = None
    sources: list[str]
    tvl_history: list[MetricPoint] = Field(default_factory=list)
    fees_history: list[MetricPoint] = Field(default_factory=list)
    stablecoin_history: list[MetricPoint] = Field(default_factory=list)
    hashrate_history: list[MetricPoint] = Field(default_factory=list)
    difficulty_history: list[MetricPoint] = Field(default_factory=list)
    network_fees_history: list[MetricPoint] = Field(default_factory=list)


class OptionsSnapshot(BaseModel):
    symbol: Literal["BTC", "ETH"]
    as_of: datetime
    total_open_interest_usd: float
    put_call_oi_ratio: float
    near_atm_iv_pct: float | None = None
    near_call_put_iv_spread_pct: float | None = None
    term_iv_slope_pct: float | None = None
    contracts_count: int
    sources: list[str]


class ProfessionalMetricsSnapshot(BaseModel):
    symbol: AssetSymbol
    as_of: datetime
    spot_etf_net_flow_usd: float | None = None
    exchange_netflow_usd: float | None = None
    stablecoin_supply_change_7d_pct: float | None = None
    options_25d_skew_pct: float | None = None
    cme_basis_annualized_pct: float | None = None
    active_addresses_24h: float | None = None
    transaction_count_24h: float | None = None
    sources: list[str] = Field(min_length=1)


class MarketAggregateSnapshot(BaseModel):
    as_of: datetime
    total_market_cap_usd: float | None = None
    total_volume_24h_usd: float | None = None
    market_cap_change_24h_pct: float | None = None
    btc_dominance_pct: float | None = None
    eth_dominance_pct: float | None = None
    active_cryptocurrencies: int | None = None
    breadth_advancers: int | None = None
    breadth_decliners: int | None = None
    breadth_unchanged: int | None = None
    breadth_universe: int | None = None
    source: str = "CoinGecko"


class SentimentSnapshot(BaseModel):
    as_of: datetime
    value: int | None = Field(default=None, ge=0, le=100)
    classification: str = "UNAVAILABLE"
    previous_value: int | None = Field(default=None, ge=0, le=100)
    source: str = "Alternative.me Crypto Fear & Greed Index"
    attribution_url: str = "https://alternative.me/crypto/fear-and-greed-index/"


class LiquidationAssetBreakdown(BaseModel):
    symbol: AssetSymbol
    total_usd_24h: float | None = None
    long_usd_24h: float | None = None
    short_usd_24h: float | None = None


class LiquidationSnapshot(BaseModel):
    as_of: datetime
    coverage: Literal["MARKET_WIDE", "VENUE_SAMPLE", "UNAVAILABLE"] = "UNAVAILABLE"
    window: str = "24H"
    total_usd_24h: float | None = None
    long_usd_24h: float | None = None
    short_usd_24h: float | None = None
    assets: list[LiquidationAssetBreakdown] = Field(default_factory=list)
    venues: list[str] = Field(default_factory=list)
    source: str = ""
    note: str = ""


class MacroCalendarEvent(BaseModel):
    name: str
    scheduled_at: datetime
    source: str
    url: str
    category: Literal["FOMC", "NONFARM", "CPI", "LABOR", "INFLATION", "MACRO"] = "MACRO"


class MarketContextSnapshot(BaseModel):
    as_of: datetime
    aggregate: MarketAggregateSnapshot | None = None
    sentiment: SentimentSnapshot | None = None
    liquidations: LiquidationSnapshot | None = None
    macro_calendar: list[MacroCalendarEvent] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)


class FeatureObservation(BaseModel):
    """A value with availability semantics. Missing values are never represented as zero."""

    key: str
    label: str
    is_available: bool
    value: float | str | None = None
    unit: str = ""
    timestamp: datetime | None = None
    source: str | None = None
    freshness_seconds: int | None = Field(default=None, ge=0)
    quality: Literal["HIGH", "MEDIUM", "LOW", "UNAVAILABLE"] = "UNAVAILABLE"
    horizon: FeatureHorizon

    @model_validator(mode="after")
    def unavailable_values_are_null(self):
        if not self.is_available and self.value is not None:
            raise ValueError("unavailable feature value must be null")
        if self.is_available and self.value is None:
            raise ValueError("available feature value must not be null")
        return self


class ConfidenceProfile(BaseModel):
    view_conviction: int = Field(ge=0, le=100)
    evidence_quality: int = Field(ge=0, le=100)
    data_coverage: int = Field(ge=0, le=100)
    model_confidence: int | None = Field(default=None, ge=0, le=100)
    trade_quality: int | None = Field(default=None, ge=0, le=100)


class TradePlanTarget(BaseModel):
    """Structured target metadata while keeping TradeIdea.targets backward compatible."""

    label: Literal["TP1", "TP2", "TP3"]
    price: float
    risk_reward: float = Field(gt=0)
    reduce_pct: int = Field(ge=0, le=100)


class TradeIdea(BaseModel):
    trigger_price: float | None = None
    action: Literal["LONG", "SHORT", "PAIR_TRADE", "WAIT", "NO_TRADE"]
    state: Literal["INACTIVE", "APPROACHING", "ARMED", "TRIGGERED", "ACTIVE", "INVALIDATED", "EXPIRED"]
    symbol: str
    thesis: str
    trigger: str = ""
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    targets: list[float] = Field(default_factory=list)
    target_plan: list[TradePlanTarget] = Field(default_factory=list)
    direction: Literal["LONG", "SHORT"] | None = None
    entry_method: str = ""
    cancel_condition: str = ""
    data_status: Literal["FRESH", "STALE", "UNAVAILABLE"] = "FRESH"
    freshness_seconds: int | None = Field(default=None, ge=0)
    expected_r: float | None = None
    expected_return_pct: float | None = None
    time_horizon: str
    expiration: datetime | None = None
    invalidation: str
    distance_to_trigger_pct: float | None = None


class DailyScenario(BaseModel):
    name: Literal["BASE", "BULL", "BEAR"]
    likelihood: Literal["HIGH", "MEDIUM", "LOW"]
    condition: str
    expected_range_low: float | None = None
    expected_range_high: float | None = None
    catalyst: str
    confirmation: str
    invalidation: str


class DailySessionWindow(BaseModel):
    session_date: date
    start_at: datetime
    end_at: datetime
    timezone: str = "Asia/Shanghai"
    completeness: Literal["COMPLETE", "PARTIAL", "FALLBACK"] = "COMPLETE"


class MarketDaySummary(BaseModel):
    one_liner: str
    trend: str
    total_market_cap_usd: float | None = None
    total_market_cap_change_24h_pct: float | None = None
    total_volume_24h_usd: float | None = None
    btc_dominance_pct: float | None = None
    breadth: str = "UNAVAILABLE"
    main_up_phase: str = ""
    main_down_phase: str = ""
    attribution: list[str] = Field(default_factory=list)


class ChainActivityView(BaseModel):
    symbol: AssetSymbol
    activity: Literal["积极", "改善", "中性", "降温", "数据不足"] = "数据不足"
    score: int | None = Field(default=None, ge=-100, le=100)
    coverage_pct: int = Field(default=0, ge=0, le=100)
    transactions_24h: float | None = None
    active_addresses_24h: float | None = None
    fees_24h_usd: float | None = None
    fees_change_1d_pct: float | None = None
    tvl_usd: float | None = None
    tvl_change_7d_pct: float | None = None
    stablecoin_supply_usd: float | None = None
    stablecoin_supply_change_7d_pct: float | None = None
    exchange_netflow_usd: float | None = None
    conclusion: str = ""
    sources: list[str] = Field(default_factory=list)


class NewsImpact(BaseModel):
    event_id: str
    headline: str
    occurred_at: datetime
    source: str
    url: str
    category: str
    affected_assets: list[AssetSymbol] = Field(default_factory=list)
    observed_market_reaction: str
    transmission_path: str
    onchain_sentiment_impact: str
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "LOW"
    is_inference: bool = True


class AssetLevelSummary(BaseModel):
    symbol: AssetSymbol
    supports: list[float] = Field(default_factory=list)
    resistances: list[float] = Field(default_factory=list)
    invalidation: str = ""


class MarketOutlookScenario(BaseModel):
    name: Literal["BASE", "BULL", "BEAR"]
    likelihood: Literal["HIGH", "MEDIUM", "LOW"]
    condition: str
    market_path: str
    confirmation: str
    invalidation: str
    risk_events: list[str] = Field(default_factory=list)


class DataHealthGroup(BaseModel):
    source: str
    availability: Literal["AVAILABLE", "PARTIAL", "UNAVAILABLE"]
    quality: Literal["HIGH", "MEDIUM", "LOW", "UNAVAILABLE"]
    freshness: str
    available_features: list[str] = Field(default_factory=list)
    missing_features: list[str] = Field(default_factory=list)


class AttributionItem(BaseModel):
    driver: Literal["MACRO", "SPOT_DEMAND", "ETF_FLOWS", "DERIVATIVES", "OPTIONS", "ON_CHAIN", "TECHNICAL", "EVENT"]
    contribution: Literal["POSITIVE", "NEGATIVE", "MIXED", "UNAVAILABLE"]
    mechanism: str
    evidence_ids: list[str] = Field(default_factory=list)


class DailyAssetView(BaseModel):
    session_volume_unit: str = ""
    comparison_source: str = ""
    comparison_notes: list[str] = Field(default_factory=list)
    symbol: AssetSymbol
    spot: float | None
    return_24h_pct: float | None
    view: Stance
    view_change: str
    time_horizon: str = "24–72H"
    thesis: str
    what_changed: str
    key_driver: str
    supporting_evidence: list[str]
    contradicting_evidence: list[str]
    catalysts: list[str]
    invalidation: str
    expected_range_low: float | None = None
    expected_range_high: float | None = None
    preferred_expression: str = "NO_TRADE"
    session_open: float | None = None
    session_high: float | None = None
    session_low: float | None = None
    session_close: float | None = None
    session_volume: float | None = None
    return_day_pct: float | None = None
    max_drawdown_pct: float | None = None
    strength_rank: int | None = Field(default=None, ge=1, le=3)
    trend_summary: str = ""
    supports: list[float] = Field(default_factory=list)
    resistances: list[float] = Field(default_factory=list)
    confidence: ConfidenceProfile
    observations: list[FeatureObservation] = Field(default_factory=list)


class RelativeValueView(BaseModel):
    pair: Literal["BTC/ETH", "ETH/SOL", "SOL/BTC"]
    view: str
    rationale: str
    evidence: list[str] = Field(default_factory=list)


class DataHealthItem(BaseModel):
    source: str
    feature: str
    freshness: str
    availability: Literal["AVAILABLE", "PARTIAL", "UNAVAILABLE"]
    quality: Literal["HIGH", "MEDIUM", "LOW", "UNAVAILABLE"]


class DailyStrategyPayload(BaseModel):
    objective: str = "复盘北京时间完整自然日，并形成未来 24–72H 市场情景。"
    executive_dashboard: list[DailyAssetView]
    key_takeaways: list[str]
    market_regime: str
    regime_evidence: list[str]
    what_changed_24h: list[str]
    attribution: list[AttributionItem]
    relative_value: list[RelativeValueView]
    positioning_and_flows: list[FeatureObservation]
    volatility_and_options: list[FeatureObservation]
    scenarios: dict[AssetSymbol, list[DailyScenario]]
    preferred_trades: list[TradeIdea] = Field(default_factory=list)
    catalyst_calendar: list[str]
    risk_monitor: list[str]
    data_health: list[DataHealthItem]
    session_window: DailySessionWindow | None = None
    market_day: MarketDaySummary | None = None
    chain_activity: list[ChainActivityView] = Field(default_factory=list)
    sentiment: SentimentSnapshot | None = None
    liquidations: LiquidationSnapshot | None = None
    news_impacts: list[NewsImpact] = Field(default_factory=list)
    levels: dict[AssetSymbol, AssetLevelSummary] = Field(default_factory=dict)
    outlook: list[MarketOutlookScenario] = Field(default_factory=list)
    macro_calendar: list[MacroCalendarEvent] = Field(default_factory=list)
    data_health_groups: list[DataHealthGroup] = Field(default_factory=list)


class TacticalAssetState(BaseModel):
    comparison_source: str = ""
    comparison_notes: list[str] = Field(default_factory=list)
    symbol: AssetSymbol
    previous_view: Stance | None = None
    current_view: Stance
    price: float | None = None
    price_change_since_checkpoint_pct: float | None = None
    return_24h_pct: float | None = None
    volume_24h_usd: float | None = None
    funding_rate: float | None = None
    open_interest_usd: float | None = None
    open_interest_change_1h_pct: float | None = None
    trend_15m: str = "UNAVAILABLE"
    trend_1h: str = "UNAVAILABLE"
    trend_4h: str = "UNAVAILABLE"
    trend_1d: str = "UNAVAILABLE"
    signal_change: str
    action: Literal["LONG", "SHORT", "WAIT", "NO_ACTION"]
    state: str
    bias: str
    momentum: str
    positioning: str
    volatility: str
    dominant_driver: str = ""
    counter_evidence: str = ""
    trend_summary: str = ""
    decision: Literal["LONG", "SHORT", "UNCLEAR"] = "UNCLEAR"
    supports: list[float] = Field(default_factory=list)
    resistances: list[float] = Field(default_factory=list)
    price_freshness_seconds: int | None = Field(default=None, ge=0)
    derivatives_freshness_seconds: int | None = Field(default=None, ge=0)
    primary_setup: TradeIdea | None = None
    conditional_setups: list[TradeIdea] = Field(default_factory=list)
    trigger_distance_pct: float | None = None
    trade_state: Literal["INACTIVE", "APPROACHING", "ARMED", "TRIGGERED", "ACTIVE", "INVALIDATED", "EXPIRED"]
    thesis_status: Literal["THESIS_VALID", "THESIS_STRENGTHENING", "THESIS_WEAKENING", "THESIS_INVALIDATED"]
    thesis_status_reason: str
    what_changed: list[str]
    before_next_checkpoint: list[str]
    confidence: ConfidenceProfile
    active_setup: TradeIdea | None = None
    reference_setups: list[TradeIdea] = Field(default_factory=list)
    observations: list[FeatureObservation] = Field(default_factory=list)


class TacticalMonitorPayload(BaseModel):
    objective: str = "Describe changes since the last checkpoint and actions before the next checkpoint."
    previous_checkpoint: datetime | None = None
    current_checkpoint: datetime
    next_checkpoint: datetime
    market_overview: list[str] = Field(default_factory=list)
    market_brief: str = ""
    market_metrics: MarketAggregateSnapshot | None = None
    market_breadth: str = ""
    leadership: str = ""
    volatility_state: str = ""
    positioning_state: str = ""
    since_last_checkpoint: list[str]
    dashboard: list[TacticalAssetState]
    active_setups: list[TradeIdea]
    changed_variables: list[str]
    before_next_checkpoint: list[str]
    no_action: bool = False


class EvidenceItem(BaseModel):
    id: str
    claim_id: str
    source_id: str
    source_type: str
    source_tier: int = Field(ge=1, le=4)
    source_quality: int = Field(ge=0, le=100)
    primary_or_secondary: Literal["PRIMARY", "SECONDARY"]
    topic_relevance: float = Field(ge=0, le=1)
    thesis_relevance: float = Field(ge=0, le=1)
    causal_relevance: float = Field(ge=0, le=1)
    supports_or_contradicts: Literal["SUPPORTS", "CONTRADICTS", "CONTEXT"]
    fact_extracted: str
    timestamp: datetime
    entity: str = ""
    evidence_role: Literal["ANCHOR_FACT", "MECHANISM", "CONTEXT", "COUNTER_EVIDENCE", "MARKET_DATA", "PRIMARY_SOURCE", "CATALYST", "INVALIDATION"]


class ThesisStrength(BaseModel):
    specificity: int = Field(ge=0, le=20)
    novelty: int = Field(ge=0, le=20)
    evidence_support: int = Field(ge=0, le=20)
    causal_depth: int = Field(ge=0, le=20)
    investor_relevance: int = Field(ge=0, le=20)

    @property
    def total(self) -> int:
        return self.specificity + self.novelty + self.evidence_support + self.causal_depth + self.investor_relevance


class ThesisCandidate(BaseModel):
    research_question: str
    observed_anomaly: str
    market_consensus: str = ""
    market_consensus_evidence_ids: list[str] = Field(default_factory=list)
    meridian_thesis: str
    causal_mechanism: list[str]
    why_now: str
    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str]
    investment_implication: str
    catalysts: list[str]
    invalidation_conditions: list[str]
    confidence: ConfidenceProfile
    strength: ThesisStrength


class NarrativeParagraphPlan(BaseModel):
    paragraph_goal: str
    claim: str
    evidence_ids: list[str]
    transition_from: str = ""
    transition_to: str = ""


class NarrativePlan(BaseModel):
    hook_anomaly: str
    core_question: str
    thesis: str
    causal_chain: list[str]
    strongest_counterargument: str
    second_order_effects: list[str]
    investment_implication: str
    what_to_watch: list[str]
    invalidation: list[str]
    paragraphs: list[NarrativeParagraphPlan] = Field(default_factory=list)


class ResearchCritique(BaseModel):
    thesis_coherence: int = Field(ge=0, le=20)
    evidence_quality: int = Field(ge=0, le=20)
    causal_depth: int = Field(ge=0, le=20)
    originality: int = Field(ge=0, le=15)
    investor_utility: int = Field(ge=0, le=15)
    narrative_quality: int = Field(ge=0, le=10)
    issues: list[str] = Field(default_factory=list)
    revision_notes: list[str] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return self.thesis_coherence + self.evidence_quality + self.causal_depth + self.originality + self.investor_utility + self.narrative_quality


class DeepResearchPayload(BaseModel):
    news_brief: dict[str, str] = Field(default_factory=dict)
    editorial_version: str = ""
    editorial_review: dict[str, Any] = Field(default_factory=dict)
    publication_commitment: Literal["DAILY_DEEP_RESEARCH"] = "DAILY_DEEP_RESEARCH"
    research_type: Literal["EVENT_DRIVEN", "THESIS_UPDATE", "STRUCTURAL_RESEARCH", "ASSET_PROTOCOL_DEEP_DIVE", "POST_MORTEM", "DATA_DIVERGENCE"]
    topic_origin: Literal["DAILY_EVENTS", "ROLLING_BACKLOG", "BACKUP_TOPIC", "DETERMINISTIC_FALLBACK"]
    primary_topic: str
    backup_topic: str = ""
    thesis: ThesisCandidate | None = None
    evidence_graph: list[EvidenceItem] = Field(default_factory=list)
    narrative_plan: NarrativePlan | None = None
    critique: ResearchCritique | None = None
    publication_notes: list[str] = Field(default_factory=list)


class DimensionAssessment(BaseModel):
    key: Literal["news", "technical", "flow", "fundamental"]
    label: str
    score: int = Field(ge=-100, le=100)
    coverage: int = Field(ge=0, le=100)
    headline: str
    evidence: list[str]


class Scenario(BaseModel):
    name: Literal["基准", "上行", "下行"]
    probability: int = Field(ge=0, le=100)
    path: str
    range_low: float
    range_high: float


class KeyLevel(BaseModel):
    label: str
    value: float


class TradeTarget(BaseModel):
    label: Literal["TP1", "TP2", "TP3"]
    price: float
    risk_reward: float = Field(gt=0)
    reduce_pct: int = Field(ge=0, le=100)


class TradeSetup(BaseModel):
    name: str
    priority: str
    direction: Literal["做多", "做空"]
    status: Literal["可执行", "等待确认", "等待触发"]
    entry_low: float
    entry_high: float
    entry_method: str
    trigger: str
    stop_loss: float
    targets: list[TradeTarget]
    cancel_condition: str
    holding_period: str = "24–48H"
    risk_per_trade_pct: float = Field(gt=0, le=2)
    max_leverage: float = Field(gt=0, le=3)
    position_size_formula: str
    advice: str
    rationale: list[str]


class AssetReport(BaseModel):
    symbol: AssetSymbol
    as_of: datetime
    horizon: str = "24h"
    price: float
    change_24h_pct: float
    stance: Stance
    conviction: int = Field(ge=0, le=100)
    target_low: float
    target_high: float
    invalidation_price: float
    invalidation_condition: str
    thesis: str
    dominant_driver: str
    decision_hinge: str = ""
    counter_case: str = ""
    monitoring_points: list[str] = Field(default_factory=list)
    dimensions: list[DimensionAssessment]
    scenarios: list[Scenario]
    key_levels: list[KeyLevel]
    trade_setups: list[TradeSetup] = Field(default_factory=list)
    chart: list[float]
    evidence_ids: list[str]
    data_sources: list[str] = Field(default_factory=list)
    data_quality: int = Field(ge=0, le=100)
    quality_flags: list[str]

    @model_validator(mode="after")
    def probabilities_sum_to_100(self):
        if sum(s.probability for s in self.scenarios) != 100:
            raise ValueError("scenario probabilities must sum to 100")
        return self


class ResearchFact(BaseModel):
    unit: str = ""
    window: str = ""
    method: str = ""
    published_at: datetime | None = None
    id: str
    category: Literal["market", "technical", "flow", "fundamental", "news", "valuation", "risk", "event", "background", "industry"]
    statement: str
    source: str
    as_of: datetime
    url: str | None = None
    claim_type: Literal["FACT", "INFERENCE", "MERIDIAN_VIEW", "FORECAST"] = "FACT"
    supporting_fact_ids: list[str] = Field(default_factory=list)
    time_horizon: str | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    catalyst: str | None = None
    invalidation: str | None = None


class ResearchSource(BaseModel):
    id: str
    title: str
    url: str
    source: str
    published_at: datetime
    source_type: Literal["media", "official"] = "media"
    access: Literal["full", "summary", "title"] = "title"
    assets: list[str] = Field(default_factory=list)
    summary: str = ""
    key_facts: list[str] = Field(default_factory=list)
    source_score: int = Field(ge=0, le=100)
    content_chars: int = 0
    text_hash: str = ""
    source_tier: int = Field(default=3, ge=1, le=4)
    topic_relevance: float = Field(default=0, ge=0, le=1)
    thesis_relevance: float = Field(default=0, ge=0, le=1)
    causal_relevance: float = Field(default=0, ge=0, le=1)
    evidence_role: Literal["ANCHOR_FACT", "MECHANISM", "CONTEXT", "COUNTER_EVIDENCE", "MARKET_DATA", "PRIMARY_SOURCE", "CATALYST", "INVALIDATION"] = "CONTEXT"


class ResearchNote(BaseModel):
    label: str = ""
    text: str
    source_ids: list[str] = Field(default_factory=list)


class ResearchPacket(BaseModel):
    news_brief: dict[str, str] = Field(default_factory=dict)
    editorial_version: str = ""
    source_texts: dict[str, str] = Field(default_factory=dict, exclude=False)
    id: str
    research_date: date
    created_at: datetime
    updated_at: datetime
    topic: str
    research_question: str
    thesis_angle: str
    selection_score: float = 0
    selection_factors: dict[str, float] = Field(default_factory=dict)
    outline: list[str] = Field(default_factory=list)
    sources: list[ResearchSource] = Field(default_factory=list)
    facts: list[ResearchFact] = Field(default_factory=list)
    timeline: list[ResearchNote] = Field(default_factory=list)
    source_consensus: list[ResearchNote] = Field(default_factory=list)
    source_conflicts: list[ResearchNote] = Field(default_factory=list)
    historical_context: list[ResearchNote] = Field(default_factory=list)
    data_points: list[ResearchNote] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    research_type: Literal["EVENT_DRIVEN", "THESIS_UPDATE", "STRUCTURAL_RESEARCH", "ASSET_PROTOCOL_DEEP_DIVE", "POST_MORTEM", "DATA_DIVERGENCE"] = "EVENT_DRIVEN"
    topic_origin: Literal["DAILY_EVENTS", "ROLLING_BACKLOG", "BACKUP_TOPIC", "DETERMINISTIC_FALLBACK"] = "DAILY_EVENTS"
    backup_topic: str = ""
    thesis_candidate: ThesisCandidate | None = None
    evidence_graph: list[EvidenceItem] = Field(default_factory=list)
    narrative_plan: NarrativePlan | None = None
    status: Literal["prepared", "generating", "drafting", "repairing", "validating", "published", "failed"] = "prepared"
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class ChartSeries(BaseModel):
    name: str
    values: list[float]


class ChartSpec(BaseModel):
    id: str
    title: str
    subtitle: str = ""
    chart_type: Literal["line", "bar"] = "line"
    unit: str = ""
    labels: list[str]
    series: list[ChartSeries]
    source: str
    insight: str
    chart_question: str = ""
    claim_supported: str = ""
    why_it_matters: str = ""

    @model_validator(mode="after")
    def series_match_labels(self):
        if any(len(item.values) != len(self.labels) for item in self.series):
            raise ValueError("chart series length must match labels")
        return self


class ArticleParagraph(BaseModel):
    text: str
    fact_ids: list[str] = Field(default_factory=list)


class ArticleSection(BaseModel):
    heading: str
    paragraphs: list[ArticleParagraph]


class ArticleNarrative(BaseModel):
    research_question: str
    deck: str
    lead: str
    lead_fact_ids: list[str] = Field(default_factory=list)
    consensus_view: str = ""
    consensus_fact_ids: list[str] = Field(default_factory=list)
    meridian_view: str = ""
    meridian_fact_ids: list[str] = Field(default_factory=list)
    sections: list[ArticleSection]
    conclusion: str
    conclusion_fact_ids: list[str] = Field(default_factory=list)
    calculations: list[dict[str, Any]] = Field(default_factory=list)


class InsightDraftFragment(BaseModel):
    id: str
    research_date: date
    packet_id: str
    fragment_key: str
    kind: Literal["frame", "sections", "article"]
    position: int = Field(ge=0)
    headings: list[str] = Field(default_factory=list)
    status: Literal["pending", "generating", "complete", "failed"] = "pending"
    payload: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str = ""
    created_at: datetime
    updated_at: datetime


class InsightJobRun(BaseModel):
    id: str
    research_date: date
    stage: Literal["prepare", "generate", "retry"]
    status: Literal["running", "succeeded", "failed", "skipped"] = "running"
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class EditorialAudit(BaseModel):
    sections: dict[str, str] = Field(default_factory=dict)
    provider: str = "deterministic"
    model: str = ""
    status: Literal["deterministic", "model", "model_short", "fallback", "failed"] = "deterministic"
    attempts: int = 0
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    warnings: list[str] = Field(default_factory=list)


class ReportHeader(BaseModel):
    id: str
    as_of: datetime
    slot: Literal["00:10", "08:00", "12:00", "16:00", "20:00", "24:00", "manual"]
    report_type: Literal["standard", "midday", "daily", "insight"] = "standard"
    title: str
    created_at: datetime
    published_at: datetime | None = None
    publication_key: str | None = None
    revision: int = Field(default=1, ge=1)
    publication_status: Literal["CURRENT", "SUPERSEDED"] = "CURRENT"
    is_saved: bool = False


class ResearchReport(ReportHeader):
    market_regime: str
    executive_judgement: str
    key_takeaways: list[str] = Field(default_factory=list)
    what_changed: list[str] = Field(default_factory=list)
    watchlist: list[str] = Field(default_factory=list)
    assets: list[AssetReport] = Field(default_factory=list)
    news: list[NewsItem]
    methodology_version: str = "meridian-execution-v2"
    schema_version: str = "4.0"
    supersedes_id: str | None = None
    superseded_by_id: str | None = None
    facts: list[ResearchFact] = Field(default_factory=list)
    charts: list[ChartSpec] = Field(default_factory=list)
    article: ArticleNarrative | None = None
    editorial_audit: EditorialAudit = Field(default_factory=EditorialAudit)
    research_packet_id: str | None = None
    insight_sources: list[ResearchSource] = Field(default_factory=list)
    insight_status: Literal["full", "short"] | None = None
    daily_strategy: DailyStrategyPayload | None = None
    tactical_monitor: TacticalMonitorPayload | None = None
    deep_research: DeepResearchPayload | None = None
    disclaimer: str = "研究信息不构成投资建议。预测包含不确定性，请以失效条件和风险预算管理风险。"
    markdown: str = ""


class ForecastOutcome(BaseModel):
    report_id: str
    symbol: AssetSymbol
    horizon: str = "24h"
    evaluated_at: datetime
    entry_price: float
    exit_price: float
    return_pct: float
    direction_correct: bool
    target_hit: bool
    max_favorable_excursion_pct: float
    max_adverse_excursion_pct: float
    brier_score: float | None = Field(default=None, ge=0)
    revision: int = 1
    forecast_at: datetime | None = None
