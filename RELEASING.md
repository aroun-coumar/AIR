# Releasing airobotics

AIR is published on PyPI as **`airobotics`**:
- install with `pip install airobotics`
- import `agentic_robotics`
- run the CLI as `air`, or `airobotics`

Releases are built and uploaded by `.github/workflows/release.yml` using **trusted
publishing**. No API token or password exists to leak: PyPI trusts that workflow in this
repository and issues a short-lived credential for each run.

## One-time setup (the project owner)

These are account actions on pypi.org and GitHub; only the owner can do them.

1. **Create accounts with two-factor authentication** on
   [pypi.org](https://pypi.org/account/register/) and
   [test.pypi.org](https://test.pypi.org/account/register/). TestPyPI is a separate
   site with its own account.
2. **Add a pending trusted publisher on PyPI.** This reserves the name `airobotics`
   before the first upload. Go to
   [Publishing → Add a new pending publisher](https://pypi.org/manage/account/publishing/)
   and enter:
   - PyPI project name: `airobotics`
   - Owner: `aroun-coumar`
   - Repository name: `AIR`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. **Do the same on TestPyPI**
   ([publishing page](https://test.pypi.org/manage/account/publishing/)), with
   environment name `testpypi`.
4. **Create two GitHub environments** in the repository
   (*Settings → Environments*): `testpypi` and `pypi`. On `pypi`, add yourself as a
   **required reviewer**, so every real release waits for your approval.

A pending publisher reserves the name only when you add it. Until then someone else can
register `airobotics`, so do step 2 soon.

## Each release

1. Set the version in `src/agentic_robotics/__init__.py` (`__version__`). PEP 440:
   `0.1.0a1`, `0.1.0b1`, `0.1.0rc1`, `0.1.0`.
2. Make sure all five checks pass (see `CLAUDE.md`). Then collect the news fragments:

   ```bash
   uvx towncrier build --version 0.1.0a1
   ```

3. Commit, then tag the release with the same version, prefixed `v`:

   ```bash
   git tag -a v0.1.0a1 -m "airobotics 0.1.0a1"
   ```

   ```bash
   git push origin v0.1.0a1
   ```

4. The workflow builds, checks the metadata (`twine check --strict`), installs the wheel
   alone and smoke-tests it, uploads to TestPyPI, then waits for your approval in the
   `pypi` environment before uploading to PyPI.
5. Check it from a clean machine:

   ```bash
   pip install --pre airobotics
   ```

   ```bash
   air quickstart
   ```

   `--pre` is needed while only prereleases exist.

A version can never be re-uploaded, even after you delete it from PyPI. If something is
wrong, publish the next version.
