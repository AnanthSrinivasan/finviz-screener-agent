# Auth & Secrets Design — Anva Trade Publisher Layer

## Current approach (shipped)

```
GitHub Actions
  └── static keys (finviz-screener-bot IAM user)
        ├── s3:PutObject/GetObject/ListBucket  → screener-data-repository
        ├── ssm:PutParameter/GetParameter      → /anva-trade/*  (setup only)
        └── events:PutEvents                   → finviz-events bus

Lambda (XPublisher)
  └── execution role (CDK auto-created)
        └── SSM values baked into env vars at deploy time via CloudFormation
```

**What needs updating if you add a new credential:**
1. `aws ssm put-parameter` using bot credentials
2. `cdk deploy PublisherStack` to re-bake env vars into Lambda

**Weakness:** Static keys in GHA secrets. One key for all concerns (S3 + SSM + EventBridge).

---

## Option 2 — Separate IAM users per concern

```
GitHub Actions
  ├── finviz-screener-bot  (S3 only — unchanged)
  └── finviz-publisher-bot (EventBridge PutEvents only — new user)

SSM write
  └── admin_user (manual one-time setup, no GHA involvement)
```

**What changes:**
- New IAM user `finviz-publisher-bot` in `screener_stack.py`
- New GHA secret: `PUBLISHER_AWS_ACCESS_KEY_ID` / `PUBLISHER_AWS_SECRET_ACCESS_KEY`
- `finviz-screener-bot` loses SSM permission (back to S3 only)
- SSM `put-parameter` done manually by admin, not via bot

**Benefit:** Blast radius reduced. Compromised publisher key can't touch S3.

---

## Option 3 — GitHub Actions OIDC (no static keys)

```
GitHub Actions
  └── OIDC token → assumes IAM Role: AnvaTrade-GHA-Role
        ├── events:PutEvents  → finviz-events bus
        └── s3:*              → screener-data-repository
        (scoped to repo: AnanthSrinivasan/finviz-screener-agent, branch: main)

SSM write
  └── admin_user (manual one-time setup)
```

**What changes:**
- Add GitHub OIDC provider to IAM (one-time, account-level)
- New IAM Role with trust policy scoped to your repo + branch
- Replace GHA secrets `AWS_ACCESS_KEY_ID/SECRET` with:
  ```yaml
  - uses: aws-actions/configure-aws-credentials@v4
    with:
      role-to-assume: arn:aws:iam::090960193599:role/AnvaTrade-GHA-Role
      aws-region: eu-central-1
  ```
- Delete `finviz-screener-bot` static keys from GHA secrets
- `finviz-screener-bot` user can be deleted entirely

**Benefit:** No long-lived keys anywhere. Role expires after each GHA run. AWS recommended approach.

**SSM permissions:** Role gets `ssm:GetParameter` only — no write. Admin puts params manually.

---

## Option 4 — Multi-tenant (productized)

```
Per-tenant deployment:
  tenant_id = "user123"

GitHub Actions (OIDC)
  └── assumes role: AnvaTrade-Deploy-Role
        └── cdk deploy --context tenant=user123

Lambda per tenant:
  └── execution role: AnvaTrade-{tenant_id}-LambdaRole
        └── reads SSM: /anva-trade/{tenant_id}/*

SSM namespace:
  /anva-trade/user123/X_API_KEY
  /anva-trade/user123/X_API_SECRET
  /anva-trade/user123/X_ACCESS_TOKEN
  /anva-trade/user123/X_ACCESS_SECRET

EventBus per tenant:
  finviz-events-{tenant_id}
```

**What changes:**
- CDK stack parameterized by `tenant_id`
- SSM paths namespaced per tenant
- Lambda role scoped to tenant's SSM path only
- Onboarding = `cdk deploy --context tenant=user123` + SSM put for that tenant
- No shared IAM user — each tenant stack is isolated

**SSM permissions:** Each tenant's Lambda role gets `ssm:GetParameter` on `/anva-trade/{tenant_id}/*` only. Cross-tenant reads are impossible by policy.

---

## Decision matrix

| | Current | Option 2 | Option 3 OIDC | Option 4 Multi-tenant |
|---|---|---|---|---|
| Static keys in GHA | Yes | Yes (2 users) | **No** | **No** |
| Key rotation needed | Yes | Yes | No | No |
| Setup complexity | Low | Low | Medium | High |
| Multi-tenant ready | No | No | Partial | **Yes** |
| SSM write via | Bot user | Admin only | Admin only | Per-tenant admin |
| Recommended for prod | No | No | **Yes** | **Yes (SaaS)** |

---

## Recommended path

**Now:** Current approach — ship it.  
**Next sprint:** Option 3 (OIDC) — remove static keys, clean up bot user.  
**When productizing:** Option 4 — parameterize CDK stack by tenant, namespace SSM.
