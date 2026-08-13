# Purging the voice fixtures from git history — runbook

**Status:** prepared, **not executed**. Executing it rewrites shared history and is the
repository owner's decision.
**Date:** 2026-08-14

## What is being removed and why

`tests/fixtures/voiceprint_parity/` carried five seconds of **raw voice audio** for six real
people — `ben`, `ben_chinese`, `Joe`, `Leo`, `Mike`, `Zoe` — plus their **192-d voiceprints**
in `references.json`. Committed in `b9ea9c5` on 2026-08-13 17:23 NZ by the ONNX export work,
into a **public** repository, with no consent record and no expiry.

Four of the six are third parties. They did not agree to this.

`HEAD` is already clean: the fixtures are deterministic synthetic tones and a test fails if
one is ever named after a person again. **This runbook is only about the past.**

## Measured before writing this

| | |
|---|---|
| commits containing the files | **2** (the add, and the removal) |
| commits that would be **rewritten** | **131** (everything after `b9ea9c5` on `develop`) |
| open PRs that would break | **2** (#404, #474) |
| remote branches carrying the blobs at their tip | **~50** |
| forks | 0 |
| `git-filter-repo` installed on this machine | **no** |

That last row matters: the tool is not present, so a runbook that opens with
`git filter-repo …` fails at the first line.

## Do this first, and separately

**Make the repository private.** It takes seconds, is reversible, breaks no clone (there are
no forks), and — unlike everything below — it also covers the history. Rewriting history
while the repo is public leaves the window open for however long the coordination takes.

```bash
gh repo edit benzn-tech/fieldsight-pipeline --visibility private
```

The one real cost: private repositories consume Actions minutes from the account allowance,
where public ones are unlimited. This repo ran ~100 workflows on 2026-08-13 alone. That is a
budget question, not a reason to leave third parties' biometric data public.

## Prerequisites for the rewrite

1. **Every other session has stopped.** 131 commits change SHA; every branch not based on the
   new history has to be re-created. On 2026-08-13 there were several sessions working in
   parallel, and the user-deletion work alone had nine branches.
2. **The two open PRs are merged or closed.** They cannot survive the rewrite.
3. `git-filter-repo` is installed: `pip install git-filter-repo` (or `uv tool install
   git-filter-repo`). It requires a **fresh clone** — it refuses to run in a repo with other
   worktrees or uncommitted state, which every worktree on this machine has.

## The rewrite

```bash
# 1. A fresh mirror, away from every existing worktree.
cd /c/Users/camil/AppData/Local/Temp
git clone --mirror https://github.com/benzn-tech/fieldsight-pipeline.git purge.git
cd purge.git

# 2. Confirm the blobs are there BEFORE, so step 4 means something.
git cat-file -e b9ea9c5:tests/fixtures/voiceprint_parity/ben.npy && echo "present"

# 3. Remove the path from every commit.
git filter-repo --path tests/fixtures/voiceprint_parity --invert-paths --force

# 4. Confirm they are gone. This must print nothing.
git log --all --oneline -- tests/fixtures/voiceprint_parity | head

# 5. Push. This is the irreversible step.
git push --force --mirror
```

## After the push, two things are still true

**GitHub keeps unreachable objects.** The old commits stop being reachable by branch, but
remain fetchable **by direct SHA** until GitHub garbage-collects. For sensitive data GitHub's
own guidance is to open a Support request asking them to purge the cached views and run GC.
Do that; the push alone is not the end of it.

**Every clone still has them.** Anyone who cloned in the last day holds the blobs in their
own `.git`. Tell them to re-clone rather than pull, and delete the old copy.

## And the part no command covers

Four of the six voices belong to people who are not the account owner. Whatever the technical
remediation, they are the ones whose data this was. That conversation is not something this
runbook can do.

## Rollback

There is none. `--force --mirror` replaces the remote's refs. Keep the pre-rewrite mirror
clone until you are satisfied:

```bash
cp -r purge.git purge-backup.git   # BEFORE step 3
```
