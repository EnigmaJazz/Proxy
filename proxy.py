import asyncio
import httpx
import json
import os
import re
import time
import logging
import difflib
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn
from contextlib import asynccontextmanager

from constants import FRONTEND_KEYS, CACHE_DIR, RECOVERY_FILE, PERSISTENT_QUEUE_FILE, THERMAL_LIMITS, ENV_NGL_FILE, NATIVE_TOOLS
from hardware import get_service_info, scan_systemd_for_models, send_wayland_notification, set_predictive_cooling, calculate_dynamic_ngl, verify_vram_availability, send_telegram_alert, send_bash_notification
from llm import wait_for_port_readiness, clear_model_cache, manage_slot_cache, load_role_prompt, call_model, call_model_chat, openrouter_cloud_escalation, STOP_SEQS
from tools import execute_tool 
from warden import HardwareWarden

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
hub_warden = HardwareWarden()

# Global State & Queues
STATE = {"active_heavy_model": None, "current_project": None, "active_priority": 99}
job_queue = asyncio.PriorityQueue()
preempt_event = asyncio.Event()
thermal_halt_event = asyncio.Event()

# Manages OS state transitions and dynamic pauses
transition_pause_event = asyncio.Event() 
pause_timer_task = None 

# Shared State Control Functions
async def execute_queue_pause(duration_secs: int) -> tuple[bool, str]:
    """Handles the shared logic for pausing the queue and preempting background tasks."""
    global pause_timer_task
    
    if STATE["active_priority"] <= 2:
        return False, "Cannot pause: A high-priority task is actively running."

    if STATE["active_priority"] == 3:
        preempt_event.set()

    if pause_timer_task and not pause_timer_task.done():
        pause_timer_task.cancel()

    async def _pause_timer(seconds: int):
        try:
            transition_pause_event.set()
            logging.info(f"Proxy: Queue paused for {seconds} seconds.")
            await asyncio.sleep(seconds)
            transition_pause_event.clear()
            logging.info("Proxy: Queue resumed automatically.")
        except asyncio.CancelledError:
            pass

    pause_timer_task = asyncio.create_task(_pause_timer(duration_secs))
    await asyncio.sleep(1.5) # Give the queue a moment to halt
    return True, f"Queue paused for {duration_secs} seconds."

def execute_queue_resume() -> str:
    """Handles the shared logic for resuming the queue."""
    global pause_timer_task
    if pause_timer_task and not pause_timer_task.done():
        pause_timer_task.cancel()
        
    transition_pause_event.clear()
    logging.info("Proxy: Queue manually resumed.")
    return "Queue manually resumed."

class JobItem:
    """Represents a discrete inference task in the priority queue."""
    def __init__(self, priority, prompt, messages, domain, complexity, project, file_paths, force_domain=None, tools=None, local_only=False):
        self.priority = priority        
        self.prompt = prompt           
        self.messages = messages       
        self.domain = domain
        self.complexity = complexity
        self.project = project
        self.file_paths = file_paths
        self.fatal_errors = 0           
        self.force_domain = force_domain 
        self.output_queue = asyncio.Queue() 
        self.tools = tools             
        self.local_only = local_only
        
    def __lt__(self, other): return self.priority < other.priority
    
    def to_dict(self):
        """Serializes the job for Phase 4/Background Queue persistence."""
        return {"priority": self.priority, "prompt": self.prompt, "messages": self.messages, "domain": self.domain, "complexity": self.complexity, "project": self.project, "file_paths": self.file_paths, "local_only": self.local_only}

def discriminate_caller(request: Request) -> str:
    """Categorizes traffic to protect IDE strict payloads from persona injection."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        if token in FRONTEND_KEYS: return FRONTEND_KEYS[token]
    user_agent = request.headers.get("user-agent", "").lower()
    if "aider" in user_agent or "cline" in user_agent or "vscode" in user_agent: return "IDE"
    return "AGENTIC"

def extract_project_context(prompt):
    """Regex extracts file paths to segregate NVMe memory slots by project."""
    path_pattern = r'(?:/[a-zA-Z0-9_.-]+)+/[a-zA-Z0-9_.-]+\.[a-zA-Z0-9]+'
    file_paths = list(set(re.findall(path_pattern, prompt)))
    project_name = "default"
    if file_paths:
        parts = file_paths[0].split('/')
        if len(parts) >= 3: project_name = parts[-3] if len(parts) > 3 else parts[-2]
    return {"project": project_name, "file_paths": file_paths}

def detect_tool_loops(messages, domain="standard"):
    """Zero-dependency difflib loop detection blocks Agentic Death Spirals."""
    ROLE_LIMITS = {"scholar": 6, "architect": 4, "coder": 4, "standard": 3, "worker": 2}
    max_loops = ROLE_LIMITS.get(domain, ROLE_LIMITS["standard"])
    historical_calls = []
    
    for m in messages:
        if m.get("role") == "assistant" and "tool_calls" in m:
            for tc in m["tool_calls"]:
                if tc.get("type") == "function":
                    historical_calls.append({"name": tc["function"]["name"], "args": tc["function"]["arguments"]})
                    
    if not historical_calls: return False, ""
    if len(historical_calls) >= max_loops: return True, f"Role limit of {max_loops} tool calls reached"
        
    if len(historical_calls) > 1:
        latest_call = historical_calls[-1]
        for prev_call in historical_calls[:-1]:
            if latest_call["name"] == prev_call["name"]:
                if difflib.SequenceMatcher(None, latest_call["args"], prev_call["args"]).ratio() > 0.85: 
                    return True, "Repetitive/similar tool parameters detected"
    return False, ""

async def stream_system_feedback(job, message):
    """Injects transparent proxy updates to the frontend streaming UI."""
    if not job.force_domain: await job.output_queue.put(f"_⏳ [Proxy: {message}]_\n\n")

async def submit_job(raw_prompt, messages):
    """Routes initial text to RAM-resident Front Desk for JSON-GBNF triage."""
    _, fd_port = get_service_info("front_desk")
    triage_prompt = f"[System: {load_role_prompt('front_desk')}]\nPrompt: {raw_prompt}"
    
    triage_json = await call_model(fd_port, triage_prompt, profile="json_gbnf")
    try: 
        job_data = json.loads(triage_json)
        cleaned_text = job_data.get("cleaned_prompt", raw_prompt)
        is_valid = job_data.get("is_valid", True)
        local_only = job_data.get("local_only", False)
    except: 
        job_data = {"cleaned_prompt": raw_prompt, "priority": "normal", "complexity": "low", "domain": "general", "project": "default", "file_paths": []}
        cleaned_text = raw_prompt
        is_valid, local_only = True, False
        
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] = cleaned_text
    
    domain = job_data.get("domain", "general")
    if not is_valid: domain = "invalid"
    
    p_text = job_data.get("priority", "normal")
    priority_val = 1 if p_text == "high" else (3 if p_text == "background" else 2)
    
    job = JobItem(priority_val, cleaned_text, messages, domain, job_data.get("complexity", "low"), job_data.get("project", "default"), job_data.get("file_paths", []), local_only=local_only)
    
    if priority_val == 3:
        try:
            with open(PERSISTENT_QUEUE_FILE, "a") as f: f.write(json.dumps(job.to_dict()) + "\n")
        except: pass
    
    await job_queue.put(job)
    if priority_val < STATE["active_priority"]: preempt_event.set()
    return job

async def stream_and_ingest_with_checkpoint(job, target_port, planner_port, auditor_port):
    """The Heavy Executor Loop."""
    if not any(m.get("role") == "system" for m in job.messages):
        final_messages = [{"role": "system", "content": load_role_prompt(job.domain)}] + job.messages
    else:
        final_messages = job.messages
        
    payload = {
        "messages": final_messages, 
        "temperature": 0.2, 
        "stream": True,
        "stop": STOP_SEQS
    }
    if job.tools: payload["tools"] = job.tools 
        
    if job.domain in ["coder", "architect"]: 
        payload.update({"temperature": 0.1, "top_p": 0.9, "max_tokens": -1, "thinking_budget_tokens": 4096})
    elif job.domain in ["creative", "scholar"]:
        payload.update({"temperature": 0.4, "top_p": 0.95, "max_tokens": 8192, "thinking_budget_tokens": 2048})
    elif job.domain == "professional": 
        payload.update({"temperature": 0.3, "top_p": 0.95, "max_tokens": 4096, "thinking_budget_tokens": 1024})
    else:
        payload.update({"max_tokens": 2048, "thinking_budget_tokens": 0})
    
    generated_text, warnings, chunk_counter = "", [], 0
    p_payload, a_payload = {"prompt": "", "max_tokens": 0, "cache_prompt": True, "thinking_budget_tokens": 0}, {"prompt": "", "max_tokens": 0, "cache_prompt": True, "thinking_budget_tokens": 0}
    tool_call_buffer = {} 

    async with httpx.AsyncClient() as client:
        async with client.stream("POST", f"http://127.0.0.1:{target_port}/v1/chat/completions", json=payload, timeout=None) as response:
            async for chunk in response.aiter_text():
                if thermal_halt_event.is_set(): raise RuntimeError("Thermal Halt")
                if preempt_event.is_set(): raise InterruptedError("Preempted")
                
                try:
                    if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                        data_json = json.loads(chunk[6:])
                        delta = data_json["choices"][0]["delta"]
                        
                        if "tool_calls" in delta and delta["tool_calls"]:
                            for tc in delta["tool_calls"]:
                                idx = tc["index"]
                                if idx not in tool_call_buffer:
                                    tool_call_buffer[idx] = {
                                        "id": tc.get("id", f"call_{int(time.time())}_{idx}"), 
                                        "type": "function", 
                                        "function": {"name": tc.get("function", {}).get("name", ""), "arguments": ""}
                                    }
                                    tool_name = tool_call_buffer[idx]["function"]["name"]
                                    if tool_name:
                                        await job.output_queue.put(f"\n\n_⏳ [Proxy: LLM executing tool: {tool_name}]_\n\n")
                                
                                if "function" in tc and "arguments" in tc["function"]:
                                    tool_call_buffer[idx]["function"]["arguments"] += tc["function"]["arguments"]
                            continue 
                            
                        if "content" in delta and delta["content"]:
                            content = delta["content"]
                            generated_text += content
                            await job.output_queue.put(content) 
                except: pass

                chunk_counter += 1
                with open(RECOVERY_FILE, "w") as f: json.dump({"recovery_state": generated_text}, f)
                
                p_payload["prompt"], a_payload["prompt"] = chunk, chunk
                try: await asyncio.gather(client.post(f"http://127.0.0.1:{planner_port}/completion", json=p_payload), client.post(f"http://127.0.0.1:{auditor_port}/completion", json=a_payload))
                except: pass 

                if chunk_counter % 50 == 0:
                    try:
                        eval_prompt = f"\n[System: {load_role_prompt('auditor')}. Reply ONLY OK, WARNING: <reason>, or FATAL: <reason>.]\n"
                        eval_text = (await client.post(f"http://127.0.0.1:{auditor_port}/completion", json={"prompt": eval_prompt, "max_tokens": 30, "temperature": 0.0, "thinking_budget_tokens": 0})).json().get("content", "").strip()
                        if eval_text.startswith("FATAL"): raise ValueError(f"Auditor Fatal: {eval_text}") 
                        elif eval_text.startswith("WARNING"): warnings.append(eval_text) 
                    except ValueError: raise 
                    except: pass 
                    
    if tool_call_buffer:
        native_tool_names = ["web_search", "search", "search_web"]
        native_calls = [tc for tc in tool_call_buffer.values() if tc["function"]["name"] in native_tool_names]
        frontend_calls = [tc for tc in tool_call_buffer.values() if tc["function"]["name"] not in native_tool_names]
        job.messages.append({"role": "assistant", "content": generated_text, "tool_calls": list(tool_call_buffer.values())})
        
        if native_calls:
            for tc in native_calls:
                tool_result = await execute_tool(job, tc["function"], stream_system_feedback)
                job.messages.append({"role": "tool", "tool_call_id": tc["id"], "name": tc["function"]["name"], "content": tool_result})
            await stream_system_feedback(job, "Synthesizing final response...")
            return await stream_and_ingest_with_checkpoint(job, target_port, planner_port, auditor_port)
            
        if frontend_calls:
            await job.output_queue.put({"frontend_tool_calls": frontend_calls})
                    
    await job.output_queue.put("[DONE]") 
    return generated_text, warnings

async def manage_heavy_model(target_service):
    """Secures VRAM and safely triggers HardwareWarden clocks."""
    if STATE["active_heavy_model"] == target_service: 
        _, target_port = get_service_info(target_service.replace("llama-", ""))
        await asyncio.to_thread(hub_warden.arm_gpu_for_inference, target_service, target_port)
        return 
    
    worker_svc, _ = get_service_info("worker")
    await asyncio.create_subprocess_exec("sudo", "systemctl", "stop", worker_svc)
    if STATE["active_heavy_model"]: await asyncio.create_subprocess_exec("sudo", "systemctl", "stop", STATE["active_heavy_model"])
    
    await asyncio.sleep(2) 
    await verify_vram_availability() 
    await calculate_dynamic_ngl(target_service) 
    
    _, target_port = get_service_info(target_service.replace("llama-", ""))
    await asyncio.to_thread(hub_warden.arm_gpu_for_inference, target_service, target_port)
    await asyncio.create_subprocess_exec("sudo", "systemctl", "start", target_service)
    STATE["active_heavy_model"] = target_service
    await wait_for_port_readiness(target_port)

async def queue_worker():
    """Main lifecycle orchestrator executing Priority queue items."""
    while True:
        # Pause queue if thermal limits are breached OR if the OS is booting Wayland/Gaming
        if thermal_halt_event.is_set() or transition_pause_event.is_set():
            await asyncio.sleep(2)
            continue
            
        job = await job_queue.get()
        STATE["active_priority"] = job.priority
        preempt_event.clear() 
        
        if job.domain not in ["cloud", "invalid"]: 
            await stream_system_feedback(job, f"Triage complete. Initial domain: {job.domain.capitalize()} (Complexity: {job.complexity.capitalize()}).")
            
        if job.domain in ["coder", "architect", "professional", "creative", "scholar"]: set_predictive_cooling(100)
        elif job.complexity == "high": set_predictive_cooling(75)
        else: set_predictive_cooling(50)
        
        try:
            if STATE["current_project"] != job.project:
                if STATE["current_project"] is not None:
                    await stream_system_feedback(job, f"Switched project to '{job.project}'. Wiping memory...")
                    if os.path.exists(CACHE_DIR):
                        for file in os.listdir(CACHE_DIR):
                            if file.endswith("_cache.bin"):
                                try: os.remove(os.path.join(CACHE_DIR, file))
                                except: pass
                    _, p_port = get_service_info("planner")
                    _, a_port = get_service_info("auditor")
                    await asyncio.gather(clear_model_cache(p_port), clear_model_cache(a_port))
                STATE["current_project"] = job.project
            
            if job.domain == "invalid":
                await stream_system_feedback(job, "Front Desk rejected the prompt as unintelligible.")
                await job.output_queue.put("I couldn't understand your request. Could you please clarify or provide more details?")
                await job.output_queue.put("[DONE]")
                continue
            
            if job.domain == "cloud":
                await stream_system_feedback(job, "Routing directly to OpenRouter Cloud API...")
                cloud_resp = await openrouter_cloud_escalation(1, job.prompt)
                await job.output_queue.put(cloud_resp)
                await job.output_queue.put("[DONE]")
                continue
            
            if job.complexity == "low" and job.domain == "general" and not job.force_domain:
                if STATE["active_heavy_model"] is not None:
                    await stream_system_feedback(job, "GPU is busy. Routing basic query to RAM-resident Lifeboat...")
                    active_service = "lifeboat"
                    _, fallback_port = get_service_info(active_service)
                    res = await call_model_chat(fallback_port, [{"role": "system", "content": load_role_prompt(active_service)}] + job.messages, profile="analytical")
                    await job.output_queue.put(res)
                    await job.output_queue.put("[DONE]")
                    continue
                else:
                    await stream_system_feedback(job, "Executing via Fast Lane (Worker)...")
                    active_service = "worker"
                    worker_svc, worker_port = get_service_info(active_service)
                    await asyncio.to_thread(hub_warden.arm_gpu_for_inference, worker_svc, worker_port)
                    if not await wait_for_port_readiness(worker_port): await stream_system_feedback(job, "Warning: Worker model delayed.")
                    res = await call_model_chat(worker_port, [{"role": "system", "content": load_role_prompt(active_service)}] + job.messages, profile="analytical")
                    await job.output_queue.put(res)
                    await job.output_queue.put("[DONE]")
                    continue
            
            if not job.force_domain:
                await stream_system_feedback(job, "Consulting Planner AI for architectural review...")
                _, p_port = get_service_info("planner")
                planner_prompt = f"[System: Check Front Desk domain '{job.domain}'. Output exactly: DOMAIN:<domain_name> PLAN:<plan>.]\nTask: {job.prompt}"
                planner_decision = await call_model(p_port, planner_prompt, profile="deterministic")
                
                domain_match = re.search(r'DOMAIN:\s*([a-zA-Z_]+)', planner_decision)
                if domain_match: 
                    new_domain = domain_match.group(1).lower().strip()
                    if new_domain != job.domain:
                        await stream_system_feedback(job, f"Planner override: Domain shifted from {job.domain.capitalize()} to {new_domain.capitalize()}.")
                    job.domain = new_domain
                    
                plan_match = re.search(r'PLAN:\s*(.*)', planner_decision, re.DOTALL)
                if plan_match: job.messages.append({"role": "system", "content": f"Execution Plan: {plan_match.group(1).strip()}"})

            if job.domain in ["creative", "coder", "professional", "scholar", "architect"]:
                target_service, target_port = get_service_info(job.domain)
                cache_filename = f"{job.project}_{job.domain}.bin"
                if STATE["active_heavy_model"] != target_service: await stream_system_feedback(job, f"Hot-swapping VRAM to boot {job.domain.capitalize()} model...")
                
                await manage_heavy_model(target_service)
                await manage_slot_cache(target_port, "restore", cache_filename)
                await stream_system_feedback(job, f"Generating response...")
                
                _, p_port = get_service_info("planner")
                _, a_port = get_service_info("auditor")
                await stream_and_ingest_with_checkpoint(job, target_port, p_port, a_port)
                
                await manage_slot_cache(target_port, "save", cache_filename)
            else: 
                _, worker_port = get_service_info("worker")
                res = await call_model_chat(worker_port, [{"role": "system", "content": load_role_prompt(job.domain)}] + job.messages, profile="analytical")
                await job.output_queue.put(res)
                await job.output_queue.put("[DONE]")
                
            if job.priority == 3:
                snippet = res[:100].replace('\n', ' ') + "..." if 'res' in locals() else "Check output for details."
                msg_body = f"Domain: {job.domain.capitalize()}\nProject: {job.project}\n\nSnippet: {snippet}"
                await asyncio.gather(
                    send_telegram_alert("✅ AI Proxy: Background Task Complete", msg_body),
                    send_bash_notification("✅ AI Proxy: Background Task Complete", msg_body)
                )
                
        except ValueError:
            job.fatal_errors += 1
            if job.fatal_errors >= 3: 
                if job.local_only:
                    await stream_system_feedback(job, "Task failed 3 times. Local-only flag active. Cloud escalation forbidden.")
                    await job.output_queue.put("[FATAL LOCAL ERROR: Auditor rejected output 3 times. Escalation denied.]")
                else:
                    await stream_system_feedback(job, "Auditor rejected output 3 times. Escalating...")
                    cloud_resp = await openrouter_cloud_escalation(3, job.prompt)
                    await job.output_queue.put(cloud_resp)
                await job.output_queue.put("[DONE]")
            else: await job_queue.put(job) 
        except InterruptedError:
            with open(RECOVERY_FILE, "r") as f: job.prompt += "\n" + json.load(f)["recovery_state"] 
            await job_queue.put(job)
        except RuntimeError: await job_queue.put(job)
        finally:
            STATE["active_priority"] = 99
            job_queue.task_done()
            if job_queue.empty():
                set_predictive_cooling(30) 
                if STATE["active_heavy_model"]: await asyncio.create_subprocess_exec("sudo", "systemctl", "stop", STATE["active_heavy_model"])
                worker_svc, _ = get_service_info("worker")
                await asyncio.create_subprocess_exec("sudo", "systemctl", "start", worker_svc)
                STATE["active_heavy_model"] = None

async def zram_keepalive_worker():
    """Pings core models to prevent Linux swap-out."""
    core_domains = ["front_desk", "planner", "auditor", "worker"]
    while True:
        await asyncio.sleep(240) 
        async with httpx.AsyncClient() as client:
            for domain in core_domains:
                _, port = get_service_info(domain)
                try: await client.post(f"http://127.0.0.1:{port}/completion", json={"prompt": "[SYSTEM_KEEPALIVE]", "max_tokens": 0, "cache_prompt": False, "thinking_budget_tokens": 0}, timeout=2)
                except: pass

async def temperature_monitor_worker():
    """Discrete Core vs VRAM thermal watchdog."""
    critical_counters = {key: 0 for key in THERMAL_LIMITS.keys()}
    last_warning_time = 0
    while True:
        await asyncio.sleep(2) 
        try:
            proc = await asyncio.create_subprocess_exec("sensors", "-j", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1.0)
            data = json.loads(stdout.decode())
        except: continue 

        max_temps = {}
        for adapt, sensors in data.items():
            adapt_lower = adapt.lower()
            target_key = None
            if "k10temp" in adapt_lower: target_key = "k10temp"
            elif "spd5118" in adapt_lower: target_key = "spd5118"
            elif "nvme" in adapt_lower: target_key = "nvme"
            
            if "amdgpu" in adapt_lower:
                for group, reads in sensors.items():
                    for name, val in reads.items():
                        if "input" in name and isinstance(val, (int, float)):
                            if "mem" in group.lower() or "vram" in group.lower():
                                max_temps["amdgpu_vram"] = max(max_temps.get("amdgpu_vram", 0), val)
                            else:
                                max_temps["amdgpu_core"] = max(max_temps.get("amdgpu_core", 0), val)
                continue 

            if target_key:
                for group, reads in sensors.items():
                    for name, val in reads.items():
                        if "input" in name and isinstance(val, (int, float)):
                            max_temps[target_key] = max(max_temps.get(target_key, 0), val)

        sys_crit, warnings = False, []
        for key, temp in max_temps.items():
            lim = THERMAL_LIMITS.get(key)
            if not lim: continue
            
            if temp >= lim["max"]:
                sys_crit = True
                warnings.append(f"ABSOLUTE MAX: {lim['name']} at {temp}°C!")
                break
            elif temp >= lim["crit"]:
                critical_counters[key] += 1
                if critical_counters[key] >= 2:
                    sys_crit = True
                    warnings.append(f"SUSTAINED CRIT: {lim['name']} at {temp}°C!")
            else:
                critical_counters[key] = 0
                if temp >= lim["warn"]: warnings.append(f"Warn: {lim['name']} {temp}°C.")

        if warnings and not sys_crit and (time.time() - last_warning_time > 60):
            alert_text = "\n".join(warnings)
            await send_wayland_notification("Thermal Warning", alert_text)
            await asyncio.gather(
                send_telegram_alert("🔥 AI Proxy: Thermal Warning", alert_text),
                send_bash_notification("🔥 AI Proxy: Thermal Warning", alert_text)
            )
            last_warning_time = time.time()

        worker_svc, _ = get_service_info("worker")
        if sys_crit and not thermal_halt_event.is_set():
            await send_wayland_notification("THERMAL HALT", "Max temp reached. Pausing inference queue.")
            halt_msg = "Max temp reached. Inference paused to protect GPU."
            await asyncio.gather(
                send_telegram_alert("🚨 AI Proxy: THERMAL HALT", halt_msg),
                send_bash_notification("🚨 AI Proxy: THERMAL HALT", halt_msg)
            )
            thermal_halt_event.set() 
            if STATE["active_heavy_model"]: await asyncio.create_subprocess_exec("sudo", "systemctl", "stop", STATE["active_heavy_model"])
            await asyncio.create_subprocess_exec("sudo", "systemctl", "stop", worker_svc)
            STATE["active_heavy_model"] = None
        elif not sys_crit and thermal_halt_event.is_set():
            await send_wayland_notification("Recovery", "Temperatures normalized. Resuming.")
            thermal_halt_event.clear()
            await asyncio.create_subprocess_exec("sudo", "systemctl", "start", worker_svc)

@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.path.exists(PERSISTENT_QUEUE_FILE):
        with open(PERSISTENT_QUEUE_FILE, "r") as f:
            for line in f:
                try:
                    data = json.loads(line)
                    job = JobItem(data["priority"], data["prompt"], data["messages"], data["domain"], data["complexity"], data["project"], data["file_paths"], local_only=data.get("local_only", False))
                    job_queue.put_nowait(job)
                except: pass
        open(PERSISTENT_QUEUE_FILE, 'w').close()
        
    worker_svc, _ = get_service_info("worker")
    if not os.path.exists(ENV_NGL_FILE): await calculate_dynamic_ngl(worker_svc)
    
    task_queue = asyncio.create_task(queue_worker())
    task_zram = asyncio.create_task(zram_keepalive_worker())
    task_temp = asyncio.create_task(temperature_monitor_worker())
    yield 
    
    task_queue.cancel()
    task_zram.cancel()
    task_temp.cancel()

app = FastAPI(title="Local AI Multi-Agent Proxy", lifespan=lifespan)

@app.get("/v1/system/transition-check")
async def transition_check(duration: int = 10):
    """API for OS transitions and gaming mode. Accepts ?duration=X (seconds)."""
    success, message = await execute_queue_pause(duration)
    if not success:
        return JSONResponse(status_code=423, content={"status": "busy", "message": message})
    return JSONResponse(status_code=200, content={"status": "ready", "pause_duration": duration})

@app.get("/v1/system/queue/resume")
async def resume_queue():
    """Manual override to unpause the queue if a gaming session ends early."""
    message = execute_queue_resume()
    return JSONResponse(status_code=200, content={"status": "resumed", "message": message})

@app.get("/v1/models")
async def list_models():
    models = scan_systemd_for_models()
    models.insert(0, {"id": "auto", "object": "model", "owned_by": "proxy-orchestrator", "port": 0})
    return JSONResponse(content={"object": "list", "data": models})

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    caller_type = discriminate_caller(request)
    body = await request.json()
    requested_model = body.get("model", "auto").lower()
    messages = body.get("messages", [])
    extracted_tools = body.get("tools", None)
    processed_messages = []
    
    if caller_type == "IDE":
        extracted_tools = None 
        processed_messages = messages 
        
    elif caller_type == "AGENTIC":
        raw_text_dump = " ".join([m.get("content", "") for m in messages if isinstance(m.get("content"), str)]).lower()
        is_dream_process = "soul.md" in raw_text_dump and "memory.md" in raw_text_dump and "user.md" in raw_text_dump
        effective_domain = requested_model if requested_model in ["coder", "architect", "planner", "professional", "creative", "scholar"] else "standard"

        for m in messages:
            if m.get("role") == "system":
                if is_dream_process or effective_domain == "standard":
                    processed_messages.append(m)
                else:
                    sanitized = re.sub(r'(?i)#\s*(SOUL|AGENTS).*?(?=\n#|$)', '', m.get("content", ""), flags=re.DOTALL)
                    if sanitized.strip(): processed_messages.append({"role": "system", "content": sanitized.strip()})
            else:
                processed_messages.append(m)
                
        is_looping, loop_reason = detect_tool_loops(processed_messages, domain=effective_domain)
        
        if is_looping:
            loop_override = f"\n\n[PROXY OVERRIDE: Tool access temporarily revoked ({loop_reason}). Synthesize a final response immediately.]"
            processed_messages.append({"role": "system", "content": loop_override})
            extracted_tools = None 
            
        elif extracted_tools:
            for native_tool in NATIVE_TOOLS:
                native_name = native_tool["function"]["name"]
                extracted_tools = [t for t in extracted_tools if t.get("function", {}).get("name") not in [native_name, "search"]]
                extracted_tools.append(native_tool)
            processed_messages.append({"role": "system", "content": "\n\n[PROXY SYSTEM OVERRIDE: Issue ALL required tool calls simultaneously in a single parallel JSON array.]"})

    raw_prompt_for_triage = "\n".join([m.get("content", "") for m in processed_messages if isinstance(m.get("content"), str)])
    
    # 1. Intercept /pause [minutes]
    pause_match = re.search(r'(?i)^\s*/pause(?:\s+(\d+))?\s*$', raw_prompt_for_triage)
    if pause_match:
        duration_mins = int(pause_match.group(1)) if pause_match.group(1) else 60
        
        async def concurrent_pause_stream():
            yield f"data: {json.dumps({'id': f'sys-{int(time.time())}', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'content': f'_⏸️ [Proxy: Attempting to pause queue for {duration_mins} minutes...]_\n\n'}}]})}\n\n"
            
            success, msg = await execute_queue_pause(duration_mins * 60)
            if not success:
                yield f"data: {json.dumps({'id': f'sys-{int(time.time())}', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'content': f'⚠️ **Failed:** {msg}'}}]})}\n\n"
            else:
                yield f"data: {json.dumps({'id': f'sys-{int(time.time())}', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'content': f'✅ **Success:** Queue paused.'}}]})}\n\n"
            yield "data: [DONE]\n\n"
            
        return StreamingResponse(concurrent_pause_stream(), media_type="text/event-stream")

    # 2. Intercept /resume
    if re.search(r'(?i)^\s*/resume\s*$', raw_prompt_for_triage):
        async def concurrent_resume_stream():
            execute_queue_resume()
            yield f"data: {json.dumps({'id': f'sys-{int(time.time())}', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'content': '_▶️ [Proxy: Queue resumed manually.]_\n\n'}}]})}\n\n"
            yield "data: [DONE]\n\n"
            
        return StreamingResponse(concurrent_resume_stream(), media_type="text/event-stream")

    # 3. Intercept /cloud
    if "/cloud" in raw_prompt_for_triage.lower():
        async def concurrent_cloud_stream():
            status = {"id": f"chatcmpl-{int(time.time())}", "object": "chat.completion.chunk", "created": int(time.time()), "model": "cloud", "choices": [{"index": 0, "delta": {"content": "_⏳ [Proxy: Routing concurrently to OpenRouter...]_\n\n"}, "finish_reason": None}]}
            yield f"data: {json.dumps(status)}\n\n"
            cloud_resp = await openrouter_cloud_escalation(1, raw_prompt_for_triage)
            chunk = {"id": f"chatcmpl-{int(time.time())}", "object": "chat.completion.chunk", "created": int(time.time()), "model": "cloud", "choices": [{"index": 0, "delta": {"content": cloud_resp}, "finish_reason": None}]}
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(concurrent_cloud_stream(), media_type="text/event-stream")
        
    elif caller_type == "AGENTIC" and "is_dream_process" in locals() and is_dream_process:
        job = JobItem(3, raw_prompt_for_triage, processed_messages, "scholar", "high", "default", [], tools=extracted_tools)
        await job_queue.put(job)
        
    elif requested_model in ["coder", "architect", "planner", "professional", "creative", "scholar"] or caller_type == "IDE":
        force_domain = requested_model if requested_model != "auto" else "coder"
        ctx = extract_project_context(raw_prompt_for_triage)
        job = JobItem(2, raw_prompt_for_triage, processed_messages, force_domain, "high", ctx["project"], ctx["file_paths"], force_domain, tools=extracted_tools)
        if "is_looping" in locals() and is_looping: job.output_queue.put_nowait(f"_⏳ [Proxy: Tool Access Limited - {loop_reason}]_\n\n")
        await job_queue.put(job)
        if 2 < STATE["active_priority"]: preempt_event.set()
        
    else:
        job = await submit_job(raw_prompt_for_triage, processed_messages)
        job.tools = extracted_tools 
        if "is_looping" in locals() and is_looping: job.output_queue.put_nowait(f"_⏳ [Proxy: Tool Access Limited - {loop_reason}]_\n\n")

    async def event_stream():
        while True:
            chunk = await job.output_queue.get()
            if chunk == "[DONE]":
                yield "data: [DONE]\n\n"
                break
                
            if isinstance(chunk, dict) and "frontend_tool_calls" in chunk:
                openai_chunk = {
                    "id": f"chatcmpl-{int(time.time())}", 
                    "object": "chat.completion.chunk", 
                    "created": int(time.time()), 
                    "model": requested_model, 
                    "choices": [{"index": 0, "delta": {"tool_calls": chunk["frontend_tool_calls"]}, "finish_reason": "tool_calls"}]
                }
                yield f"data: {json.dumps(openai_chunk)}\n\n"
                continue
                
            openai_chunk = {"id": f"chatcmpl-{int(time.time())}", "object": "chat.completion.chunk", "created": int(time.time()), "model": requested_model, "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}]}
            yield f"data: {json.dumps(openai_chunk)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

if __name__ == "__main__":
    uvicorn.run("proxy:app", host="0.0.0.0", port=13000, loop="asyncio")