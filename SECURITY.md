# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in NexusAI, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

### How to Report

1. Go to [GitHub Security Advisories](https://github.com/Jacobdrosol/NexusAI/security/advisories/new)
2. Click "Report a vulnerability"
3. Provide:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

You will receive a response within 72 hours.

### What to Expect

- We will acknowledge your report within 72 hours
- We will investigate and validate the vulnerability
- We will work on a fix and coordinate disclosure with you
- Credit will be given to the reporter (unless you prefer to remain anonymous)

## Security Best Practices for Deployments

When deploying NexusAI in production:

- **Set strong secrets**: `NEXUSAI_SECRET_KEY`, `CONTROL_PLANE_API_TOKEN`, `NEXUS_MASTER_KEY`
- **Never commit secrets** to any repository
- **Run behind TLS** (reverse proxy with HTTPS)
- **Restrict internal ports** to localhost or private networks
- **Use `NEXUSAI_ENV=production`** so placeholder encryption keys fail at startup
- **Review worker permissions** before enabling `can_edit` or browser-backed workers
- **Audit bot system prompts** before deploying bots that can mutate external systems

## Scope

This policy covers the NexusAI framework repository (`Jacobdrosol/NexusAI`). It does not cover third-party dependencies (report those to their respective maintainers) or your own deployment configuration (keep your secrets safe).