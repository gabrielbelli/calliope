"""Present so that pytest puts the repository root on sys.path.

tests/test_openai_speech.py and its neighbours import `app.main`, which is a
package in this directory rather than an installed distribution. Under
pytest's default import mode the rootdir is added to sys.path only when a
conftest.py lives there, so this file is the entire reason those imports
resolve: without it `.venv/bin/pytest tests/ -q` — the command this service's
README and every CI job use — fails at collection with "No module named
'app'", which reads like a broken install and is not one. services/stt carries
the same file for the same reason. It has nothing else to do.
"""
