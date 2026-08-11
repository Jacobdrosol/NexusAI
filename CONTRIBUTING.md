# Contributing to NexusAI

Thank you for your interest in contributing to NexusAI! This is an open-source framework for building autonomous AI agent swarms. We welcome contributions from the community.

## Table of Contents

- [Getting Started](#getting-started)
- [How to Contribute](#how-to-contribute)
- [Pull Request Process](#pull-request-process)
- [Code Style](#code-style)
- [Testing](#testing)
- [Reporting Bugs](#reporting-bugs)
- [Feature Requests](#feature-requests)
- [Security Vulnerabilities](#security-vulnerabilities)
- [Code of Conduct](#code-of-conduct)

---

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR_USERNAME/NexusAI.git`
3. Create a feature branch: `git checkout -b my-feature`
4. Make your changes
5. Push to your fork: `git push origin my-feature`
6. Open a Pull Request against `main`

## How to Contribute

We accept contributions in the following areas:

- **Bug fixes** — fix something that's broken
- **Features** — add new capabilities (please open an issue first to discuss)
- **Documentation** — improve docs, examples, or guides
- **Tests** — improve test coverage
- **Performance** — optimize existing code

## Pull Request Process

1. **Open an issue first** for any new feature or significant change. This ensures your work aligns with the project direction before you invest time coding.
2. **Keep PRs focused** — one feature or fix per PR. Large, multi-purpose PRs are harder to review and slower to merge.
3. **Write tests** for your changes. All existing tests must pass.
4. **Update documentation** if your change affects public APIs, configuration, or behavior.
5. **Request review** — a maintainer will review your PR. Be responsive to feedback.
6. **Approval required** — all PRs require approval from a maintainer before merging. Only maintainers can merge PRs.

### PR Checklist

- [ ] Issue exists and is linked in the PR description
- [ ] Branch is up to date with `main`
- [ ] Tests pass (`python -m pytest`)
- [ ] Linting passes (`python -m ruff check`)
- [ ] No secrets, API keys, or private credentials in the diff
- [ ] Documentation updated if needed
- [ ] Commit messages are descriptive

## Code Style

- Python 3.11+
- Use `ruff` for linting: `ruff check .`
- Follow existing code conventions in the repository
- No comments unless they explain non-obvious logic
- Use type hints where the codebase already uses them

## Testing

```bash
# Run all tests
python -m pytest

# Run a specific test file
python -m pytest tests/test_worker_fleet_renderer.py

# Run with verbose output
python -m pytest -v
```

## Reporting Bugs

Open a [GitHub Issue](https://github.com/Jacobdrosol/NexusAI/issues/new?template=bug_report.md) with:

- Clear description of the bug
- Steps to reproduce
- Expected vs actual behavior
- Environment details (OS, Python version, Docker version)
- Logs or error messages (redact any secrets)

## Feature Requests

Open a [GitHub Issue](https://github.com/Jacobdrosol/NexusAI/issues/new?template=feature_request.md) with:

- Clear description of the feature
- Use case and motivation
- Proposed approach (optional but helpful)

## Security Vulnerabilities

**Do not open a public issue for security vulnerabilities.**

Instead, please report them privately via [GitHub Security Advisories](https://github.com/Jacobdrosol/NexusAI/security/advisories/new).

See [SECURITY.md](SECURITY.md) for full details.

## Code of Conduct

Be respectful and constructive. Personal attacks, harassment, or toxic behavior will not be tolerated. Disagreements are fine — disrespect is not.

---

By contributing, you agree that your contributions will be licensed under the same license as the project.