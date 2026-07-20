# Security Policy

## Sensitive material

Keep secrets and machine-local sensor data out of Git. The root `.gitignore` excludes the normal secret, calibration, capture, build, and runtime paths, but contributors remain responsible for reviewing staged changes before every push.

Recommended pre-push checks:

```powershell
git status --short
git diff --cached --check
git grep -n -I -E "(OPENAI_API_KEY=.+|GEMINI_API_KEY=.+|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|ghp_[A-Za-z0-9]+|github_pat_)" -- .
```

## Shared memory

Named shared-memory identifiers and BufferRefs should be treated as access-sensitive runtime information. Do not expose them beyond the local trust boundary without authentication and authorization.

## Reporting

Use a private maintainer channel for vulnerabilities involving secret exposure, unsafe process control, shared-memory access, or physical-device behavior. Do not include active credentials, private captures, or device serial numbers in a public issue.
