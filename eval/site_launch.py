"""Boot the TestOps site locally for acceptance testing.

Run under `railway run` to inject the real SF creds + API key from Railway
(ephemeral, never stored). The website auth is overridden to a local test login
(louie / testpin123) so you don't need the production door key, and CLIENT_ID is
'sitetest' so test runs don't touch real client data.

  railway run <venv-python> -m eval.site_launch
  # then browse / log in at http://127.0.0.1:8100  (louie / testpin123)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["TESTOPS_PIN"] = "testpin123"
os.environ["TESTOPS_AUTH_TOKEN"] = "testtok1234567890abcdef"
os.environ["CLIENT_ID"] = "sitetest"

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("ui.server:app", host="127.0.0.1", port=8100, log_level="warning")
