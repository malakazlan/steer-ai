"""The probe runner: repeated snapshots of chosen windows, summarised into a report."""

import json

from steerai.probes.stats import LatencyStats
from steerai.probes.uia_latency import ProbeResult, format_report, parse_args
from steerai.probes.uia_snapshot import Snapshot


def _result(title: str, samples: list[float], nodes: int) -> ProbeResult:
    return ProbeResult(
        title=title,
        hwnd=0x1234,
        pid=42,
        stats=LatencyStats.from_samples(samples),
        last=Snapshot(
            scope="subtree",
            node_count=nodes,
            elapsed_ms=samples[-1],
            by_control_type={"Text": nodes},
        ),
    )


def test_parse_args_defaults() -> None:
    args = parse_args(["--title", "Chrome"])
    assert args.titles == ["Chrome"]
    assert args.runs == 10
    assert args.warmup == 2
    assert args.out is None


def test_parse_args_multiple_titles_and_output() -> None:
    argv = ["--title", "Chrome", "--title", "Explorer", "--runs", "3", "--out", "r.json"]
    args = parse_args(argv)
    assert args.titles == ["Chrome", "Explorer"]
    assert args.runs == 3
    assert args.out == "r.json"


def test_report_table_has_one_row_per_window_with_numbers() -> None:
    text = format_report([_result("Chrome - Inbox", [120.0, 130.0, 140.0], 2500)])
    assert "Chrome - Inbox" in text
    assert "2500" in text
    assert "130.0" in text  # p50
    assert "140.0" in text  # p95 and max


def test_report_json_is_machine_readable() -> None:
    result = _result("Explorer", [5.0, 6.0], 40)
    payload = json.loads(json.dumps([result.to_dict()]))
    assert payload[0]["title"] == "Explorer"
    assert payload[0]["node_count"] == 40
    assert payload[0]["p50_ms"] == 5.0  # nearest rank: ceil(0.5 * 2) = rank 1
    assert payload[0]["max_ms"] == 6.0
    assert payload[0]["by_control_type"] == {"Text": 40}
