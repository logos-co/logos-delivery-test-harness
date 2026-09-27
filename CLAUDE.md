@AGENTS.md

## Claude Code specifics

- `./harness.sh build` and `./harness.sh run` outlast the Bash tool's 10-minute
  limit. Start them with `run_in_background` and wait for the completion
  notification; do not chain `sleep`s to poll. To wait on a condition inside a
  run (e.g. "all members report start"), use one bounded until-loop.
- Send build output to a file (`> out/build.log 2>&1`) and read the tail, not
  the whole log.
- Traces, stage logs and container logs are large: filter them (`grep`, `awk`)
  or use the context-mode tools rather than printing them whole.
- Put scratch manifests in `out/manifests/` rather than a session scratchpad,
  so the manifest stays next to the run it produced.
