import json
import pytest
from pathlib import Path
import config
from sim_logs import simulate_today
from strategy.arbiter import Arbiter

@pytest.mark.asyncio
async def test_simulate_today_market_open_time_filtering(tmp_path, monkeypatch):
    # Prepare a mock trades log file
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "trades_005930.jsonl"
    
    chart_dir = tmp_path / "charts"
    chart_dir.mkdir()
    chart_file = chart_dir / "3m.png"
    chart_file.touch()
    
    # We will write records:
    # 1. Before MARKET_OPEN_TIME (09:15)
    # 2. At MARKET_OPEN_TIME (10:00)
    # 3. After MARKET_OPEN_TIME (10:15)
    records = [
        {"event": "CYCLE", "gate_passed": True, "timestamp": "2026-06-19T09:15:00", "current_price": 70000, "chart_paths": {"3": str(chart_file)}},
        {"event": "CYCLE", "gate_passed": True, "timestamp": "2026-06-19T10:00:00", "current_price": 70000, "chart_paths": {"3": str(chart_file)}},
        {"event": "CYCLE", "gate_passed": True, "timestamp": "2026-06-19T10:15:00", "current_price": 70000, "chart_paths": {"3": str(chart_file)}},
    ]
    with open(log_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
            
    # Mock os.environ to point to our temp log dir
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    
    # Mock config.MARKET_OPEN_TIME to "10:00"
    monkeypatch.setattr(config, "MARKET_OPEN_TIME", "10:00")
    
    # Track which cycles are processed by the arbiter
    processed_timestamps = []
    
    async def mock_ask(self, new_prompt_text, chart_paths):
        # Extract timestamp from prompt_text or keep track via external way
        # Since we just want to verify that Arbiter.ask gets called only for the filtered list,
        # we can inspect the arguments or match them.
        # Actually, let's just record that it was called.
        return {
            "action": "HOLD",
            "confidence": 80,
            "reason": "mock hold",
            "analysis": {
                "trend_context": "N/A",
                "entry_trigger": "N/A",
                "orderbook_strength": "N/A"
            }
        }
    
    # Intercept Arbiter.ask
    monkeypatch.setattr(Arbiter, "ask", mock_ask)
    
    # We can also spy on cycles inside simulate_today if we mock arbiter build_prompt or similar,
    # but the easiest way is to mock Arbiter.build_prompt to save the timestamp of mock_snapshot.
    original_build_prompt = Arbiter.build_prompt
    def mock_build_prompt(self, mock_snapshot):
        # We can extract timestamp from mock_snapshot or record somewhere
        # The cycles loop does build_prompt, then checks charts, then calls ask.
        # Let's record the processed times.
        return "mock prompt"
        
    monkeypatch.setattr(Arbiter, "build_prompt", mock_build_prompt)
    
    # Let's also mock the print/logger output to avoid spam, but not strictly necessary.
    # Now run simulate_today for 2026-06-19
    # To check which records got passed to ask, we can record the ts when ask is called.
    # In simulate_today:
    # ts = record["timestamp"]
    # ...
    # decision = await arbiter.ask(new_prompt_text, chart_paths)
    
    # So we can capture 'ts' in mock_ask if we wrap it, but mock_ask doesn't receive ts.
    # But mock_ask can access the local variables or we can just spy on cycles list.
    # Wait, we can mock `Arbiter.ask` to check a global list of timestamps, but wait:
    # simulate_today does:
    # for record in cycles:
    #     ts = record["timestamp"]
    #     ...
    #     await arbiter.ask(new_prompt_text, chart_paths)
    #
    # We can define a counter or list, and in mock_ask, we can check how many times it was called.
    # Since we have 3 records (one before 10:00, two at/after 10:00), if the filter works,
    # only the 2 records (10:00 and 10:15) should be processed, so mock_ask should be called exactly twice.
    
    call_count = 0
    async def mock_ask_spy(self, new_prompt_text, chart_paths):
        nonlocal call_count
        call_count += 1
        return {
            "action": "HOLD",
            "confidence": 80,
            "reason": "mock hold"
        }
    monkeypatch.setattr(Arbiter, "ask", mock_ask_spy)
    
    await simulate_today("005930", "2026-06-19")
    
    assert call_count == 2
