# Public integration boundary

This directory preserves the historical validation lineage, not the original
development filesystem. The original 75/79/103/129 records refer to their own
versions. Changes to test discovery and wrapper imports require new local checks;
no historical green result is relabelled as validation of this integration tree.

Two raw failure reports and two ZIP snapshots remain unchanged in the separate
retained development tree. They are deliberately absent from this public tree.
Historical handoffs referring to those names describe that retained local tree.
The separately named `*.public-redacted-v1.json` files replace only decoded local
path prefixes; test counts, failures, exception types and relative source locations
remain. `PUBLIC_DERIVATION_PROVENANCE_v1.json` identifies original and derivative
hashes, the exact transformation, and inspected safe ZIP member metadata. Neither
the derivative nor the absence of private originals establishes byte identity with
the historical report. ZIP members and original digests do not authorize access to
private materials. No ZIP is published or extracted into this source tree.

The three historical HANDOFF Markdown files also contain local paths. Their
unmodified originals stay in the retained development tree, not this public tree.
`HANDOFF.public-redacted-v1.md` derivatives and the separate
`PUBLIC_HANDOFF_DERIVATION_PROVENANCE_v1.json` preserve the same text apart from
explicit path-prefix replacements. This supplemental provenance does not replace
the earlier report/ZIP inspection record.

Current validation receipts belong under ignored `output/`, outside the source
selector. The original failure ledgers are never overwritten to make publication
appear clean. Internal30, 20/60, historical attribution and external T7 stay unchanged.
