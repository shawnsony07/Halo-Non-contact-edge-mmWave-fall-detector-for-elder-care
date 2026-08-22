# Halo: Non-Contact Edge mmWave Fall Detector for Elder Care

Halo is a privacy-first, non-contact monitoring system designed for elder care facilities and private homes. By leveraging high-frequency mmWave radar technology, it provides highly accurate fall detection, presence tracking, and vital signs monitoring without the use of invasive cameras or wearable devices. 

This repository contains the hardware integration, edge-processing logic, and machine learning models required to run the Halo system locally on edge devices.

## 🌟 Key Features

*   **Privacy-First Design:** Complete absence of optical cameras; only abstract point cloud data and bounding boxes are processed.
*   **Edge Computing:** No constant cloud uplink required for detection—minimizes latency and removes privacy concerns related to raw data transmission.
*   **Vital Signs Monitoring:** Real-time heart rate and breathing rate extraction computed directly on the radar chip.
*   **Robust Fall Detection:** Utilizes a PyTorch-based neural network trained on point cloud dynamics to classify activities and detect falls.

---

## 🏗️ System Architecture

The Halo system architecture is distributed across three primary compute tiers: the Radar Sensor, the Real-Time Coprocessor, and the Linux Application MPU.

```mermaid
graph TD
    A[TI IWR6843ISK<br/>mmWave Radar] -->|Raw Binary Telemetry Stream| B(mmWaveICBoost Carrier)
    B -->|UART Polling| C[STM32 Real-Time Coprocessor<br/>Arduino UNO Q]
    
    subgraph Arduino UNO Q Processing
    C1[Triple TLV Parser]
    C2[Point Cloud Type 1]
    C3[Target List Type 1010]
    C4[Vitals Type 1040]
    C5[On-device Bounding Box Pruning<br/>Drops out-of-zone points]
    C6[Defensive Caps & Sanity Checks]
    
    C --> C1
    C1 --> C2 & C3 & C4
    C2 --> C5
    C3 --> C5
    C5 --> C6
    C4 --> C6
    end
    
    C6 -->|3 Compact Bridge Channels<br/>radar_targets / radar_vitals / radar_pointcloud| D[Linux Application MPU]
    
    style A fill:#0052cc,color:#fff
    style B fill:#172b4d,color:#fff
    style C fill:#00875a,color:#fff
    style D fill:#ff5630,color:#fff
```

### 1. Hardware Stack

*   **Texas Instruments IWR6843ISK:** A 60-GHz to 64-GHz mmWave sensor. Flashed with the Vital Signs + Tracking firmware from the TI Radar Toolbox.
*   **mmWaveICBoost Carrier:** Provides standard interfaces, debugging capabilities, and bridges the high-speed telemetry to the coprocessor.
*   **Arduino UNO Q (STM32 MCU):** Acts as the real-time telemetry ingester and parser.

---

## 📡 Data Pipeline & Telemetry

The TI radar streams a highly condensed raw binary telemetry output. The STM32 microcontroller is responsible for parsing this real-time stream into actionable structural data.

### The Parsing Engine

The telemetry is formatted using Type-Length-Value (TLV) packets. The MCU implements a custom parser based on `parseTLVs.py` specifications to decode three primary structs:

1.  **Point Cloud (Type 1):** Returns the 3D spatial points $(X, Y, Z)$ and Doppler velocity of moving subjects.
2.  **Target List (Type 1010):** Returns the tracked targets, handled by the IWR6843's on-chip grouping and tracking algorithms.
3.  **Vitals (Type 1040):** Returns pre-calculated, filtered heart rate and respiration rate values.

```mermaid
sequenceDiagram
    participant Radar as TI IWR6843
    participant MCU as Arduino UNO Q (STM32)
    participant MPU as Linux MPU
    
    loop Real-Time Telemetry Stream
        Radar->>MCU: Serial UART Stream
        Note over MCU: Polled UART ingestion (Not DMA)
        MCU->>MCU: Parse Magic Word & Packet Header
        MCU->>MCU: Validate packet-length sanity bounds
        
        par Point Cloud (TLV 1)
            MCU->>MCU: Extract (X, Y, Z, Velocity)
            MCU->>MCU: Bounding-box pruning
        and Target List (TLV 1010)
            MCU->>MCU: Extract Target Track Data
            MCU->>MCU: Bounding-box pruning
        and Vitals (TLV 1040)
            MCU->>MCU: Extract pre-computed Heart/Breath Rate
        end
        
        MCU->>MCU: Enforce max points/TLV & max TLVs/frame
        
        par bridge_channels
            MCU->>MPU: radar_pointcloud
            MCU->>MPU: radar_targets
            MCU->>MPU: radar_vitals
        end
    end
```

### Spatial Filtering and Defensive Bounds

Because edge MPUs have limited resources, the MCU implements physical world constraints *before* passing data upstream:
*   **Bounding-Box Pruning:** Points that fall outside the defined physical room bounds are instantly dropped to prevent ghosting or processing artifacts.
*   **Sanity Caps:** The parser strictly enforces maximum points per TLV, maximum TLVs per frame, and absolute packet-length sanity to prevent buffer overruns or segmentation faults in the Linux MPU.

---

## 🧠 Linux Application MPU Architecture

The downstream Linux application (implemented in Python) acts as the brain of the edge device. To ensure real-time latency, it uses a 4-Thread architecture.

```mermaid
flowchart LR
    subgraph Linux Application MPU - 4-Thread Architecture
    
    subgraph Thread 1: Spatial Engine
    T1[Consume radar_targets<br/>or radar_pointcloud]
    T1_task[Handle Tracker Data / Custom Clustering]
    T1 --> T1_task
    end
    
    subgraph Thread 2: Activity Classifier
    T2[Consume radar_pointcloud]
    T2_task[Rolling-window tensor<br/>Fed to PyTorch Model]
    T2 --> T2_task
    end
    
    subgraph Thread 3: Vitals Consumer
    T3[Consume radar_vitals]
    T3_task[Format & Threshold<br/>Heart/Breath Rates]
    T3 --> T3_task
    end
    
    subgraph Thread 4: Router & Dashboard
    T4[Event Aggregator]
    T4_task[Transport Layer<br/>MQTT/WebSockets/HTTP]
    T4 --> T4_task
    end
    
    end
    
    T1_task -->|Spatial Events| T4
    T2_task -->|Fall Detection Events| T4
    T3_task -->|Vitals Alerts| T4
```

### Thread Breakdown

*   **Thread 1: Spatial Engine:** Consumes the `radar_targets` channel. Uses the pre-tracked target lists from the TI chip, or performs custom DBSCAN/K-Means clustering on the `radar_pointcloud` if the on-chip tracker loses confidence.
*   **Thread 2: Activity Classifier:** Consumes the `radar_pointcloud` channel. Aggregates data into a rolling-window time-series tensor. This tensor is fed into the PyTorch Neural Network (`fall_model.pth`) to classify activities and trigger Fall Detection alerts.
*   **Thread 3: Vitals Consumer:** Consumes the `radar_vitals` channel. The phase-shift algorithms run on the TI radar, so this thread is purely responsible for smoothing, formatting, and thresholding critical heart and breath rate deviations.
*   **Thread 4: Router & Dashboard:** The event aggregator and transport layer. Publishes the serialized events (falls, vital spikes, presence) to a local dashboard or an external MQTT/HTTP server.

---

## 📂 Repository Structure

The project has been refactored into a highly modular directory structure:

```text
├── docs/
│   └── diagrams/             # High-resolution Mermaid diagrams
├── model_training/
│   ├── data/                 # Raw datasets and pointcloud captures
│   └── notebooks/            # Jupyter notebooks (e.g., FallDetection.ipynb)
└── src/
    ├── mcu_arduino/          # STM32 / Arduino UNO Q parser sketch
    └── mpu_linux/            # The 4-Thread Python backend application
```

## ⚙️ Model Training & AI

The Fall Detection model is a PyTorch-based sequence classifier. 
*   **Notebook:** Located at `model_training/notebooks/FallDetection_root.ipynb`.
*   **Weights:** The production weights are saved as `src/mpu_linux/fall_model.pth`.
*   **Data:** Training datasets are stored in `model_training/data/GatheredData`.

## 🚀 Setup and Deployment

### 1. Arduino MCU Setup
1. Open `src/mcu_arduino/sketch.ino` in the Arduino IDE.
2. Select your STM32 / Arduino UNO Q board configuration.
3. Flash the firmware to enable the TLV parser and bridge channels.

### 2. Linux MPU Setup
1. Navigate to the Python application directory:
   ```bash
   cd src/mpu_linux
   ```
2. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the main multi-threaded application:
   ```bash
   python main.py
   ```

*(Ensure the Arduino is connected via USB/Serial and the port is correctly mapped in `main.py`).*

---
*Developed for elder care environments demanding the highest degree of privacy, dignity, and reliability.*
