# Gap+RVOL D9 repair candidate external replica

- status: `COMPLETE`
- created UTC: `2026-08-04T02:22:22Z`
- target: Apple iCloud Drive (`icloud-documents`, `cloud_sync`)
- source scope: isolated D9 repair candidate only; canonical/frozen data was not copied or changed

## Candidate SQLite

The ignored candidate database was copied with SQLite's backup API, then reopened from a temporary restore location.

| Check | Result |
| --- | --- |
| source rows / symbols | `49,150` / `30` |
| source integrity | `ok` |
| replica basename | `gap-rvol-d9-toss-candidate.20260804T022222Z.sqlite` |
| replica size | `16,842,752` bytes |
| snapshot = replica SHA-256 | `d787122acb051db42db1182bba1284fa0fd35bc18c7027c7d78ef79778224f0d` |
| restore integrity | `ok` |
| restore semantic match | `true` |

The source file SHA differs from the replica SHA because the SQLite backup API creates a new page layout. The verified invariants are snapshot-to-replica byte identity plus restored integrity and semantic equality.

## Immutable raw responses

The content-addressed raw response tree was packed deterministically and restored into a temporary location.

| Check | Result |
| --- | --- |
| payload files | `299` |
| payload files decompressed and verified | `299` |
| raw content SHA-256 | `7a55bd24deaaad9be9888a150e4aadb768dde6edfb0dead734e1fc01136d05da` |
| archive basename | `gap-rvol-d9-toss-raw.20260804T022222Z.tar` |
| archive SHA-256 | `de3af5e0fb2bcd37658a602b9baef5ffa6b3535d37e68f6b7e7310da8d8ecacf` |
| restore content match | `true` |

## Cloud durability observation

After both files were created, `brctl status com.apple.CloudDocs` reported the CloudDocs container as `caught-up` with `last-sync=2026-08-04 11:22:34 KST`. This is evidence that the iCloud synchronization service processed the replica target; the tracked receipts contain no local absolute path or credential.

## Scope and retention

This replica preserves the ignored inputs needed to reproduce the tracked D9 overlay audit after the feature worktree is removed. It does not authorize OOS access, forward-return calculation, or performance evaluation. Issue #104 remains open because the event-day overlay resolved zero exclusions; the next repair must target prior-close and RVOL-lookback dependency days.

