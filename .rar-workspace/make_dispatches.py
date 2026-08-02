#!/usr/bin/env python3
import json
from pathlib import Path

ws = Path(__file__).resolve().parent
ids = json.loads((ws / "run_ids.json").read_text(encoding="utf-8"))
config = json.loads((ws / "resolved_config.json").read_text(encoding="utf-8"))
dispatches = []
for lens in config["lens_roster"]:
    dispatches.append(
        {
            "dispatch_id": f"{ids['full_pass_id']}-{lens['lens_id']}",
            "attempt_id": f"{ids['full_pass_id']}-{lens['lens_id']}-a1",
            "retry_of_attempt_id": None,
            "lens_id": lens["lens_id"],
            "lens_kind": lens["kind"],
            "focus_statement": lens["focus_statement"],
            "source": lens["source"],
            "user_focus_ids": lens["user_focus_ids"],
        }
    )
(ws / "dispatches.json").write_text(
    json.dumps(dispatches, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)
print(json.dumps({"context_package_hash": ids["context_package_hash"], "dispatches": dispatches}, indent=2))
