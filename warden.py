import os
import subprocess
import time
import socket
import logging
import threading

# --- Constants ---
SHARED_ENV_FILE = "/home/james/kinver-hub/gpu_state.env"
COOLDOWN_SECONDS = 600  # 10 Minutes of inactivity before dropping VRAM clocks

class HardwareWarden:
    """
    Zero-Touch hardware manager.
    Automatically detects backends, pins VRAM, and manages thermal cooldowns.
    """
    def __init__(self):
        self.last_activity_time = 0
        self.current_state = "Safe" 
        self.lock = threading.Lock()
        
        # Launch the passive cooldown observer in the background immediately
        threading.Thread(target=self._cooldown_observer, daemon=True).start()

    def _detect_backend(self, target_service: str) -> str:
        """Reads systemd files to identify Vulkan vs ROCm optimization targets."""
        try:
            result = subprocess.run(
                ["systemctl", "cat", target_service], 
                capture_output=True, text=True, check=True
            )
            if "vulkan" in result.stdout.lower():
                return "vulkan"
            return "rocm"
        except subprocess.CalledProcessError:
            logging.warning(f"Warden: Could not read {target_service}. Defaulting to ROCm.")
            return "rocm"

    def is_system_headless(self) -> bool:
        """Blocks dangerous VRAM pinning if Wayland graphical environments are active."""
        graphical_processes = ["gnome-shell", "kwin_wayland", "sway", "Xwayland", "Xorg"]
        for proc in graphical_processes:
            try:
                if subprocess.run(["pgrep", "-x", proc], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    return False 
            except Exception:
                pass
        return True

    def wait_for_worker_hub(self, port: int, timeout: int = 30):
        """Holds proxy network traffic until the AI model is fully loaded."""
        start_time = time.time()
        logging.info(f"Warden: Waiting for Worker Hub on port {port} to come online...")
        while time.time() - start_time < timeout:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    logging.info(f"Warden: Port {port} is active.")
                    return
            time.sleep(0.5)

    def arm_gpu_for_inference(self, target_service: str, target_port: int):
        """Called by the Proxy to prepare the physical hardware for optimal generation."""
        with self.lock:
            self.last_activity_time = time.time()
            
            # Abort if Wayland is active to prevent display crash
            if not self.is_system_headless():
                return
            
            # Skip if hardware is already prepared
            if self.current_state == "Pinned" and os.path.exists(SHARED_ENV_FILE):
                return

            backend = self._detect_backend(target_service)
            logging.info(f"Triage: Arming GPU for {target_service} via {backend.upper()}.")

            try:
                # 1. Lock VRAM to 2150 MHz & underclock core
                subprocess.run(["sudo", "lact", "cli", "profile", "set", "Headless_Pinned"], check=True)
                time.sleep(0.2) 
                self.current_state = "Pinned"

                # 2. Inject optimal backend variables directly into the filesystem
                os.makedirs(os.path.dirname(SHARED_ENV_FILE), exist_ok=True)
                if backend == "rocm":
                    env_config = "PAL_ALWAYS_RESIDENT=1\nHSA_ENABLE_SDMA=0\n"
                elif backend == "vulkan":
                    env_config = "VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/radeon_icd.x86_64.json\n"
                
                subprocess.run(f"echo '{env_config}' | sudo tee {SHARED_ENV_FILE} > /dev/null", shell=True, check=True)

                # 3. Restart the targeted model and hold traffic
                subprocess.run(["sudo", "systemctl", "restart", target_service], check=True)
                self.wait_for_worker_hub(target_port)

            except subprocess.CalledProcessError as e:
                logging.error(f"Warden Critical: Failed to arm GPU. Error: {e}")

    def _cooldown_observer(self):
        """Passively drops VRAM frequencies to safe defaults after 10 minutes of inactivity."""
        while True:
            time.sleep(30) 
            with self.lock:
                now = time.time()
                if self.current_state == "Pinned" and (now - self.last_activity_time > COOLDOWN_SECONDS):
                    logging.info("Warden: 10-minute inactivity. Cooling down VRAM.")
                    try:
                        subprocess.run(["sudo", "lact", "cli", "profile", "set", "Desktop_Compute"], check=True)
                        self.current_state = "Safe"
                    except subprocess.CalledProcessError:
                        pass