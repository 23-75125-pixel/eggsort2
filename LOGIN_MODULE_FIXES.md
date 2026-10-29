# Login Module fixes

Run focused tests from the project directory:

```powershell
.\.venv\Scripts\python.exe -B -m unittest test_auth_flow test_login_regressions test_security -v
node --test test_security_js.js
node --test --test-skip-pattern=dashboard test_notifications.js
```

`auth_test_support.py` loads the real application routes under a separate module
name with an explicit memory-only SQLite configuration. It suppresses `.env`
loading and database bootstrap, substitutes camera/ESP32/inference stand-ins,
and creates only User, AuditLog and PasswordResetAttempt tables. It does not
alter production configuration, schema or accounts. Regression tests replace
mail delivery and runtime stop and block network connections.

Do not launch this harness as the sorting application or use its fixed test
secret outside tests. Broad app smoke tests are separate: they still import
the normal application and are not part of this isolated command.

## Outcomes

- Password recovery uses the existing reset-attempt model, normalized identifier
  hashes, direct peer IP (not untrusted forwarded headers), configured cooldown
  and identifier/IP limits. Invalid, inactive, unknown and limited requests keep
  the same generic response. Existing 30-minute expiry and 8–128 character,
  matching-password validation remain. Conditional token consumption prevents
  successful reuse, including a request whose token was consumed meanwhile.
- Authentication audit writes are best effort and logged on failure. A valid
  login remains successful; invalid credentials never become authenticated.
- Logout always clears authentication and still attempts the existing runtime
  stop after an audit failure. A stop exception returns HTTP 503 with explicit
  signed-out/unverified-hardware-shutdown wording, never a shutdown success.
- Login throttling remains five failures per five minutes. Checks no longer
  allocate entries; insertion is capped at 2,048 and expired entries are pruned
  on subsequent checks/writes. No background cleanup thread is added.
- Malformed/non-ASCII CSRF tokens return the existing HTTP 400 response.

No migrations or new schema fields are required. Deployment must already have
the existing password-reset attempt table and user reset columns; this patch
does not inspect or repair production schema. SMTP delivery, real Google OAuth,
and browser end-to-end testing remain manual validation steps.

## Deferred policy decisions

Other active sessions remain valid after password reset, as before. Consider a
reviewed per-user session version if revocation is approved; that can require a
migration. Google persistent login, idle timeout, and distributed throttling
remain unchanged. Process-local login limits reset with process restarts and
evict old keys at capacity. Reset-request count checks are not a distributed,
atomic reservation mechanism under simultaneous requests; strict concurrent
rate enforcement needs a separately approved design.

No camera, servo, sorting, serial-protocol, calibration, experimental scheduler,
or database-schema behavior is changed by these authentication fixes.
