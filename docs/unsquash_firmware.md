# unsquash_firmware.py — Extract a Tesla firmware image

A downloaded Tesla firmware blob is a squashfs filesystem. Inside the root it
ships further squashfs images whose mount path is flattened into the filename:
dots are path separators and `%2E` is a literal dot. The on-car script
`bin/mount-all-dirsquashed` mounts each `<dotted>.dirsquashed` accordingly
(e.g. `deploy.seed_artifacts_v2.dirsquashed` → `deploy/seed_artifacts_v2`).

This reproduces that as a one-shot extraction.

```
python unsquash_firmware.py <firmware_file>
python unsquash_firmware.py ~/Downloads/2024.44.x.ice --name 2024.44.3.1.ice.extracted
python unsquash_firmware.py <file> --keep-download   # don't delete the blob
```

## Pipeline

1. `unsquashfs` the top-level image → `<out>/<name>/` (the firmware root).
2. Recursively find every `*.dirsquashed`, decode its path (`.`→`/`, `%2E`→`.`),
   and `unsquashfs` it into that path — repeating until none remain (handles
   nesting).
3. Delete the original download (these are ~1–2 GB each) — unless
   `--keep-download`.

The original blob is removed **only after a successful extraction**: a failed
`unsquashfs` exits earlier, so a partial run never drops the download.

## Options

| flag | effect |
|---|---|
| `--name NAME` | extraction dir name (default: file stem) |
| `--out DIR` | parent dir for the extraction (default: `~/dev/tesla-fw`) |
| `--keep-squashed` | keep the `.dirsquashed` files after extracting them |
| `--keep-download` | keep the original downloaded blob |

## Notes

- Runs `unsquashfs -no-xattrs` so it works unprivileged (restoring
  `security.capability` xattrs would otherwise require root and fail the run).
- If extraction reports `xattr_ids is 0` / a fatal error, the download is
  likely still copying or truncated — verify with `unsquashfs -s <file>`
  (a complete image shows a non-zero `Number of xattr ids` and a valid
  superblock).
