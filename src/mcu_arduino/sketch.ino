#include <Arduino_RouterBridge.h>
#include <cmath>
#include <string.h>
#include <vector>

// The complete Elderly Care Fall Detection profile
const char *radarConfig[] = {
    "sensorStop",
    "flushCfg",
    "pmicCfg 1 1",
    "dfeDataOutputMode 1",
    "channelCfg 15 7 0",
    "adcCfg 2 1",
    "adcbufCfg -1 0 1 1 1",
    "lowPower 0 0",
    "profileCfg 0 60.75 30.00 1.00 10.00 0 0 200.0 1   96 10785.00 2 1 36",
    "chirpCfg 0 0 0 0 0 0 0 5",
    "chirpCfg 1 1 0 0 0 0 0 2",
    "chirpCfg 2 2 0 0 0 0 0 5",
    "frameCfg 0 2 48 0 90.00 1 0",
    "dynamicRACfarCfg -1 4 4 2 2 8 12 4 12 5.00 8.00 0.40 1 1",
    "staticRACfarCfg -1 6 2 2 2 8 8 6 4 8.00 15.00 0.30 0 0",
    "dynamicRangeAngleCfg -1 0.75 0.0010 1 0",
    "dynamic2DAngleCfg -1 1.5 0.0300 1 0 1 0.30 0.85 8.00",
    "staticRangeAngleCfg -1 0 8 8",
    "fineMotionCfg -1 1",
    "bpmCfg -1 1 0 2",
    "antGeometry0 0 -1 -2 -3 -2 -3 -4 -5 -4 -5 -6 -7",
    "antGeometry1 -1 -1 -1 -1 0 0 0 0 -1 -1 -1 -1",
    "antPhaseRot 1 1 1 1 1 1 1 1 1 1 1 1",
    "fovCfg -1 70.0 20.0",
    "compRangeBiasAndRxChanPhase 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 "
    "1 0",
    "staticBoundaryBox -3 3 0.3 5.5 0 3",
    "boundaryBox -4 4 0.3 6 0 3",
    "sensorPosition 0.75 0 0",
    "gatingParam 3 2 2 2 10",
    "stateParam 3 3 80 215 50 6000",
    "allocationParam 20 100 0.1 20 0.5 20",
    "trackingCfg 1 2 800 1 46 96 90",
    "presenceBoundaryBox -3 3 0.3 5.5 0 3",
    "maxAcceleration 2.0 2.0 2.0",
    "vitalsign 15 300",
    "VSRangeIdxCfg 0 21",
    "sensorStart"};
const int configLines = sizeof(radarConfig) / sizeof(radarConfig[0]);

static const uint8_t MAGIC_WORD[8] = {0x02, 0x01, 0x04, 0x03,
                                      0x06, 0x05, 0x08, 0x07};
static const size_t HEADER_LEN = 40;
static const size_t TLV_HEADER_LEN = 8;
static const size_t TARGET_STRUCT_SIZE = 112; // 1 uint32 ID + 27 floats

static const uint32_t TLV_TYPE_POINT_CLOUD = 1;
static const uint32_t TLV_TYPE_TARGET_LIST = 1010;
static const uint32_t TLV_TYPE_COMPRESSED_POINTS =
    1020; // Added for compressed cloud
static const uint32_t TLV_TYPE_VITALS = 1040;

// On-device bounding box pruning -- matches the tuned boundaryBox zone
static const float PRUNE_X_MIN = -4.0f, PRUNE_X_MAX = 4.0f;
static const float PRUNE_Y_MIN = 0.3f, PRUNE_Y_MAX = 6.0f;
static const float PRUNE_Z_MIN = 0.0f, PRUNE_Z_MAX = 3.0f;
static const uint32_t MAX_POINTS_PER_TLV = 200;

std::vector<uint8_t> buffer;
std::vector<uint8_t> outBuffer;
std::vector<uint8_t> vitalsOutBuffer;
std::vector<uint8_t> pointCloudOutBuffer;

uint16_t readU16LE(const uint8_t *p) {
  uint16_t v;
  memcpy(&v, p, 2);
  return v;
}
void pushU16(std::vector<uint8_t> &v, uint16_t x) {
  const uint8_t *p = reinterpret_cast<const uint8_t *>(&x);
  v.insert(v.end(), p, p + 2);
}

uint32_t readU32LE(const uint8_t *p) {
  uint32_t v;
  memcpy(&v, p, 4);
  return v;
}
float readF32LE(const uint8_t *p) {
  float v;
  memcpy(&v, p, 4);
  return v;
}
void pushU32(std::vector<uint8_t> &v, uint32_t x) {
  const uint8_t *p = reinterpret_cast<const uint8_t *>(&x);
  v.insert(v.end(), p, p + 4);
}
void pushF32(std::vector<uint8_t> &v, float x) {
  const uint8_t *p = reinterpret_cast<const uint8_t *>(&x);
  v.insert(v.end(), p, p + 4);
}

std::vector<uint32_t> seenTlvTypes;

void logTlvTypeOnce(uint32_t tlvType, uint32_t tlvLength) {
  for (size_t i = 0; i < seenTlvTypes.size(); i++) {
    if (seenTlvTypes[i] == tlvType)
      return;
  }
  seenTlvTypes.push_back(tlvType);
  Serial.print("First seen TLV type: ");
  Serial.print(tlvType);
  Serial.print(" length: ");
  Serial.println(tlvLength);
}

void parseBuffer() {
  size_t pos = 0;
  while (true) {
    if (buffer.size() < 8)
      break;
    long syncIdx = -1;
    for (size_t i = pos; i + 8 <= buffer.size(); i++) {
      if (memcmp(&buffer[i], MAGIC_WORD, 8) == 0) {
        syncIdx = (long)i;
        break;
      }
    }
    if (syncIdx == -1) {
      if (buffer.size() > 8)
        buffer.erase(buffer.begin(), buffer.end() - 8);
      break;
    }
    if (syncIdx > 0)
      buffer.erase(buffer.begin(), buffer.begin() + syncIdx);

    if (buffer.size() < HEADER_LEN)
      break;

    uint32_t packetLength = readU32LE(&buffer[12]);
    uint32_t frameNumber = readU32LE(&buffer[20]);
    uint32_t numTlvs = readU32LE(&buffer[32]);

    if (packetLength < HEADER_LEN || packetLength > 16384 || numTlvs > 20) {
      buffer.erase(buffer.begin(), buffer.begin() + 8);
      continue;
    }
    if (buffer.size() < packetLength)
      break;

    size_t offset = HEADER_LEN;
    size_t frameEnd = packetLength;

    for (uint32_t i = 0; i < numTlvs; i++) {
      if (offset + TLV_HEADER_LEN > frameEnd)
        break;
      uint32_t tlvType = readU32LE(&buffer[offset]);
      uint32_t tlvLength = readU32LE(&buffer[offset + 4]);
      offset += TLV_HEADER_LEN;
      if (offset + tlvLength > frameEnd)
        break;

      // ==========================================
      // DEFENSIVE HARDENING: ABORT GARBAGE FRAMES
      // ==========================================
      if (tlvType != 1010 && tlvType != 1011 && tlvType != 1012 &&
          tlvType != 1020 && tlvType != 1040 && tlvType != 1) {
        break; // Abort this frame's remaining TLVs immediately
      }

      logTlvTypeOnce(tlvType, tlvLength);

      // ==========================================
      // PARSE 1020: COMPRESSED POINT CLOUD
      // ==========================================
      if (tlvType == TLV_TYPE_COMPRESSED_POINTS && tlvLength > 20) {
        const uint8_t *base = &buffer[offset];

        float elevUnit = readF32LE(base);
        float azimUnit = readF32LE(base + 4);
        float dopplerUnit = readF32LE(base + 8);
        float rangeUnit = readF32LE(base + 12);
        float snrUnit = readF32LE(base + 16);

        uint32_t numPoints = (tlvLength - 20) / 8;
        if (numPoints > MAX_POINTS_PER_TLV)
          numPoints = MAX_POINTS_PER_TLV;

        for (uint32_t p = 0; p < numPoints; p++) {
          const uint8_t *pbase = base + 20 + (p * 8);

          int8_t elev = (int8_t)pbase[0];
          int8_t azim = (int8_t)pbase[1];
          int16_t doppler = (int16_t)readU16LE(pbase + 2);
          uint16_t range = readU16LE(pbase + 4);

          float r = (float)range * rangeUnit;
          float el = (float)elev * elevUnit;
          float az = (float)azim * azimUnit;
          float pv = (float)doppler * dopplerUnit;

          float px = r * cosf(el) * sinf(az);
          float py = r * cosf(el) * cosf(az);
          float pz = r * sinf(el);

          bool inBox = (px >= PRUNE_X_MIN && px <= PRUNE_X_MAX) &&
                       (py >= PRUNE_Y_MIN && py <= PRUNE_Y_MAX) &&
                       (pz >= PRUNE_Z_MIN && pz <= PRUNE_Z_MAX);
          if (!inBox)
            continue;

          pushU32(pointCloudOutBuffer, frameNumber);
          pushF32(pointCloudOutBuffer, px);
          pushF32(pointCloudOutBuffer, py);
          pushF32(pointCloudOutBuffer, pz);
          pushF32(pointCloudOutBuffer, pv);
        }
      }

      // ==========================================
      // PARSE 1010: TARGET LIST
      // ==========================================
      if (tlvType == TLV_TYPE_TARGET_LIST && tlvLength > 0 &&
          tlvLength % TARGET_STRUCT_SIZE == 0) {
        uint32_t numTargets = tlvLength / TARGET_STRUCT_SIZE;
        for (uint32_t t = 0; t < numTargets; t++) {
          const uint8_t *base = &buffer[offset + t * TARGET_STRUCT_SIZE];
          uint32_t tid = readU32LE(base);
          float x = readF32LE(base + 4);
          float y = readF32LE(base + 8);
          float z = readF32LE(base + 12);

          bool valid = (tid < 250) && (x > -6.0f && x < 6.0f) &&
                       (y > -1.0f && y < 7.0f) && (z > -2.0f && z < 4.0f);
          if (!valid)
            continue;

          pushU32(outBuffer, frameNumber);
          pushU32(outBuffer, tid);
          pushF32(outBuffer, x);
          pushF32(outBuffer, y);
          pushF32(outBuffer, z);
        }
      }

      // ==========================================
      // PARSE 1040: VITALS
      // ==========================================
      if (tlvType == TLV_TYPE_VITALS && tlvLength >= 16) {
        const uint8_t *base = &buffer[offset];
        uint16_t vid = readU16LE(base);
        uint16_t rangeBin = readU16LE(base + 2);
        float breathDeviation = readF32LE(base + 4);
        float heartRate = readF32LE(base + 8);
        float breathRate = readF32LE(base + 12);

        bool heartValid =
            (heartRate == 0.0f) || (heartRate >= 30.0f && heartRate <= 220.0f);
        bool breathValid =
            (breathRate == 0.0f) || (breathRate >= 3.0f && breathRate <= 60.0f);
        bool devValid = fabsf(breathDeviation) <= 100.0f;
        if (!heartValid || !breathValid || !devValid)
          continue;

        pushU16(vitalsOutBuffer, vid);
        pushU16(vitalsOutBuffer, rangeBin);
        pushF32(vitalsOutBuffer, breathDeviation);
        pushF32(vitalsOutBuffer, heartRate);
        pushF32(vitalsOutBuffer, breathRate);
      }
      offset += tlvLength;
    }

    buffer.erase(buffer.begin(), buffer.begin() + packetLength);
    pos = 0;
  }
}

std::vector<uint8_t> txBuffer;
unsigned long lastFlush = 0;
const unsigned long FLUSH_INTERVAL_MS = 10;

void setup() {
  Serial.begin(115200);
  Bridge.begin();

  Serial1.begin(115200);
  delay(5000);

  for (int i = 0; i < configLines; i++) {
    Serial1.print(String(radarConfig[i]) + "\r\n");
    delay(100);
  }

  Serial1.end();
  delay(50);
  Serial1.begin(921600);
  txBuffer.reserve(2048);
  buffer.reserve(4096);
}

void loop() {
  int avail = Serial1.available();
  if (avail > 0) {
    size_t oldSize = txBuffer.size();
    txBuffer.resize(oldSize + avail);
    Serial1.readBytes(txBuffer.data() + oldSize, avail);
    buffer.insert(buffer.end(), txBuffer.begin(), txBuffer.end());
    txBuffer.clear();
  }

  parseBuffer();

  if (!outBuffer.empty()) {
    unsigned long t0 = millis();
    Bridge.notify("radar_targets", outBuffer);
    unsigned long dt = millis() - t0;
    if (dt > 20) {
      Serial.print("notify() took ms: ");
      Serial.println(dt);
    }
    outBuffer.clear();
  }

  if (!vitalsOutBuffer.empty()) {
    Bridge.notify("radar_vitals", vitalsOutBuffer);
    vitalsOutBuffer.clear();
  }

  if (!pointCloudOutBuffer.empty()) {
    Bridge.notify("radar_pointcloud", pointCloudOutBuffer);
    pointCloudOutBuffer.clear();
  }

  Bridge.update();
}