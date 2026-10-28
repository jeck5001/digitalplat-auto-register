# Progress

1. Goal: deliver the fixed-invite-code Web manager to the configured NAS at port 8400 and verify one persisted real registration.
2. Order: baseline, Web/persistence tests, image/volume verification, publish, then NAS deployment and browser verification.
3. Baseline: compile and Compose render pass; remote main is 1f28d1b7; required trim-cli NAS probes now succeed.
4. Web: atomic redacted jobs, restart recovery, fixed referral code, and five focused unit tests pass.
5. Image: linux/amd64 build, Web CLI help, healthy Compose, and uid 10001 named-volume jobs.json write all pass.
6. Published: final SHA 1f28d1b7 Actions passed; anonymous GHCR pull resolved digest 5676c39d; final Compose and .env are on the NAS.
7. NAS: deployed container is healthy; named volume, uid 10001 Web process, and in-container curl /health all pass.
8. Attempt 1 failed on phone format; +1-XXXXXXXXXX fix has 6 focused tests, compile, Compose, and linux/amd64 build passing.
9. Maximum risk: external challenge/email providers may still reject the next rebuilt-image attempt; inspect NAS logs before any retry.
10. 2026-10-28 submit-step fix (local code only, NOT yet on NAS): the live registration page no longer matches the old fill selectors. Address inputs have no placeholders and are named contact_line1/contact_city/contact_region/contact_postal_code/contact_country; the billing checkbox is input[name='billing_same'] (not the old English label); the phone input is a national-number field (phone_country is a separate select) so "+1-..." values are now stripped to 10 digits before filling; the Turnstile callback is onTokenChange (was missing from the injection list, so the submit button stayed disabled); the submit button is now found by form position, not by its localised text, and a disabled button now reports which fields are invalid. Phone generation (web + registrar defaults) now avoids the fictional 555 range and invalid 0/1-leading blocks. Verified: py_compile passes on the three edited files. PENDING: rebuild/publish the image and redeploy on the NAS (docker compose pull + up -d --force-recreate); until then the NAS still runs the old image and will still fail.
