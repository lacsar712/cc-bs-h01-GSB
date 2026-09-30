from h01_surface_trap import half_dirt, surface_reason, surface_verdict

def on_worker_save(verdict: str, reason: str):
    return surface_verdict(verdict), surface_reason(verdict, reason)

def assert_dirt_armed() -> bool:
    return half_dirt()
