"""Scheduled analysis runner (analysis/runner.py) against known, synthetic
data dirs.

Uses tests/fixtures/make_fixtures.py's deterministic throwaway-key
approach (FIXTURE_KEY_1/2, FIXTURE_DID_1/2, _sign), the same pattern
tests/test_cohort.py and tests/test_diurnal.py use -- no real did:key
identity involved, and make_fixtures.py itself is not modified.

The main scenario (BASE = 2_000_000_000, a 5 hour continuous coverage
span [BASE, BASE+18000]):

  cohort shape:  window=1h, gap=2h -> w1=[BASE+3600, BASE+7200),
                 gap=[BASE+7200, BASE+14400), w2=[BASE+14400, BASE+18000)
  diurnal shape: span=3h -> window=[BASE+7200, BASE+18000)

"Cadence" messages (signed by FIXTURE_KEY_1/FIXTURE_DID_1) run every 1800s
from BASE to BASE+18000 inclusive (11 messages) purely to give the diurnal
curve something to bin; FIXTURE_DID_1's own first-ever ts is BASE, before
w1 starts, so it is never a cohort member itself.

One additional key (FIXTURE_KEY_2/FIXTURE_DID_2) posts once inside w1
(BASE+4000, its first-ever appearance anywhere in the file) and again
inside w2 (BASE+15000) -- the cohort scenario: cohort_size=1, returned=1.
"""

import json
import os
from datetime import datetime, timezone

from make_fixtures import FIXTURE_DID_1, FIXTURE_DID_2, FIXTURE_KEY_1, FIXTURE_KEY_2, _sign

from analysis.runner import (
    LONGITUDINAL_MEASUREMENTS,
    WINDOW_DEPENDENT_MEASUREMENTS,
    determine_continuous_span,
    run_once,
)

ROOM = "lobby"
BASE = 2_000_000_000


def _iso(ts_seconds):
    return datetime.fromtimestamp(ts_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _signed(key, did, seq, ts_seconds, nonce, text):
    ts = _iso(ts_seconds)
    return {
        "room": ROOM,
        "seq": seq,
        "ts": ts,
        "from": did,
        "text": text,
        "nonce": str(nonce),
        "sig": _sign(key, ROOM, str(nonce), text),
        "captured_at": ts,
        "source": "test",
    }


def _write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _coverage_record(captured_at_seconds, captured_total, dropped_total):
    ts = _iso(captured_at_seconds)
    return {
        "room": ROOM,
        "captured_total": captured_total,
        "dropped_total": dropped_total,
        "coverage": None,
        "cursor": None,
        "captured_at": ts,
        "source": "test",
    }


def _cadence_messages(start_offset=0, end_offset=18000, step=1800):
    records = []
    seq = 1
    for offset in range(start_offset, end_offset + 1, step):
        records.append(
            _signed(FIXTURE_KEY_1, FIXTURE_DID_1, seq, BASE + offset, 8000000 + offset, f"cadence tick {offset}")
        )
        seq += 1
    return records


def _cohort_probe_messages(seq_start=1000):
    return [
        _signed(FIXTURE_KEY_2, FIXTURE_DID_2, seq_start, BASE + 4000, 9100001, "cohort probe first appearance"),
        _signed(FIXTURE_KEY_2, FIXTURE_DID_2, seq_start + 1, BASE + 15000, 9100002, "cohort probe returns"),
    ]


def _setup_main_scenario(tmp_path):
    data_dir = tmp_path / "data"
    all_messages = _cadence_messages() + _cohort_probe_messages()
    _write_jsonl(str(data_dir / "rooms" / ROOM / "messages.jsonl"), all_messages)
    _write_jsonl(
        str(data_dir / "coverage.jsonl"),
        [_coverage_record(BASE, 0, 0), _coverage_record(BASE + 18000, 20, 2)],
    )
    return str(data_dir), all_messages


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_measurement_kind_lists_are_disjoint_and_match_the_task_split():
    assert set(LONGITUDINAL_MEASUREMENTS) == {"cohort", "diurnal", "selfaudit"}
    assert set(WINDOW_DEPENDENT_MEASUREMENTS) == {
        "duplication",
        "coordination",
        "nonce",
        "clustering",
        "diversity",
    }
    assert set(LONGITUDINAL_MEASUREMENTS).isdisjoint(WINDOW_DEPENDENT_MEASUREMENTS)


def test_continuous_span_from_coverage_matches_the_snapshot_bounds(tmp_path):
    data_dir, _ = _setup_main_scenario(tmp_path)
    span = determine_continuous_span(data_dir, ROOM)

    assert span["span_source"] == "coverage"
    assert span["span_start_seconds"] == BASE
    assert span["span_end_seconds"] == BASE + 18000
    assert span["restarts_detected"] == 0


def test_runner_positions_cohort_and_diurnal_on_the_correct_fixed_windows(tmp_path):
    data_dir, all_messages = _setup_main_scenario(tmp_path)
    out_dir = os.path.join(data_dir, "analysis", "history")

    summary = run_once(
        data_dir,
        room=ROOM,
        cohort_window_hours=1.0,
        cohort_gap_hours=2.0,
        diurnal_span_hours=3.0,
        out_dir=out_dir,
    )

    assert summary["skipped"] == []
    ran_names = {entry["measurement"] for entry in summary["ran"]}
    # default scope: longitudinal only, no window-dependent full-file passes
    assert ran_names == set(LONGITUDINAL_MEASUREMENTS)
    assert ran_names.isdisjoint(WINDOW_DEPENDENT_MEASUREMENTS)

    cohort_records = _read_jsonl(os.path.join(out_dir, "cohort.jsonl"))
    assert len(cohort_records) == 1
    cohort_record = cohort_records[0]
    assert cohort_record["window_kind"] == "fixed_shape"
    assert cohort_record["w1_start"] == _iso(BASE + 3600)
    assert cohort_record["w1_end"] == _iso(BASE + 7200)
    assert cohort_record["w2_start"] == _iso(BASE + 14400)
    assert cohort_record["w2_end"] == _iso(BASE + 18000)
    assert cohort_record["result"]["cohort_size"] == 1
    assert cohort_record["result"]["returned_count"] == 1
    assert cohort_record["result"]["persistence_rate"] == 1.0
    # self-describing: coverage is right there beside the window bounds
    assert "w2_coverage_ratio" in cohort_record["result"]

    diurnal_records = _read_jsonl(os.path.join(out_dir, "diurnal.jsonl"))
    assert len(diurnal_records) == 1
    diurnal_record = diurnal_records[0]
    assert diurnal_record["window_kind"] == "fixed_shape"
    assert diurnal_record["window_start"] == _iso(BASE + 7200)
    assert diurnal_record["window_end"] == _iso(BASE + 18000)
    expected_captured = sum(
        1 for m in all_messages if BASE + 7200 <= _iso_to_seconds(m["ts"]) < BASE + 18000
    )
    assert diurnal_record["result"]["total_captured_posts"] == expected_captured
    assert "overall_coverage_ratio" in diurnal_record["result"]

    selfaudit_records = _read_jsonl(os.path.join(out_dir, "selfaudit.jsonl"))
    assert len(selfaudit_records) == 1
    assert selfaudit_records[0]["window_kind"] == "cumulative"


def _iso_to_seconds(ts):
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    return datetime.fromisoformat(normalized).timestamp()


def test_history_files_are_append_only_across_two_runs(tmp_path):
    data_dir, _ = _setup_main_scenario(tmp_path)
    out_dir = os.path.join(data_dir, "analysis", "history")

    run_once(data_dir, room=ROOM, cohort_window_hours=1.0, cohort_gap_hours=2.0, diurnal_span_hours=3.0, out_dir=out_dir)
    run_once(data_dir, room=ROOM, cohort_window_hours=1.0, cohort_gap_hours=2.0, diurnal_span_hours=3.0, out_dir=out_dir)

    cohort_records = _read_jsonl(os.path.join(out_dir, "cohort.jsonl"))
    assert len(cohort_records) == 2
    selfaudit_records = _read_jsonl(os.path.join(out_dir, "selfaudit.jsonl"))
    assert len(selfaudit_records) == 2
    # both runs' records are still there, in order, neither overwritten
    assert cohort_records[0]["run_timestamp"] <= cohort_records[1]["run_timestamp"]
    # default scope never touches diversity.jsonl at all
    assert not os.path.exists(os.path.join(out_dir, "diversity.jsonl"))


def test_cohort_skipped_for_insufficient_span_not_run_on_a_shrunk_window(tmp_path):
    data_dir = tmp_path / "data"
    messages = _cadence_messages(start_offset=0, end_offset=100, step=50)  # tiny span: 100s
    _write_jsonl(str(data_dir / "rooms" / ROOM / "messages.jsonl"), messages)
    _write_jsonl(
        str(data_dir / "coverage.jsonl"),
        [_coverage_record(BASE, 0, 0), _coverage_record(BASE + 100, 3, 0)],
    )
    out_dir = os.path.join(str(data_dir), "analysis", "history")

    summary = run_once(
        str(data_dir),
        room=ROOM,
        cohort_window_hours=1.0,  # needs 2*3600 + 7200 = 14400s, only 100s available
        cohort_gap_hours=2.0,
        diurnal_span_hours=1.0,
        out_dir=out_dir,
    )

    skipped_names = {entry["measurement"] for entry in summary["skipped"]}
    assert "cohort" in skipped_names
    assert "diurnal" in skipped_names
    cohort_skip = next(e for e in summary["skipped"] if e["measurement"] == "cohort")
    assert cohort_skip["status"] == "skipped"
    assert "insufficient" in cohort_skip["reason"]
    assert cohort_skip["available_span_hours"] < cohort_skip["required_span_hours"]
    # never archived on a shrunk window
    assert not os.path.exists(os.path.join(out_dir, "cohort.jsonl"))
    assert not os.path.exists(os.path.join(out_dir, "diurnal.jsonl"))

    # window-dependent measurements are not part of the default round at
    # all (span-sufficient or not); selfaudit has no shape to skip
    ran_names = {entry["measurement"] for entry in summary["ran"]}
    assert ran_names.isdisjoint(WINDOW_DEPENDENT_MEASUREMENTS)
    assert "selfaudit" in ran_names


def test_restart_seam_is_not_straddled_by_a_window(tmp_path):
    data_dir = tmp_path / "data"
    # Naive full range is BASE .. BASE+18000 (5h), plenty for a 1h/2h cohort
    # shape (needs 4h) if restarts were ignored. But a restart happens at
    # BASE+9000: captured_total resets from 40 down to 2. The continuous
    # span usable for windows is only the post-restart run,
    # [BASE+9000, BASE+18000) = 2.5h, too short for the 4h shape.
    coverage_records = [
        _coverage_record(BASE, 0, 0),
        _coverage_record(BASE + 9000, 40, 4),   # pre-restart snapshot
        _coverage_record(BASE + 9000, 2, 0),    # restart: counters reset lower
        _coverage_record(BASE + 18000, 25, 3),
    ]
    _write_jsonl(str(data_dir / "coverage.jsonl"), coverage_records)
    messages = _cadence_messages(start_offset=0, end_offset=18000, step=1800)
    _write_jsonl(str(data_dir / "rooms" / ROOM / "messages.jsonl"), messages)
    out_dir = os.path.join(str(data_dir), "analysis", "history")

    span = determine_continuous_span(str(data_dir), ROOM)
    assert span["span_start_seconds"] == BASE + 9000
    assert span["span_end_seconds"] == BASE + 18000
    assert span["restarts_detected"] == 1

    summary = run_once(
        str(data_dir),
        room=ROOM,
        cohort_window_hours=1.0,
        cohort_gap_hours=2.0,
        diurnal_span_hours=1.0,
        out_dir=out_dir,
    )

    cohort_skip = next(e for e in summary["skipped"] if e["measurement"] == "cohort")
    assert "insufficient" in cohort_skip["reason"]
    assert not os.path.exists(os.path.join(out_dir, "cohort.jsonl"))

    # diurnal's 1h shape DOES fit inside the 2.5h post-restart span, and
    # must be positioned entirely within it, never crossing back before
    # the restart at BASE+9000.
    diurnal_records = _read_jsonl(os.path.join(out_dir, "diurnal.jsonl"))
    assert len(diurnal_records) == 1
    assert _iso_to_seconds(diurnal_records[0]["window_start"]) >= BASE + 9000


def test_window_dependent_measurements_are_not_run_by_default(tmp_path):
    data_dir, _ = _setup_main_scenario(tmp_path)
    out_dir = os.path.join(data_dir, "analysis", "history")

    summary = run_once(
        data_dir, room=ROOM, cohort_window_hours=1.0, cohort_gap_hours=2.0, diurnal_span_hours=3.0, out_dir=out_dir
    )

    assert summary["include_snapshots"] is False
    ran_names = {entry["measurement"] for entry in summary["ran"]}
    for name in WINDOW_DEPENDENT_MEASUREMENTS:
        assert name not in ran_names
        assert not os.path.exists(os.path.join(out_dir, f"{name}.jsonl"))


def test_include_snapshots_opt_in_runs_and_archives_window_dependent_as_snapshots(tmp_path):
    data_dir, _ = _setup_main_scenario(tmp_path)
    out_dir = os.path.join(data_dir, "analysis", "history")

    summary = run_once(
        data_dir,
        room=ROOM,
        cohort_window_hours=1.0,
        cohort_gap_hours=2.0,
        diurnal_span_hours=3.0,
        out_dir=out_dir,
        include_snapshots=True,
    )

    assert summary["include_snapshots"] is True
    ran_names = {entry["measurement"] for entry in summary["ran"]}
    assert set(WINDOW_DEPENDENT_MEASUREMENTS) <= ran_names
    assert set(LONGITUDINAL_MEASUREMENTS) <= ran_names

    for name in WINDOW_DEPENDENT_MEASUREMENTS:
        records = _read_jsonl(os.path.join(out_dir, f"{name}.jsonl"))
        assert len(records) == 1
        record = records[0]
        assert record["window_kind"] == "snapshot"
        assert "as_of" in record
        assert "fixed_shape" != record["window_kind"]
        assert "not a trend point" in record["note"] or "NOT a trend point" in record["note"]

    # longitudinal measurements, by contrast, never carry the snapshot label
    for name in ("cohort", "diurnal"):
        records = _read_jsonl(os.path.join(out_dir, f"{name}.jsonl"))
        assert records[0]["window_kind"] != "snapshot"


def test_cli_include_snapshots_flag_runs_window_dependent_measurements(tmp_path):
    from analysis.runner import main as runner_main

    data_dir, _ = _setup_main_scenario(tmp_path)
    out_dir = os.path.join(data_dir, "analysis", "history")

    exit_code = runner_main(
        [
            "--data-dir",
            data_dir,
            "--room",
            ROOM,
            "--cohort-window-hours",
            "1.0",
            "--cohort-gap-hours",
            "2.0",
            "--diurnal-span-hours",
            "3.0",
            "--out-dir",
            out_dir,
            "--include-snapshots",
        ]
    )

    assert exit_code == 0
    for name in WINDOW_DEPENDENT_MEASUREMENTS:
        assert os.path.exists(os.path.join(out_dir, f"{name}.jsonl"))


def test_no_key_material_referenced_anywhere_in_runner_module():
    import ast
    import inspect

    import analysis.runner as runner_module

    source = inspect.getsource(runner_module)
    tree = ast.parse(source)

    identifiers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.append(node.id)
        elif isinstance(node, ast.arg):
            identifiers.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            identifiers.append(node.arg)
        elif isinstance(node, ast.Attribute):
            identifiers.append(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.append(node.name)

    forbidden_substrings = ("passphrase", "getpass", "subprocess", "privatekey", "private_key", "pem")
    for identifier in identifiers:
        lowered = identifier.lower()
        for token in forbidden_substrings:
            assert token not in lowered, f"forbidden token {token!r} found in identifier {identifier!r}"

    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.extend(alias.name for alias in node.names)
    for name in imported_names:
        assert "private" not in name.lower(), f"unexpected private-key-shaped import: {name}"

    own_functions = [
        name
        for name, obj in inspect.getmembers(runner_module, inspect.isfunction)
        if obj.__module__ == runner_module.__name__
    ]
    for name in own_functions:
        assert "sign" not in name.lower(), f"unexpected signing-shaped function defined: {name}"


def test_no_did_string_in_any_archived_record(tmp_path):
    data_dir, _ = _setup_main_scenario(tmp_path)
    out_dir = os.path.join(data_dir, "analysis", "history")

    run_once(
        data_dir,
        room=ROOM,
        cohort_window_hours=1.0,
        cohort_gap_hours=2.0,
        diurnal_span_hours=3.0,
        out_dir=out_dir,
        include_snapshots=True,
    )

    for name in set(LONGITUDINAL_MEASUREMENTS) | set(WINDOW_DEPENDENT_MEASUREMENTS):
        path = os.path.join(out_dir, f"{name}.jsonl")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "did:key:" not in content
        assert FIXTURE_DID_1 not in content
        assert FIXTURE_DID_2 not in content


def test_no_em_dash_in_runner_module_source():
    import inspect

    import analysis.runner as runner_module

    source = inspect.getsource(runner_module)
    assert "—" not in source


def test_reads_only_never_writes_outside_out_dir(tmp_path):
    data_dir, _ = _setup_main_scenario(tmp_path)
    out_dir = os.path.join(data_dir, "analysis", "history")

    messages_path = os.path.join(data_dir, "rooms", ROOM, "messages.jsonl")
    coverage_path = os.path.join(data_dir, "coverage.jsonl")
    before_messages = os.path.getmtime(messages_path)
    before_coverage = os.path.getmtime(coverage_path)

    run_once(data_dir, room=ROOM, cohort_window_hours=1.0, cohort_gap_hours=2.0, diurnal_span_hours=3.0, out_dir=out_dir)

    assert os.path.getmtime(messages_path) == before_messages
    assert os.path.getmtime(coverage_path) == before_coverage
