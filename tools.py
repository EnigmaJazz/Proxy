import asyncio
import httpx
import json
import logging
from flashrank import Ranker, RerankRequest
from trafilatura import extract

from constants import DEPTH_CONFIG
from hardware import get_service_info
from llm import call_model, load_role_prompt 

logging.info("Initializing FlashRank CPU Reranker...")
try: ranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2")
except Exception as e:
    logging.error(f"Failed to initialize FlashRank: {e}")
    ranker = None

SEARXNG_URL = "http://localhost:8081/search"

async def execute_tool(job, tool_call_dict, stream_feedback_callback):
    """Central registry router for proxy-native tools."""
    name = tool_call_dict.get("name")
    try: args = json.loads(tool_call_dict.get("arguments", "{}"))
    except: args = {}

    if name in ["web_search", "search", "search_web"]:
        query = args.get("query", "")
        depth = args.get("depth", "standard")
        
        await stream_feedback_callback(job, f"Executing Native Web Search: '{query}' ({depth} depth)")
        raw_markdown = await execute_web_search(query, depth)
        
        if "[Search Error" in raw_markdown or "[Search failed" in raw_markdown: return raw_markdown 
            
        await stream_feedback_callback(job, "Synthesizing research via RAM-resident Lifeboat & Auditor...")
        return await lifeboat_reflexion_loop(raw_markdown, query, depth)
        
    return f"[Error: Native tool '{name}' not recognized.]"

async def lifeboat_reflexion_loop(raw_markdown: str, query: str, depth: str) -> str:
    """CPU-bound Reflexion loop. Summarizes search data and self-audits."""
    _, lb_port = get_service_info("lifeboat")
    _, aud_port = get_service_info("auditor")
    cfg = DEPTH_CONFIG.get(depth.lower(), DEPTH_CONFIG["standard"])
    
    lb_prompt = (
        f"[System: {load_role_prompt('lifeboat_search')}. Limit response to maximum {cfg['summary_words']} words. "
        f"Prioritize raw facts.]\n\nQuery: {query}\n\nContext:\n{raw_markdown}"
    )
    
    for attempt in range(3):
        summary = await call_model(lb_port, lb_prompt, profile="analytical", max_tokens=1024)
        
        auditor_prompt = (
            f"[System: {load_role_prompt('auditor_search')} ]\n\n"
            f"Raw Context:\n{raw_markdown[:2000]}...\n\nSummary to Evaluate:\n{summary}"
        )
        evaluation = await call_model(aud_port, auditor_prompt, profile="deterministic", max_tokens=50)
        
        if evaluation.strip().startswith("OK"): return summary
        else: lb_prompt += f"\n\n[Auditor Feedback: {evaluation}. Rewrite the summary.]"
    return summary 

async def scrape_site(client, url, char_limit):
    """Trafilatura HTML to Markdown conversion."""
    try:
        resp = await client.get(url, timeout=4.0, follow_redirects=True)
        text = extract(resp.text, output_format="markdown", include_tables=True)
        return {"id": url, "text": text[:char_limit], "meta": {"url": url}} if text else None
    except: return None

async def execute_web_search(query: str, depth: str) -> str:
    """Core data pipeline for web discovery and semantic CPU reranking."""
    if not ranker: return "[Search System Error: Reranker failed to initialize.]"
    cfg = DEPTH_CONFIG.get(depth.lower(), DEPTH_CONFIG["standard"])
    
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(SEARXNG_URL, params={'q': query, 'format': 'json', 'count': cfg['count']}, timeout=10.0)
            if r.status_code != 200: return f"[Search Error: HTTP {r.status_code} from SearxNG]"
                
            results = r.json().get('results', [])[:cfg['count']]
            if not results: return "[Search returned no URLs to scrape.]"
            
            tasks = [scrape_site(client, res['url'], cfg['chars']) for res in results]
            pages = await asyncio.gather(*tasks)
            candidates = [p for p in pages if p]

            if not candidates: return "[Search failed: Could not extract readable text.]"

            rerank_request = RerankRequest(query=query, passages=candidates)
            ranked = ranker.rerank(rerank_request)
            
            formatted_blocks = []
            for i, r in enumerate(ranked[:cfg['gold']]):
                source_url = r['meta']['url'] if 'meta' in r and 'url' in r['meta'] else r['id']
                formatted_blocks.append(f"### Source {i+1}: {source_url}\n{r['text']}")
                
            return "\n\n---\n\n".join(formatted_blocks)
        except httpx.RequestError: return f"[Search Execution Failed: Could not connect to SearxNG instance at {SEARXNG_URL}.]"