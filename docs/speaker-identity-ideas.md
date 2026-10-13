# Speaker identity — idea list (brainstorm, 2026-09-29)

The full list from the owner's voiceprint brainstorm. Starred ideas were promoted to
`ROADMAP.md` ("Speaker identity — roadmap"). The rest are kept here on purpose: nothing
in this file is scheduled, and nothing is to be deleted because it is not scheduled.

Customer-facing rule for all of these: **no scores or thresholds on screen.** A customer
sees plain words ("probably Ben", "we're not sure who this is"), never 0.43.

## Recognising people
- **Addressing cues.** "Thanks John" makes the next speaker likely to be John — an
  independent signal alongside the voice.
- **Wearer first.** The loudest, closest voice is most likely the device wearer; use
  loudness and reverberation to tell near from far.
- **Devices vouch for each other.** Two devices in one meeting: A's near-field voice on
  B's recording is A.

## The library
- **Library health.** Tell an admin what is missing: "no windy-site sample for Ben",
  "Sam has only English samples".
- **"Not them" as a negative.** Use rejected proposals to tighten that person's own
  threshold instead of discarding them.
- **Voice drift.** Weight recent samples more; treat a cold or shouting on site as its
  own condition.
- **Accuracy over time.** Show an admin how recognition improves as they confirm names
  ("38 names confirmed this month; most passages now named").

## Using the identity
- **Search and Ask by person.** "What did Neil say about the pour this week?"
- **Commitments over time.** "Petros said three times he'd send the drawings" — ties
  into recurring-item threading.
- **Roll-ups by subcontractor.** Summaries per company/trade from identified speakers.
- **Personal daily digest.** Each identified person (not just the wearer) gets "what
  you agreed to today" — gives non-wearers a reason to consent.
- **Per-speaker transcription language.** Mandarin-first for Ben, English plus his own
  vocabulary for Petros.

## Privacy and trust
- **Delete by person.** "Remove everything Benny said in this project" — reuses
  life-conversation separation.
- **Private talk hidden automatically.** The wearer's phone calls and personal chat,
  detected from who is speaking plus what is said.
- **Show the evidence.** "Why we think this is Ben": two reference snippets side by
  side to listen to.

## Security
- **Synthetic voice detection.** Device playback, phone speakers and spoofed audio
  classed as "not a person" (the FieldSight App profile was the first case).
- **Device handover detection.** The wearer changes mid-day; attribution after that
  point is corrected.
- **Unregistered voices.** "3 voices on site today that are not signed in" — site
  security and insurance.

## Platform and frontier
- **Cross-company consented pool / voice passport.** Population-level calibration for
  new companies; a worker-held voiceprint that moves between GCs with them.
- **On-device embeddings.** Only the voiceprint leaves the device, not the audio.
- **Hardware.** Dual-mic beamforming to separate the wearer; windshields.
- **Communication graph.** Who talks to whom; trades that never talk.
- **(Handle with care) stress, shouting, alarm signals.** Safety value is real; ethical
  and legal risk is the highest on this list. Only with explicit consent and only for
  safety.
