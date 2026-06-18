import asyncio
import json
import os
import re
import logging
import httpx
from constants import ENV_NGL_FILE, SYSTEMD_DIR, SERVICE_PATTERN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

PORT_CACHE = {}

def get_service_info(domain):
    """Dynamically resolves systemd service names and ports."""
    domain = domain.lower().strip()
    service_name = f"llama-{domain}"
    if domain in PORT_CACHE: return service_name, PORT_CACHE[domain]
    try:
        with open(f"/etc/systemd/system/{service_name}.service", "r") as f:
            match = re.search(r'--port\s+(\d+)', f.read())
            if match:
                port = int(match.group(1))
                PORT_CACHE[domain] = port
                return service_name, port
    except FileNotFoundError: pass 
    if domain != "worker": return get_service_info("worker")
    return "llama-worker", 13105 

def scan_systemd_for_models() -> list[dict]:
    """Populates /v1/models endpoint."""
    discovered_models = []
    try:
        for filename in os.listdir(SYSTEMD_DIR):
            match = SERVICE_PATTERN.match(filename)
            if match:
                model_name = match.group(1)
                with open(os.path.join(SYSTEMD_DIR, filename), 'r') as f:
                    port_match = re.search(r"--port\s+(131\d{2})", f.read())
                    if port_match:
                        discovered_models.append({"id": model_name, "object": "model", "owned_by": "systemd", "port": int(port_match.group(1))})
    except Exception as e: logging.error(f"Failed to scan models: {e}")
    return discovered_models

async def send_wayland_notification(title, message):
    """Sends native desktop alerts via libnotify."""
    try:
        env = os.environ.copy()
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
        proc = await asyncio.create_subprocess_exec("notify-send", title, message, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
    except Exception: pass

async def send_telegram_alert(title, message):
    """Native Python async Telegram notifier using .env credentials."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return 
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    full_message = f"<b>{title}</b>\n\n{message}"
    async with httpx.AsyncClient() as client:
        try: await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": full_message, "parse_mode": "HTML"}, timeout=5.0)
        except Exception as e: logging.error(f"Failed to send Telegram alert: {e}")

async def send_bash_notification(title, message):
    """Executes your custom async bash notification script."""
    BASH_FILE_PATH = "~/ar-notify.sh"
    ASYNC_FUNCTION_NAME = "notify_phone"
    try:
        command = f"bash -c \"source {BASH_FILE_PATH} && {ASYNC_FUNCTION_NAME} 'ubuntu=:=active=:=green=:={title}:{message}'\""
        proc = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
    except Exception as e:
        logging.error(f"Failed to trigger bash notification: {e}")

def set_predictive_cooling(synthetic_temp=30000):
    """Triggers fan controllers ahead of heavy inference."""
    try:
        with open("/tmp/ai_proxy_sensor.txt", "w") as f: f.write(f"{synthetic_temp}")
    except: pass

async def get_total_vram_mb():
    """Gets total VRAM capacity from ROCm."""
    try:
        proc = await asyncio.create_subprocess_exec("rocm-smi", "--showmeminfo", "vram", "--json", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            logging.error(f"rocm-smi failed with return code {proc.returncode}: {stderr.decode()}")
            return 12800
        data = json.loads(stdout.decode())
        for card, info in data.items():
            if "card" in card: return int(info.get("VRAM Total Memory (B)", 12884901888)) // (1024 * 1024)
    except FileNotFoundError:
        logging.error("rocm-smi not found. Please install ROCm tools to detect VRAM.")
    except asyncio.TimeoutError:
        logging.error("rocm-smi command timed out")
    except Exception as e:
        logging.error(f"Failed to get total VRAM: {e}")
    return 12800  # Default fallback for 12GB card

async def get_free_vram_mb():
    """Polls ROCm asynchronously for VRAM metrics."""
    try:
        proc = await asyncio.create_subprocess_exec("rocm-smi", "--showmeminfo", "vram", "--json", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            logging.error(f"rocm-smi failed with return code {proc.returncode}: {stderr.decode()}")
            return 0
        data = json.loads(stdout.decode())
        for card, info in data.items():
            if "card" in card: return (int(info.get("VRAM Total Memory (B)", 12884901888)) - int(info.get("VRAM Total Used Memory (B)", 0))) // (1024 * 1024)
    except FileNotFoundError:
        logging.error("rocm-smi not found. Please install ROCm tools to detect VRAM.")
    except asyncio.TimeoutError:
        logging.error("rocm-smi command timed out")
    except Exception as e:
        logging.error(f"Failed to get free VRAM: {e}")
    return 0 

async def calculate_dynamic_ngl(target_service, warden=None):
    """Sets fit-target buffer size based on session type - smaller for headless, larger for graphical."""
    # Import here to avoid circular dependency
    from warden import HardwareWarden
    
    if warden is None:
        warden = HardwareWarden()
    
    is_headless = warden.is_system_headless()
    
    # fit-target is the MB buffer to leave free when --fit calculates offloading
    # Headless: minimal buffer (500MB) since no additional GPU workload expected
    # Graphical: larger buffer (2000MB) to handle compositor/user actions
    if is_headless:
        fit_target = 256
    else:
        fit_target = 1024
    
    with open(ENV_NGL_FILE, "w") as f: f.write(f"FIT_TARGET={fit_target}\n")
    logging.info(f"FIT_TARGET set to {fit_target}MB (headless={is_headless})")

async def verify_vram_availability(required_mb=8000):
    """Prevents OOM crashes by halting orchestrator until VRAM clears."""
    alerted = False
    free_vram = await get_free_vram_mb()
    logging.info(f"Current free VRAM: {free_vram}MB, Required: {required_mb}MB")
    
    # Only wait if VRAM is actually occupied (less than total VRAM minus requirement)
    # If GPU is completely free, proceed immediately
    total_vram = await get_total_vram_mb()
    if free_vram >= total_vram - 100:  # Allow 100MB margin for system use
        logging.info("GPU appears to be free, skipping VRAM wait")
        return
    
    while await get_free_vram_mb() < required_mb:
        await send_wayland_notification("Pipeline Paused", "Waiting for VRAM.")
        
        if not alerted:
            alert_msg = f"Pipeline paused. Waiting for at least {required_mb}MB of free VRAM to continue."
            await asyncio.gather(
                send_telegram_alert("⚠️ AI Proxy: Waiting for VRAM", alert_msg),
                send_bash_notification("⚠️ AI Proxy: Waiting for VRAM", alert_msg)
            )
            alerted = True
            
        await asyncio.sleep(30)