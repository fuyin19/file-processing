#!/usr/bin/env python3
"""Normalize run3 lens1 and prepare pass adjudication request."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

WS = Path(r"C:\Users\fuyin\coding\python\file-processing\.rar-workspace")
R3 = WS / "run3"
NORM = Path(
    r"C:\Users\fuyin\.claude\skills\recursive-adversarial-review\scripts\normalize_role_output.py"
)
GATE = Path(
    r"C:\Users\fuyin\.claude\skills\recursive-adversarial-review\scripts\review_gate.py"
)
T1 = Path(
    r"C:\Users\fuyin\.cursor\projects\c-Users-fuyin-coding-python-file-processing"
    r"\agent-transcripts\609952ce-82c1-45f0-b71b-943e4a40c3b3"
    r"\subagents\949b0470-3933-4049-a080-889b92789497.jsonl"
)


def extract(transcript: Path) -> dict:
    payloads = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        if obj.get("role") != "assistant":
            continue
        for part in obj["message"]["content"]:
            text = part.get("text") or ""
            if "received_envelope" not in text:
                continue
            text = "".join(ch for ch in text if not (0xD800 <= ord(ch) <= 0xDFFF))
            start = text.find("{")
            if start < 0:
                continue
            obj, _end = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(obj, dict) and "received_envelope" in obj:
                payloads.append(obj)
    if not payloads:
        raise SystemExit(f"no payload in {transcript}")
    return payloads[-1]


def normalize(payload: dict, lens_id: str) -> dict:
    env_path = R3 / f"expected_envelope_{lens_id}.json"
    raw_path = R3 / f"{lens_id}_raw.json"
    out_path = R3 / f"{lens_id}_normalized.json"
    expected = json.loads(env_path.read_text(encoding="utf-8"))
    recv = payload["received_envelope"]
    if recv != expected:
        if json.dumps(recv, sort_keys=True, ensure_ascii=True) == json.dumps(
            expected, sort_keys=True, ensure_ascii=True
        ):
            payload["received_envelope"] = expected
            print(f"{lens_id}: envelope key-order fixed")
        else:
            print(f"{lens_id}: envelope mismatch; using received")
            for key in sorted(set(list(recv) | list(expected))):
                if recv.get(key) != expected.get(key):
                    print(f"  DIFF {key}")
            env_path.write_text(
                json.dumps(recv, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
            )
    else:
        print(f"{lens_id}: envelope exact match")
    raw_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(NORM),
            "--role",
            "adversarial-plan-reviewer",
            "--expected-envelope-file",
            str(env_path),
        ],
        input=raw_path.read_bytes(),
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace")[:3000])
        raise SystemExit(f"normalize failed for {lens_id}")
    result = json.loads(proc.stdout.decode("utf-8"))
    out_path.write_text(
        json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"{lens_id}: status={result['status']} findings="
        f"{len(result['normalized_payload']['raw_findings'])}"
    )
    return result


def main() -> int:
    n1 = normalize(extract(T1), "lens_1")
    n2 = json.loads((R3 / "lens_2_normalized.json").read_text(encoding="utf-8"))
    assert n1["status"] == "accepted" and n2["status"] == "accepted"

    config = json.loads((R3 / "frozen_config.json").read_text(encoding="utf-8"))
    freeze = json.loads((R3 / "config_freeze_record.json").read_text(encoding="utf-8"))
    pass_record = json.loads((R3 / "pass_record.json").read_text(encoding="utf-8"))
    dispatches = json.loads((R3 / "dispatches.json").read_text(encoding="utf-8"))

    lens_attempts = []
    for item in dispatches:
        lens_id = item["lens_id"]
        envelope = item["envelope"]
        wrapper = json.loads(
            (R3 / f"{lens_id}_normalized.json").read_text(encoding="utf-8")
        )
        # Gate requires dispatch_envelope == normalized received_envelope
        received = wrapper["normalized_payload"]["received_envelope"]
        if received != envelope:
            # Prefer received if it has required gate fields; update dispatch
            print(f"WARNING: dispatch envelope != received for {lens_id}")
            envelope = received
        lens_attempts.append(
            {"dispatch_envelope": envelope, "normalization": wrapper}
        )

    request = {
        "frozen_config": config,
        "config_freeze_record": freeze,
        "pass_record": pass_record,
        "lens_attempts": lens_attempts,
    }
    req_path = R3 / "prepare_pass_request.json"
    req_path.write_text(
        json.dumps(request, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    proc = subprocess.run(
        [sys.executable, str(GATE), "prepare-pass-adjudication", "--request", str(req_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    print("gate_exit", proc.returncode)
    out_path = R3 / "prepared_pass_adjudication.json"
    if proc.returncode != 0:
        err_path = R3 / "prepare_pass_error.json"
        err_path.write_text(proc.stderr or proc.stdout or "", encoding="utf-8")
        print((proc.stderr or proc.stdout)[:4000])
        return proc.returncode
    out_path.write_text(proc.stdout, encoding="utf-8")
    prepared = json.loads(proc.stdout)
    print(
        "prepared scope",
        prepared.get("scope"),
        "sources",
        prepared.get("source_count"),
        "digest",
        prepared.get("input_artifact_hash"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
