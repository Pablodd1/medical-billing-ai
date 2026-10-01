# 🏛️ AGENTS.md — Mandatory Engineering & Agent Operational Directives

> **Lead Platform Architect, Quality Engineer & DevSecOps Mandate**  
> **Repository:** `medical-billing-ai`  
> **Enforcement Scope:** All AI Agents, Autonomous Subagents, Automated CI/CD Pipelines, and Human Contributors.

---

## 1. Core Directives & Skill Registry

Every AI agent operating in this repository is strictly bound by the 13 installed official agent skills tracked in `skills-lock.json` and mirrored in `.agents/skills/`. Agents must invoke and adhere to these guidelines across all code generation, refactoring, and auditing tasks.

| Skill Domain | Installed Official Skills | Primary Objective |
| :--- | :--- | :--- |
| **Code Review & Quality** | `code-review`, `autofix` (CodeRabbit AI) | Pre-commit diff auditing, security vulnerability scans, linting, and AST-level automated fixes. |
| **Architecture & Performance** | `vercel-react-best-practices`, `vercel-composition-patterns`, `vercel-optimize`, `deploy-to-vercel`, `web-design-guidelines` | Elimination of request waterfalls, efficient client-server component boundaries, and zero-waste bundles. |
| **Web Quality & Compliance** | `seo`, `performance`, `accessibility`, `core-web-vitals`, `web-quality-audit`, `best-practices` (Addy Osmani) | WCAG 2.2 AA accessibility, Core Web Vitals guarantees (LCP, INP, CLS), semantic technical SEO. |
| **Error Classification** | `jev-audit.mjs` (TypeSafe Jev-1 System-1) | Sub-second neural error severity triage and alerting via Resend & Webhooks. |

---

## 2. Mandate 1: GitHub Open Code Review & DevSecOps Standards

Prior to committing or pushing any code to this repository, agents must execute the following verification sequence:

### A. Pre-Commit Diff Check & Scope Isolation
- Inspect the minimal contiguous diff using `git diff` or static analysis before staging.
- Never stage binary databases (`*.sqlite3`, `*.sqlite3-wal`), HIPAA audit logs (`*.jsonl`), or transient environment caches.
- Preserve existing comments, docstrings, and architectural patterns unless explicitly instructed to refactor.

### B. Zero-Secret Exposure Policy (HIPAA & DevSecOps)
- **STRICT PROHIBITION:** Hardcoded API keys, SFTP passwords, private keys, or raw PHI in codebase files.
- All secrets must resolve through environment variables (`TYPESAFE_API_KEY`, `OA_SFTP_PASSWORD`, `API_KEYS`, `PHI_ENCRYPTION_KEY`).
- Validate that `.gitignore` strictly protects `.env*`, `*.pem`, `*.key`, `*.sqlite3`, and `*.jsonl`.

### C. Build & Verification Gates
- Every code change must be validated against `npm run build` or the Python test suite:
  ```bash
  npm run build
  python -m pytest tests/ (or review_audit.py)
  ```
- Any unhandled exception, syntax regression, or missing import results in an immediate build fail.

### D. Clean Conventional Commits
- All commit messages must strictly adhere to the Conventional Commits specification:
  - `feat(...)`: New user-facing or platform functionality
  - `fix(...)`: Bug fix or error resolution
  - `refactor(...)`: Code cleanup without functional modification
  - `perf(...)`: Performance optimization or latency reduction
  - `test(...)`: Adding or updating test suites
  - `chore(...)`: Maintenance, dependency, or configuration updates

---

## 3. Mandate 2: Modern Web Quality, SEO & Accessibility

When modifying web templates, API endpoints, or user interfaces (`templates/portal.html`, `api.py`):

### A. Waterfall Elimination & Performance
- Eliminate client-side sequential request waterfalls: fetch independent resources in parallel (`Promise.all` or asynchronous batching).
- Ensure server-side rendered or template payloads stream with initial byte response time (TTFB) < 150ms.
- Enforce strict Core Web Vitals thresholds:
  - **Largest Contentful Paint (LCP):** $\le 2.5\text{s}$
  - **Interaction to Next Paint (INP):** $\le 200\text{ms}$
  - **Cumulative Layout Shift (CLS):** $\le 0.1$

### B. Technical SEO & Discoverability
- Enforce canonical URL formatting with consistent trailing slash semantics.
- Maintain and verify discovery endpoints:
  - `/robots.txt`: Explicit crawl permissions protecting private `/api/` routes and directing crawlers to the sitemap.
  - `/sitemap.xml`: Well-formed XML sitemap indexing all public entry points.
- Implement structured semantic headers (`h1`, `h2`, `h3`) with relevant meta descriptions and OpenGraph tags.

### C. WCAG 2.2 Level AA Accessibility
- All interactive controls (buttons, links, inputs) must possess descriptive accessible names or `aria-label` attributes.
- Ensure minimum contrast ratio of 4.5:1 for normal text and 3:1 for large text / UI components.
- Complete keyboard navigability: visible focus rings (`focus:ring-2`), logical tab order, and modal escape key handling.
- Form inputs must be linked to explicit `<label>` elements.

---

## 4. Mandate 3: Automated Jev-1 Error Monitoring & Alerting

- **Execution Routine:** The automated Jev audit runner (`scripts/jev-audit.mjs`) executes automatically via GitHub Actions (`.github/workflows/jev-monitor.yml`) every 6 hours and on push to `main`.
- **Local Verification:**
  ```bash
  npm run jev:audit
  ```
- **Error Triage Protocol:**
  - `severity: none`: Operational. Log latency.
  - `severity: low`: Transient or cosmetic warning. Log to analytics.
  - `severity: medium`: Degraded performance. Queue for developer review.
  - `severity: high` / `critical`: Immediate dispatch to Resend email and configured incident webhooks.

---

*Authored & Certified by DevSecOps, Quality Engineering & Platform Architecture.*
