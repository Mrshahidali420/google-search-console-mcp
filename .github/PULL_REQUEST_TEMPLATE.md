<!-- Thanks for sending this. Nothing below is a formality; each line is
     something that has caught a real defect in this project. -->

## What this changes

<!-- One or two sentences. If it fixes an issue, "Fixes #123" here. -->

## Why

<!-- The reasoning, not the diff. A reviewer can read what changed; what they
     cannot read is what you knew that made it the right change. -->

## How it was verified

<!-- Delete what does not apply. -->

- [ ] `pytest` passes locally
- [ ] New tests cover the change, and fail without it
- [ ] Touches the submission path — if so, say whether it was exercised
      against a real property or only against the fake
- [ ] Touches quota accounting — say which of the rules in
      `docs/manual-smoke.md` still hold

## Anything a reviewer should be suspicious of

<!-- Where you are least confident. This is the most useful box on the form. -->

---

- [ ] No credential, token or client secret appears in the diff, in a test
      fixture, or in a log line this adds. **A Google client secret committed
      to this public repository gets reported to Google and revoked, which
      breaks every installed user at once.**
- [ ] `CHANGELOG.md` updated if the behaviour anyone depends on changed
