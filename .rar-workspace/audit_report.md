# Adversarial Plan Audit: okf-frontmatter + markdown-conversion --okf/--workspace

## Result

Terminal: audit_complete
Review mode: audit
Convergence claim: none
Lens count: 2
Pass policy: required_full_passes=1
Completed full passes: 1
Write status: not_requested
Config revision/hash: 1 / sha256:efa9be4b10c458808d825f565dea7d971d0f2cadd83f63ebc5c18efca921efd6
Immutable plan version/hash: plan-v1 / sha256:1b4ffab5f116a1b5c6a831abb4e33b44e7ff28a511dcfb56fe4f359302f273ac
Operational attempts: adjudication a1 rejected (invalid recommendation "fix"); a2 rejected (corrupted input_artifact_hash + unresolved_contradictions); a3 accepted (adj-pass-1-ee82e96a)
Reconciliation: clean (pass-1 total mapping; zero rejected sources; zero unresolved contradictions)

## Summary

One-pass audit of the plan to add `/file-processing:okf-frontmatter` and link it from markdown-conversion via `--okf` / `--workspace`. Gate-validated adjudication admitted 10 raw sources into 9 canonical findings (ruamel dependency findings merged). Findings remain open; this terminal confirms audit coverage only — no revision, convergence, or execution authority.

## Frozen lens roster and focus coverage

- lens_1 (universal): goal, deliverable, feasibility, sequence, and prerequisites; source=universal; user_focus_ids=[]
- lens_2 (universal): assumptions, risk, recovery, completeness, and verification; source=universal; user_focus_ids=[]
- user focuses: none; focus_coverage: []

## Full-pass evidence

| pass | plan digest | lens id | kind | dispatch/result | context sufficiency | pass adjudication |
|---|---|---|---|---|---|---|
| 1 | sha256:1b4ffab5f116a1b5c6a831abb4e33b44e7ff28a511dcfb56fe4f359302f273ac | lens_1 | universal | pass-1-lens_1-a1 accepted (5 raw) | sufficient | adj-pass-1-ee82e96a validated |
| 1 | sha256:1b4ffab5f116a1b5c6a831abb4e33b44e7ff28a511dcfb56fe4f359302f273ac | lens_2 | universal | pass-1-lens_2-a1 accepted (5 raw) | sufficient | adj-pass-1-ee82e96a validated |

Context package: ctx-3879b6a7a72a / sha256:3a6cc94c80ecce4183dcfc11d35fab19a19ef792b7ca85ded332e6ea1c2a171c. Sources: 10. Canonical: 9. Rejected: 0. validated_result_digest: `sha256:f951bfd5ce80329537f8ac1dfc3f1a686a9bd27a41518f411ef4531fd2445055`.

## Final audit aggregation

required_full_passes=1: validated pass adjudication is reused; no separate final aggregation required.

## Open findings

| id | pass/source lens | original severity | evidence | recommendation | status |
|---|---|---|---|---|---|
| CF-01 | lens_1 | must-fix | Public CLI advertises a publish subcommand while Assumptions explicitly exclude executing publish and Cortex index/bundle generation, so the promised skill surf // 公开接口 L24 vs Assumptions L57 / 新脚本提供 `prepare / publish / apply / validate` 子命令 … 首版处理普通 concept Markdown；不负责生成 bundle `index.md`、日志、Cortex 索引或执行 publi | adopt (non-binding) | open |
| CF-02 | lens_1 | must-fix | The --okf conversion path gates final output on Agent propose → diff → Plan → Apply, but markdown-conversion today is a deterministic single CLI with no Plan/Ap // Summary L7; Implementation Changes L39; Bounded context: Plan/Apply not in markdown-conversion; pipeline.py is only CLI / 所有写入均采用“Agent 提议 → 展示差异 → Plan → Apply”。… 只有 frontmatter 计划获批并通过校验后才创建最终输出 | adopt (non-binding) | open |
| CF-03 | lens_1,lens_2 | must-fix | Round-trip YAML preservation depends on ruamel.yaml, which is not a plugin dependency today; the plan states the requirement but omits install, packaging, and v // Implementation Changes L28; Bounded context: no ruamel.yaml in this plugin; it is a Cortex sibling dependency / okf-frontmatter 使用 `ruamel.yaml` round-trip 解析，保留已有字段、未知扩展、顺序、引号、注释和正文 | adopt (non-binding) | open |
| CF-04 | lens_1 | should-fix | --workspace is said to satisfy Cortex authoring and active policy, yet policy-missing mode may still emit generic authoring frontmatter marked unverified, so th // 公开接口 L22; Implementation Changes L36 / `--workspace <path>`：隐含 `--okf`，并满足 Cortex authoring 与活动 policy。… policy 缺失：可生成通用 authoring frontmatter，但明确标记为“policy 未验 | adopt (non-binding) | open |
| CF-05 | lens_1 | should-fix | Cortex mode presupposes method contract 2.1, eighteen public schemas, manage config show, and quick validation via an external CLI that this plugin does not own // Summary L8; Implementation Changes L34-37 / 不修改 Cortex；仅通过其只读 CLI 接口获取 workspace、target 和 policy 状态。… Cortex 模式先验证 method contract 为 2.1、18 个公开 schema及目标操作集合，再读取 `m | adopt (non-binding) | open |
| CF-06 | lens_2 | must-fix | The OKF conversion path writes body to a runtime staging area and only creates the final target after plan approval and validation, but the plan never defines r // Implementation Changes L39; also Test Plan L46 mentions failure reports but no recovery/cleanup contract / markdown-conversion --okf 先把无 frontmatter 的转换正文写入运行暂存区，再把来源字段交给新 skill。只有 frontmatter 计划获批并通过校验后才创建最终输出；拒绝审核或失败时不留下伪称 OK | adopt (non-binding) | open |
| CF-07 | lens_2 | must-fix | The Test Plan lists broad coverage themes and three pytest invocations plus an undefined 'skill quick validation', but Definition-of-Done evidence is incomplete // Test Plan L47-L52 / 回归验证： python -m pytest tests/okf-frontmatter/test_frontmatter_pipeline.py -v ; python -m pytest tests/markdown-conversio | adopt (non-binding) | open |
| CF-08 | lens_2 | should-fix | Plans bind source SHA-256, rendered SHA-256, target path, and Cortex policy digest, and tests mention stale digest / source change / plan tamper, but the operat // 公开接口 L24; Implementation Changes L36-L37; Test Plan L45-L46 / 新脚本提供 prepare / publish / apply / validate 子命令；计划绑定源文件 SHA-256、渲染结果 SHA-256、目标路径及 Cortex policy digest。 ... 覆盖 Plan/Appl | adopt (non-binding) | open |
| CF-09 | lens_2 | should-fix | The plan adds skill-layer flags (--okf, --workspace, --no-frontmatter mutual exclusion) on markdown-conversion while bounded context states there is presently n // 公开接口 L20-L23; Implementation Changes L39-L40 / markdown-conversion 新增 skill 层参数： --okf：生成通用 OKF-ready Markdown。 --workspace <path>：隐含 --okf，并满足 Cortex authoring 与活动 po | adopt (non-binding) | open |

## Coverage limits and unresolved questions

- Audit only: open must-fix/should-fix items are compatible with audit_complete.
- No specialist lenses; no user focuses.
- Cortex CLI/environment behavior was reviewed via bounded context and sibling docs, not live CLI execution in this audit.
- Authority effect: none (cannot authorize execution).

## Frozen run config

```json
{
  "base_preset": "audit",
  "config_confirmation": "explicit_before_dispatch",
  "config_schema_version": "3.0",
  "focus_coverage": [],
  "invocation_mode": "standalone",
  "lens_count": 2,
  "lens_roster": [
    {
      "focus_statement": "goal, deliverable, feasibility, sequence, and prerequisites",
      "kind": "universal",
      "lens_id": "lens_1",
      "source": "universal",
      "user_focus_ids": []
    },
    {
      "focus_statement": "assumptions, risk, recovery, completeness, and verification",
      "kind": "universal",
      "lens_id": "lens_2",
      "source": "universal",
      "user_focus_ids": []
    }
  ],
  "max_full_passes": null,
  "metadata": {
    "config_hash": "sha256:efa9be4b10c458808d825f565dea7d971d0f2cadd83f63ebc5c18efca921efd6",
    "config_revision": 1,
    "explicit_overrides": {
      "required_full_passes": 1,
      "review_mode": "audit"
    },
    "initial_plan_snapshot_hash": "sha256:1b4ffab5f116a1b5c6a831abb4e33b44e7ff28a511dcfb56fe4f359302f273ac",
    "legacy_inputs": [
      "review_mode=single_pass"
    ],
    "parent_config_hash": null,
    "resolution_source": "legacy_alias"
  },
  "operational_retry_limit": 1,
  "required_full_passes": 1,
  "review_checkpoint_policy": "exceptions_only",
  "review_mode": "audit",
  "user_focuses": [],
  "workflow_version": "0.6.0",
  "write_policy": "no_write",
  "config_hash": "sha256:efa9be4b10c458808d825f565dea7d971d0f2cadd83f63ebc5c18efca921efd6"
}
```

