#include "reject_gate_protocol.h"

// Compile-time simulations only. These assumptions are NOT evidence of
// physical clearance. No runtime test instances or servo commands are emitted.
namespace {
using namespace reject_experiment;
constexpr Config model() {
  Config c;
  c.enabled = true;
  c.arrival_bounds_verified = true; // Assumed for this mathematical model ONLY.
  c.initial_normal_route_clear = true;
  c.clearance_verified = true;
  // Observed sloped-bin transfer completes during closing. Zero EXTRA dwell
  // is a nominal model case only; positive/unknown dwell is tested separately.
  c.extra_clearance_ms = 0;
  c.min_arrival_after_accept_ms = 4510;
  c.max_arrival_after_accept_ms = 4860;
  c.max_source_age_ms = 50;
  return c;
}
constexpr Request request(uint32_t id, Quality quality = Quality::Crack, uint32_t age = 0) {
  return {1, id, quality, age};
}
constexpr bool single(Quality quality) {
  Scheduler s(model());
  if (s.begin(1) != Status::Started || s.submit(request(1, quality), 1010).deadline != 7510) return false;
  if (s.tick(1010).motion != Motion::Open || s.state() != State::Opening) return false;
  s.tick(1190);
  if (s.state() != State::Holding || s.tick(7509).motion != Motion::None) return false;
  if (s.tick(7510).motion != Motion::Close) return false;
  s.tick(7810); // Old 300 ms interval must NOT finish a 450 ms close.
  if (s.state() != State::ClosingRelease) return false;
  if (s.tick(7960).reply.status != Status::SettledUnverified) return false;
  return s.state() == State::ClosedClearance && s.tick(7960).motion == Motion::None;
}
constexpr bool pair(Quality first, Quality second) {
  Scheduler s(model()); s.begin(1);
  s.submit(request(1, first), 1010); s.tick(1010);
  const Reply next = s.submit(request(2, second), 3980);
  if (next.status != Status::Queued || next.deadline != 10480 || s.activeDeadline() != 7510) return false;
  if (s.tick(3980).motion != Motion::None || s.tick(7510).motion != Motion::Close) return false;
  if (s.tick(7959).motion != Motion::None) return false;
  s.tick(7960);
  if (s.tick(7960).motion != Motion::Open || s.activeDeadline() != 10480) return false;
  s.tick(8140);
  return s.state() == State::Holding && s.tick(10479).motion == Motion::None &&
    s.tick(10480).motion == Motion::Close;
}
constexpr bool duplicatesAndRunIsolation() {
  Scheduler s(model()); s.begin(1);
  const Reply first = s.submit(request(1), 1010); s.tick(1010);
  const Reply duplicate = s.submit(request(1, Quality::Crack, 9000), 5000);
  if (duplicate.status != Status::Duplicate || duplicate.deadline != first.deadline || s.pending()) return false;
  if (s.submit(request(1, Quality::Rotten), 5000).status != Status::Conflict) return false;
  if (s.begin(2) != Status::RunMismatch) return false;
  if (s.submit({2, 2, Quality::Crack, 0}, 5000).status != Status::RunMismatch) return false;
  s.tick(7510); s.tick(7960); s.tick(7960);
  return s.submit(request(1), 9000).status == Status::Duplicate && s.tick(9000).motion == Motion::None;
}
constexpr bool spacingConflictPreservesFirstClose() {
  Scheduler s(model()); s.begin(1); s.submit(request(1), 1010); s.tick(1010);
  return s.submit(request(2), 3010).status == Status::SpacingConflict &&
    s.activeDeadline() == 7510 && s.tick(7510).motion == Motion::Close;
}
constexpr bool unknownClearanceNeverReopens() {
  Config c = model(); c.clearance_verified = false;
  Scheduler s(c); s.begin(1); s.submit(request(1), 1010); s.tick(1010);
  s.submit(request(2), 3980); s.tick(7510); s.tick(7960);
  const Action blocked = s.tick(8000);
  if (blocked.motion != Motion::None || blocked.reply.status != Status::UnverifiedClearance ||
      s.state() != State::ClosedClearance) return false;
  return s.tick(10480).reply.status == Status::Expired && s.tick(12000).motion == Motion::None;
}
constexpr bool slopedBinBudget(uint32_t extra, uint32_t guard,
                               uint32_t firstAcceptLag, bool shouldFit) {
  // A parameter study, not approved physical constants. The closing/pushing
  // interval is counted ONCE; extra is only the residual beyond full closure.
  Config c = model(); c.extra_clearance_ms = extra; c.guard_ms = guard;
  Scheduler s(c); s.begin(1);
  const uint32_t accepted = 1010 + firstAcceptLag;
  const uint32_t firstClose = accepted + 6500;
  s.submit(request(1), accepted); s.tick(accepted);
  const Reply next = s.submit(request(2), 3980);
  if (s.activeDeadline() != firstClose || next.deadline != 10480) return false;
  if (!shouldFit)
    return next.status == Status::SpacingConflict && s.tick(firstClose).motion == Motion::Close;
  if (next.status != Status::Queued || s.tick(firstClose).motion != Motion::Close) return false;
  s.tick(firstClose + 450);
  const uint32_t reopen = firstClose + 450 + extra;
  if (extra && s.tick(reopen - 1).motion != Motion::None) return false;
  if (s.tick(reopen).motion != Motion::Open || s.activeDeadline() != 10480) return false;
  s.tick(reopen + 180);
  return s.state() == State::Holding && s.tick(10480).motion == Motion::Close;
}
constexpr bool goodClearanceWindowArithmetic() {
  // Separate from rejection clearance: a Good egg follows the normal route,
  // so its clearance is NOT established by the sloped Reject Bin observation.
  const uint32_t nextArrival = 8490;
  const uint32_t assumedGoodClear = 7000; // Hypothetical sensitivity input only.
  const uint32_t lateGoodClear = 8200;
  return assumedGoodClear + 180 + 100 < nextArrival &&
    lateGoodClear + 180 + 200 > nextArrival;
}
constexpr bool goodThenCrackIsUnverified() {
  Scheduler s(model()); s.begin(1);
  return s.submit(request(1, Quality::Good), 1010).status == Status::UnsafeGood &&
    s.submit(request(2), 3980).status == Status::UnsafeGood && s.tick(3980).motion == Motion::None;
}
constexpr bool crackThenGoodDoesNotDisturbActiveDeadline() {
  Scheduler s(model()); s.begin(1); s.submit(request(1), 1010); s.tick(1010);
  if (s.submit(request(2, Quality::Good), 3980).status != Status::UnsafeGood) return false;
  return s.activeDeadline() == 7510 && s.tick(7510).motion == Motion::Close && s.pending() == 0;
}
constexpr bool boundedOverflow() {
  Config c = model(); c.clearance_verified = false;
  Scheduler s(c); s.begin(1); s.submit(request(1), 1010); s.tick(1010);
  for (uint32_t id = 2; id <= 5; ++id) s.submit(request(id), 1020);
  return s.pending() == Scheduler::Capacity && s.submit(request(6), 1020).status == Status::QueueFull &&
    s.submit(request(6), 1021).status == Status::QueueFull && s.pending() == Scheduler::Capacity &&
    s.activeDeadline() == 7510 && s.tick(7510).motion == Motion::Close;
}
constexpr bool expiryAndLateClose() {
  Scheduler queued(model()); queued.begin(1); queued.submit(request(1), 1010);
  if (queued.tick(7510).reply.status != Status::Expired || queued.tick(8000).motion != Motion::None) return false;
  Scheduler active(model()); active.begin(1); active.submit(request(1), 1010); active.tick(1010);
  return active.tick(7511).motion == Motion::Close && active.fault() == Status::DeadlineMissed;
}
constexpr bool rollover(uint32_t start) {
  Scheduler s(model()); s.begin(1);
  const Reply r = s.submit(request(1), start); s.tick(start); s.tick(start + 180);
  return r.deadline == uint32_t(start + 6500) && s.tick(start + 6499).motion == Motion::None &&
    s.tick(start + 6500).motion == Motion::Close &&
    s.tick(start + 6950).reply.status == Status::SettledUnverified;
}
constexpr bool defaultsAndStale() {
  Scheduler disabled;
  if (disabled.begin(1) != Status::Disabled || disabled.tick(0).motion != Motion::None) return false;
  Config c; c.enabled = true;
  Scheduler unknown(c); unknown.begin(1);
  if (unknown.submit(request(1), 100).status != Status::UnverifiedTiming) return false;
  Scheduler s(model()); s.begin(1);
  return s.submit(request(1, Quality::Crack, 51), 100).status == Status::Stale && s.tick(100).motion == Motion::None;
}
constexpr bool evictedIdsNeverExecuteAgain() {
  Scheduler s(model()); s.begin(1); s.submit(request(1), 1010); s.tick(1010);
  for (uint32_t id = 2; id < 30; ++id) s.submit(request(id, Quality::Good), 2000);
  return s.submit(request(1), 3000).status == Status::Stale && s.pending() == 0;
}
constexpr bool protocolParsing() {
  Frame f;
  if (!parseRequest("XR:12345678:0000000000000001:2:R:15", f)) return false;
  if (f.boot != 0x12345678 || f.request.run != 1 || f.request.id != 2 ||
      f.request.quality != Quality::Rotten || f.request.source_age_ms != 15) return false;
  if (parseRequest("XR:12345678:0000000000000001:-2:C:0", f)) return false;
  if (parseRequest("XR:12345678:0000000000000001:4294967296:C:0", f)) return false;
  if (parseRequest("XR:12345678:0000000000000001:2:C:0:EXTRA", f)) return false;
  if (parseRequest("XR:", f)) return false;
  uint32_t boot = 0; uint64_t run = 0;
  return parseBegin("XB:12345678:0000000000000001", boot, run) && boot == 0x12345678 && run == 1;
}
static_assert(single(Quality::Crack), "Single Crack lifecycle");
static_assert(single(Quality::Rotten), "Single Rotten lifecycle");
static_assert(pair(Quality::Crack, Quality::Crack), "Crack -> Crack fixed deadlines");
static_assert(pair(Quality::Crack, Quality::Rotten), "Crack -> Rotten fixed deadlines");
static_assert(pair(Quality::Rotten, Quality::Crack), "Rotten -> Crack fixed deadlines");
static_assert(duplicatesAndRunIsolation(), "Duplicate/retry and run isolation");
static_assert(spacingConflictPreservesFirstClose(), "Insufficient spacing");
static_assert(unknownClearanceNeverReopens(), "Unknown clearance cannot be treated as zero");
static_assert(goodThenCrackIsUnverified(), "Good -> Crack must fail closed");
static_assert(crackThenGoodDoesNotDisturbActiveDeadline(), "Crack -> Good remains unverified");
static_assert(boundedOverflow(), "Bounded queue overflow");
static_assert(expiryAndLateClose(), "Expired jobs never open late");
static_assert(rollover(UINT32_MAX - 2000), "millis wraparound");
static_assert(rollover(uint32_t(0) - 6500), "Zero is a valid wrapped deadline");
static_assert(defaultsAndStale(), "Disabled defaults and stale commands");
static_assert(evictedIdsNeverExecuteAgain(), "History eviction cannot replay an ID");
static_assert(protocolParsing(), "Bounded tagged serial protocol");
static_assert(slopedBinBudget(100, 100, 0, true), "Positive extra dwell fits nominal sloped-bin budget");
static_assert(slopedBinBudget(200, 150, 0, true), "Exact accounted budget is not universal clearance proof");
static_assert(slopedBinBudget(201, 150, 0, false), "Excess clearance/guard budget prevents reopening");
static_assert(slopedBinBudget(100, 100, 100, true), "Controller acceptance lag consumes nominal headroom");
static_assert(slopedBinBudget(150, 100, 150, false), "Acceptance lag plus dwell can make the pair infeasible");
static_assert(goodClearanceWindowArithmetic() && goodThenCrackIsUnverified(),
              "Reject-bin transfer evidence does not establish Good-route clearance");
} // namespace
