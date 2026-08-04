import time
import os
import json
import threading
import customtkinter as ctk
import serial.tools.list_ports

# --- GLOBAL CONFIGURATION FOR GUI & SYSTEM ---

def find_arduino_port():
    """
    Awtomatikong hahanapin ang Arduino Uno sa available USB ports.
    Arduino Uno VID: 0x2341 (Official) o 0x1A86 (CH340 clone)
    """
    import serial.tools.list_ports as list_ports
    import serial as pyserial

    ports = list_ports.comports()

    # Official Arduino Uno (VID: 0x2341)
    for port in ports:
        if port.vid == 0x2341:
            print(f"✅ Nahanap ang Arduino Uno sa: {port.device}")
            return port.device

    # Clone Arduino Uno — CH340 chip (VID: 0x1A86)
    for port in ports:
        if port.vid == 0x1A86:
            print(f"✅ Nahanap ang Arduino Uno Clone (CH340) sa: {port.device}")
            return port.device

    # Fallback — Windows COM ports
    print("⚠️ Auto-detect failed, trying fallback ports...")
    for fallback in ['COM3', 'COM4', 'COM5', 'COM6', 'COM7']:
        try:
            test = pyserial.Serial(fallback, 115200, timeout=0.1)
            test.close()
            print(f"⚠️ Using fallback port: {fallback}")
            return fallback
        except Exception:
            continue

    # Linux fallback
    for fallback in ['/dev/ttyACM0', '/dev/ttyACM1', '/dev/ttyUSB0']:
        if os.path.exists(fallback):
            print(f"⚠️ Using fallback port: {fallback}")
            return fallback

    print("❌ Walang nahanap na Arduino Uno!")
    return None

SERIAL_PORT = find_arduino_port()
BAUD_RATE = 115200
CONFIG_FILE = "config.json"

VENDO_ID = "vendo_004"
VENDO_NAME = "Lobby Dispenser 1"

current_water_level = 16000
active_student_uid = None
esp32 = None

# Lock para siguraduhing IISANG thread lang ang gumagalaw sa serial port
serial_lock = threading.Lock()
app_instance = None

# GLOBAL VARIABLES PARA SA ASYNCHRONOUS COIN & FLOW TRACKING
LIVE_ML_PER_PESO = 100

# === PHYSICAL PUMP CALIBRATION CONSTANT ===
# Default: 25.0 ms/mL. Mag-o-overwrite ito mula sa config.json o Firebase Sync.
MS_PER_ML = 25.0

coin_amount = 0
last_coin_time = time.time()
timeout_duration = 5.0
is_coin_accumulation_mode = False
is_flow_monitoring_mode = False

# === BAGONG STATE PARA SA PAUSE/RESUME TOGGLE BUTTON ===
is_pump_paused = False
pause_started_at = 0.0
paused_time_offset = 0.0  # kabuuang oras (segundo) na ginugol sa pag-pause sa kasalukuyang session
ml_to_dispense = 0

vendo_ref = None  # Gagamitin ng Firebase handler


def save_config_to_local(name_id, name_public, ms_per_ml_val=None):
    global VENDO_ID, VENDO_NAME, MS_PER_ML
    VENDO_ID = name_id
    VENDO_NAME = name_public
    if ms_per_ml_val is not None:
        MS_PER_ML = ms_per_ml_val

    config_data = {
        "vendo_id": name_id,
        "vendo_name": name_public,
        "ms_per_ml": MS_PER_ML
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(config_data, f, indent=4)
    print("💾 Configuration & Calibration persistent data saved locally.")


def load_local_config():
    global VENDO_ID, VENDO_NAME, MS_PER_ML
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                if "vendo_id" in data:
                    VENDO_ID = data["vendo_id"]
                if "vendo_name" in data:
                    VENDO_NAME = data["vendo_name"]
                if "ms_per_ml" in data:
                    MS_PER_ML = float(data["ms_per_ml"])
                print(f"⚙️ Loaded local config: ID={VENDO_ID}, Name={VENDO_NAME}, Calibration={MS_PER_ML} ms/mL")
                return data
        except Exception as e:
            print(f"⚠️ Error reading configuration: {e}")
            return None
    return None

# ✅ ISA-EXECUTE AGAD PAGKA-IMPORT NITO SA PYTHON
load_local_config()