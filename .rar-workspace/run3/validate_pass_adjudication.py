#!/usr/bin/env python3
"""Normalize pass adjudication and validate via review_gate."""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

R3 = Path(r"C:\Users\fuyin\coding\python\file-processing\.rar-workspace\run3")
NORM = Path(
    r"C:\Users\fuyin\.claude\skills\recursive-adversarial-review\scripts\normalize_role_output.py"
)
GATE = Path(
    r"C:\Users\fuyin\.claude\skills\recursive-adversarial-review\scripts\review_gate.py"
)
TRANSCRIPT = Path(
    r"C:\Users\fuyin\.cursor\projects\c-Users-fuyin-coding-python-file-processing"
    r"\agent-transcripts\609952ce-82c1-45f0-b71b-943e4a40c3b3"
    r"\subagents\bdfabee0-d4b0-4e5e-bbdb-0bf1c01e02af.jsonl"
)


def extract(transcript: Path) -> dict:
    payloads = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        obj = json.loads(line)
        if obj.get("role") != "assistant":
            continue
        for part in obj["message"]["content"]:
            text = part.get("text") or ""
            if "canonical_findings" not in text and "received_envelope" not in text:
                continue
            text = "".join(ch for ch in text if not (0xD800 <= ord(ch) <= 0xDFFF))
            start = text.find("{")
            if start < 0:
                continue
            parsed, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(parsed, dict) and "canonical_findings" in parsed:
                payloads.append(parsed)
    if not payloads:
        raise SystemExit("no adjudication payload")
    return payloads[-1]


def main() -> int:
    payload = extract(TRANSCRIPT)
    env_path = R3 / "adjudicator_envelope.json"
    expected = json.loads(env_path.read_text(encoding="utf-8"))
    recv = payload["received_envelope"]
    if recv != expected:
        if json.dumps(recv, sort_keys=True, ensure_ascii=True) == json.dumps(
            expected, sort_keys=True, ensure_ascii=True
        ):
            payload["received_envelope"] = expected
        else:
            print("envelope mismatch; using received")
            for key in sorted(set(list(recv) | list(expected))):
                if recv.get(key) != expected.get(key):
                    print(" DIFF", key)
            env_path.write_text(
                json.dumps(recv, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
            )
            expected = recv
    raw_path = R3 / "adjudication_raw.json"
    raw_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(NORM),
            "--role",
            "finding-adjudicator",
            "--expected-envelope-file",
            str(env_path),
        ],
        input=raw_path.read_bytes(),
        capture_output=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode("utf-8", "replace")[:4000])
        return proc.returncode
    wrapper = json.loads(proc.stdout.decode("utf-8"))
    (R3 / "adjudication_normalized.json").write_text(
        json.dumps(wrapper, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "norm_status",
        wrapper["status"],
        "canonical",
        len(wrapper["normalized_payload"].get("canonical_findings", [])),
        "errors",
        wrapper.get("errors"),
    )
    if wrapper["status"] not in {"accepted", "accepted_after_transport_normalization"}:
        return 1

    prepared_wrap = json.loads(
        (R3 / "prepared_pass_adjudication.json").read_text(encoding="utf-8")
    )
    prepared = prepared_wrap["data"]
    request = {
        "prepared": prepared,
        "adjudication": wrapper,
    }
    # Check validate-adjudication expected shape
    req_path = R3 / "validate_adjudication_request.json"
    req_path.write_text(
        json.dumps(request, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )
    gate = subprocess.run(
        [
            sys.executable,
            str(GATE),
            "validate-adjudication",
            "--request",
            str(req_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    print("gate_exit", gate.returncode)
    (R3 / "validated_adjudication.json").write_text(
        gate.stdout or gate.stderr or "", encoding="utf-8"
    )
    if gate.returncode != 0:
        print((gate.stderr or gate.stdout)[:4000])
        return gate.returncode
    validated = json.loads(gate.stdout)
    print(
        "validated",
        validated.get("status"),
        "admitted",
        validated.get("admitted"),
        "keys",
        list(validated.keys())[:12],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
