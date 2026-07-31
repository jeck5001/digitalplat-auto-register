`venv/bin/python -m pytest -q` has nine pre-existing failures in files outside the
allowed edit list: configuration tests require an unsupported `mock` email enum
and older Pydantic URL behavior, and the phone helper returns 15 digits while
its unchanged test expects 16. Focused Web tests pass; no existing test or
out-of-scope source has been changed to hide these failures.
