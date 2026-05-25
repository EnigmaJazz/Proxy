import asyncio
import httpx
import os
import time
from constants import PROMPTS_DIR, OPENROUTER_API_KEY

# Universal stop sequences
STOP_SEQS = ["<|eot_id|>", "<|im_end|>", "<|endoftext|>", "</s>", "Observation:", "```output"]

async def wait_for_port_readiness(port, timeout=15):
    """Pings a service to prevent sending traffic during cold-starts."""
    start_time = time.time()
    async with httpx.AsyncClient() as client:
        while time.time() - start_time < timeout:
            try:
                res = await client.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if res.status_code == 200: return True
            except: pass
            await asyncio.sleep(0.5) 
    return False

async def clear_model_cache(port):
    """Wipes VRAM memory slots dynamically."""
    async with httpx.AsyncClient() as client:
        try: await client.post(f"http://127.0.0.1:{port}/slots/0?action=erase", timeout=5)
        except: pass

async def manage_slot_cache(port, action, filename):
    """Saves/loads memory states directly to the NVMe disk."""
    async with httpx.AsyncClient() as client:
        try: await client.post(f"http://127.0.0.1:{port}/slots/0?action={action}", json={"filename": filename}, timeout=10.0)
        except Exception: pass

def load_role_prompt(role_name):
    """Dynamically loads the agent persona from disk."""
    try:
        with open(os.path.join(PROMPTS_DIR, f"{role_name}.txt"), "r") as f: return f.read()
    except FileNotFoundError: return f"You are the {role_name} AI."

async def call_model(port, prompt, profile="analytical", max_tokens=2048):
    """Legacy completions endpoint for utility AI. Strictly forces 0 reasoning budget."""
    payload = {
        "prompt": prompt, 
        "n_predict": max_tokens, 
        "cache_prompt": True,
        "stop": STOP_SEQS,
        "thinking_budget_tokens": 0
    }
    
    if profile == "deterministic": payload.update({"temperature": 0.0})
    elif profile == "json_gbnf": 
        payload.update({
            "temperature": 0.0, 
            "json_schema": {
                "type": "object", 
                "properties": {
                   "cleaned_prompt": {"type": "string"},
                    "priority": {"type": "string"},
                    "complexity": {"type": "string"},
                    "domain": {"type": "string"},
                    "project": {"type": "string"},
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"}
                    },
                    "is_valid": {"type": "boolean"},
                    "local_only": {"type": "boolean"}
                }
            }
        })
    else: payload.update({"temperature": 0.2, "top_p": 1.0})

    async with httpx.AsyncClient() as client:
        try: return (await client.post(f"http://127.0.0.1:{port}/completion", json=payload, timeout=300)).json().get("content", "")
        except: return ""

async def call_model_chat(port, messages, tools=None, profile="analytical", max_tokens=8192):
    """Structured Chat endpoint for Worker/Lifeboat."""
    payload = {
        "messages": messages, 
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stop": STOP_SEQS,
        "thinking_budget_tokens": 0 
    }
    if tools: payload["tools"] = tools 
    if profile == "deterministic": payload.update({"temperature": 0.0})

    async with httpx.AsyncClient() as client:
        try: return (await client.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=payload, timeout=300)).json()["choices"][0]["message"]["content"]
        except: return ""

async def openrouter_cloud_escalation(stage, prompt):
    """Fallback network request for Cloud Bypass and Auditor failures."""
    if not OPENROUTER_API_KEY: return "[Cloud Escalation Failed: OPENROUTER_API_KEY not found.]"
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "HTTP-Referer": "http://localhost:13000", "X-Title": "Local Proxy"}
    payload = {
        "model": "anthropic/claude-3.5-sonnet", 
        "messages": [{"role": "system", "content": "You are a cloud escalation AI."}, {"role": "user", "content": prompt}],
        "temperature": 0.2
    }
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=60.0)
            if response.status_code == 200: return response.json()["choices"][0]["message"]["content"]
            else: return f"[Cloud Escalation API Error: {response.status_code}]"
        except Exception as e: return f"[Cloud Escalation Network Error: {str(e)}]"