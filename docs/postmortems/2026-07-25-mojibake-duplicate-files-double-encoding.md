# Postmortem: Double-encoded (mojibake) duplicate files from a legacy copy tool

**Date:** 2026-07-25
**Author:** Bartosz Suszko
**Status:** Partially resolved (fixed in one folder; known, non-blocking
issue remains in a second, larger folder)

## Summary
A from-scratch Syncthing rescan (triggered by the config-loss incident
the same day) surfaced scan errors for files with Polish diacritics.
Unlike the separately-diagnosed `nls_utf8` kernel module issue, this was
a *data* problem, not a mount problem: at some point in the past, files
had been copied through a tool that mis-interpreted valid UTF-8 bytes as
a different encoding and re-saved them, leaving a second, garbled
duplicate filename sitting next to the correct original with identical
content. Confirmed byte-identical, then removed the ~64 stray duplicates
in the affected folder; the same pattern exists at larger scale in a
second folder, currently left as a known, non-blocking issue.

## Impact
64 duplicate files consuming storage and generating persistent Syncthing
scan warnings in the `Projects` folder; a much larger number (several
hundred, unquantified) of the same pattern in a second folder,
unaddressed. No data loss -- in every case checked, a correctly-named,
byte-identical copy already existed.

## Timeline
| Time | Event |
|---|---|
| T+0 | Full folder rescan (forced by the same-day Syncthing config loss) surfaces `scan: item is not in UTF8 encoding` and `hashing: open ...: no such file or directory` for files containing ą, ę, ż, ń, ś, ó |
| T+10m | `ls` shows broken names with a literal `?` where the letter should be; `convmv -f windows-1250 -t utf8` reports these as "already UTF-8" and makes no change |
| T+20m | Recognized this as a double-encoding artifact rather than a simple encoding mismatch -- the corrupted names are technically valid UTF-8, just the *wrong* UTF-8, re-encoded from bytes that were already correct |
| T+30m | Diagnosis method established: `find | cat -v` to see real bytes past terminal-mangled `?` placeholders; paired candidates spotted by filename length (the mojibake name is always longer than the healthy one) |
| T+40m | Verified byte-identical content between each healthy/mojibake pair using shell globs/array expansion (`files=(Strona*Biznesowy.html)`) rather than hand-typing broken filenames, which further mangles multi-byte sequences |
| T+50m | Removed all 64 confirmed-identical duplicates in the `Projects` folder scope (`Bujalski/E/db/W toku/`, `AnalitykBiznesowy_AI_Team_v2/gsc-exports/`) |
| T+55m | Noted the same pattern at much larger scale in a second Syncthing folder (`02_Dev`, `03_Osobiste`, `01_Praca`) -- mostly old build artifacts and personal documents; deferred |

## Root Cause
Files were, at some point in the past, copied through a tool (never
positively identified) that read already-valid UTF-8 bytes, misinterpreted
them as Windows-1252/Latin-1, and re-encoded them -- producing a second,
garbled filename with byte-identical file content sitting next to the
correctly-encoded original.

## Trigger
Not a single trigger -- these files had existed in this corrupted state
for an unknown period. The full rescan (itself triggered by the
config-loss incident) is what surfaced the existing problem broadly,
rather than causing it.

## Detection
Manual, via Syncthing's scan error log during a full rescan.

## Resolution
For each healthy filename, `diff -q` it against every longer candidate
name in the same directory; delete a candidate only if it diffs
byte-identical. `rm`, not rename, since a valid healthy copy already
existed in every case checked. Scoped to the `Projects` folder only.

## Action Items
- [ ] Apply the same diff-and-delete process to the second, larger
      Syncthing folder (`02_Dev`, `03_Osobiste`, `01_Praca`)
- [ ] For the Visual Studio build-artifact trees specifically (`.vs/`,
      `bin/`, `obj/`, `.suo`) inside that second folder, adding them to
      Syncthing's ignore patterns is likely a better fix than repairing
      their names, since they're disposable build output anyway
- [ ] Try to identify which historical tool caused the double-encoding,
      to confirm it's no longer in the current workflow and won't
      reintroduce the same corruption

## Lessons Learned
- `convmv` reporting "already UTF-8" does not mean a filename is
  correct -- it means the bytes are *valid* UTF-8, which double-encoded
  mojibake technically is. This is a different class of problem
  (duplicate stray files) than a genuine encoding mismatch, and needs a
  different fix (find-and-delete, not re-encode)
- Never hand-type or copy/paste a broken multi-byte filename into a
  terminal command -- both further mangle the bytes. Let the shell
  resolve the real bytes via globs or array expansion instead
- Verifying byte-identical content before deleting a suspected duplicate
  is worth the extra step even when confident -- it converts "probably
  a duplicate" into a provable, safe deletion
