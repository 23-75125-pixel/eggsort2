#pragma once

#include <stddef.h>
#include <stdint.h>

// Experimental, hardware-independent core. All times are controller millis().
// No calls to motors, Serial, delay(), allocation, or host clocks occur here.
namespace reject_experiment {
enum class Quality : uint8_t { Crack, Rotten, Good };
enum class State : uint8_t { Closed, Opening, Holding, ClosingRelease, ClosedClearance };
enum class Status : uint8_t {
  Ok, Disabled, Started, RunMismatch, Invalid, Stale, Duplicate, Conflict,
  Accepted, Queued, QueueFull, UnverifiedTiming, UnverifiedClearance,
  UnsafeGood, SpacingConflict, Expired, DeadlineMissed, SettledUnverified
};
enum class Motion : uint8_t { None, Open, Close };

struct Config {
  bool enabled = false;
  uint32_t hold_ms = 6500; // Preserve the working controller-command reference.
  // Sloped-bin observation: pushing/transfer overlaps this closing movement;
  // do not add a second full egg-transfer interval after it. This is nominal,
  // not a verified worst-case bound for every egg or loading condition.
  uint32_t close_ms = 450;
  uint32_t open_ms = 180;
  // These defaults mean UNKNOWN, not verified zero clearance or zero latency.
  bool arrival_bounds_verified = false;
  bool initial_normal_route_clear = false;
  bool clearance_verified = false;
  uint32_t extra_clearance_ms = 0; // Only additional dwell AFTER full closure; still unknown.
  uint32_t min_arrival_after_accept_ms = 0;
  uint32_t max_arrival_after_accept_ms = 0;
  uint32_t guard_ms = 0;
  uint32_t max_source_age_ms = 0;
};
struct Request {
  uint64_t run = 0;
  uint32_t id = 0;
  Quality quality = Quality::Crack;
  uint32_t source_age_ms = 0;
};
struct Reply {
  uint32_t id = 0;
  uint32_t deadline = 0;
  Status status = Status::Ok;
};
struct Action {
  Motion motion = Motion::None;
  Reply reply{};
};
struct Job {
  Request request{};
  uint32_t accepted_at = 0;
  uint32_t deadline = 0;
};
struct Seen {
  Request request{};
  Reply reply{};
};

constexpr bool reached(uint32_t now, uint32_t target) {
  return static_cast<int32_t>(now - target) >= 0;
}
constexpr bool before(uint32_t a, uint32_t b) { return !reached(a, b); }

class Scheduler {
 public:
  static constexpr size_t Capacity = 4; // Pending jobs, in addition to active.
  static constexpr size_t History = 16;
  constexpr explicit Scheduler(Config config = Config{}) : config_(config) {}
  constexpr State state() const { return state_; }
  constexpr Status fault() const { return fault_; }
  constexpr size_t pending() const { return count_; }
  constexpr uint64_t run() const { return run_; }
  constexpr uint32_t activeDeadline() const { return active_.deadline; }
  constexpr bool active() const { return active_valid_; }

  // One run per controller boot: delayed BEGIN cannot reset dedup/deadlines.
  constexpr Status begin(uint64_t run) {
    if (!config_.enabled) return Status::Disabled;
    if (!run) return Status::Invalid;
    if (run_ && run_ != run) return Status::RunMismatch;
    run_ = run;
    return Status::Started;
  }

  constexpr Reply submit(Request request, uint32_t now) {
    if (!config_.enabled) return {request.id, 0, Status::Disabled};
    if (!run_ || request.run != run_) return {request.id, 0, Status::RunMismatch};
    if (!request.id) return {0, 0, Status::Invalid};
    if (request.quality != Quality::Crack && request.quality != Quality::Rotten && request.quality != Quality::Good)
      return {request.id, 0, Status::Invalid};
    for (size_t i = 0; i < seen_count_; ++i) {
      if (seen_[i].request.id != request.id) continue;
      if (seen_[i].request.quality != request.quality)
        return {request.id, seen_[i].reply.deadline, Status::Conflict};
      Reply cached = seen_[i].reply;
      if (cached.status == Status::Accepted || cached.status == Status::Queued ||
          cached.status == Status::SettledUnverified) cached.status = Status::Duplicate;
      return cached; // Age on a retry never changes the original deadline.
    }
    if (request.id <= high_water_) return {request.id, 0, Status::Stale};
    high_water_ = request.id; // Evicted/rejected IDs cannot later become new jobs.
    if (request.quality == Quality::Good) {
      // No invented Good-clearance timer. Latch until a new supervised boot.
      latch(Status::UnsafeGood);
      return remember(request, {request.id, 0, Status::UnsafeGood});
    }
    if (!validTiming())
      return remember(request, {request.id, 0, Status::UnverifiedTiming});
    if (request.source_age_ms > config_.max_source_age_ms)
      return remember(request, {request.id, 0, Status::Stale});
    if (fault_ != Status::Ok)
      return remember(request, {request.id, 0, fault_});
    if (count_ == Capacity) {
      latch(Status::QueueFull);
      return remember(request, {request.id, 0, Status::QueueFull});
    }
    Job job{request, now, now + config_.hold_ms};
    // Detect schedule conflicts even while another egg is still travelling.
    if (config_.clearance_verified) {
      uint32_t available = now;
      if (count_) available = queue_[count_ - 1].deadline + config_.close_ms + config_.extra_clearance_ms;
      else if (active_valid_) available = active_.deadline + config_.close_ms + config_.extra_clearance_ms;
      else if (state_ == State::ClosedClearance) available = state_at_ + config_.extra_clearance_ms;
      if (before(available, now)) available = now;
      if (!fits(job, available)) {
        latch(Status::SpacingConflict);
        return remember(request, {request.id, job.deadline, Status::SpacingConflict});
      }
    }
    const bool busy = active_valid_ || count_ || state_ == State::ClosedClearance;
    queue_[count_++] = job;
    return remember(request, {request.id, job.deadline, busy ? Status::Queued : Status::Accepted});
  }

  constexpr Action tick(uint32_t now) {
    if (!config_.enabled) return {};
    if (active_valid_ && (state_ == State::Opening || state_ == State::Holding)) {
      if (reached(now, active_.deadline)) {
        const bool late = now - active_.deadline > config_.guard_ms;
        if (late) latch(Status::DeadlineMissed);
        state_ = State::ClosingRelease;
        state_at_ = now; // Real CLOSE command time; never backdate movement.
        return {Motion::Close, {active_.request.id, active_.deadline,
                late ? Status::DeadlineMissed : Status::Ok}};
      }
      if (state_ == State::Opening && now - state_at_ >= config_.open_ms)
        state_ = State::Holding;
      return {};
    }
    if (state_ == State::ClosingRelease) {
      if (now - state_at_ < config_.close_ms) return {};
      state_ = State::ClosedClearance;
      state_at_ += config_.close_ms;
      active_valid_ = false;
      Reply reply{active_.request.id, active_.deadline, Status::SettledUnverified};
      updateHistory(reply);
      return {Motion::None, reply}; // Timer settling is NOT proof of egg clearance.
    }
    // Retire missed work without opening late or changing its deadline.
    if (count_ && reached(now, queue_[0].deadline)) {
      Reply reply{queue_[0].request.id, queue_[0].deadline, Status::Expired};
      pop(); updateHistory(reply); latch(Status::Expired);
      return {Motion::None, reply};
    }
    if (state_ == State::ClosedClearance) {
      if (!config_.clearance_verified) {
        if (count_ && !clearance_reported_) {
          clearance_reported_ = true;
          return {Motion::None, {queue_[0].request.id, queue_[0].deadline, Status::UnverifiedClearance}};
        }
        return {};
      }
      if (now - state_at_ < config_.extra_clearance_ms) return {};
      state_ = State::Closed;
    }
    if (state_ != State::Closed || !count_ || fault_ != Status::Ok) return {};
    if (!fits(queue_[0], now)) {
      Reply reply{queue_[0].request.id, queue_[0].deadline, Status::SpacingConflict};
      pop(); updateHistory(reply); latch(Status::SpacingConflict);
      return {Motion::None, reply};
    }
    active_ = queue_[0]; pop(); active_valid_ = true;
    state_ = State::Opening; state_at_ = now;
    return {Motion::Open, {active_.request.id, active_.deadline, Status::Ok}};
  }

 private:
  Config config_;
  uint64_t run_ = 0;
  uint32_t high_water_ = 0;
  Job active_{};
  bool active_valid_ = false;
  Job queue_[Capacity]{};
  size_t count_ = 0;
  Seen seen_[History]{};
  size_t seen_count_ = 0;
  size_t seen_next_ = 0;
  State state_ = State::Closed;
  uint32_t state_at_ = 0;
  Status fault_ = Status::Ok;
  bool clearance_reported_ = false;
  constexpr void latch(Status status) { if (fault_ == Status::Ok) fault_ = status; }
  constexpr bool validTiming() const {
    return config_.arrival_bounds_verified && config_.initial_normal_route_clear &&
      config_.hold_ms == 6500 && config_.close_ms >= 450 && config_.close_ms <= 60000 &&
      config_.open_ms >= 180 && config_.open_ms <= 60000 &&
      config_.min_arrival_after_accept_ms > config_.open_ms &&
      config_.max_arrival_after_accept_ms >= config_.min_arrival_after_accept_ms &&
      config_.max_arrival_after_accept_ms < config_.hold_ms &&
      config_.guard_ms < config_.hold_ms && config_.extra_clearance_ms <= 60000 &&
      config_.max_source_age_ms <= 60000;
  }
  constexpr bool fits(Job job, uint32_t opening) const {
    const uint32_t ready = opening + config_.open_ms + config_.guard_ms;
    const uint32_t arrival = job.accepted_at + config_.min_arrival_after_accept_ms;
    return !before(arrival, ready) &&
      !before(job.deadline, job.accepted_at + config_.max_arrival_after_accept_ms + config_.guard_ms) &&
      before(ready, job.deadline);
  }
  constexpr void pop() {
    for (size_t i = 1; i < count_; ++i) queue_[i - 1] = queue_[i];
    if (count_) --count_;
  }
  constexpr Reply remember(Request request, Reply reply) {
    seen_[seen_next_] = {request, reply};
    seen_next_ = (seen_next_ + 1) % History;
    if (seen_count_ < History) ++seen_count_;
    return reply;
  }
  constexpr void updateHistory(Reply reply) {
    for (size_t i = 0; i < seen_count_; ++i)
      if (seen_[i].request.id == reply.id) seen_[i].reply = reply;
  }
};
} // namespace reject_experiment
