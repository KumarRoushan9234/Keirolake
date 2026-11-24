# Contributing to KeiroLake

Thanks for wanting to help.

## Before you start

Email **[roushan9234620012@gmail.com](mailto:roushan9234620012@gmail.com)** with what you'd like to add or change.
A short note is enough: the problem, and how you plan to solve it. This avoids two people building the same thing,
or a pull request that can't be merged.

Ideas, bug reports and feature requests are welcome by email or as GitHub issues, no code needed.
Security problems: email only, please don't open a public issue.

## Making a change

1. Fork the repository and create a branch.
2. Keep the change focused: one fix or feature per pull request.
3. Run the checks:
   ```bash
   cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # .venv/Scripts/pip on Windows
   .venv/bin/python -m pytest -q -m "not integration"                       # engine tests, no network needed
   cd ../frontend && npm ci && npm run build                                # interface type-check and build
   ```
   Changing the desktop shell or packaging? `cd desktop && npm ci && python scripts/launch.py` runs the app, and
   `bash desktop/scripts/ci_linux.sh` builds and tests the Linux packages in Docker.
4. New behaviour needs a test. Provider code is tested with mocked HTTP (`respx`), never with real keys.
5. Never commit keys, tokens or `.env` files.
6. Add a line under **Unreleased** in [CHANGELOG.md](CHANGELOG.md).
7. Open a pull request saying what changed and why, and mention your email thread.

The pull request runs the same tests as a release. Once it's merged into `main`, the release workflow builds, tests and
publishes the next version automatically.

Keep it simple: fewer files, fewer dependencies, plain code, plain words in the interface.
