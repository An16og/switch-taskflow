import os
import sys
import subprocess

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

env_file = r"C:\Users\anujj\AppData\Roaming\switchdash\local-switch-server\native\run\switch-core.env"
env = dict(os.environ)
with open(env_file, "r") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v

env["PYTHONUNBUFFERED"] = "1"
python_exe = r"C:\Users\anujj\AppData\Roaming\switchdash\local-switch-server\native\switch-core\0.31.0\venv\Scripts\python.exe"
cwd = r"C:\Users\anujj\AppData\Roaming\switchdash\local-switch-server\native\switch-core\0.31.0"

print(f"Starting switch_core server on port {env.get('SERVER_PORT', 52422)}...")
sys.stdout.flush()

# Execute switch-core directly so this process is the server
subprocess.run([python_exe, "-m", "switch_core.main"], cwd=cwd, env=env)
