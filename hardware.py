import serial
import serial.tools.list_ports
import time
import threading
from firebase_admin import db
import shared_state
import firebase_handler

MS_PER_ML = 31.37

def calculate_target_ml(coin_amount, ml_amount):
    if ml_amount and ml_amount > 0:
        return float(ml_amount)
    else:
        return float(coin_amount * shared_state.LIVE_ML_PER_PESO)


def process_scanned_student(scanned_uid):
    scanned_uid = scanned_uid.strip()
    if not scanned_uid:
        return

    user_ref = db.reference(f'users/{scanned_uid}')
    user_data = user_ref.get()
    if not user_data:
        print(f"\n❌ User {scanned_uid} not found.")
        if shared_state.app_instance:
            shared_state.app_instance.update_status_label(f"❌ UID {scanned_uid} Not Found!", "#e74c3c")
        shared_state.active_student_uid = None
        return

    shared_state.active_student_uid = scanned_uid
    user_ref.update({'coin_trigger': False, 'is_scanning': False, 'last_credits': 0})
    welcome_msg = f"👋 Welcome, {user_data.get('name', 'Student')}!"
    print(f"\n{welcome_msg}")

    if shared_state.app_instance:
        shared_state.app_instance.update_status_label(welcome_msg, "#3498db")

    shared_state.coin_amount = 0
    shared_state.last_coin_time = time.time()
    shared_state.is_coin_accumulation_mode = True

    if shared_state.esp32:
        try:
            with shared_state.serial_lock:
                shared_state.esp32.reset_input_buffer()
                shared_state.esp32.write(b'READY_FOR_COINS\n')
        except Exception as e:
            print(f"\n⚠️ Serial Write Error (READY_FOR_COINS): {e}")

    print(f"🪙 Waiting for coins for user: {scanned_uid}...")

    # 1. COIN ACCUMULATION LOOP
    while shared_state.is_coin_accumulation_mode:
        try:
            if shared_state.vendo_ref.child('force_dispense').get() is True:
                shared_state.coin_amount = 0
                shared_state.is_coin_accumulation_mode = False
                break
        except Exception:
            pass

        if shared_state.coin_amount > 0 and (time.time() - shared_state.last_coin_time > shared_state.timeout_duration):
            print(f"\n⏳ Coin insertion timeout. Total accumulated: ₱{shared_state.coin_amount}")
            shared_state.is_coin_accumulation_mode = False
            break

        if shared_state.coin_amount == 0 and (time.time() - shared_state.last_coin_time > 40.0):
            print(f"\n⏳ Kiosk Session Timeout. Walang baryang hinulog.")
            if shared_state.esp32:
                try:
                    with shared_state.serial_lock:
                        shared_state.esp32.write(b'TIMEOUT_RESET\n')
                except Exception as e:
                    print(f"\n⚠️ Serial Write Error (TIMEOUT_RESET): {e}")
            shared_state.is_coin_accumulation_mode = False
            break

        time.sleep(0.2)

    if shared_state.coin_amount == 0:
        shared_state.active_student_uid = None
        if shared_state.app_instance:
            shared_state.app_instance.update_status_label("⏳ Ready to Scan QR Code", "#2ecc71")
        return

    shared_state.ml_to_dispense = shared_state.coin_amount * shared_state.LIVE_ML_PER_PESO
    user_ref.update({'last_credits': shared_state.coin_amount, 'is_scanning': True})

    if shared_state.app_instance:
        shared_state.app_instance.update_status_label(
            f"🪙 Final Total: ₱{shared_state.coin_amount} ({shared_state.ml_to_dispense}mL). Tap Dispense on Phone!",
            "#f1c40f"
        )

    print("\n📱 Waiting for Mobile App 'Dispense' click...")

    # 2. DISPENSE TRIGGER WAIT LOOP 
    while True:
        try:
            if shared_state.vendo_ref.child('force_dispense').get() is True:
                break

            current_status = user_ref.get()

            if current_status and current_status.get('coin_trigger') == True:

                shared_state.actual_dispensed_ml = 0

                if shared_state.esp32:
                    shared_state.is_flow_monitoring_mode = True
                    shared_state.is_pump_paused = False
                    shared_state.paused_time_offset = 0.0

                    target_ml = calculate_target_ml(
                        shared_state.coin_amount,
                        shared_state.ml_to_dispense
                    )
                    command_to_send = f"START_PUMP_ML:{target_ml:.1f}\n"

                    with shared_state.serial_lock:
                        shared_state.esp32.reset_input_buffer()
                        shared_state.esp32.write(command_to_send.encode())

                    print(f"\n📡 [SERIAL SENT]: {command_to_send.strip()} para sa {shared_state.ml_to_dispense}mL")

                # 3. DISPENSING PROGRESS MONITORING LOOP (FIXED PAUSE LOGIC)
                start_flow_time = time.time()
                safety_timeout_seconds = (target_ml / 100.0) + 30.0

                while shared_state.is_flow_monitoring_mode:
                    # ✅ FIX: Kung naka-pause, i-adjust natin ang start_flow_time para hindi mag-count ang pause duration sa safety timeout
                    if shared_state.is_pump_paused:
                        start_flow_time += 0.05  # Binabawi ang tinulog na oras ng sleep
                        time.sleep(0.05)
                        continue

                    total_elapsed_wall_time = time.time() - start_flow_time

                    if total_elapsed_wall_time > safety_timeout_seconds:
                        print("\n⚠️ [PAUSE/SAFETY TIMEOUT] Safety limit reached. Forcing stop.")
                        if shared_state.esp32:
                            try:
                                with shared_state.serial_lock:
                                    shared_state.esp32.write(b'PAUSE_TIMEOUT_STOP\n')
                            except Exception as e:
                                print(f"\n⚠️ Serial Write Error (PAUSE_TIMEOUT_STOP): {e}")
                        shared_state.is_flow_monitoring_mode = False
                        break

                    time.sleep(0.05)

                # VALIDATION NG NAIBUHOS NA TUBIG
                poured_ml = getattr(shared_state, 'actual_dispensed_ml', 0)
                poured_ml = min(poured_ml, shared_state.ml_to_dispense)

                print(f"\n📊 FINAL DISPENSED AMOUNT: {poured_ml} mL (Target was {shared_state.ml_to_dispense} mL)")

                if poured_ml <= 0:
                    print("⚠️ 0 mL dispensed. Skipping Firebase intake update and logs.")
                    user_ref.update({
                        'is_scanning': False,
                        'coin_trigger': False,
                        'last_credits': 0
                    })
                    break

                shared_state.current_water_level = max(0, shared_state.current_water_level - poured_ml)
                water_percentage = round((shared_state.current_water_level / 20000) * 100)

                try:
                    shared_state.vendo_ref.update({'water_level': water_percentage})
                    print(f"📉 GALLON UPDATE: {shared_state.current_water_level}mL remaining ({water_percentage}%)")
                except Exception as e:
                    print(f"⚠️ Water level push error: {e}")

                new_intake = (current_status.get('intake', 0) or 0) + poured_ml
                finish_time = time.strftime("%Y-%m-%d %H:%M:%S")

                user_ref.update({
                    'intake': new_intake,
                    'last_drink_time': finish_time,
                    'is_scanning': False,
                    'coin_trigger': False,
                    'last_credits': 0
                })

                user_psu_id = user_data.get('psu_id', 'N/A')
                is_full = poured_ml >= (shared_state.ml_to_dispense - 5)

                db.reference('dispense_logs').push({
                    'uid': scanned_uid,
                    'psu_id': user_psu_id,
                    'name': user_data.get('name', 'Unknown'),
                    'course': user_data.get('course', 'Unknown'),
                    'section': user_data.get('section', 'Unknown'),
                    'year': user_data.get('year', 'Unknown'),
                    'vendo_id': shared_state.VENDO_ID,
                    'amount_ml': poured_ml,
                    'timestamp': finish_time,
                    'status': "Success" if is_full else "Partial (Paused Timeout)"
                })
                break

        except Exception as e:
            print(f"\n⚠️ Error inside active listen loop: {e}")

        time.sleep(0.4)

    shared_state.active_student_uid = None
    print("🔒 Kiosk lock released. Ready for next transaction.")
    if shared_state.app_instance:
        shared_state.app_instance.update_status_label("⏳ Ready to Scan QR Code", "#2ecc71")


def connect_to_arduino(max_retries=3, retry_delay=2):
    for attempt in range(1, max_retries + 1):
        current_port = shared_state.find_arduino_port()

        if current_port is None:
            print(f"❌ [Attempt {attempt}/{max_retries}] Walang nahanap na Arduino Uno.")
            time.sleep(retry_delay)
            continue

        shared_state.SERIAL_PORT = current_port

        try:
            shared_state.esp32 = serial.Serial(
                shared_state.SERIAL_PORT,
                shared_state.BAUD_RATE,
                timeout=0.1,
            )
            print(f"✅ Arduino Uno Linked: {shared_state.SERIAL_PORT}")
            time.sleep(2)
            return True

        except Exception as e:
            print(f"⚠️ [Attempt {attempt}/{max_retries}] Error: {e}")
            shared_state.esp32 = None
            time.sleep(retry_delay)

    shared_state.esp32 = None
    return False


def start_h2o_core_system():
    print("\n--- H2O HUB: SMART SYSTEM RUNNING ---")

    connect_to_arduino()

    if not firebase_handler.initialize_firebase_system():
        return

    def hardware_listener_loop():
        while True:
            if shared_state.esp32:
                hardware_data = None

                try:
                    with shared_state.serial_lock:
                        if shared_state.esp32.in_waiting > 0:
                            hardware_data = shared_state.esp32.readline().decode('utf-8', errors='ignore').strip()
                except Exception as read_err:
                    print(f"\n⚠️ Serial Reading Error: {read_err}")

                if not hardware_data:
                    time.sleep(0.02)
                    continue

                # FILTER OUT PROGRESS MESSAGES FROM RAW LOG PRINT
                if not hardware_data.startswith("DISPENSING_PROGRESS_ML:"):
                    print(f"\n📡 [RAW HARDWARE DATA]: {hardware_data}")

                # 1. QR / CARD UID SCAN HANDLER
                if hardware_data.startswith("UID_"):
                    uid = hardware_data.replace("UID_", "").strip()
                    if shared_state.active_student_uid is not None:
                        print(f"⚠️ KIOSK BUSY: Tinanggihan si {uid}.")
                        if shared_state.app_instance:
                            shared_state.app_instance.update_status_label("⚠️ System Busy!", "#e74c3c")
                        continue
                    threading.Thread(
                        target=process_scanned_student,
                        args=(uid,),
                        daemon=True
                    ).start()

                # 2. COIN INSERTION HANDLER
                elif shared_state.is_coin_accumulation_mode and (
                    "_PESO" in hardware_data or "_PESOS" in hardware_data
                ):
                    shared_state.last_coin_time = time.time()
                    coin_detected = 0

                    if "1_PESO" in hardware_data:
                        coin_detected = 1
                    elif "5_PESOS" in hardware_data:
                        coin_detected = 5
                    elif "10_PESOS" in hardware_data:
                        coin_detected = 10
                    elif "20_PESOS" in hardware_data:
                        coin_detected = 20

                    if coin_detected > 0:
                        shared_state.coin_amount += coin_detected
                        print(f"🪙 Coin Added: ₱{coin_detected} | Total: ₱{shared_state.coin_amount}")

                        if shared_state.app_instance:
                            shared_state.app_instance.update_status_label(
                                f"🪙 Total Coins: ₱{shared_state.coin_amount} "
                                f"({shared_state.coin_amount * shared_state.LIVE_ML_PER_PESO}mL).",
                                "#f1c40f"
                            )

                # 3. BUTTON PAUSE/RESUME HANDLER
                elif hardware_data == "PUMP_PAUSED":
                    shared_state.is_pump_paused = True
                    shared_state.pause_started_at = time.time()
                    print("⏸️ Pump paused via physical button.")
                    if shared_state.app_instance:
                        shared_state.app_instance.update_status_label(
                            "⏸️ Paused - Pindutin ang button sa kiosk para ituloy",
                            "#F59E0B"
                        )

                elif hardware_data == "PUMP_RESUMED":
                    if shared_state.is_pump_paused:
                        shared_state.paused_time_offset += (
                            time.time() - shared_state.pause_started_at
                        )
                    shared_state.is_pump_paused = False
                    print("▶️ Pump resumed via physical button.")
                    if shared_state.app_instance:
                        shared_state.app_instance.update_status_label(
                            f"💧 {shared_state.actual_dispensed_ml} mL",
                            "#e67e22"
                        )

                # 4. DISPENSING PROGRESS & FINAL VOLUME RECEIVER
                elif shared_state.is_flow_monitoring_mode:

                    if hardware_data.startswith("DISPENSED_FINAL_ML:"):
                        try:
                            final_ml = float(hardware_data.split(":")[1].strip())
                            shared_state.actual_dispensed_ml = int(round(final_ml))
                            print(
                                f"\n💧 Final Dispensed (Flow Sensor): "
                                f"{final_ml} mL -> {shared_state.actual_dispensed_ml} mL"
                            )
                        except Exception as e:
                            print(f"\n⚠️ Error parsing final ml: {e}")

                    elif hardware_data.startswith("DISPENSING_PROGRESS_ML:"):
                        try:
                            progress_part = hardware_data.split(":")[1].strip()
                            elapsed_ml, target_ml = progress_part.split("/")

                            shared_state.actual_dispensed_ml = int(round(float(elapsed_ml)))

                            # PINASIMPLE: mL NUMBER NALANG
                            status_txt = f"💧 {shared_state.actual_dispensed_ml} mL"
                            
                            # Overwrite sa terminal sa iisang linya
                            print(f"\r{status_txt:<30}", end="", flush=True)

                            if shared_state.app_instance:
                                shared_state.app_instance.update_status_label(
                                    status_txt, "#e67e22"
                                )
                        except (IndexError, ValueError):
                            pass

                    elif (
                        "TARGET_REACHED" in hardware_data
                        or "PUMP_OFF" in hardware_data
                        or "PAUSE_TIMEOUT" in hardware_data
                    ):
                        print(
                            f"\n✅ Dispensing terminated ({hardware_data}). "
                            f"Final volume: {shared_state.actual_dispensed_ml} mL"
                        )
                        shared_state.is_flow_monitoring_mode = False
                        shared_state.is_pump_paused = False

            time.sleep(0.02)

    threading.Thread(target=hardware_listener_loop, daemon=True).start()