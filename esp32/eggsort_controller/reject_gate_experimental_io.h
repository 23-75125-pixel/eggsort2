#pragma once
// Included AFTER setServoAngle(), only in the default-OFF experimental build.
#include "reject_gate_protocol.h"
#include <esp_system.h>

namespace {
reject_experiment::Config experimentalRejectConfig() {
  reject_experiment::Config config;
  config.enabled = true; // This file is excluded entirely by the default build.
  config.hold_ms = CRACK_GATE_OPEN_TIME;
  // Deliberately leave clearance, arrival bounds and normal-route evidence
  // UNKNOWN. Flipping the build flag alone does not authorize motor opening.
  return config;
}
reject_experiment::Scheduler experimentalReject(experimentalRejectConfig());
uint32_t experimentalRejectBoot = 0;
uint32_t rejectBootToken() {
  if (!experimentalRejectBoot) {
    experimentalRejectBoot = esp_random();
    if (!experimentalRejectBoot) experimentalRejectBoot = 1;
  }
  return experimentalRejectBoot;
}
void rejectReply(reject_experiment::Reply reply) {
  Serial.printf("XA:%08lX:%016llX:%lu:%lu:%s\n",
    static_cast<unsigned long>(rejectBootToken()),
    static_cast<unsigned long long>(experimentalReject.run()),
    static_cast<unsigned long>(reply.id), static_cast<unsigned long>(reply.deadline),
    reject_experiment::statusName(reply.status));
}
}

bool handleExperimentalReject(const String& command) {
  using namespace reject_experiment;
  if (command == "XH") {
    Serial.printf("XH:%08lX\n", static_cast<unsigned long>(rejectBootToken()));
    return true;
  }
  if (command.startsWith("XB:")) {
    uint32_t boot = 0; uint64_t run = 0;
    if (!parseBegin(command.c_str(), boot, run) || boot != rejectBootToken())
      Serial.println("XE:INVALID_OR_STALE_BOOT");
    else rejectReply({0, 0, experimentalReject.begin(run)});
    return true;
  }
  if (!command.startsWith("XR:")) return false;
  Frame frame;
  if (!parseRequest(command.c_str(), frame) || frame.boot != rejectBootToken()) {
    Serial.println("XE:INVALID_OR_STALE_BOOT");
    return true;
  }
  if (!pcaReady) { Serial.println("XE:PCA9685_UNAVAILABLE"); return true; }
  rejectReply(experimentalReject.submit(frame.request, millis()));
  return true;
}

void serviceExperimentalReject() {
  using namespace reject_experiment;
  const Action action = experimentalReject.tick(millis());
  if (action.motion == Motion::Open) setServoAngle(CRACK_SERVO, CRACK_OPEN);
  else if (action.motion == Motion::Close) setServoAngle(CRACK_SERVO, CRACK_CLOSED);
  if (action.motion != Motion::None || action.reply.status != Status::Ok) rejectReply(action.reply);
}
