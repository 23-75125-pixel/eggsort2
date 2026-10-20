#include "stable_weight.h"

// These tests run during compilation; a regression fails the firmware build.
constexpr bool stableWeightHoldsAndAverages() {
  StableWeightWindow window;
  for (uint32_t t = 0; t < 1000; t += 100) {
    if (window.add(t % 200 ? 59.5f : 60.5f, t)) return false;
  }
  return window.add(60.5f, 1000) && window.average() > 60.0f && window.average() < 60.1f;
}

constexpr bool unstableWeightRestartsTimer() {
  StableWeightWindow window;
  for (uint32_t t = 0; t <= 900; t += 100) window.add(59, t);
  if (window.add(63, 1000) || window.elapsed(1000) != 0) return false;
  for (uint32_t t = 1100; t < 2000; t += 100) {
    if (window.add(63, t)) return false;
  }
  return window.add(63, 2000) && window.average() == 63;
}

constexpr bool missingSamplesAndRemovalResetStability() {
  StableWeightWindow window;
  for (uint32_t t = 0; t <= 1000; t += 100) window.add(59, t);
  if (window.add(59, 1700) || window.elapsed(1700) != 0) return false;
  window.reset();
  return window.average() == 0 && !window.add(63, 3000) && window.elapsed(3000) == 0;
}

constexpr bool slowDriftCannotPassAsStable() {
  StableWeightWindow window;
  for (uint32_t t = 0; t <= 3000; t += 100) {
    // Drift by more than the allowed spread inside the shortened 1s hold.
    if (window.add(50.0f + float(t) / 300.0f, t)) return false;
  }
  return true;
}

static_assert(stableWeightHoldsAndAverages(), "Stable weight must hold 1s and average all samples");
static_assert(unstableWeightRestartsTimer(), "A weight change must restart the full stability period");
static_assert(missingSamplesAndRemovalResetStability(), "Stale samples and egg removal must reset stability");
static_assert(slowDriftCannotPassAsStable(), "Cumulative drift must not release the gate");
