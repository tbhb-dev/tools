---
base: main
draft: true
---

# fix(alpha): prepare hook shims before build runs

## Why

Build runs can reach commit hooks before the workspace has the tools those hooks need. A successful run can also finish without a new commit when the brief requires one. This change addresses both cases for [tools#363](https://github.com/tbhb-dev/tools/pull/363).

## Behavior change

Build runs check three hook configs for `.bazel_env/bin` after workspace preparation. When that path is missing, the host builds the shims with Bazel before starting the harness. The build has a 600-second timeout and requires `.bazel_env/bin/cog`, even when Bazel exits with an error. The `shims` phase adds 600 seconds to launch polling, and the host extends the run deadline by the build time.

Hosts default to `build_shims = true`. Setting it to `false` refuses builds that need shims. Review and preflight runs skip the build. `--expect-commit` returns status 6 when an otherwise successful build doesn't record a new commit. Review runs reject the flag. Older requests default it to `false`.

## Verification

`git rev-parse origin/fetched/heads/agent/shims-7` returned `626697270cec5077dd2431fb4d9bc76193747ae1`. Reviewed `git diff origin/main...HEAD` and the affected code.

`tbhbdev alpha pr check` and `mise exec -- tbhbdev alpha pr check` failed because tools were missing. `bazelisk run //:bazel_env` built the tools but exited 1 because direnv and its PATH setup were missing.

`PATH="$PWD/.bazel_env/bin:$PATH" npm exec --yes --package=cspell -- tbhbdev alpha pr check` initially found two prose issues and an unset base. After fixes, it passed with exit 0.

Not run: automated tests, coverage, mutation testing, or a live build run.
