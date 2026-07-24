# CinderX candidate source provenance

The scalar-piercing image uses an openEuler CinderX source snapshot based on
upstream commit `ac09c68527153b43cc8b4f16f36d9245cb861d12`, plus the deterministic
runtime overlay in `patches/0001-runtime-candidate.patch`.

The patch contains the complete runtime/API/test delta of the validated
candidate, including pre-existing platform fixes on which the UDF intrinsic
depends. Build output, virtual environments, IDE state, CI-only files,
generated egg metadata, and documentation are intentionally outside the
runtime-tree identity. The exact exclusions and before/after tree hashes are
recorded in `patches/manifest.json`.

Apply the overlay from the root of the matching CinderX baseline:

```text
patch --batch -p1 < 0001-runtime-candidate.patch
```

Formal acceptance recomputes the committed patch SHA-256, records the
normalized candidate runtime-tree SHA-256, and carries both values into the
candidate image labels and CinderX test proof. A wheel or image built from a
different patch/tree identity is rejected.
