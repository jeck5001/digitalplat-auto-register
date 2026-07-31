# Progress

1. Goal: deliver the fixed-invite-code Web manager to the configured NAS at port 8400 and verify one persisted real registration.
2. Order: baseline, Web/persistence tests, image/volume verification, publish, then NAS deployment and browser verification.
3. Baseline: compile and Compose render pass; remote main is d078e034; NAS trim-cli requests currently return errno 135168.
4. Web: atomic redacted jobs, restart recovery, fixed referral code, and five focused unit tests pass.
5. Image: linux/amd64 build, Web CLI help, healthy Compose, and uid 10001 named-volume jobs.json write all pass.
6. Current: rebase onto remote main, commit/push, publish the public GHCR image, then deploy and verify the real browser flow.
7. Maximum risk: the external registration and challenge/email providers may reject the one permitted attempt; any failure will be diagnosed from NAS logs before retry.
