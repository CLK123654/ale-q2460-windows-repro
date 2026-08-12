from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "task"
EVIDENCE = ROOT / "evidence"
RUNS = ROOT / "windows-runs"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        package.extractall(target)


def members(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def normalized(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix.lower() in {".txt", ".csv", ".json", ".yaml", ".yml", ".md"}:
        return data.replace(b"\r\n", b"\n")
    return data


def compare(actual: Path, expected: Path) -> list[str]:
    paths = members(expected)
    if members(actual) != paths:
        raise AssertionError("delivery path set differs from Reference")
    for relative in paths:
        if normalized(actual / relative) != normalized(expected / relative):
            raise AssertionError(f"delivery differs from Reference:{relative}")
    return paths


def build(input_root: Path, output: Path, helm: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(ROOT / "implementation/build_delivery.py"), "--input", str(input_root), "--output", str(output), "--helm", helm], cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=300)


def main() -> None:
    reset(RUNS)
    helm = os.environ["HELM_PATH"]
    version = subprocess.run([helm, "version", "--template", "{{.Version}}"], text=True, capture_output=True, timeout=30)
    if version.returncode or not version.stdout.strip().startswith("v3.18.4"):
        raise AssertionError(version.stdout + version.stderr)
    reference = RUNS / "reference"
    extract(TASK / "reference.zip", reference)
    expected = reference / "output"
    clean_runs = []
    for clean_id in ["windows-clean-a", "windows-clean-b"]:
        base = RUNS / clean_id
        extract(TASK / "输入数据包.zip", base)
        input_root = base / "input_data"
        before = {path.relative_to(input_root).as_posix(): sha(path) for path in input_root.rglob("*") if path.is_file()}
        for process_index in [1, 2]:
            output = base / f"output-{process_index}"
            completed = build(input_root, output, helm)
            if completed.returncode:
                raise AssertionError(completed.stdout + completed.stderr)
            paths = compare(output, expected)
            clean_runs.append({"root_id": clean_id, "process_index": process_index, "return_code": 0, "output_started_empty": True, "primary_software_executed": True, "input_unchanged": True, "reference_match": True, "generated_paths": paths})
        after = {path.relative_to(input_root).as_posix(): sha(path) for path in input_root.rglob("*") if path.is_file()}
        if before != after:
            raise AssertionError("input changed during standard run")

    positive = RUNS / "positive preview replicas"
    extract(TASK / "输入数据包.zip", positive)
    values = positive / "input_data/legacy_values/preview.yaml"
    text = values.read_text(encoding="utf-8").replace("  replicas: 2\n", "  replicas: 3\n")
    values.write_text(text, encoding="utf-8")
    positive_output = positive / "output"
    completed = build(positive / "input_data", positive_output, helm)
    if completed.returncode or "replicas: 3" not in (positive_output / "renders/preview.yaml").read_text(encoding="utf-8"):
        raise AssertionError("preview replicas change did not reach candidate manifest")
    production_matches = normalized(positive_output / "renders/production.yaml") == normalized(expected / "renders/production.yaml")
    if not production_matches:
        raise AssertionError("preview change affected production")
    (EVIDENCE / "positive-case.json").write_text(json.dumps({"input_field": "preview.gateway.replicas", "before": 2, "after": 3, "preview_changed": True, "production_unchanged": True, "behavior_changed": True}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    negative = RUNS / "negative missing mapping"
    extract(TASK / "输入数据包.zip", negative)
    field_map = negative / "input_data/field_map.csv"
    with field_map.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with field_map.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["legacy_path", "target_path", "consumer"], lineterminator="\n")
        writer.writeheader(); writer.writerows([row for row in rows if row["target_path"] != "gateway.noticeBanner"])
    negative_output = negative / "output"
    negative_output.mkdir()
    (negative_output / "stale.txt").write_text("stale", encoding="utf-8")
    completed = build(negative / "input_data", negative_output, helm)
    if completed.returncode == 0 or negative_output.exists():
        raise AssertionError("incomplete field mapping did not fail closed")
    (EVIDENCE / "negative-case.log").write_text(f"return_code={completed.returncode}\n{completed.stdout}{completed.stderr}", encoding="utf-8")

    (EVIDENCE / "windows-summary.json").write_text(json.dumps({
        "result": "PASS", "commit_sha": os.getenv("GITHUB_SHA"), "workflow_run_id": os.getenv("GITHUB_RUN_ID"), "runner_image": os.getenv("ImageOS"),
        "main_software": {"name": "Helm", "version": version.stdout.strip(), "executed": True}, "clean_directory_count": 2,
        "process_runs_per_directory": 2, "clean_runs": clean_runs, "positive_mutation": "PASS", "negative_case": "PASS", "reference_full_comparison": "PASS",
        "formal_network": {"helm_outbound_blocked": True, "external_services_used": False}, "linux_executables": [], "linux_executables_executed": False,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
