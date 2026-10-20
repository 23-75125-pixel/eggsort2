#pragma once
#include <stdint.h>

// Each call represents one fresh HX711 conversion, never a cached reading.
class StableWeightWindow {
 public:
  static constexpr uint32_t HOLD_MS = 1000;
  static constexpr uint32_t MAX_SAMPLE_GAP_MS = 500;
  static constexpr float MAX_SPREAD_GRAMS = 2.0f;

  constexpr void reset() { count_ = 0; }

  constexpr bool add(float grams, uint32_t now) {
    const float nextMin = count_ && minimum_ < grams ? minimum_ : grams;
    const float nextMax = count_ && maximum_ > grams ? maximum_ : grams;
    if (!count_ || uint32_t(now - lastSample_) > MAX_SAMPLE_GAP_MS ||
        nextMax - nextMin > MAX_SPREAD_GRAMS) {
      started_ = now;
      minimum_ = maximum_ = grams;
      total_ = grams;
      count_ = 1;
    } else {
      minimum_ = nextMin;
      maximum_ = nextMax;
      total_ += grams;
      ++count_;
    }
    lastSample_ = now;
    return count_ >= 5 && uint32_t(now - started_) >= HOLD_MS;
  }

  constexpr float average() const { return count_ ? float(total_ / count_) : 0.0f; }
  constexpr uint32_t elapsed(uint32_t now) const { return count_ ? uint32_t(now - started_) : 0; }

 private:
  uint32_t started_ = 0;
  uint32_t lastSample_ = 0;
  uint32_t count_ = 0;
  float minimum_ = 0;
  float maximum_ = 0;
  double total_ = 0;
};
