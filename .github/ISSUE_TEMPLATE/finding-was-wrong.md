---
name: The reviewer was wrong
about: A finding that is not a real defect, or a real defect it walked past
title: ''
labels: 'finding-quality'
---

<!--
The most useful issue this project can receive. Every measured claim in the
README came from someone looking at a specific finding and disagreeing with it.

Do not paste anything you cannot make public. A finding quotes the code it is
about, and credentials are redacted before a comment is posted but a diff you
paste here is not.
-->

**What it said**

<!-- The finding, or a link to the comment if the repository is public. -->

**Why that is wrong**

<!--
For a false positive: what guards this, and where. "The caller validates" is
the commonest answer, and the reviewer is supposed to go and read the caller —
if it did not, that is the bug.

For a miss: what the defect is, and whether it can be decided from the diff
alone or needs another file.
-->

**Configuration**

- Version:
- `scan` / `verification` / `repo-access`:
- Models, if not the defaults:

**Did the summary say the review was degraded?**

<!--
A run that could not read the checkout, lost a model, or dropped files says so.
That changes what the finding means, and it is the first thing worth checking.
-->
