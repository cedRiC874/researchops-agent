# Frozen offline replay inputs

This directory contains the exact historical source and contract bytes needed
by the separately published completion-telemetry tests. These are replay inputs,
not new Provider runs, current execution authority or passing releases. Copied
sanitized probe/fixture metadata retains its original provenance; it is not
relabeled as synthetic or counted again as new live evidence.

- `t6c-source-baseline-c1c25226-v1`: the preserved v7 source-profile inputs.
- `t6c-pre-source-v4-20260908`: the preserved 252-file v3 selection.
- The two v7 review directories retain the earlier source/profile pairs.
- The three `timed-closure-*` directories retain superseded authoring candidates,
  including their defects; later schemas must not silently replace these bytes.
- The five top-level diagnostic JSON files describe bounded offline observations
  and counterexamples with their stated scope, not actual campaign outcomes.

Git attributes preserve archive bytes, including files ending in `.before.txt`.
Original manifest hashes must continue to match after checkout. Historical
identity checks verify archive hashes without executing archived code. Separate
offline process tests may execute temporary fixtures built from archived source,
sometimes with current-source overlays; these tests do not constitute new live
Provider evidence.

Local full-run logs, operator paths, temporary checkpoints, API credentials,
email output, private task packages and model response bodies are not part of
this publication. Those local files are not deleted or overwritten.

This archive alone implements no runtime gate and authorizes no Provider call,
registration, historical rescoring or T7 closure. Depth-60 remains 20/60.
