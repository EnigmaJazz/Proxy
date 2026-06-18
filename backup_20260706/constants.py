import os
import re

# ==========================================
# 1. ROUTING & DISCRIMINATION CONSTANTS
# ==========================================
FRONTEND_KEYS = {
    "ide-key": "IDE",       
    "agent-key": "AGENTIC"  
}

SYSTEMD_DIR = "/etc/systemd/system/"
SERVICE_PATTERN = re.compile(r"^llama-([a-zA-Z0-9_]+)\.service$")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ==========================================
# 2. FILE PATHS
# ==========================================
MODELS_DIR = "/home/james/kinver-hub/models/"
PROMPTS_DIR = "/home/james/kinver-hub/prompts/"
ENV_NGL_FILE = "/home/james/kinver-hub/.env.ngl"       
CACHE_DIR = "/home/james/kinver-hub/cache/"            
RECOVERY_FILE = "/home/james/kinver-hub/proxy/recovery_state.json"                         
PERSISTENT_QUEUE_FILE = "/home/james/kinver-hub/proxy/background_queue.json"

# ==========================================
# 3. TOOL CONFIGURATIONS
# ==========================================
DEPTH_CONFIG = {
    "fast": {"count": 10, "gold": 3, "chars": 2000, "summary_words": 150}, 
    "standard": {"count": 25, "gold": 7, "chars": 3500, "summary_words": 300}, 
    "deep": {"count": 50, "gold": 15, "chars": 4000, "summary_words": 600}
}

NATIVE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Searches the live web and reads full articles. Use this instead of frontend search tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The explicit search query."},
                    "depth": {"type": "string", "enum": ["fast", "standard", "deep"]}
                },
                "required": ["query"]
            }
        }
    }
]

# ==========================================
# 4. HARDWARE LIMITS
# ==========================================
THERMAL_LIMITS = {
    "k10temp": {"warn": 85.0, "crit": 93.0, "max": 95.0, "name": "Ryzen 7700 CPU"},
    "amdgpu_core": {"warn": 90.0, "crit": 100.0, "max": 105.0, "name": "RX 6700XT Core"}, 
    "amdgpu_vram": {"warn": 95.0, "crit": 102.0, "max": 105.0, "name": "RX 6700XT VRAM"},
    "spd5118": {"warn": 70.0, "crit": 80.0, "max": 85.0, "name": "Crucial DDR5 RAM"},
    "nvme": {"warn": 65.0, "crit": 75.0, "max": 80.0, "name": "Samsung 990 PRO NVMe"}
}