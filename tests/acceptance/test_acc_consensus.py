"""Эталонные тесты CONTRACTS §5 (is_issued, build_consensus, is_priority). Не править в шагах."""
import pytest

consensus = pytest.importorskip("trading_assistant.llm.consensus")
models = pytest.importorskip("trading_assistant.models")
config = pytest.importorskip("trading_assistant.config")


def signal(**kw):
    base = dict(
        min_confidence=85, min_severity=6, min_move_pct=15, max_horizon_days=180,
        magnitude_tolerance=0.20, priority_move_pct=25, priority_fast_days=120,
    )
    base.update(kw)
    return config.SignalSettings(**base)


def item(ticker="SBER", direction="up", conf=90, h=60, pct=18.0):
    return models.LLMInstrument(
        ticker=ticker, direction=direction, confidence=conf,
        horizon_days=h, expected_change_pct=pct, reason="x",
    )


def analysis(*items, severity=8):
    return models.LLMAnalysis(severity=severity, instruments=list(items))


@pytest.mark.parametrize(
    "kw,ok",
    [
        (dict(conf=85), True), (dict(conf=84), False),
        (dict(pct=15.0), True), (dict(pct=14.9), False),
        (dict(h=180), True), (dict(h=181), False), (dict(h=1), True),
    ],
)
def test_is_issued_boundaries(kw, ok):
    assert consensus.is_issued(item(**kw), 8, signal()) is ok


@pytest.mark.parametrize("sev,ok", [(6, True), (5, False)])
def test_is_issued_severity(sev, ok):
    assert consensus.is_issued(item(), sev, signal()) is ok


def test_consensus_within_tolerance():
    rows = consensus.build_consensus(
        analysis(item(conf=90, h=60, pct=15)), analysis(item(conf=88, h=90, pct=18)), signal())
    assert len(rows) == 1
    r = rows[0]
    assert (r.direction, r.confidence, r.expected_change_pct, r.horizon_days) == ("up", 88, 15, 90)


def test_consensus_magnitude_gap_too_big():
    assert consensus.build_consensus(
        analysis(item(pct=15)), analysis(item(pct=19)), signal()) == []


def test_consensus_gap_exactly_20_percent_ok():
    assert len(consensus.build_consensus(
        analysis(item(pct=15)), analysis(item(pct=18)), signal())) == 1


def test_consensus_gap_just_over_20_percent_rejected():
    assert consensus.build_consensus(
        analysis(item(pct=15)), analysis(item(pct=18.01)), signal()) == []


def test_consensus_directions_differ():
    assert consensus.build_consensus(
        analysis(item(direction="up")), analysis(item(direction="down")), signal()) == []


def test_consensus_horizons_not_compared_takes_larger():
    rows = consensus.build_consensus(
        analysis(item(h=30)), analysis(item(h=170)), signal())
    assert len(rows) == 1 and rows[0].horizon_days == 170


def test_consensus_requires_both_models_issued():
    assert consensus.build_consensus(
        analysis(item(conf=84)), analysis(item(conf=95)), signal()) == []
    assert consensus.build_consensus(
        analysis(item(h=181)), analysis(item(h=60)), signal()) == []


def test_consensus_severity_is_minimum_and_checked():
    assert consensus.build_consensus(
        analysis(item(), severity=5), analysis(item(), severity=9), signal()) == []
    rows = consensus.build_consensus(
        analysis(item(), severity=7), analysis(item(), severity=9), signal())
    assert rows[0].severity == 7


def test_consensus_only_common_tickers():
    rows = consensus.build_consensus(
        analysis(item("SBER"), item("LKOH")), analysis(item("SBER"), item("TATN")), signal())
    assert [r.ticker for r in rows] == ["SBER"]


@pytest.mark.parametrize(
    "pct,h,expected",
    [
        (25.0, 150, False), (25.1, 150, True),
        (15.0, 119, True), (15.0, 120, False), (14.9, 10, False),
    ],
)
def test_is_priority(pct, h, expected):
    row = consensus.ConsensusRow("SBER", "up", 90, 8, h, pct, "x")
    assert consensus.is_priority(row, signal()) is expected
