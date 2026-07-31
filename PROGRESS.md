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
