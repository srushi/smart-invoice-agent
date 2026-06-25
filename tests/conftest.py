import os
from unittest.mock import MagicMock

# Set required GCP environment variables for tests
os.environ["GOOGLE_CLOUD_PROJECT"] = "mock-project-id"
os.environ["GOOGLE_CLOUD_LOCATION"] = "us-central1"

# Mock google.auth.default to prevent DefaultCredentialsError during test discovery/execution
import google.auth
google.auth.default = lambda *args, **kwargs: (MagicMock(), "mock-project-id")
