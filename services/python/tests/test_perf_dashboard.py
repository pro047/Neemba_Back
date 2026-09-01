"""Grafana 성능 대시보드(infra/grafana/provisioning/dashboards/perf.json) 정적 회귀 테스트.

대시보드는 앱 코드가 아니지만 패널의 PromQL 이 참조하는 지표 이름은 앱 코드
(src/monitoring/metrics.py · services/node/src/monitoring/metrics.ts)와 계약이다.
이름이 한쪽에서 바뀌면 Grafana 는 조용히 빈 그래프를 그린다 — 그 단절을 여기서
잡는다. Grafana 런타임 렌더링은 이 테스트 범위 밖이다(perf-grafana-dashboard DESIGN §6).

python 쪽 허용 지표는 하드코딩하지 않고 prometheus_client REGISTRY 에서 읽는다
(metrics 모듈 임포트 시 등록). node 쪽은 TS 라 상수 목록으로 둔다.
"""
import copy
import json
import re
from collections.abc import Iterator
from pathlib import Path

from prometheus_client import REGISTRY

import src.monitoring.metrics  # noqa: F401  — registers neemba_* collectors on REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[3]
DASHBOARD_PATH = REPO_ROOT / "infra" / "grafana" / "provisioning" / "dashboards" / "perf.json"
GITIGNORE_PATH = REPO_ROOT / ".gitignore"
GITIGNORE_EXCEPTION_LINE = "!infra/grafana/provisioning/dashboards/*.json"

EXPECTED_DATASOURCE = {"type": "prometheus", "uid": "${DS}"}

# Keep in sync with services/node/src/monitoring/metrics.ts (6 custom metrics)
# plus the collectDefaultMetrics event-loop series the dashboard uses.
NODE_METRIC_NAMES: frozenset[str] = frozenset({
    "neemba_stt_paused",
    "neemba_ffmpeg_stale_total",
    "neemba_publish_buffer_dropped_total",
    "neemba_publish_buffer_size",
    "neemba_rtmp_auth_enabled",
    "neemba_session_stopped_total",
    "nodejs_eventloop_lag_p50_seconds",
    "nodejs_eventloop_lag_p99_seconds",
    "nodejs_eventloop_lag_mean_seconds",
})
# Process/platform collectors exposed by both prometheus_client and prom-client,
# plus Prometheus's own `up`.
PROCESS_METRIC_NAMES: frozenset[str] = frozenset({
    "process_cpu_seconds_total",
    "process_resident_memory_bytes",
    "process_open_fds",
    "process_max_fds",
    "python_gc_collections_total",
    "up",
})
# nginx-prometheus-exporter — only the series the dashboard actually uses.
# Metrics the dashboard must reference somewhere (DESIGN §5 T7).
REQUIRED_EXPR_METRICS: frozenset[str] = frozenset({
    "neemba_event_loop_lag_seconds_bucket",
    "neemba_translate_duration_seconds_bucket",
    "neemba_sentence_queue_depth",
    "neemba_publish_buffer_size",
    "neemba_consumer_unparseable_total",
    "neemba_hub_listeners",
    "process_cpu_seconds_total",
})

_METRIC_TOKEN = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_CANDIDATE_PREFIXES = ("neemba_", "nodejs_", "process_", "python_", "nginx_")


def load_dashboard() -> dict:
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))


def iter_panels(dashboard: dict) -> Iterator[dict]:
    """Yield every panel, flattening panels nested inside (collapsed) rows."""
    for panel in dashboard.get("panels", []):
        yield panel
        yield from panel.get("panels", [])


def iter_targets(dashboard: dict) -> Iterator[tuple[dict, dict]]:
    for panel in iter_panels(dashboard):
        for target in panel.get("targets", []):
            yield panel, target


def metric_names_in(expr: str) -> set[str]:
    """Tokens that look like metric names from the scraped jobs.

    Function names (rate, sum, histogram_quantile, increase, time, by, le),
    label names (job) and label values (python, node) never carry one of the
    candidate prefixes, so they are excluded by construction.
    """
    return {
        tok for tok in _METRIC_TOKEN.findall(expr)
        if tok == "up" or tok.startswith(_CANDIDATE_PREFIXES)
    }


def python_metric_names() -> set[str]:
    """Sample names prometheus_client would expose for the neemba_* collectors.

    Reading REGISTRY.collect() gives the exact exposition names (``_total``,
    ``_bucket``/``_count``/``_sum``) instead of guessing suffixes — a rename in
    metrics.py breaks this test the same way it would break the dashboard.
    """
    names: set[str] = set()
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name.startswith("neemba_"):
                names.add(sample.name)
    return names


def allowed_metric_names() -> set[str]:
    return python_metric_names() | NODE_METRIC_NAMES | PROCESS_METRIC_NAMES


def unknown_metric_names(dashboard: dict) -> dict[str, set[str]]:
    """expr -> metric tokens not in the allow-list (empty dict when all known)."""
    allowed = allowed_metric_names()
    unknown: dict[str, set[str]] = {}
    for _, target in iter_targets(dashboard):
        expr = target["expr"]
        bad = metric_names_in(expr) - allowed
        if bad:
            unknown[expr] = bad
    return unknown


# --- T1 ------------------------------------------------------------------------

def test_dashboard_file_exists_and_parses_as_json_object():
    assert DASHBOARD_PATH.is_file(), f"missing {DASHBOARD_PATH}"

    dashboard = load_dashboard()

    assert isinstance(dashboard, dict)


# --- T2 ------------------------------------------------------------------------

def test_dashboard_has_provisioning_keys_with_null_id_and_fixed_uid():
    dashboard = load_dashboard()

    assert dashboard["id"] is None
    assert dashboard["uid"] == "neemba-perf"
    for key in ("title", "panels", "schemaVersion", "templating"):
        assert key in dashboard, f"missing top-level key {key!r}"
    schema_version = dashboard["schemaVersion"]
    assert isinstance(schema_version, int) and not isinstance(schema_version, bool)
    assert schema_version >= 36


# --- T3 ------------------------------------------------------------------------

def test_templating_has_exactly_one_prometheus_datasource_variable_named_DS():
    dashboard = load_dashboard()

    ds_vars = [
        v for v in dashboard["templating"]["list"]
        if v.get("name") == "DS" and v.get("type") == "datasource" and v.get("query") == "prometheus"
    ]

    assert len(ds_vars) == 1, f"expected one DS datasource variable, got {ds_vars}"


# --- T4 ------------------------------------------------------------------------

def test_every_non_row_panel_and_target_uses_the_DS_template_datasource():
    dashboard = load_dashboard()
    non_row_panels = [p for p in iter_panels(dashboard) if p.get("type") != "row"]
    assert non_row_panels, "dashboard has no non-row panels"

    panel_violations = [
        (p.get("id"), p.get("datasource"))
        for p in non_row_panels
        if p.get("datasource") != EXPECTED_DATASOURCE
    ]
    target_violations = [
        (p.get("id"), t.get("refId"), t.get("datasource"))
        for p in non_row_panels
        for t in p.get("targets", [])
        if t.get("datasource") != EXPECTED_DATASOURCE
    ]

    assert panel_violations == [], f"panels not on {EXPECTED_DATASOURCE}: {panel_violations}"
    assert target_violations == [], f"targets not on {EXPECTED_DATASOURCE}: {target_violations}"


# --- T5 ------------------------------------------------------------------------

def test_panel_ids_are_unique_integers():
    dashboard = load_dashboard()

    ids = [p.get("id") for p in iter_panels(dashboard)]

    assert ids, "dashboard has no panels"
    non_int = [i for i in ids if not isinstance(i, int) or isinstance(i, bool)]
    assert non_int == [], f"non-integer panel ids: {non_int}"
    assert len(set(ids)) == len(ids), f"duplicate panel ids: {sorted(i for i in ids if ids.count(i) > 1)}"


# --- T6 ------------------------------------------------------------------------

def test_every_metric_name_in_expr_is_a_known_scraped_metric():
    dashboard = load_dashboard()

    unknown = unknown_metric_names(dashboard)

    assert unknown == {}, "unknown metric names in expr:\n" + "\n".join(
        f"  {expr!r}: {sorted(bad)}" for expr, bad in unknown.items()
    )


def test_metric_allowlist_check_rejects_typo_in_expr():
    # Mutation self-check (DESIGN §5): prove T6 has teeth without editing perf.json.
    dashboard = load_dashboard()
    typo = "neemba_translate_duration_second_bucket"  # missing 's'
    mutated = copy.deepcopy(dashboard)
    first_target = next(t for _, t in iter_targets(mutated) if "neemba_translate_duration_seconds_bucket" in t["expr"])
    first_target["expr"] = first_target["expr"].replace("neemba_translate_duration_seconds_bucket", typo)

    unknown = unknown_metric_names(mutated)

    assert any(typo in bad for bad in unknown.values()), f"typo {typo!r} was not reported: {unknown}"


def test_metric_name_extractor_ignores_functions_labels_and_grafana_variables():
    expr = 'histogram_quantile(0.99, sum(rate(neemba_x_bucket{job="python"}[$__rate_interval])) by (le)) + process_cpu_seconds_total{job="node"} + up'

    names = metric_names_in(expr)

    assert names == {"neemba_x_bucket", "process_cpu_seconds_total", "up"}


def test_python_metric_names_come_from_registry_with_exposition_suffixes():
    names = python_metric_names()

    # Histogram expands to _bucket/_count/_sum, Counter to _total, Gauge stays bare.
    assert {
        "neemba_event_loop_lag_seconds_bucket",
        "neemba_event_loop_lag_seconds_count",
        "neemba_translate_duration_seconds_bucket",
        "neemba_translate_duration_seconds_count",
        "neemba_hub_send_failed_total",
        "neemba_sentence_queue_depth",
    } <= names
    # Bare histogram/counter names are NOT valid PromQL selectors and must not be allowed.
    assert "neemba_event_loop_lag_seconds" not in names
    assert "neemba_hub_send_failed" not in names


# --- T7 ------------------------------------------------------------------------

def test_required_metrics_all_appear_somewhere_in_dashboard_exprs():
    dashboard = load_dashboard()

    referenced: set[str] = set()
    for _, target in iter_targets(dashboard):
        referenced |= metric_names_in(target["expr"])

    missing = REQUIRED_EXPR_METRICS - referenced
    assert missing == set(), f"required metrics not referenced by any panel: {sorted(missing)}"


# --- T8 ------------------------------------------------------------------------

def test_one_timeseries_panel_overlays_python_loop_lag_with_python_cpu():
    dashboard = load_dashboard()

    matching = [
        p for p in iter_panels(dashboard)
        if p.get("type") == "timeseries"
        and any("neemba_event_loop_lag_seconds_bucket" in t["expr"] for t in p.get("targets", []))
        and any('process_cpu_seconds_total{job="python"}' in t["expr"] for t in p.get("targets", []))
    ]

    assert matching, "no timeseries panel combines neemba_event_loop_lag_seconds_bucket with process_cpu_seconds_total{job=\"python\"}"


# --- T9 ------------------------------------------------------------------------

def test_bucket_exprs_use_histogram_quantile_over_sum_by_le():
    dashboard = load_dashboard()
    bucket_exprs = [t["expr"] for _, t in iter_targets(dashboard) if "_bucket" in t["expr"]]
    assert bucket_exprs, "dashboard has no _bucket exprs"

    bad = [
        e for e in bucket_exprs
        if not e.startswith("histogram_quantile(") or "by (le)" not in e
    ]

    assert bad == [], f"_bucket exprs that do not use histogram_quantile(... by (le)): {bad}"


# --- T10 -----------------------------------------------------------------------

def test_gitignore_has_exception_line_for_grafana_dashboard_json():
    lines = GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()

    assert GITIGNORE_EXCEPTION_LINE in lines, (
        f"{GITIGNORE_EXCEPTION_LINE!r} missing from .gitignore — perf.json would be dropped from commits"
    )
