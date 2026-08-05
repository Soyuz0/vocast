# Future work

Known gaps, recorded so they are not rediscovered from scratch. Each one is
something real that was found, understood, and deliberately left. Nothing here
is blocking; the service runs without any of it.

Ordered roughly by how much a listener would notice.

## Judge an extraction on its shape, not its wording

**Problem.** A fetch can succeed and still return something that is not the
article: a JavaScript rendering shell, a consent screen, a bot check, an error
page. The only guard on a *successful* extraction is the 400-character minimum,
which such pages clear easily because they arrive with the site's navigation. A
failure is visible; a page that returns the wrong thing is not.

**Evidence.** Thirty-one LessWrong episodes were the site's shell read aloud:
"This website requires javascript to properly function… LESSWRONG LW Login",
then the title and tag list. LessWrong serves a megabyte of React to a plain
fetcher, from which the extractor recovers 659 characters. A 47-second episode
stood where a 20-minute one belonged, with no error recorded anywhere.

**What exists now.** Two rules, of different quality:

- If the feed holds more than twice the text the page yielded, the feed's copy
  is used. Genuinely general: no phrases, no domains. It also catches consent
  screens and Cloudflare checks, because it reasons about how little the page
  gave us rather than about why.
- A "requires JavaScript" notice is treated as a failed extraction. This one is
  phrase-based, and catches roughly five of eight wordings seen in the wild. It
  misses Vue and Nuxt's "doesn't work properly without JavaScript enabled",
  "works best with scripting turned on", and Cloudflare's "checking your
  browser".

**The gap.** With no feed body there is nothing to compare against, so only the
phrase rule applies, and it is incomplete. A source that publishes no body,
behind a shell with unfamiliar wording, would still be narrated as chrome. No
such source exists in the current corpus -- all thirty-one affected entries had a
feed copy -- but the hole is real rather than theoretical.

**Proposed fix.** Not more phrases. Judge the extracted text on its *shape*:
navigation is short lines with no sentence-terminating punctuation, a high
proportion of link text, and few function words. An article is the opposite. A
check along those lines rejects shells, consent screens and error pages
uniformly, in any language, and needs no list to maintain. It would also improve
the failure message, which currently guesses ("probably a paywall, consent
screen, or navigation stub").

## Refuse to synthesize when the library is unreachable

**Problem.** Two related risks when the audio share is not mounted.

Replacing an episode in place raises `FileNotFoundError` if its directory is
missing, and the fallback creates a *new* episode, which means a new podcast
GUID. During a mount outage that would fire for every article being re-narrated,
withdrawing and re-publishing each one for every subscriber.

Worse, writing into an unmounted mount point silently succeeds against the local
filesystem. Episodes would be written to the root disk instead of the share,
filling it with audio that the feeds cannot see, since the database still points
at paths under the mount.

**Why it is still open.** A first attempt guarded on `library_path().is_dir()`,
which does not address the risk: an unmounted mount point still exists as an
empty directory, so the check passes and the fallback fires anyway. It also
broke the legitimate case of a fresh, empty library. The attempt was reverted
rather than shipped, because it looked like a fix without being one.

**Proposed fix.** A storage health check before the worker synthesizes at all,
rather than a guess inside the generator. The distinction it needs is between
"the library is empty" and "the library is unreachable", which the worker can
make because it can see what the database expects to be there. Refuse to run
while they disagree.

## Exit 139 on a clean shutdown

**Problem.** A normal stop ends with `terminate called without an active
exception` after the application has shut down correctly, so Docker records exit
139, a segfault. The shutdown itself is clean: loops stop, workers cancel and
requeue their in-flight articles, nothing is lost.

**Why it matters anyway.** Every deliberate stop looks like a crash. Any
monitoring added later will report crashes that did not happen, and a genuine
crash will be indistinguishable from the noise.

**Proposed fix.** Release the ONNX session, and whatever espeak holds, before
interpreter teardown, rather than leaving it to garbage collection at exit.

## Retention evicts the wrong end of the library

Retention deletes earliest-synthesized first while narration works
newest-published first, so under pressure it removes episodes that were just made
while keeping ones already heard. Harmless today: retention is disabled. The
config file already warns about it at the `retention` block; this is a note that
the code, not just the comment, should be fixed if it is ever switched on.

## Give failures a reason, so they can be triaged

Failures are classified only by their message text. Of the several hundred that
accumulate, the overwhelming majority are stubs that will never narrate --
comics, YouTube pages, podcast episode pages, paywalls -- and the interesting
ones hide among them. The count grows steadily with the backlog, which is the
problem: the signal does not. The
real bugs this session (a synthesis crash, retryable blocks treated as
permanent, a whole publication behind an unresolvable hostname) were found by
reading the list by hand.

**Proposed fix.** Record a reason on the entry: repeated site chrome, paywall,
podcast page, too short, fetch failed. Repeated chrome needs no phrase list --
hash the extracted text and compare against other entries from the same
publication, which identified 87 of 141 failures when tried offline. Then the
library can filter by reason and the eight worth acting on stop being buried
under four hundred that are not.

**Note.** This is for triage, not for admitting more articles. Lowering the
400-character floor was measured and rejected: junk spans 105-399 characters and
genuine short posts span 290-367, so no threshold separates them. A
per-publication minimum would serve the two publications that legitimately post
short pieces.

## Smaller things

- **`PATCH /api/sources`** validates a new URL against `{}` instead of the
  source's retained options, so a private URL can be rejected and then accepted
  on resend with the options repeated. Only reachable on the tailnet, and only
  matters if sources are managed through the API rather than `config.yaml`.
- **Silent truncation before the phoneme fix.** Some episodes narrated before
  the kokoro batching fix are missing audio: an oversized phoneme batch was
  truncated without a word. Which ones cannot be identified after the fact,
  since narration text is stored but the audio's completeness is not recorded.
  Accepted rather than regenerating everything.
- **Feed titles** were unified to `Vocast - <what>`; a podcast client may show
  renamed shows as new subscriptions. Episode GUIDs were untouched, so nothing
  re-downloads.

## Disk, for reference

Neither filesystem is urgent, but both are worth watching, since a full disk
would stop the database, the nightly backup, or episode writes.

| filesystem | holds | used | free |
|---|---|---|---|
| root | database, model cache | 53% | 108 GB |
| SMB share | episode audio | 93% | 590 GB of 7.3 TB |

The share is the one with a high percentage, but 590 GB against a projected
backlog of well under 100 GB leaves ample room. Storing feed bodies as a
narration fallback adds tens of megabytes to the database, not gigabytes.
