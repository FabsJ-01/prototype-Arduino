import firebase_admin
from firebase_admin import credentials, db
import time
import threading
import sys
import shared_state

# Define MAX_CAPACITY Constant para madaling baguhin sa hinaharap
MAX_WATER_CAPACITY = 20000  # 20 Liters = 20,000 mL

def initialize_firebase_system():
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate("key.json")
            firebase_admin.initialize_app(cred, {
                'databaseURL': 'https://h2o-project-e83d9-default-rtdb.firebaseio.com'
            })
        
        print(f"🔗 Firebase targeting active node: vendos/{shared_state.VENDO_ID} ({shared_state.VENDO_NAME})")
        shared_state.vendo_ref = db.reference(f'vendos/{shared_state.VENDO_ID}')
        
        # 1. FETCH INITIAL NAME FROM CLOUD (KUNG MAYROON NA)
        cloud_name = shared_state.vendo_ref.child('name').get()
        if cloud_name:
            shared_state.VENDO_NAME = cloud_name
            shared_state.save_config_to_local(shared_state.VENDO_ID, cloud_name)
            print(f"🏷️ Synced Vendo Name from Cloud: {shared_state.VENDO_NAME}")

        # 2. FETCH INITIAL WATER LEVEL
        firebase_water_percent = shared_state.vendo_ref.child('water_level').get()
        if firebase_water_percent is not None:
            shared_state.current_water_level = int((int(firebase_water_percent) / 100) * MAX_WATER_CAPACITY)
            print(f"📥 FIREBASE SYNC: Kasalukuyang laman sa cloud ay {firebase_water_percent}% ({shared_state.current_water_level}mL)")
        else:
            shared_state.current_water_level = MAX_WATER_CAPACITY
            shared_state.vendo_ref.child('water_level').set(100)
            print(f"📥 FIREBASE INITIALIZED: Itinakda sa 100% ({MAX_WATER_CAPACITY}mL)")

        # 3. WATER LEVEL REFILL LISTENER
        def water_level_listener(event):
            if event.data is not None:
                try:
                    val = int(event.data)
                    if val == 100 and shared_state.current_water_level < MAX_WATER_CAPACITY:
                        shared_state.current_water_level = MAX_WATER_CAPACITY
                        print(f"\n🔄 [LIVE EVENT] ADMIN REFILL DETECTED: Internal water level successfully reset to {MAX_WATER_CAPACITY}mL!")
                except Exception:
                    pass
                    
        shared_state.vendo_ref.child('water_level').listen(water_level_listener)

        # 🚀 MGA BACKGROUND THREADS / LISTENERS
        threading.Thread(target=start_heartbeat_loop, daemon=True).start()
        threading.Thread(target=listen_for_price_config, daemon=True).start()
        threading.Thread(target=listen_for_name_config, daemon=True).start()
        threading.Thread(target=listen_for_admin_commands, daemon=True).start()
        threading.Thread(target=listen_for_calibration, daemon=True).start() # <--- BAGONG CALIBRATION LISTENER
        
        return True
    except Exception as e:
        print(f"❌ Firebase Connection Error: {e}")
        return False

def update_vending_status(status, water_level):
    if shared_state.vendo_ref:
        try:
            # FIX: Hindi na isasama sa update ang 'name' para HINDI ma-overwrite
            # ang bagong pangalan na pinalitan sa Web Admin!
            shared_state.vendo_ref.update({
                'wifi_status': status,
                'water_level': water_level,
                'last_online': time.strftime("%Y-%m-%d %H:%M:%S")
            })
        except Exception as e:
            print(f"⚠️ Failed to update firebase status: {e}")

def start_heartbeat_loop():
    while True:
        try:
            water_percentage = round((shared_state.current_water_level / MAX_WATER_CAPACITY) * 100)
            update_vending_status("Connected", water_percentage)
        except Exception:
            pass
        time.sleep(5) 

def listen_for_name_config():
    """Nakikinig kapag pinalitan ng Admin ang Vendo Name sa Web Dashboard"""
    print("📡 Vendo Name Listener Active...")
    def name_listener(event):
        if event.data is not None and isinstance(event.data, str):
            new_name = event.data.strip()
            if new_name and new_name != shared_state.VENDO_NAME:
                print(f"\n✏️ WEB ADMIN NAME UPDATE: '{shared_state.VENDO_NAME}' -> '{new_name}'")
                shared_state.VENDO_NAME = new_name
                # Permanenteng i-save sa config.json
                shared_state.save_config_to_local(shared_state.VENDO_ID, new_name)

    shared_state.vendo_ref.child('name').listen(name_listener)

def listen_for_price_config():
    """Nakikinig sa bagong Ratio (mL/₱) at Timing Rate (ms/mL)"""
    def settings_listener(event):
        if event.data is not None and isinstance(event.data, dict):
            try:
                if 'ml_per_peso' in event.data:
                    shared_state.LIVE_ML_PER_PESO = int(event.data['ml_per_peso'])
                    print(f"\n⚙️ CLOUD CONFIG UPDATE: new ratio: ₱1 = {shared_state.LIVE_ML_PER_PESO}mL")
                
                if 'ms_per_ml' in event.data:
                    ms_rate = float(event.data['ms_per_ml'])
                    if ms_rate != shared_state.MS_PER_ML:
                        shared_state.MS_PER_ML = ms_rate
                        # Permanenteng i-save sa config.json
                        shared_state.save_config_to_local(shared_state.VENDO_ID, shared_state.VENDO_NAME, ms_rate)
                        print(f"⏱️ PUMP TIMING RATE SYNC & SAVED: {ms_rate} ms/mL")
            except Exception as e:
                print(f"⚠️ Error parsing price/timing config: {e}")

    shared_state.vendo_ref.child('settings').listen(settings_listener)

def listen_for_admin_commands():
    print("📡 Admin Command Listener Active (Watching for Force Dispense)...")
    
    def listener(event):
        if event.data is True:
            if shared_state.active_student_uid is None:
                print("\n🚨 [WEB APP COMMAND] Force Dispense triggered in Standby Mode!")
                
                if shared_state.esp32:
                    admin_pesos = 2.5
                    ms_rate = getattr(shared_state, 'MS_PER_ML', 25.0)
                    simulated_ml = admin_pesos * shared_state.LIVE_ML_PER_PESO
                    admin_test_ms = int(simulated_ml * ms_rate)
                    
                    command_to_send = f"START_PUMP_MS:{admin_test_ms}\n"

                    with shared_state.serial_lock:
                        shared_state.esp32.reset_input_buffer()
                        shared_state.esp32.write(command_to_send.encode())

                    print(f"📡 [SERIAL SENT - STANDBY BYPASS]: {command_to_send.strip()}")
                    
                    if hasattr(shared_state, 'app_instance') and shared_state.app_instance:
                        shared_state.app_instance.update_status_label("🚨 Admin Force Dispense Active!", "#e67e22")
                    
                    shared_state.current_water_level = max(0, shared_state.current_water_level - simulated_ml)
                    
                    shared_state.is_flow_monitoring_mode = True
                    time.sleep(admin_test_ms / 1000.0)
                    shared_state.is_flow_monitoring_mode = False
                else:
                    print("❌ Cannot dispense: ESP32 connection is offline!")
                
                try:
                    shared_state.vendo_ref.update({'force_dispense': False})
                    print("✅ Standby override done! Firebase flag reset to False.")
                    if hasattr(shared_state, 'app_instance') and shared_state.app_instance:
                        shared_state.app_instance.update_status_label("⏳ Ready to Scan QR Code", "#2ecc71")
                except Exception as fb_err:
                    print(f"⚠️ Error resetting force_dispense flag: {fb_err}")
            
            else:
                print("\n🚨 [WEB APP COMMAND] Force Dispense detected! Transaction active, passing control to hardware.py loop...")

    shared_state.vendo_ref.child('force_dispense').listen(listener)

def listen_for_calibration():
    """Nakikinig sa Test Dispense trigger para sa Hardware Calibration"""
    print("🛠️ Pump Calibration Listener Active...")
    
    def calibration_listener(event):
        duration_ms = event.data
        if duration_ms is not None and isinstance(duration_ms, (int, float)) and duration_ms > 0:
            print(f"\n🧪 [CALIBRATION] Test Dispense Command Received: {duration_ms} ms")
            
            if shared_state.esp32:
                command_to_send = f"START_PUMP_MS:{int(duration_ms)}\n"
                
                with shared_state.serial_lock:
                    shared_state.esp32.reset_input_buffer()
                    shared_state.esp32.write(command_to_send.encode())
                
                print(f"📡 [SERIAL SENT - CALIBRATION]: {command_to_send.strip()}")
                
                if hasattr(shared_state, 'app_instance') and shared_state.app_instance:
                    shared_state.app_instance.update_status_label("🧪 Calibrating Pump (5s Test)...", "#3498db")
                
                # Bawasan ang estimated water level (assuming ~200ml default dispense para sa 5s)
                ms_rate = getattr(shared_state, 'MS_PER_ML', 25.0)
                estimated_ml = duration_ms / ms_rate
                shared_state.current_water_level = max(0, shared_state.current_water_level - estimated_ml)
                
                shared_state.is_flow_monitoring_mode = True
                time.sleep(duration_ms / 1000.0)
                shared_state.is_flow_monitoring_mode = False
                
                if hasattr(shared_state, 'app_instance') and shared_state.app_instance:
                    shared_state.app_instance.update_status_label("⏳ Ready to Scan QR Code", "#2ecc71")
            else:
                print("❌ Cannot calibrate: ESP32 connection is offline!")
                
            # Reset the trigger flag back to 0 in Firebase
            try:
                shared_state.vendo_ref.child('test_dispense_ms').set(0)
                print("✅ Calibration test complete. Firebase flag reset to 0.")
            except Exception as fb_err:
                print(f"⚠️ Error resetting test_dispense_ms flag: {fb_err}")

    shared_state.vendo_ref.child('test_dispense_ms').listen(calibration_listener)