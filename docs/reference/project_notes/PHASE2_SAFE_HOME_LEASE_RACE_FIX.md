# Phase 2 Safe-Home Lease Race Fix

Date: 2026-07-27
Status: implemented, software-tested, and powered-startup verified; pending
guarded Basic-only safe-home retest

## Incident

During the first guarded Phase 2 hardware gate, Basic safe-home revoked the
Integrated controller's operational lease as designed. Integrated remained
HOT, classified the revocation as lease loss, and automatically reacquired a
new operational lease in the background.

Basic previously allowed operational lease acquisition while `SAFE_HOME`.
The operational acquisition handler then unconditionally requested
gravity-float. This changed the Basic state to
`SAFE_HOLD_GRAVITY_FLOAT` while the original safe-home request thread
continued transmitting rate-limited home targets.

The result was two internal writers:

- the safe-home request thread transmitted fixed/rate-limited home targets;
- the Basic 50 Hz loop transmitted measured-position gravity-float targets.

No Integrated IK or motion envelope was accepted. Captured Basic telemetry
reported zero submitted operational command envelopes. The conflict arose from
the lease-acquisition side effect changing the Basic safety state.

## Correction

Basic now:

- treats safe-home as an exclusive hardware-writing operation;
- blocks operational lease acquisition, renewal, payload updates, and command
  submission while that operation is active;
- rejects even a direct lease acquisition when the current state is
  `SAFE_HOME`;
- lets an explicit gravity-float request cancel safe-home, and makes the
  safe-home loop check cancellation and state ownership before every later
  hardware write;
- publishes the last safe-home result with position/velocity failure indices
  and maximum errors.

Integrated now:

- changes residency to `RECOVERY_REQUIRED` after any fenced Basic lease loss;
- marks itself not ready and stops the background lease loop;
- requires an explicit HOT transition before reacquiring Basic authority.

## Regression coverage

New tests verify:

- safe-home rejects concurrent Integrated-style lease reacquisition;
- rejected operational acquisition cannot invoke the former gravity-float
  side effect;
- explicit gravity-float cancels the safe-home writer;
- Integrated does not reacquire in the background after lease loss;
- an explicit HOT transition can recover after the safety operation is over.

The complete Basic suite passes 78 tests and the complete Integrated suite
passes 65 tests.

## Powered startup verification

With the user present and arm power restored, Basic and Integrated were started
through the guarded physical startup script. No safe-home or action endpoint
was invoked.

Across six state samples:

- Basic remained `HEALTHY` in `SAFE_HOLD_GRAVITY_FLOAT`;
- Integrated remained disengaged, ready, and without an active trajectory;
- the fenced lease remained at generation 1;
- maximum observed absolute joint speed was `0.0073 rad/s`;
- Basic operational command submissions remained zero;
- Integrated motion command and commit counts remained zero;
- safe-home attempt count remained zero;
- Manager physical enforcement remained false.

## Next guarded retest boundary

The next round must be separately authorized. It should stop or transition
Integrated out of HOT without invoking Basic safe-home, then exercise
Basic-only safe-home, confirm exclusive ownership and successful settling, and
return to gravity-float. Integrated may enter HOT again only after Basic
safe-home completes.
