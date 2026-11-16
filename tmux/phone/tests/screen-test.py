import importlib.machinery, importlib.util, os, sys
sys.dont_write_bytecode = True
sp = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("phone_hub", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "phone-hub"))
spec = importlib.util.spec_from_loader("phone_hub", loader); ph = importlib.util.module_from_spec(spec); loader.exec_module(ph)
F = os.path.join(sp, "fixtures")
# fixture -> (agent kind, expected state)
cases = {
 "claude-idle-empty": ("claude", "idle"), "claude-placeholder": ("claude", "idle"), "claude-ghost": ("claude", "idle"), "claude-plan-approval": ("claude", "dialog"), "copilot-permission": ("copilot", "dialog"), "copilot-permission-mode-menu": ("copilot", "dialog"), "claude-after-esc-status-waiting": ("claude", "idle"), "claude-draft": ("claude", "draft"),
 "claude-working": ("claude", "working"), "claude-question-menu": ("claude", "dialog"), "claude-permission": ("claude", "dialog"),
 "copilot-idle-empty": ("copilot", "idle"), "copilot-draft": ("copilot", "draft"), "copilot-working": ("copilot", "working"),
 "copilot-trust-dialog": ("copilot", "dialog"),
 "opencode-idle": ("opencode", "idle"), "opencode-idle-empty": ("opencode", "idle"), "opencode-draft": ("opencode", "draft"),
 "opencode-working": ("opencode", "working"), "opencode-permission": ("opencode", "dialog"),
}
bad = 0
for name, (kind, want) in cases.items():
    raw = open(os.path.join(F, name + ".txt"), errors="replace").read()
    i = ph.screen_info(raw, kind)
    ok = i["state"] == want
    bad += not ok
    print(("ok  " if ok else "FAIL"), f"{name:24s} want={want:8s} got={i['state']:8s} draft={i['draft'][:40]!r}")
print("failures:", bad)
