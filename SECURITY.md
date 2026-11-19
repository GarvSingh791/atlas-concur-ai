# Security policy

Atlas is a portfolio project: an independent engineering study of a travel-and-expense copilot, with a public demo that anyone can sign in to. This page explains what is in scope, how to report a vulnerability privately, what to expect after you report, and how to test the shared demo without harming it or other visitors. The design of the controls themselves is in [docs/design/security-model.md](docs/design/security-model.md).

> Atlas is an independent engineering study inspired by SAP Concur. It is not affiliated with, authorized by, sponsored by, or endorsed by SAP SE or its affiliates. SAP and SAP Concur are trademarks or registered trademarks of SAP SE in Germany and other countries. All data in this project is synthetic.

## Supported versions

| Version | Supported |
|---|---|
| `main` (the code deployed to the public demo) | Yes |
| Older commits, forks, branches | No |

Fixes land on `main` and reach the demo through the normal CI deploy.

## Scope

**In scope**

- The source code in this repository on `main`: backend (`backend/`), AI layer (`ai_agents/`), provider adapters (`services/`), database code (`database/`), configuration (`config/`), frontend (`frontend/`), CI and deploy scripts (`.github/workflows/`, `scripts/`, `docker/`).
- The public demo: the frontend at <https://atlas-concur-ai.vercel.app> and the API at <https://atlas-backend-tsf8.onrender.com>.

Examples of what I want to hear about: reading or changing another user's or another organisation's records, escalating from one role to another, approving or reimbursing your own claim, bypassing the upload checks, getting an agent to perform an action your role does not permit, recovering someone else's account, leaking secrets or tokens, or anything that lets one visitor break the demo for everyone in a way the demo protections should have prevented.

**Out of scope**

- **The published demo credentials.** The demo accounts and their password are public on purpose (see the README). Signing in with them is not a finding.
- **Data visible to every demo visitor.** All visitors share the same demo organisation, so records one visitor creates are visible to the next until the daily reset. This is by design.
- **Known, documented limitations** listed under [residual risks](docs/design/security-model.md#residual-risks), unless you can show impact beyond what is described there.
- Volumetric denial of service, load testing and rate-limit exhaustion as the only result.
- Vulnerabilities in third-party platforms (Vercel, Render, Neon, Cloudflare, OpenRouter, GitHub). Please report those to the vendor.
- Social engineering, physical attacks, and reports produced only by automated scanners without a demonstrated issue.

## How to report a vulnerability

Please report privately through **GitHub's private vulnerability reporting** on this repository:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability** (this opens a private security advisory visible only to you and the maintainer).
3. Describe the issue.

Please **do not** open a public issue, pull request or discussion for a security problem, and do not post details publicly until a fix is available.

A useful report includes:

- what the issue is and its impact;
- the affected endpoint, page or file, and the commit or date you tested;
- step-by-step reproduction (requests and responses with tokens and passwords removed);
- the demo account or role you used;
- any suggested fix.

## What to expect

This project is maintained by one person, so timings are best effort:

| Step | Target |
|---|---|
| Acknowledge your report | within 7 days |
| Initial assessment (confirmed, needs more information, or out of scope) | within 14 days |
| Fix for a confirmed issue | depends on severity; critical issues first |
| Disclosure | coordinated with you once a fix is deployed; credit in the advisory if you would like it |

I will keep you updated in the advisory thread. If a report is out of scope, I will say why.

## Testing the shared demo

The demo is shared by everyone who visits, runs on free-tier hosting and pays for its AI calls. If you test it, please:

- **Use only the published demo accounts or accounts you create** inside the demo. Do not try to access real accounts or data outside the demo organisation beyond what is needed to show an issue exists, and stop as soon as you have shown it.
- **Use synthetic data only.** Do not upload real receipts or any real personal data. Uploaded images are processed by a third-party OCR model and are visible to other demo visitors until the daily reset.
- **Do not degrade the service.** No denial of service, flooding, brute force beyond a few attempts, or automated scanning at a rate that slows the demo for others. The API has rate limits; please do not try to exhaust them at scale.
- **Do not run up costs.** Avoid scripted, high-volume calls to the assistant, policy Q&A or receipt upload.
- **Do not attack the hosting providers** or try to pivot from the demo into their infrastructure.
- **Do not keep, share or use** any data or secret you come across. If you find real personal data or a live credential, stop, report it, and delete your copy.
- **Expect resets.** The demo organisation is restored to its seed data daily (due at 04:00 UTC, or on the first wake after it), so anything you create may disappear.

If you make a good-faith effort to follow this policy, I will consider your research authorised, will work with you to understand and fix the issue, and will not pursue or support legal action against you for it.

## More information

- [Security model](docs/design/security-model.md): threat model, every control with its code path and tests, residual risks.
- [Architecture](docs/ARCHITECTURE.md): request lifecycle and middleware order.
- [Key rotation runbook](docs/runbooks/key-rotation.md) and [public demo checklist](docs/runbooks/public-demo-checklist.md).
