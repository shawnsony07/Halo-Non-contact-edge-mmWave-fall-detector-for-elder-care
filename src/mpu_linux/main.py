import time
import struct
import os
import csv
import json
import threading
import queue
import socket
import requests
import paho.mqtt.client as mqtt 
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from arduino.app_utils import Bridge

# ==========================================
# LINUX MPU APPLICATION LAYER
# ==========================================
# Four worker threads, one per data stream + one event router. Bridge
# callbacks below do nothing but enqueue raw bytes -- this is the same
# receive/parse decoupling that fixed the bursty-catch-up lag earlier in
# this project's STM32 pipeline, applied here for the same reason: never
# let processing time feed back into how fast we can receive.

targets_queue = queue.Queue()
vitals_queue = queue.Queue()
pointcloud_queue = queue.Queue()
event_queue = queue.Queue()  # fall/vitals alerts, consumed by Thread 4

RECORD_STRUCT = '<2I3f'          # frameNum, tid, x, y, z
RECORD_SIZE = struct.calcsize(RECORD_STRUCT)
VITALS_STRUCT = '<2H3f'          # id, rangeBin, breathDeviation, heartRate, breathRate
VITALS_SIZE = struct.calcsize(VITALS_STRUCT)
POINTCLOUD_STRUCT = '<I4f'       # frameNum, x, y, z, v
POINTCLOUD_SIZE = struct.calcsize(POINTCLOUD_STRUCT)


def handle_radar_targets(data):
    if data:
        targets_queue.put(bytes(data) if isinstance(data, list) else data)
    return True


def handle_radar_vitals(data):
    if data:
        vitals_queue.put(bytes(data) if isinstance(data, list) else data)
    return True


def handle_radar_pointcloud(data):
    if data:
        pointcloud_queue.put(bytes(data) if isinstance(data, list) else data)
    return True


def push_event(event_type, payload):
    event_queue.put({"type": event_type, "time": time.time(), **payload})


# ==========================================
# THREAD 1 -- SPATIAL ENGINE
# ==========================================
# TI's on-chip tracker already clustered points and assigned target IDs
# (that's what arrives on radar_targets) -- this thread does NOT re-cluster
# raw points. It maintains the live multi-target state and runs fall
# detection per target. If you later want custom clustering instead of
# trusting the on-chip tracker, that's a different design built off
# radar_pointcloud -- not what's implemented here.

REQUIRED_CONSECUTIVE_FRAMES = 2
target_history = {}
active_targets = {}  # tid -> {"x","y","z","frame","last_seen"} -- live room state

# Fall detection thresholds -- see prior notes: mount-dependent, retune if
# sensor position changes. Values here match the table-mount setup tuned
# earlier this session.
FALL_DROP_THRESHOLD_M = 0.4
FALL_WINDOW_FRAMES = 20
FALL_FLOOR_Z_M = 0.3
FALL_SETTLE_FRAMES = 15

z_history = {}
fall_state = {}


def check_fall(tid, frame_number, z):
    hist = z_history.setdefault(tid, deque(maxlen=FALL_WINDOW_FRAMES))
    hist.append((frame_number, z))
    state = fall_state.setdefault(tid, {"drop_seen": False, "settle_count": 0, "alerted": False})

    if state["alerted"]:
        return

    if len(hist) >= 2:
        max_recent_z = max(h[1] for h in hist)
        if max_recent_z - z >= FALL_DROP_THRESHOLD_M:
            state["drop_seen"] = True

    if state["drop_seen"]:
        if z <= FALL_FLOOR_Z_M:
            state["settle_count"] += 1
        else:
            state["drop_seen"] = False
            state["settle_count"] = 0

        if state["settle_count"] >= FALL_SETTLE_FRAMES:
            state["alerted"] = True
            print(f"*** FALL DETECTED *** Target ID {tid} at Frame {frame_number} (Z settled at {z:.2f}m)")
            push_event("fall", {"tid": tid, "frame": frame_number, "z": z})


def thread_spatial_engine():
    print("[Thread1] Spatial Engine active -- waiting for target streams.")
    while True:
        data = targets_queue.get()
        try:
            for i in range(0, len(data) - RECORD_SIZE + 1, RECORD_SIZE):
                frame_number, tid, x, y, z = struct.unpack(RECORD_STRUCT, data[i:i + RECORD_SIZE])

                # Filter degenerate records: Y and Z both exactly zero carries
                # no real spatial signal (seen recurring in live logs -- may be
                # a coast/low-confidence tracker state, not confirmed against
                # TI docs, but not worth feeding into fall detection either way).
                if y == 0.0 and z == 0.0:
                    continue

                target_history[tid] = target_history.get(tid, 0) + 1
                if target_history[tid] >= REQUIRED_CONSECUTIVE_FRAMES:
                    active_targets[tid] = {"x": x, "y": y, "z": z, "frame": frame_number, "last_seen": time.time()}
                    print(f"[Frame {frame_number}] Target ID {tid} -> X: {x:.2f}m | Y: {y:.2f}m | Z: {z:.2f}m")
                    check_fall(tid, frame_number, z)
        except Exception as e:
            print(f"[Thread1] Parse error: {e}")


# ==========================================
# THREAD 2 -- ACTIVITY CLASSIFIER
# ==========================================
# Builds the rolling window shape your FallDetection.ipynb trains on
# (frames x points x [x,y,z,v]). Model loading is a REAL hook: if a
# trained model file exists, it loads and runs; if not, this says so
# plainly and skips inference. No fake classifier output is produced.
#
# MyCNN class mirrors FallDetection.ipynb exactly (Conv2d layers, dtype
# double). Loading uses state_dict + this class definition, NOT a raw
# torch.load() of a pickled model object -- state_dict is just weights,
# needs a model instance to load into. This was the source of the
# 'OrderedDict has no attribute eval' error from the last run.

MODEL_PATH = "python/fall_model.pth"
MODEL_WINDOW_FRAMES = 25   # matches notebook's window size
# MODEL_MAX_POINTS must match whatever H_in your training data actually
# padded to (equal_newdf's min-max-DetObj# calculation in the notebook).
# 22 is the notebook's own example value -- confirm against your training
# run's actual number, and recompute MyCNN's fc1 in_features to match
# (see conv/pool arithmetic worked out earlier this session) if it differs.
MODEL_MAX_POINTS = 22
_model = None
_model_load_attempted = False

POINTCLOUD_LOG_FILE = "python/pointcloud_log.csv"
_pointcloud_log_initialized = False
_frame_point_counters = {}
_frame_window = deque(maxlen=MODEL_WINDOW_FRAMES)  # each entry: list of (x,y,z,v) for that frame
_pointcloud_batches_received = 0  # diagnostic: confirms radar_pointcloud is actually delivering data
_current_frame_points = []
_current_frame_num = None


try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    class MyCNN(nn.Module):
        """Mirrors FallDetection.ipynb's MyCNN exactly. If you changed
        fc1's in_features there (per the H_in recompute from earlier),
        change it identically here -- these two must match bit for bit.
        dtype=torch.float32 -- matches the retrained notebook (was
        torch.double originally, changed deliberately, correct move for
        this model size and any future TinyML/quantization path)."""
        def __init__(self):
            super(MyCNN, self).__init__()
            self.conv1 = nn.Conv2d(25, 16, kernel_size=5, stride=2, padding=2, dtype=torch.float32)
            self.conv2 = nn.Conv2d(16, 32, kernel_size=3, stride=1, padding=1, dtype=torch.float32)
            self.pool = nn.MaxPool2d(kernel_size=2, stride=2)
            self.fc1 = nn.Linear(32 * 5 * 1, 64, dtype=torch.float32)  # update if your H_in != 22
            self.fc2 = nn.Linear(64, 32, dtype=torch.float32)
            self.fc3 = nn.Linear(32, 1, dtype=torch.float32)

        def forward(self, x):
            x = self.conv1(x)
            x = F.leaky_relu(x, negative_slope=0.01)
            x = self.pool(x)
            x = self.conv2(x)
            x = F.leaky_relu(x, negative_slope=0.01)
            x = x.view(x.size(0), -1)
            x = self.fc1(x)
            x = F.leaky_relu(x, negative_slope=0.01)
            x = self.fc2(x)
            x = F.leaky_relu(x, negative_slope=0.01)
            x = self.fc3(x)
            return x

    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False


def try_load_model():
    global _model, _model_load_attempted
    _model_load_attempted = True

    if not _TORCH_AVAILABLE:
        print("[Thread2] torch not installed on this device -- classifier inference disabled. Run: pip install torch")
        return

    if not os.path.isfile(MODEL_PATH):
        print(f"[Thread2] No trained model at {MODEL_PATH} -- classifier inference disabled until one exists.")
        return

    try:
        model = MyCNN()
        state_dict = torch.load(MODEL_PATH, map_location="cpu")
        model.load_state_dict(state_dict)
        model.eval()
        _model = model
        print(f"[Thread2] Loaded model from {MODEL_PATH}")
    except Exception as e:
        print(f"[Thread2] Model load failed, inference disabled: {e}")
        _model = None


def run_classifier_if_ready():
    """Only called once a full MODEL_WINDOW_FRAMES window is available.
    Pads/truncates points per frame to MODEL_MAX_POINTS, same shape the
    notebook's fillpoints()/fillframes() produce, then runs inference if
    a model is loaded."""
    if _model is None:
        return  # no model -- nothing to do, not faking a result

    try:
        window = list(_frame_window)
        tensor_frames = []
        for frame_points in window:
            pts = frame_points[:MODEL_MAX_POINTS]
            while len(pts) < MODEL_MAX_POINTS:
                pts.append((0.0, 0.0, 0.0, 0.0))
            tensor_frames.append(pts)

        # dtype=torch.float32 -- matches MyCNN's layers above (both changed
        # from torch.double together, must stay in sync).
        arr = torch.tensor(tensor_frames, dtype=torch.float32).unsqueeze(0)  # (1, frames, points, 4)
        with torch.no_grad():
            pred = _model(arr)
            prob = torch.sigmoid(pred).item()
            is_fall = prob > 0.85

        # Print every run, not just positives -- otherwise "never ran" and
        # "ran, said not-fall" look identical from the console.
        print(f"[Thread2] Window classified: {'FALL' if is_fall else 'not fall'} (p={prob:.3f})")

        if is_fall:
            push_event("fall_cnn", {"source": "pointcloud_classifier", "probability": prob})
    except Exception as e:
        print(f"[Thread2] Inference error: {e}")


def thread_activity_classifier():
    global _pointcloud_log_initialized, _current_frame_num, _current_frame_points, _pointcloud_batches_received

    if not _model_load_attempted:
        try_load_model()

    while True:
        data = pointcloud_queue.get()
        _pointcloud_batches_received += 1
        if _pointcloud_batches_received == 1:
            print("[Thread2] First pointcloud batch received -- radar_pointcloud is flowing.")
        elif _pointcloud_batches_received % 100 == 0:
            print(f"[Thread2] {_pointcloud_batches_received} pointcloud batches received so far.")
        try:
            if not _pointcloud_log_initialized:
                file_exists = os.path.isfile(POINTCLOUD_LOG_FILE)
                with open(POINTCLOUD_LOG_FILE, 'a', newline='') as f:
                    writer = csv.writer(f)
                    if not file_exists:
                        writer.writerow(['frame', 'DetObj#', 'x', 'y', 'z', 'v'])
                _pointcloud_log_initialized = True

            rows = []
            for i in range(0, len(data) - POINTCLOUD_SIZE + 1, POINTCLOUD_SIZE):
                frame_number, x, y, z, v = struct.unpack(POINTCLOUD_STRUCT, data[i:i + POINTCLOUD_SIZE])

                det_idx = _frame_point_counters.get(frame_number, 0)
                _frame_point_counters[frame_number] = det_idx + 1
                rows.append([frame_number, det_idx, x, y, z, v])

                # Rolling window bookkeeping for the classifier
                if frame_number != _current_frame_num:
                    if _current_frame_num is not None:
                        _frame_window.append(_current_frame_points)
                        if len(_frame_window) == MODEL_WINDOW_FRAMES:
                            run_classifier_if_ready()
                    _current_frame_num = frame_number
                    _current_frame_points = []

                    # Prune old frame counters -- prevents _frame_point_counters
                    # from growing unbounded over a long-running session.
                    old_frames = [f for f in _frame_point_counters.keys() if f < frame_number - 100]
                    for f in old_frames:
                        del _frame_point_counters[f]

                _current_frame_points.append((x, y, z, v))

            if rows:
                with open(POINTCLOUD_LOG_FILE, 'a', newline='') as f:
                    csv.writer(f).writerows(rows)
        except Exception as e:
            print(f"[Thread2] Parse error: {e}")


# ==========================================
# THREAD 3 -- VITALS CONSUMER
# ==========================================
# heartRate/breathRate arrive ALREADY COMPUTED by the radar's on-chip
# Vital Signs firmware. This thread does not perform phase-shift DSP --
# that would duplicate work already done upstream. It only consumes,
# thresholds, and alerts.
#
# Sanity bounds added below -- vitals TLV had zero validation before this,
# unlike targets which already had bit-flip protection. A breathDeviation
# of 2e27 (seen in live logs) is clear UART-noise corruption, same class
# of bug fixed for targets earlier this session.

HEART_RATE_MIN, HEART_RATE_MAX = 30.0, 220.0
BREATH_RATE_MIN, BREATH_RATE_MAX = 3.0, 60.0
BREATH_DEV_MAX = 100.0  # generous upper bound, real values are small

VITALS_ZERO_BREATH_ALERT_SEC = 15.0  # sustained zero breath rate before alerting
_last_nonzero_breath = {}  # id -> timestamp


def thread_vitals_consumer():
    print("[Thread3] Vitals Consumer active -- waiting for physiological locks.")
    while True:
        data = vitals_queue.get()
        try:
            for i in range(0, len(data) - VITALS_SIZE + 1, VITALS_SIZE):
                vid, range_bin, breath_dev, heart_rate, breath_rate = struct.unpack(
                    VITALS_STRUCT, data[i:i + VITALS_SIZE]
                )

                # Reject corrupted records (bit-flip on the UART line shows
                # up as wildly out-of-range magnitudes). Zero heart/breath
                # rate is a legitimate "no lock yet" state and stays in;
                # only the physically-impossible ranges get dropped.
                if not (HEART_RATE_MIN <= heart_rate <= HEART_RATE_MAX or heart_rate == 0.0):
                    continue
                if not (BREATH_RATE_MIN <= breath_rate <= BREATH_RATE_MAX or breath_rate == 0.0):
                    continue
                if abs(breath_dev) > BREATH_DEV_MAX:
                    continue

                print(f"[Vitals] ID {vid} -> Heart Rate: {heart_rate:.1f} bpm | Breath Rate: {breath_rate:.1f} bpm | Breath Dev: {breath_dev:.3f}")

                now = time.time()
                if breath_rate > 1.0:
                    _last_nonzero_breath[vid] = now
                else:
                    last = _last_nonzero_breath.get(vid, now)
                    if now - last >= VITALS_ZERO_BREATH_ALERT_SEC:
                        print(f"*** VITALS ALERT *** ID {vid} -- no breath rate detected for {now - last:.0f}s")
                        push_event("vitals_alert", {"id": vid, "duration_sec": now - last})
                        _last_nonzero_breath[vid] = now  # avoid spamming every frame
        except Exception as e:
            print(f"[Thread3] Parse error: {e}")


# ==========================================
# THREAD 4 -- EVENT ROUTER
# ==========================================
# Transport was never actually decided (an earlier draft assumed
# FastAPI+MQTT without either being built). Minimal honest default here:
# every event gets appended to a JSON-lines log file, and the last N
# events are servable over a barebones stdlib HTTP endpoint. Swap this
# for real MQTT/WebSocket/webhook delivery once you've picked a transport
# -- this is a working placeholder, not a final design.

EVENTS_LOG_FILE = "python/events.jsonl"
_recent_events_lock = threading.Lock()

# Configuration (Update these with your actual local IPs when ready)
WEBHOOK_URL = "http://192.168.1.X:8123/api/webhook/emergency_dispatch"
MQTT_BROKER_IP = "192.168.1.X" 
MQTT_PORT = 1883
MQTT_TOPIC_FALL = "eldercare/radar/falls"
MQTT_TOPIC_VITALS = "eldercare/radar/vitals"

def thread_event_router():
    print("[Thread4] Event Router active -- pushing MQTT and Webhooks.")
    
    # Setup MQTT Client
    mqtt_client = mqtt.Client(client_id="arduino_radar_edge")
    try:
        mqtt_client.connect(MQTT_BROKER_IP, MQTT_PORT, 60)
        mqtt_client.loop_start()
        print(f"[Thread4] MQTT connected to broker at {MQTT_BROKER_IP}")
    except Exception as e:
        print(f"[Thread4] MQTT connection skipped/failed: {e}")

    while True:
        event = event_queue.get()
        
        # 1. Permanent Local Logging (The Black Box)
        with _recent_events_lock:
            try:
                with open(EVENTS_LOG_FILE, 'a') as f:
                    f.write(json.dumps(event) + "\n")
            except Exception as e:
                print(f"[Thread4] Failed to write event log: {e}")

        # 2. Routing Logic
        event_type = event.get("type")
        
        if event_type == "fall_cnn":
            prob = event.get("probability", 0.0)
            
            # Publish all runs to MQTT for dashboard plotting
            try:
                mqtt_client.publish(MQTT_TOPIC_FALL, json.dumps(event))
            except Exception:
                pass 
                
            # Emergency Dispatch Rule: > 85% Confidence
            if prob > 0.85:
                print(f"[Thread4] CRITICAL FALL DETECTED (p={prob:.3f}). Dispatching webhook!")
                try:
                    requests.post(WEBHOOK_URL, json=event, timeout=2)
                except requests.exceptions.RequestException as e:
                    print(f"[Thread4] Webhook dispatch failed: {e}")

        elif event_type == "vitals_alert":
            # Emergency Vitals Dispatch
            print("[Thread4] VITALS ANOMALY DETECTED. Dispatching webhook!")
            try:
                mqtt_client.publish(MQTT_TOPIC_VITALS, json.dumps(event))
                requests.post(WEBHOOK_URL, json=event, timeout=2)
            except Exception as e:
                print(f"[Thread4] Vitals dispatch failed: {e}")


def main():
    print("Connecting to STM32 Bridge (3 channels: targets, vitals, pointcloud)...")
    Bridge.provide("radar_targets", handle_radar_targets)
    Bridge.provide("radar_vitals", handle_radar_vitals)
    Bridge.provide("radar_pointcloud", handle_radar_pointcloud)

    threading.Thread(target=thread_spatial_engine, daemon=True, name="Thread1-Spatial").start()
    threading.Thread(target=thread_activity_classifier, daemon=True, name="Thread2-Classifier").start()
    threading.Thread(target=thread_vitals_consumer, daemon=True, name="Thread3-Vitals").start()
    threading.Thread(target=thread_event_router, daemon=True, name="Thread4-Router").start()

    print("Production radar parser active -- 4 worker threads running.")

    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nStopping parser.")

if __name__ == "__main__":
    main()