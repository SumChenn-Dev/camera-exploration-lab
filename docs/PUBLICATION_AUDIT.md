# Local publication audit

`tools/publication_audit.py` is a read-only-by-default check for the original one-commit public snapshot. It expects a standalone snapshot without a remote, so it is not the recurring audit for the published checkout. Use `tools/review_release.py` for ongoing review of the public repository. It prints the public tree manifest, file sizes and SHA-256 hashes, Git state, commit identities, remotes, and likely privacy or packaging findings. It does not modify files, create commits, configure remotes, create a GitHub repository, or publish anything.

Run it from a trusted local checkout, passing the original development tree and the separately prepared public snapshot:

```bash
cd /path/to/Camera-Exploration-Lab
python3 tools/publication_audit.py \
  --source "$PWD" \
  --snapshot /path/to/Camera-Exploration-Lab-public-snapshot \
  --source-branch main \
  --source-head <reviewed-source-commit> \
  --snapshot-branch main \
  --snapshot-head <reviewed-snapshot-commit> \
  --expected-author <approved-public-author> \
  --expected-email <approved-public-noreply-email> \
  --sensitive-term '<private host, project, or container label>'
```

Replace each placeholder with a reviewed value. The source head check protects against auditing a different development state. The snapshot head check verifies the exact one-commit publication candidate. The audit expects no remote on the staging repository.
Repeat `--sensitive-term` for local usernames, host or container labels, or other known identifiers that should not appear in public files or history. The scanner reports the file and line without printing the matched term.

An exit status of `0` means the automated checks found no configured release blocker; a nonzero status means the candidate should not be published until the findings are understood and resolved. Source-history findings are reported separately because an independent snapshot does not include that history. They remain relevant if the intended public refs ever change.

## Human review still required

Automated scanning cannot guarantee privacy. Review the complete manifest, source and test contents, documentation, images or other attachments, Git metadata, and the exact ref that would be pushed. A scanner can miss names or identifiers expressed indirectly, and it can flag synthetic examples that need human classification. Do not upload experiment databases, logs, exports, screenshots, images, or video without an explicit privacy review.

The command is intentionally local and read-only. The initial repository and snapshot are already published. Every later push or release requires separate explicit authorization of the exact commit and destination.
