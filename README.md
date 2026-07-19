## Brauze

Thin Flask-based file browser for serving files from a mounted directory.

### Behavior

1. Serves files and folders from `BRAUZE_ROOT`.
2. Defaults `BRAUZE_ROOT` to `/data`.
3. Hides any folder containing a `.brauze-ignore` file.
4. Supports direct download for individual files.
5. Supports bulk folder download as a zip archive.
6. Assumes authentication is handled upstream, for example by nginx.
7. Uses a lightweight UI without a large frontend framework.

### Local Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BRAUZE_ROOT=/path/to/files
python3 app.py
```

### Docker Build

```bash
docker build -t brauze .
docker run --rm -p 8000:8000 -e BRAUZE_ROOT=/data /host/path:/data:ro brauze
```
