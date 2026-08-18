# Limited Graph concise initial-binding spelling failure

## Affected run

- `e3e0a083-e97f-4944-94e4-ddeed362c9c3`, started
  `2026-08-18T05:57:27.433519Z`.

## Observed sequence

The Agent completed graph `establish-world-axis`. It then submitted graph
`foundationpose-arm-base-calibration` with one initial value named `request`
and this binding:

```json
{"to":"/request","from":"$initial#/request"}
```

The result was `AUTHORING_INVALID`, with zero transitions, zero physical
actions, and this retained reason:

```text
node foundationpose binding 0 references unknown initial value 'initial'
```

A subsequent invalid correction terminated with:

```text
Error running tool run_limited_graph: step foundationpose.bind[0].from must use node-id#/pointer or $initial-name#/pointer
```

No FoundationPose child or physical action started in the failed authoring
attempts.

## Cause

The FunctionTool guidance published `$initial#/pointer`, but the concise
compiler interpreted the token immediately after `$` as the initial-value name
and implemented `$name#/pointer`. The published and implemented spellings were
inconsistent.

## Repair

The reference-host concise compiler now accepts both forms:

| Form | Meaning |
|---|---|
| `$request#/payload` | Initial value `request`, pointer `/payload` |
| `$initial#/request/payload` | Initial value `request`, pointer `/payload` |
| `$initial#/request` | Initial value `request`, root pointer |

If a graph genuinely declares an initial value named `initial`, the direct
`$initial#/pointer` interpretation is preserved. Both concise forms compile to
the same canonical graph fields. Canonical graph schema, validation, digest,
authorization, execution, and safety boundaries are unchanged.

## Validation

- 485 Test Agent tests and 27 subtests passed.
- 60 standalone Limited Graph and Slicing tests passed.
- The Limited Graph package validator passed.
- 141 documentation files passed integrity checks.
- The repository-wide gate passed 1,150 Python tests and 27 subtests, all
  Python wheel builds, and 80 Rust tests.
