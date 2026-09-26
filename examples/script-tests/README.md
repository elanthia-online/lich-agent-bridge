# Synthetic script-test example

This probe sends no game commands. Its `normal` case exits normally; its
`intentional-error` case raises a deliberate exception and should report a failed
case with verified cleanup, not success. Both cases declare unchanged room and
hand observations. Missing observations are inconclusive.

After inspecting the probe and runner, a developer may copy the probe into an
explicitly chosen Lich scripts directory and the JSON manifest into a subdirectory
of that scripts directory. `files` paths are relative to the scripts root, not
the manifest directory. The installed scripts directory must also contain the
reviewed `lab-test-runner.lic` and `lab-test-runner.rb`.

Offline preparation requires an explicit character and safe-room ID. It emits
an entry for a privately maintained controller registry; it does not install or
enable that entry. Review every pinned file and registration before separately
authorizing a live test. This example is not a reproduction of a community bug,
and pinning does not sandbox Ruby or discover undeclared dynamic dependencies.

Select `normal` first for a player-authorized smoke test. `all` includes the
intentional failure and therefore should not produce an all-passed report.
Keep reports and any real character configuration outside the public repository.
