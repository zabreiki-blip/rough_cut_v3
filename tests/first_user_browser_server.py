"""Isolated app for the browser smoke. Never imports the workspace app/.env."""

import os
import json
import shutil
import sys
from pathlib import Path


def main():
    source, target, port = sys.argv[1:4]
    root = Path(target)
    shutil.copytree(
        Path(source) / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    os.chdir(root)
    demo_cache = os.environ.get("BROWSER_TEST_HF_HOME")
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"}
    for key in list(os.environ):
        if key not in allowed:
            del os.environ[key]
    os.environ.update(
        ROUGH_CUT_API_TOKEN="browser-smoke-only", FLASK_SKIP_DOTENV="1", TTS_PROVIDER="macos"
    )
    if "--offline-demo" in sys.argv[4:]:
        if demo_cache:
            os.environ["HF_HOME"] = demo_cache
        os.environ.update(
            HF_HUB_OFFLINE="1",
            HF_HUB_DISABLE_TELEMETRY="1",
            SYNC_WHISPER_MODEL="small",
            SYNC_WHISPER_BACKEND="faster-whisper",
            SYNC_WHISPER_DEVICE="cpu",
            SYNC_WHISPER_COMPUTE_TYPE="int8",
            SYNC_WHISPER_CPU_THREADS="4",
        )
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False
    sys.path.insert(0, str(root / "src"))
    import api
    from flask import jsonify, request

    @api.app.before_request
    def fixture_tts_job():
        lifecycle = root / "browser-lifecycle.json"
        if lifecycle.exists():
            data = json.loads(lifecycle.read_text())
            if request.path == "/stage1/tts-jobs/start" and "tts_start" in data:
                return jsonify(ok=False, error="start rejected by guard"), 400
            if request.path == "/stage3/status" and "stage3_status" in data:
                return jsonify(ok=True, result=data["stage3_status"])
            if request.path == "/stage3/jobs/prepare/start" and "stage3_start" in data:
                return jsonify(ok=True, result=data["stage3_start"])
            if request.path == "/stage3/jobs/browser-stage3" and "stage3_job" in data:
                return jsonify(ok=True, result=data["stage3_job"])
        fixture = root / "browser-job.json"
        if request.path.startswith("/stage1/tts-jobs/") and fixture.exists():
            return jsonify(ok=True, result=json.loads(fixture.read_text()))

    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", int(port), api.app, threaded=True)
    print(json.dumps({"port": server.server_port}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
