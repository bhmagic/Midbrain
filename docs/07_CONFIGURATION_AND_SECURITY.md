# Configuration and Security

This guide defines where local state belongs and the trust boundary of the
current development system. The root [Security Policy](../SECURITY.md) remains
the reporting entry point.

## Configuration ownership

Tracked source contains blank examples, deterministic generators, component
defaults, and the sanitized FoundationPose restore profile. Machine-local
state belongs under ignored `config` and component runtime directories.

The workspace initializer creates missing top-level configuration from safe
examples and preserves existing files:

- `config/system.env`
- `config/api_keys.env`
- `config/providers.json`
- serial-bound device calibration under `config/calibration/devices`

The audited mapping is in
[Configuration Baseline Inventory](../config/BASELINE_INVENTORY.md). A clean
checkout must have either a safe baseline or a deterministic noninteractive
generator for every required active file.

Provider installation scripts merge their entries into local
`config/providers.json` without replacing unrelated Providers. Active robot
calibration, alignment candidates, captures, caches, run state, and device
identities are generated locally and must not have populated reusable
templates.

## Secrets

API keys and signing secrets belong only in ignored local configuration or an
approved external secret store. Never place them in source, examples,
screenshots, logs, terminal transcripts, commit messages, issues, fixtures, or
support bundles.

Blank examples document supported variable names. Runtime defaults and model
selections are owned by the active configuration and component code; other
documentation should not duplicate them.

The Reference Agent's model catalog is multi-provider but retains the legacy
`OPENAI_AGENT_MODEL`, `OPENAI_AGENT_MODELS`, and
`OPENAI_AGENT_REASONING_EFFORT` names for configuration compatibility.
Selecting a Gemini model uses Google's documented OpenAI-compatible endpoint
and reads `GEMINI_API_KEY`; selecting a GPT model reads `OPENAI_API_KEY` and
uses native OpenAI Agents SDK resolution. Existing ignored configuration is
preserved by setup, so changing tracked defaults does not silently overwrite a
machine's active model selection.

If a secret appears in a log, review transcript, or Git object, treat it as
exposed and rotate it. Removing the visible file alone does not invalidate the
credential or erase history.

## Device and spatial calibration

Device calibration is owned by the hardware Provider and keyed by manufacturer,
model, and serial identity. Runtime estimator bias must not overwrite physical
device calibration.

Spatial alignment records must preserve camera and robot identity, boot,
calibration revision, frame convention, VIO epoch, evidence, decision, and
activation lineage. Serial-bound or measured calibration must not be published
unless it is an intentionally reviewed dataset.

## Hosted models and image disclosure

An Agent attachment and a live robot-camera observation are different
provenance paths, but either may disclose image content to a configured hosted
model. Before enabling a hosted backend:

- confirm which component sends the data;
- review the selected service's access, retention, and privacy terms;
- avoid sensitive workcell content; and
- use a local backend or manual workflow when data cannot leave the machine.

SAM2 and other local perception do not make a preceding hosted localization
request local. Visual model output is evidence, not physical authority.

## Local API trust boundary

Manager, Fabric, Agent, Provider, and component UI endpoints are loopback
development interfaces. The Agent run, approval, attachment, chat, journal,
Provider lifecycle, calibration, spatial reset, and motion routes do not yet
provide a field-ready identity and authorization boundary.

Do not bind them beyond loopback or place them behind a remotely reachable
proxy until the system provides:

- authenticated users and services;
- role-based observation and command authority;
- TLS or an authenticated gateway;
- browser origin and CSRF protection;
- bounded requests and rate limits;
- request and decision audit identity; and
- secure failure and credential-rotation procedures.

Loopback reduces network exposure; it does not protect against another local
process or a compromised browser session.

## Retained Agent records

Agent sessions, normalized run events, sanitized complete Skill-result detail,
attachments, and visual evidence may contain prompts, images, workcell
information, and operational history. Skill-result detail removes
credential-like and authorization-like values before retention, but the
robot-local SQLite stores remain development diagnostics. They are not
authenticated, encrypted, tamper-evident, or suitable as field-audit evidence.

Skill-result detail is session-scoped and bounded by configured result-count,
per-result-byte, total-byte, and age limits. Those limits reduce persistence and
context exposure; they do not create a remote-user security boundary. Keep the
default database under ignored runtime state, and do not point
`SKILL_RESULT_DETAIL_STORE_PATH` at a tracked or shared directory.

Exclude runtime databases and evidence from publication and support bundles
unless they have been deliberately reviewed. Field deployment requires defined
retention, deletion, export, redaction, encryption, storage-failure, and legal
hold policies.

## Shared memory and large payloads

Shared-memory discovery information and BufferRefs are local access metadata.
They must remain inside the local trust boundary until access control and a
separate authenticated remote-payload transport exist. A BufferRef is
disposable and does not grant permanent access to its payload.

## Pre-publication review

Before staging or publishing:

```powershell
git status --short
git diff --cached --check
git diff --cached --name-only
.\scripts\test_config_baselines.ps1
```

Review for secrets, active `.env` files, serial numbers, measured calibration,
captures, images, point clouds, logs, databases, SDK binaries, virtual
environments, caches, build output, backup trees, and license-restricted model
or CAD assets.
