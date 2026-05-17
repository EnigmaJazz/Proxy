import asyncio
import json
import os
import re
import logging
import httpx
from gguf import GGUFReader
from constants import MODELS_DIR, ENV_NGL_FILE, SYSTEMD_DIR, SERVICE_PATTERN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

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
    BASH_FILE_PATH = "/home/yourusername/scripts/my_notifications.sh"
    ASYNC_FUNCTION_NAME = "send_notification_async"
    try:
        command = f"bash -c \"source {BASH_FILE_PATH} && {ASYNC_FUNCTION_NAME} '{title}' '{message}'\""
        proc = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.communicate()
    except Exception as e:
        logging.error(f"Failed to trigger bash notification: {e}")

def set_predictive_cooling(synthetic_temp=30):
    """Triggers fan controllers ahead of heavy inference."""
    try:
        with open("/tmp/ai_proxy_sensor.txt", "w") as f: f.write(f"{synthetic_temp}\n")
    except: pass

async def get_free_vram_mb():
    """Polls ROCm asynchronously for VRAM metrics."""
    try:
        proc = await asyncio.create_subprocess_exec("rocm-smi", "--showmeminfo", "vram", "--json", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        data = json.loads(stdout.decode())
        for card, info in data.items():
            if "card" in card: return (int(info.get("VRAM Total Memory (B)", 12884901888)) - int(info.get("VRAM Total Used Memory (B)", 0))) // (1024 * 1024)
    except: pass
    return 0 

def get_gguf_layers(model_name):
    try:
        reader = GGUFReader(os.path.join(MODELS_DIR, f"{model_name}.gguf"))
        for f in reader.fields.values():
            if f.name == 'llama.block_count': return int(f.parts[0].tolist()[0])
    except: pass
    return 0 

def calculate_kv_cache_reserve(target_service, total_layers):
    """Calculates context window memory footprint."""
    context_size, ctk_type, ctv_type = 65536, "f16", "f16"
    try:
        with open(f"/etc/systemd/system/{target_service}.service", "r") as f:
            content = f.read()
            c_match, ctk_match, ctv_match = re.search(r'-c\s+(\d+)', content), re.search(r'-ctk\s+([a-zA-Z0-9_]+)', content), re.search(r'-ctv\s+([a-zA-Z0-9_]+)', content)
            if c_match: context_size = int(c_match.group(1))
            if ctk_match: ctk_type = ctk_match.group(1)
            if ctv_match: ctv_type = ctv_match.group(1)
    except: pass
    def get_bytes(q): return 0.5 if q in ["q4_0", "q4_1", "q4", "turbo4", "q4_K"] else 0.625 if q in ["q5_0", "q5_1", "q5", "q5_K"] else 1.0 if q in ["q8_0", "q8"] else 2.0
    return int((((get_bytes(ctk_type) + get_bytes(ctv_type)) / 2.0) / 0.5) * (total_layers / 60.0) * (context_size / 65536.0) * 3072)

async def calculate_dynamic_ngl(target_service):
    """Calculates GPU layers to offload, reserving 250MB for Wayland UI."""
    model_name = "default"
    try:
        with open(f"/etc/systemd/system/{target_service}.service", "r") as f:
            match = re.search(r'-m\s+/[^\s]+/([^/\s]+\.gguf)', f.read())
            if match: model_name = match.group(1).replace('.gguf', '')
    except: pass

    free_vram = await get_free_vram_mb()
    total_layers = get_gguf_layers(model_name)
    try: file_size_mb = os.path.getsize(os.path.join(MODELS_DIR, f"{model_name}.gguf")) / (1024 * 1024)
    except FileNotFoundError: file_size_mb = 20000 
    
    safety_buffer = 250 + (file_size_mb * 0.02)
    kv_reserve = calculate_kv_cache_reserve(target_service, total_layers) if total_layers > 0 else 3072
    usable_vram = free_vram - kv_reserve - safety_buffer
    
    adjusted_layer_size = ((file_size_mb / total_layers) if total_layers > 0 else 330) * 1.05 
    ngl = max(0, min(int(usable_vram / adjusted_layer_size) if usable_vram > 0 else 0, total_layers))
    
    with open(ENV_NGL_FILE, "w") as f: f.write(f"NGL_TARGET={ngl}\n")

async def verify_vram_availability(required_mb=8000):
    """Prevents OOM crashes by halting orchestrator until VRAM clears."""
    alerted = False
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