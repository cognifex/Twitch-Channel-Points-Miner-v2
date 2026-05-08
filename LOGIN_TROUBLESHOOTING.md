# Login Troubleshooting (Credential login fails with valid credentials)

## Symptom
The miner cannot log in with valid username/password and falls back to browser login.
In non-interactive environments (Docker/K8s/CI), this fallback cannot be completed.

## Root cause
Twitch frequently protects `https://passport.twitch.tv/login` with anti-bot controls
(CAPTCHA / challenge / risk checks). In that case, API login from scripts is denied even when
credentials are correct.

This is **not** necessarily a wrong-password issue.

## Recommended strategy
1. **Primary path (stable in automation): use cookie-based auth**
   - Run one interactive login once.
   - Persist `cookies/<username>.pkl`.
   - Mount this file into your runtime.
2. **Operational guardrails**
   - Treat credential login as best-effort only.
   - If Twitch returns challenge/captcha, fail fast with explicit message.
3. **Runbook for containers**
   - Start once with `-it` to complete browser/captcha flow.
   - Keep `cookies/` volume persistent.
   - On auth expiry, repeat step 1.

## Acceptance criteria
- Error output clearly differentiates between:
  - wrong credentials, and
  - anti-bot/captcha block.
- Non-interactive runs provide actionable next steps.
- Teams can restore service by rotating cookie file without code changes.
