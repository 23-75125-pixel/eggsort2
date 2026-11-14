# Quality verification reporting

Egg record storage remains backward-compatible: `quality` keeps its existing
Good/Crack/Rotten value and `quality_verified` records provenance. `true` means
the camera supplied the classification, `false` means an automatic fallback
stored the load-cell result without camera verification, and `NULL` means the
record predates verification tracking or its provenance is unknown. `NULL` is
never treated as verified.

The existing `quality_rate` and Reports `summary.quality_rate` retain their
historical definition: all records labelled Good divided by all records in the
selected period. The UI now calls this the historical/all-record rate and
shows verification status separately; it must not be interpreted as a
camera-confirmed rate.

The new **Verified Good Rate** is:

`records where quality = Good and quality_verified = true` divided by
`explicitly verified records labelled Good, Crack, or Rotten`.

Unverified and legacy/unknown records are excluded from both sides of that
rate. The Dashboard and Reports also expose total and per-quality verification
counts for Good, Crack, Rotten, and Other (Verified / Unverified /
Legacy/Unknown). Egg
Records, its CSV export, and Sorting Sessions' latest-record label show the
verification state per record. Size classification and tray-progress rules are
unchanged; only explicitly verified Good records advance tray progress.

## Automatic capture association limitation

In `EGGSORT_AUTO_V1`, the ESP32 emits its own cycle IDs. Camera passage IDs are
not sent to the controller or echoed with controller measurements. The current
application associates Good camera captures to completed controller cycles in
FIFO order. Tests reproduce that a missing or delayed first capture can shift a
later/older capture onto the wrong cycle and mark that row verified. This is a
known integration risk, not a verified physical occurrence. A software-only
fail-safe could stop assigning camera verification in automatic mode unless a
shared correlation is available, but that would turn otherwise camera-visible
automatic records into unverified records. Reliable positive association needs
a reviewed correlation protocol or equivalent sensor/timing evidence; no
protocol or sorting behavior is changed by this reporting update.
