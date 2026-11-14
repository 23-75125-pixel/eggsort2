#pragma once
#include "reject_gate_scheduler.h"

namespace reject_experiment {
struct Frame { uint32_t boot = 0; Request request{}; };
constexpr bool prefix(const char* text, const char* expected) {
  for (size_t i = 0; expected[i]; ++i) if (text[i] != expected[i]) return false;
  return true;
}
constexpr bool hex(const char* text, size_t& pos, size_t digits, uint64_t& value) {
  value = 0;
  for (size_t i = 0; i < digits; ++i) {
    const char c = text[pos++];
    const int digit = c >= '0' && c <= '9' ? c - '0' : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
    if (digit < 0) return false;
    value = (value << 4) | static_cast<uint64_t>(digit);
  }
  return true;
}
constexpr bool number(const char* text, size_t& pos, uint32_t& value) {
  value = 0; size_t digits = 0;
  while (text[pos] >= '0' && text[pos] <= '9') {
    const uint32_t d = text[pos++] - '0';
    if (++digits > 10 || value > (UINT32_MAX - d) / 10) return false;
    value = value * 10 + d;
  }
  return digits != 0;
}
constexpr bool parseBegin(const char* text, uint32_t& boot, uint64_t& run) {
  if (!prefix(text, "XB:")) return false;
  size_t pos = 3; uint64_t b = 0;
  if (!hex(text, pos, 8, b) || text[pos++] != ':' || !hex(text, pos, 16, run) || text[pos]) return false;
  boot = static_cast<uint32_t>(b); return run != 0;
}
constexpr bool parseRequest(const char* text, Frame& frame) {
  if (!prefix(text, "XR:")) return false;
  size_t pos = 3; uint64_t boot = 0;
  if (!hex(text, pos, 8, boot) || text[pos++] != ':' ||
      !hex(text, pos, 16, frame.request.run) || text[pos++] != ':' ||
      !number(text, pos, frame.request.id) || text[pos++] != ':') return false;
  const char q = text[pos++];
  if (q != 'C' && q != 'R' && q != 'G') return false;
  frame.request.quality = q == 'C' ? Quality::Crack : q == 'R' ? Quality::Rotten : Quality::Good;
  if (text[pos++] != ':' || !number(text, pos, frame.request.source_age_ms) || text[pos]) return false;
  frame.boot = static_cast<uint32_t>(boot);
  return frame.boot && frame.request.run && frame.request.id;
}
constexpr const char* statusName(Status s) {
  switch (s) {
    case Status::Ok: return "OK";
    case Status::Disabled: return "DISABLED";
    case Status::Started: return "STARTED";
    case Status::RunMismatch: return "RUN_MISMATCH";
    case Status::Invalid: return "INVALID";
    case Status::Stale: return "STALE";
    case Status::Duplicate: return "DUPLICATE";
    case Status::Conflict: return "CONFLICT";
    case Status::Accepted: return "ACCEPTED";
    case Status::Queued: return "QUEUED";
    case Status::QueueFull: return "QUEUE_FULL";
    case Status::UnverifiedTiming: return "UNVERIFIED_TIMING";
    case Status::UnverifiedClearance: return "UNVERIFIED_CLEARANCE";
    case Status::UnsafeGood: return "UNSAFE_GOOD";
    case Status::SpacingConflict: return "SPACING_CONFLICT";
    case Status::Expired: return "EXPIRED";
    case Status::DeadlineMissed: return "DEADLINE_MISSED";
    case Status::SettledUnverified: return "SETTLED_UNVERIFIED";
  }
  return "INVALID";
}
} // namespace reject_experiment
