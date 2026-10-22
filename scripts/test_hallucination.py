import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from orchestration.turn_pipeline import TurnContext
from orchestration.revisor_service import run_internal_revisor
from orchestration.thread_state import thread_state_manager
import services.slack as slack_client

async def mock_post_agent_response(channel_id, thread_ts, text, username, icon_url):
    print("\n" + "="*50)
    print("🚨 SLACK POST INTERCEPTED 🚨")
    print(f"Username: {username}")
    print(f"Channel: {channel_id} | Thread: {thread_ts}")
    print(f"Text:\n{text}")
    print("="*50 + "\n")
    return {"ok": True, "ts": "1234.5678"}

async def main():
    # 1. Mock the slack poster so we don't try to hit real Slack API
    slack_client.post_agent_response = mock_post_agent_response

    # 2. Setup thread state manager for a dummy thread
    channel_id = "C_TEST"
    thread_ts = "123.456"

    # Original user prompt: Asks for a simple python script using requests
    thread_state_manager.get_initial_prompt = lambda c, t: "Can you write a python script that fetches JSON from a given URL? Just use the standard 'requests' library, no other dependencies."

    # 3. Create a dummy TurnContext
    ctx = TurnContext(
        channel_id=channel_id,
        thread_ts=thread_ts,
        raw_text="Can you write a python script that fetches JSON from a given URL? Just use the standard 'requests' library, no other dependencies.",
    )

    # Simulate that we are on the 2nd turn
    # So there is 1 previous assistant message in history
    ctx.full_history = [
        {"role": "assistant", "content": "[agent-10001]: I can help with that. Let's create a script using the requests library as requested."}
    ]

    ctx.agent_key = "agent-10002"
    ctx.agent_cfg = {"username": "Agent_B"}
    ctx.skills_used = []

    # 4. Inject a HALLUCINATION in the final_text
    # The user asked explicitly to ONLY use the 'requests' library.
    # Agent B hallucinated that the user wants to use aiohttp instead.
    ctx.final_text = "I have written the script. I used the `aiohttp` library as you requested, because asynchronous requests are much faster!"

    print("Running InternalRevisor test...")
    print("User Prompt:", thread_state_manager.get_initial_prompt(channel_id, thread_ts))
    print("Agent B (Current Turn):", ctx.final_text)
    print("\nExecuting Revisor...")

    await run_internal_revisor(ctx)

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    # Check if API keys exist
    if not os.getenv("MISTRAL_API_KEY") and not os.getenv("OPENAI_API_KEY") and not os.getenv("GEMINI_API_KEY"):
        print("WARNING: No LLM API keys found in environment. The LLM call may fail.")

    asyncio.run(main())
